<#
.SYNOPSIS
  Offline state-machine tests for the Area Events deployment engine. No network, credentials, or production writes.
#>
$ErrorActionPreference = 'Stop'
$module = Join-Path $PSScriptRoot 'area-events-deploy-engine.psm1'
Import-Module $module -Force
Import-Module (Join-Path $PSScriptRoot 'area-events-deploy-hardening.psm1') -Force
$script:failures = 0
function Check([string]$Name, [scriptblock]$Test) {
  try { & $Test; Write-Host "ok   $Name" }
  catch { $script:failures++; Write-Host "FAIL $Name : $($_.Exception.Message)" -ForegroundColor Red }
}
function New-Ops([string]$FailAt = '', [switch]$NoChanges, [switch]$SchemaChanged, [bool]$CubiNeeded=$true, [bool]$HANeeded=$true) {
  $script:order = [System.Collections.Generic.List[string]]::new()
  $script:recovered = $false
  $script:noChanges = [bool]$NoChanges
  $script:schemaChanged = [bool]$SchemaChanged
  $script:cubiNeeded = $CubiNeeded
  $script:haNeeded = $HANeeded
  $call = {
    param($name, $result = $null)
    $script:order.Add($name)
    if ($script:failAt -eq $name) { throw "$name simulated failure" }
    return $result
  }
  $script:mockCall = $call
  $script:failAt = $FailAt
  return @{
    Preflight = { & $script:mockCall 'preflight' ([pscustomobject]@{ Stage='preflight' }) }
    Build = { & $script:mockCall 'build' }
    Plan = { param($s) & $script:mockCall 'plan' ([pscustomobject]@{ NoChanges=$script:noChanges;CubiNeeded=$script:cubiNeeded;HANeeded=$script:haNeeded }) }
    Backup = { param($s) & $script:mockCall 'backup' }
    Rehearse = { param($s) & $script:mockCall 'rehearse' ([pscustomobject]@{ Passed = $true }) }
    DeployCubi = { param($s,$r) & $script:mockCall 'cubi' ([pscustomobject]@{ Changed = $true; SchemaChanged = $script:schemaChanged }) }
    VerifyCubi = { param($s) & $script:mockCall 'verify-cubi' }
    DeployHA = { param($s) $null=& $script:mockCall 'ha-file-transfer'; $null=& $script:mockCall 'ha-restart'; return [pscustomobject]@{ Changed = $true } }
    VerifyHA = { param($s) & $script:mockCall 'health-check' }
    Recover = { param($phase,$s,$schema,$ha) $script:recovered = $true; $script:recoveryArgs = @($phase,$schema,$ha); & $script:mockCall 'recover' }
  }
}

