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

foreach ($file in @('deploy-area-events.ps1','deploy-ha.ps1','install-ha.ps1','area-events-deploy-hardening.psm1','area-events-deploy-engine.psm1')) {
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
Write-Host 'Deployment reconciliation tests: PASS'
