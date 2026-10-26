# Offline, host-independent regression tests for independent deployment planning.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'area-events-deploy-hardening.psm1') -Force

$cases = @(
  @{ Name='all current despite missing receipt'; Schema=9; Source=$true; Files=$true; Runtime=$true; Cubi=$false; HA=$false },
  @{ Name='Cubi succeeded, HA failed'; Schema=9; Source=$true; Files=$false; Runtime=$false; Cubi=$false; HA=$true },
  @{ Name='HA files copied but Core not restarted'; Schema=9; Source=$true; Files=$true; Runtime=$false; Cubi=$false; HA=$true },
  @{ Name='both succeeded on retry'; Schema=9; Source=$true; Files=$true; Runtime=$true; Cubi=$false; HA=$false },
  @{ Name='stale Cubi code with target DB schema'; Schema=9; Source=$false; Files=$true; Runtime=$true; Cubi=$true; HA=$false },
  @{ Name='older DB with current source'; Schema=8; Source=$true; Files=$true; Runtime=$true; Cubi=$true; HA=$false },
  @{ Name='both components need work'; Schema=8; Source=$false; Files=$false; Runtime=$false; Cubi=$true; HA=$true },
  @{ Name='stale HA share despite functioning current runtime'; Schema=9; Source=$true; Files=$false; Runtime=$true; Cubi=$false; HA=$true }
)
foreach ($c in $cases) {
  $r = Resolve-AreaEventsComponentPlan -CurrentSchema $c.Schema -TargetSchema 9 -CubiSourceMatches $c.Source -HaFilesMatch $c.Files -HaRuntimeMatches $c.Runtime
  if ([bool]$r.CubiNeeded -ne $c.Cubi -or [bool]$r.HANeeded -ne $c.HA) {
    throw "Planner mismatch ($($c.Name)): Cubi=$($r.CubiNeeded); HA=$($r.HANeeded)"
  }
  Write-Host "ok $($c.Name)"
}
$futureRejected = $false
try {
  Resolve-AreaEventsComponentPlan -CurrentSchema 10 -TargetSchema 9 -CubiSourceMatches $true -HaFilesMatch $true -HaRuntimeMatches $true | Out-Null
} catch { $futureRejected = $true }
if (-not $futureRejected) { throw 'newer production schema was accepted' }

foreach ($file in @('deploy-area-events.ps1','area-events-deploy-hardening.psm1','area-events-deploy-engine.psm1')) {
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file),[ref]$null,[ref]$errors) | Out-Null
  if ($errors.Count) { throw ("PowerShell parse failure in {0}: {1}" -f $file,$errors[0].Message) }
}
$source = Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
$start = $source.IndexOf('function Invoke-DeploymentPlan(')
$end = $source.IndexOf('function Invoke-Backups(', $start)
if ($start -lt 0 -or $end -le $start) { throw 'Planner boundaries missing' }
$planner = $source.Substring($start,$end-$start)
if ($planner -match 'remote\.receipt' -or $planner -notmatch 'Test-CubiInstalledSource' -or $planner -notmatch 'Test-HaAssets') {
  throw 'Planner must reconcile actual installed components, not trust a prior receipt.'
}
# Execute only the two pure status-classification functions from the production script.
# Avoid dot-sourcing the deployer, which initializes platform-dependent paths.
$parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile(
  (Join-Path $PSScriptRoot 'deploy-area-events.ps1'),[ref]$null,[ref]$parseErrors
)
if ($parseErrors.Count) { throw "Cannot parse deployer: $($parseErrors[0].Message)" }
foreach ($name in @('Get-HaRestartHttpStatus','Test-HaRestartRejected')) {
  $matches=@($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  },$false))
  if ($matches.Count -ne 1) { throw "Cannot find exactly one $name function" }
  . ([scriptblock]::Create($matches[0].Extent.Text))
}
$closed=[pscustomobject]@{Exception=[pscustomobject]@{Message='Connection closed'}}
$cases=@(
  @{Name='closed HTTP connection'; Error=$closed; Expected=$null},
  @{Name='missing response'; Error=[pscustomobject]@{Exception=[pscustomobject]@{Response=$null}}; Expected=$null},
  @{Name='missing status'; Error=[pscustomobject]@{Exception=[pscustomobject]@{Response=[pscustomobject]@{StatusCode=$null}}}; Expected=$null},
  @{Name='HTTP zero'; Error=[pscustomobject]@{Exception=[pscustomobject]@{Response=[pscustomobject]@{StatusCode=0}}}; Expected=$null},
  @{Name='HTTP 400'; Error=[pscustomobject]@{Exception=[pscustomobject]@{Response=[pscustomobject]@{StatusCode=400}}}; Expected=400},
  @{Name='HTTP 403'; Error=[pscustomobject]@{Exception=[pscustomobject]@{Response=[pscustomobject]@{StatusCode=[System.Net.HttpStatusCode]::Forbidden}}}; Expected=403},
  @{Name='HTTP 502'; Error=[pscustomobject]@{Exception=[pscustomobject]@{Response=[pscustomobject]@{StatusCode=502}}}; Expected=502},
  @{Name='HTTP 503'; Error=[pscustomobject]@{Exception=[pscustomobject]@{Response=[pscustomobject]@{StatusCode=503}}}; Expected=503}
)
foreach ($case in $cases) {
  $status=Get-HaRestartHttpStatus $case.Error
  if ($status -ne $case.Expected) { throw "Bad restart HTTP status ($($case.Name)): got $status, expected $($case.Expected)" }
  $reject=Test-HaRestartRejected $status
  $expectedReject=($case.Expected -in @(400,403))
  if ($reject -ne $expectedReject) { throw "Wrong restart rejection classification ($($case.Name))" }
  Write-Host "ok HA restart: $($case.Name)"
}
# Both deployment and recovery restart paths must use the same helper.
if (([regex]::Matches($source,'\$status=Get-HaRestartHttpStatus \$_')).Count -ne 2 -or
    ([regex]::Matches($source,'Test-HaRestartRejected \$status')).Count -ne 2) {
  throw 'Main HA restart and rollback do not share the tested status classifier.'
}
Write-Host 'Deployment reconciliation and restart-status tests: PASS'