Check 'script parses and production engine is explicit operations only' {
  $errors=$null
  [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'deploy-area-events.ps1'), [ref]$null, [ref]$errors) | Out-Null
  if ($errors.Count) { throw $errors[0].Message }
  if ((Get-Content -Raw $module) -notmatch 'Rehearse|DryRun|Recover') { throw 'required engine gates are missing' }
}
Check 'only the intended Cubi systemd unit and HA share are deployment targets' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  if ($text -notmatch 'area-events-discovery\.service' -or $text -notmatch 'configuration\.yaml') { throw 'expected targets are not identified' }
  if ($text -match 'docker\s+(stop|restart)|8765|(?:&|Invoke-Bounded).*deploy-cubi-2f-display\.ps1') { throw 'deployment addresses protected Docker/legacy targets' }
  if ($text -notmatch 'ConnectTimeout=8' -or $text -notmatch 'Wait-Job -Job \$job -Timeout') { throw 'external calls lack finite deadlines' }
}
Check 'backup uses SQLite backup API and migration checks preserve stable keys and links' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  foreach($needle in @('.backup(d)','quick_check','foreign_key_check','event_key','candidate_key')) { if ($text -notmatch [regex]::Escape($needle)) { throw "missing backup or invariant check: $needle" } }
  if ($text -match 'copy.*discovery\.db.*production|restore.*discovery\.db') { throw 'automatic database snapshot restore is forbidden' }
}
Check 'HA restart and module verification are authenticated and bounded' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  if ($text -notmatch 'CoreRecoveryTimeoutSeconds' -or $text -notmatch 'Test-HaWebSocket' -or $text -notmatch 'Get-AreaEventsRequiredFrontendAssets') { throw 'HA restart, websocket or manifest-driven module checks are missing' }
  if ($text -match 'Count -ne 6|six presentation modules|No current events|samples.*-lt 1') { throw 'hardcoded renderer count or nonempty event requirement remains' }
  if ($text -match 'ServerCertificateCustomValidationCallback|DangerousAcceptAnyServerCertificateValidator') { throw 'TLS verification is disabled' }
}
Check 'production preflight component decisions reach the orchestration engine' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  foreach($needle in @('CubiNeeded=[bool]$cubiNeeded','HANeeded=[bool]$haNeeded','cubi_hash','ha_hash','Built artifacts fingerprinted')) { if ($text -notmatch [regex]::Escape($needle)) { throw "missing final component deployment state: $needle" } }
}
Check 'successful deployment uses required order' {
  $ops=New-Ops
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
  if ($result.Result -ne 'Deployed') { throw "unexpected result $($result.Result)" }
  if (($script:order -join ',') -ne 'preflight,build,plan,backup,rehearse,cubi,verify-cubi,ha-file-transfer,ha-restart,health-check') { throw "wrong order: $($script:order -join ',')" }
}
Check 'dry run builds locally but makes no production writes' {
  $ops=New-Ops
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops -DryRun
  if ($result.Result -ne 'DryRun' -or ($script:order -join ',') -ne 'preflight,build,plan') { throw "dry run performed unexpected steps: $($script:order -join ',')" }
}
Check 'no-change path skips deployment and restart' {
  $ops=New-Ops -NoChanges
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
  if ($result.Result -ne 'NoChange' -or ($script:order -join ',') -ne 'preflight,build,plan') { throw 'no-change deployment was not idempotent after built-artifact comparison' }
}
Check 'Cubi-only update skips Home Assistant copy and restart' {
  $ops=New-Ops -HANeeded $false
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
  if ($result.Result -ne 'Deployed' -or $script:order -contains 'ha-file-transfer' -or $script:order -contains 'ha-restart') { throw 'unneeded Home Assistant work was performed' }
}
Check 'HA-only update skips Cubi restart and migration rehearsal' {
  $ops=New-Ops -CubiNeeded $false
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
  if ($result.Result -ne 'Deployed' -or $script:order -contains 'cubi' -or $script:order -contains 'rehearse') { throw 'unneeded Cubi work was performed' }
}
foreach ($failure in @('preflight','build','plan','backup','rehearse','cubi','verify-cubi','ha-file-transfer','ha-restart','health-check')) {
  Check "failure at $failure is surfaced" {
    $ops=New-Ops -FailAt $failure
    $threw=$false
    try { Invoke-AreaEventsDeploymentEngine -Operations $ops | Out-Null } catch { $threw=$_.Exception.Message -match "$failure simulated failure" }
    if (-not $threw) { throw 'failure was swallowed or replaced' }
    if ($failure -in @('preflight','build','plan','backup','rehearse') -and $script:recovered) { throw 'recovery ran before production modification was attempted' }
    if ($failure -in @('cubi','verify-cubi','ha-file-transfer','ha-restart','health-check') -and -not $script:recovered) { throw 'recovery was not attempted after production modification was attempted' }
  }
}
Check 'HA Samba write, rename, and removal preflight succeeds and cleans up' {
  $root=Join-Path ([IO.Path]::GetTempPath()) ('ae-share-'+[guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $root | Out-Null
  try {
    if (-not (Test-AreaEventsShareCapabilities $root)) { throw 'share capability probe returned false' }
    if (@(Get-ChildItem -LiteralPath $root -Force).Count) { throw 'temporary capability probe file was not removed' }
  } finally { Remove-Item -LiteralPath $root -Force }
}
Check 'unwritable HA share fails preflight safely' {
  $file=Join-Path ([IO.Path]::GetTempPath()) ('ae-not-dir-'+[guid]::NewGuid().ToString('N'))
  Set-Content -LiteralPath $file -Value 'x'
  try { $threw=$false; try { Test-AreaEventsShareCapabilities $file | Out-Null } catch { $threw=$true }; if (-not $threw) { throw 'invalid share unexpectedly passed' } }
  finally { Remove-Item -LiteralPath $file -Force }
}
Check 'missing HA administrator prerequisite fails safely' {
  $threw=$false
  try { Assert-AreaEventsHaPrerequisites -Config ([pscustomobject]@{state='RUNNING';components=@('area_events')}) -CurrentUser ([pscustomobject]@{is_admin=$false}) | Out-Null } catch { $threw=$true }
  if (-not $threw) { throw 'non-admin user unexpectedly passed' }
}
Check 'manifest-driven assets include an added renderer' {
  $root=Join-Path ([IO.Path]::GetTempPath()) ('ae-manifest-'+[guid]::NewGuid().ToString('N'))
  $presentation=Join-Path $root 'presentation'; New-Item -ItemType Directory -Path $presentation | Out-Null
  try {
    Set-Content (Join-Path $root 'area-events-card.js') 'card'
    $names=@('manifest.js','registry.js','community.js','generic.js','nasa-mission.js','new-renderer.js')
    foreach($name in $names) { Set-Content (Join-Path $presentation $name) 'module' }
    Set-Content (Join-Path $presentation 'manifest.js') @'
import { communityRenderer } from "./community.js";
import { newRenderer } from "./new-renderer.js";
'@
    $assets=@(Get-AreaEventsRequiredFrontendAssets $root)
    if ($assets.Count -ne 4 -or $assets -notcontains (Join-Path $presentation 'new-renderer.js')) { throw "new renderer not represented in required assets: $($assets.Count)" }
  } finally { Remove-Item -LiteralPath $root -Recurse -Force }
}
Check 'valid empty event feeds pass and malformed feeds fail' {
  Assert-AreaEventsFeedResponse -Response ([pscustomobject]@{events=@()}) -Area 'Empty' | Out-Null
  $threw=$false; try { Assert-AreaEventsFeedResponse -Response ([pscustomobject]@{events=$null}) -Area 'Bad' | Out-Null } catch { $threw=$true }
  if (-not $threw) { throw 'malformed feed unexpectedly passed' }
}
Check 'build-generated frontend change is observed before deployment planning' {
  $artifact=Join-Path ([IO.Path]::GetTempPath()) ('ae-built-'+[guid]::NewGuid().ToString('N')+'.js')
  Set-Content -LiteralPath $artifact -Value 'source'
  try {
    $ops=New-Ops -NoChanges
    $ops.Build={ & $script:mockCall 'build'; Set-Content -LiteralPath $artifact -Value 'built-output' }
    $ops.Plan={ param($s) & $script:mockCall 'plan' ([pscustomobject]@{NoChanges=((Get-Content -Raw $artifact).Trim() -eq 'source');CubiNeeded=$false;HANeeded=$true}) }
    $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
    if ($result.Result -ne 'Deployed' -or ($script:order -join ',') -notmatch '^preflight,build,plan,backup') { throw 'planner did not evaluate the generated asset after build' }
  } finally { Remove-Item -LiteralPath $artifact -Force }
}
Check 'failed recovery preserves the original deployment failure' {
  $ops=New-Ops -FailAt 'verify-cubi'
  $ops.Recover={ param($phase,$s,$schema,$ha) throw 'recovery simulated failure' }
  $message=''
  try { Invoke-AreaEventsDeploymentEngine -Operations $ops | Out-Null } catch { $message=$_.Exception.Message }
  if ($message -notmatch 'verify-cubi simulated failure') { throw "original failure was lost: $message" }
}
if ($script:failures) { Write-Error "$script:failures deployment tests failed"; exit 1 }
Write-Host 'All deployment engine checks passed.'
