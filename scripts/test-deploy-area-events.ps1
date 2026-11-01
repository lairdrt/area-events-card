<#
.SYNOPSIS
  Offline state-machine tests for the Area Events deployment engine. No network, credentials, or production writes.
#>
$ErrorActionPreference = 'Stop'
$module = Join-Path $PSScriptRoot 'area-events-deploy-engine.psm1'
Import-Module $module -Force
Import-Module (Join-Path $PSScriptRoot 'area-events-deploy-hardening.psm1') -Force
. (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
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
    Quiesce = { param($s) & $script:mockCall 'quiesce' }
    DeployCubi = { param($s,$r) & $script:mockCall 'cubi' ([pscustomobject]@{ Changed = $true; SchemaChanged = $script:schemaChanged }) }
    VerifyCubi = { param($s) & $script:mockCall 'verify-cubi' }
    RestoreDiscovery = { param($s) & $script:mockCall 'restore-discovery' }
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
Check 'Cubi deployment archives the src package and starts its CLI module' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  foreach($needle in @("'src/area_events'", "'pyproject.toml'", "argv[1:3]==['-m','area_events']")) {
    if ($text -notmatch [regex]::Escape($needle)) { throw "package-based deployment step is missing: $needle" }
  }
  if ($text -match "stage\+'/scripts/discovery.py'|stage\+'/area_events'") { throw 'deployment still starts or copies the retired application package' }
}
Check 'Cubi JSON deployment result maps to the engine contract' {
  $wire='{"changed":true,"schema_changed":false}' | ConvertFrom-Json
  $normalized=ConvertTo-CubiDeployResult $wire
  if (-not $normalized.Changed -or $normalized.SchemaChanged) { throw 'Cubi deployment flags were not mapped correctly' }
  $invalid=$false
  try { ConvertTo-CubiDeployResult ([pscustomobject]@{changed=$true}) | Out-Null } catch { $invalid=$true }
  if (-not $invalid) { throw 'invalid Cubi deployment response was accepted' }
}
Check 'HA WebSocket verification uses increasing request IDs and matching unsubscribe ID' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  $start=$text.IndexOf('function Test-HaWebSocket {')
  $end=$text.IndexOf('function Test-HaAssets {',$start)
  if ($start -lt 0 -or $end -le $start) { throw 'HA WebSocket verification function was not found' }
  $code=$text.Substring($start,$end-$start)
  $ids=@([regex]::Matches($code,"Send-HaMessage\s+@\{id=(\d+);type='area_events/(?:events|subscribe)'") | ForEach-Object { [int]$_.Groups[1].Value })
  if (($ids -join ',') -ne '1,2,3' -or $code -notmatch 'type=''unsubscribe_events'';subscription=3') { throw "HA WebSocket request IDs are not strictly increasing and unsubscribed correctly: $($ids -join ',')" }
}
Check 'bounded build runner uses the repository working directory' {
  $repoRoot=Split-Path -Parent $PSScriptRoot
  $npm=(Get-Command npm.cmd -ErrorAction Stop).Source
  $output=Invoke-Bounded $npm @('run','build','--silent') 90 $repoRoot
  if ($output -notmatch 'AREA-EVENTS [0-9]+(\.[0-9]+)*-[0-9a-f]{8}') { throw 'bounded npm build did not complete from the repository directory' }
}
Check 'only the intended Cubi systemd unit and HA share are deployment targets' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  if ($text -notmatch 'area-events-discovery\.service' -or $text -notmatch 'configuration\.yaml') { throw 'expected targets are not identified' }
  if ($text -match 'docker\s+(stop|restart)|8765') { throw 'deployment addresses protected Docker/legacy targets' }
  if ($text -notmatch 'ConnectTimeout=8' -or $text -notmatch 'Wait-Job -Job \$job -Timeout') { throw 'external calls lack finite deadlines' }
}
Check 'backup uses SQLite backup API and migration checks preserve stable keys and links' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  foreach($needle in @('.backup(d)','quick_check','foreign_key_check','event_key','candidate_key')) { if ($text -notmatch [regex]::Escape($needle)) { throw "missing backup or invariant check: $needle" } }
  if ($text -match 'copy.*discovery\.db.*production|restore.*discovery\.db') { throw 'automatic database snapshot restore is forbidden' }
}
Check 'code backup verification accepts complete legacy or modern layouts and rejects incomplete contents' {
  $legacy=@('scripts/discovery.py','scripts/workbench.py','area_events/__init__.py','area_events/discovery.py')
  if (-not (Test-AreaEventsCodeBackupMembers -Layout legacy -Required $legacy -Archived $legacy)) { throw 'complete legacy runtime backup was rejected' }
  $modern=@('pyproject.toml','src/area_events/__init__.py','src/area_events/app.py')
  if (-not (Test-AreaEventsCodeBackupMembers -Layout modern -Required $modern -Archived $modern)) { throw 'complete modern package backup was rejected' }
  foreach ($case in @(
    @{Layout='legacy';Required=$legacy;Archived=@('scripts/discovery.py','area_events/__init__.py','area_events/discovery.py')},
    @{Layout='modern';Required=$modern;Archived=@('pyproject.toml','src/area_events/__init__.py')},
    @{Layout='modern';Required=@('pyproject.toml','src/area_events/app.py');Archived=@('pyproject.toml','src/area_events/__init__.py')}
  )) {
    if (Test-AreaEventsCodeBackupMembers -Layout $case.Layout -Required $case.Required -Archived $case.Archived) {
      throw "incomplete $($case.Layout) code backup was accepted"
    }
  }
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  if ($text -match "if 'pyproject.toml' not in names or 'src/area_events' not in names") { throw 'backup verifier still requires the modern layout unconditionally' }
  if ($text -notmatch 'active Discovery Service code layout' -or $text -notmatch 'code_archived=') { throw 'backup does not verify the active layout against archived members' }
}
Check 'systemd layout detection validates WorkingDirectory and supports relative, absolute and modern startup' {
  $root='/home/robohobo/area-events-discovery'
  $relative='ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 scripts/discovery.py serve --db data/discovery/discovery.db --port 8780 ; flags= ; }'
  $absolute="ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 $root/scripts/discovery.py serve --db data/discovery/discovery.db --port 8780 ; flags= ; }"
  $modern='ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -m area_events serve --db data/discovery/discovery.db --port 8780 ; flags= ; }'
  if ((Resolve-AreaEventsSystemdLayout -WorkingDirectory $root -ExecStart $relative -Root $root -LegacyEntryExists $true -ModernPackageExists $false) -ne 'legacy') { throw 'relative legacy ExecStart was not detected' }
  if ((Resolve-AreaEventsSystemdLayout -WorkingDirectory $root -ExecStart $absolute -Root $root -LegacyEntryExists $true -ModernPackageExists $false) -ne 'legacy') { throw 'absolute legacy ExecStart was not detected' }
  if ((Resolve-AreaEventsSystemdLayout -WorkingDirectory $root -ExecStart $modern -Root $root -LegacyEntryExists $false -ModernPackageExists $true) -ne 'modern') { throw 'modern module ExecStart was not detected' }
  foreach($case in @(
    @{WorkingDirectory='/home/robohobo/other';ExecStart=$relative;Legacy=$true;Modern=$false},
    @{WorkingDirectory=$root;ExecStart='ExecStart={ argv[]=/usr/bin/python3 scripts/other.py ; }';Legacy=$true;Modern=$false},
    @{WorkingDirectory=$root;ExecStart=$relative;Legacy=$false;Modern=$false}
  )) {
    $rejected=$false
    try { Resolve-AreaEventsSystemdLayout -WorkingDirectory $case.WorkingDirectory -ExecStart $case.ExecStart -Root $root -LegacyEntryExists $case.Legacy -ModernPackageExists $case.Modern | Out-Null } catch { $rejected=$true }
    if (-not $rejected) { throw 'unsupported or incomplete systemd startup was accepted' }
  }
}
Check 'systemd conversion changes only legacy entrypoint and preserves service arguments' {
  $root='/home/robohobo/area-events-discovery'
  foreach($entry in @('scripts/discovery.py',($root+'/scripts/discovery.py'))) {
    $unit="[Service]`nWorkingDirectory=$root`nEnvironment=AE_MODE=production`nExecStart=/usr/bin/python3 $entry serve --db data/discovery/discovery.db --bind 192.168.1.88 --allow-remote --port 8780 --tls-cert certs/cert.pem --tls-key certs/key.pem`n"
    $converted=Convert-AreaEventsSystemdExecStart -UnitText $unit -Root $root
    if ($converted -notmatch 'ExecStart=/usr/bin/python3 -m area_events serve --db data/discovery/discovery.db --bind 192\.168\.1\.88 --allow-remote --port 8780 --tls-cert certs/cert\.pem --tls-key certs/key\.pem' -or $converted -notmatch 'Environment=AE_MODE=production' -or $converted -notmatch ('WorkingDirectory='+[regex]::Escape($root))) { throw "systemd conversion lost required settings for '$entry'" }
  }
  $modern='ExecStart=/usr/bin/python3 -m area_events serve --port 8780'
  $rejected=$false; try { Convert-AreaEventsSystemdExecStart -UnitText $modern -Root $root | Out-Null } catch { $rejected=$true }
  if (-not $rejected) { throw 'modern ExecStart was incorrectly treated as a legacy conversion' }
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  if ($text -notmatch 'if matches!=1:' -or $text -notmatch 'venv_python' -or $text -notmatch 'ExecStart=')
    { throw 'remote deployment does not preserve modern module startup through its virtual environment' }
}
Check 'legacy cleanup occurs only after API verification and retains verified backups' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  $start=$text.IndexOf('function Test-CubiHealth {')
  $end=$text.IndexOf('function Install-Ha {',$start)
  if ($start -lt 0 -or $end -le $start) { throw 'Cubi health verification function was not found' }
  $health=$text.Substring($start,$end-$start)
  $api=$health.IndexOf('if ($null -eq $h -or $h.state')
  $cleanup=$health.IndexOf('$cleanup=@')
  if ($api -lt 0 -or $cleanup -le $api -or $health.IndexOf('Invoke-CubiPython $cleanup') -le $api) { throw 'legacy files may be removed before API verification' }
  if ($health -notmatch 'scripts/discovery\.py' -or $health -notmatch 'scripts/workbench\.py' -or $health -notmatch "root\+'/area_events'" -or $health -notmatch "AE_TRANSITION") { throw 'cleanup does not cover all retired runtime paths safely' }
  if ($health -notmatch 'verified backup retained') { throw 'successful cleanup does not preserve the verified backup' }
  $deployStart=$text.IndexOf('function Invoke-CubiDeploy($state) {')
  $deployEnd=$text.IndexOf('function Test-CubiHealth {',$deployStart)
  $deploy=$text.Substring($deployStart,$deployEnd-$deployStart)
  if ($deploy -notmatch "stage=root\+'/.ae-stage-'\+run" -or $deploy -notmatch "transition_id'\:run") { throw 'legacy runtime is not isolated in a bounded transition staging path' }
}
Check 'HA restart and module verification are authenticated and bounded' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  if ($text -notmatch 'RestartTimeoutSeconds' -or $text -notmatch 'Test-HaWebSocket' -or $text -notmatch 'Get-AreaEventsRequiredFrontendAssets') { throw 'HA restart, websocket or manifest-driven module checks are missing' }
  if ($text -match 'Count -ne 6|six presentation modules|No current events|samples.*-lt 1') { throw 'hardcoded renderer count or nonempty event requirement remains' }
  if ($text -match 'ServerCertificateCustomValidationCallback|DangerousAcceptAnyServerCertificateValidator') { throw 'TLS verification is disabled' }
}
Check 'HA rollback artifacts are protected and stored outside integration discovery' {
  $root=Join-Path ([IO.Path]::GetTempPath()) ('ae-recovery-'+[guid]::NewGuid().ToString('N'))
  $backup=Join-Path $root 'AreaEventsBackups'
  $components=Join-Path $root 'ha-config\custom_components'
  New-Item -ItemType Directory -Path $backup,$components -Force | Out-Null
  $previousBackupRoot=$script:LocalBackupRoot
  $script:LocalBackupRoot=$root
  try {
    $paths=Get-HaRecoveryPaths -BackupRoot $backup -ComponentsRoot $components
    foreach($path in @($paths.Root,$paths.Failed,$paths.Partial)) {
      $full=[IO.Path]::GetFullPath($path)
      if ($full.StartsWith([IO.Path]::GetFullPath($components).TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw "Recovery artifact is inside custom_components: $full" }
      if (-not $full.StartsWith([IO.Path]::GetFullPath($backup).TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw "Recovery artifact escaped Area Events backup storage: $full" }
    }
    if ($paths.Failed -eq $paths.Partial -or (Split-Path -Leaf $paths.Failed) -match '^\.') { throw 'Recovery artifacts are not uniquely named ordinary backup directories.' }
    Protect-HaRecoveryRoot -Path $paths.Root
    $acl=Get-Acl -LiteralPath $paths.Root
    $rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid -or $rules[0].IsInherited) { throw 'Recovery backup ACL is not protected for the current user.' }
    $rejected=$false
    try { Get-HaRecoveryPaths -BackupRoot (Join-Path $components 'nested') -ComponentsRoot $components | Out-Null } catch { $rejected=$true }
    if (-not $rejected) { throw 'A backup location under custom_components was accepted.' }
    $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
    $start=$text.IndexOf('function Restore-HaBackup($state) {')
    $end=$text.IndexOf('function Write-Receipt($state) {',$start)
    if ($start -lt 0 -or $end -le $start) { throw 'HA recovery function was not found.' }
    $restore=$text.Substring($start,$end-$start)
    if ($restore -notmatch 'Get-HaRecoveryPaths' -or $restore -match 'Join-Path \$parent|Move-Item[^\r\n]*\$parent') { throw 'HA recovery does not use the protected external artifact paths.' }
  } finally { $script:LocalBackupRoot=$previousBackupRoot; Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
Check 'production preflight component decisions reach the orchestration engine' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  foreach($needle in @('CubiNeeded=[bool]$cubiNeeded','HANeeded=[bool]$haNeeded','cubi_hash','ha_hash','Built artifacts fingerprinted')) { if ($text -notmatch [regex]::Escape($needle)) { throw "missing final component deployment state: $needle" } }
}
Check 'successful deployment uses required order' {
  $ops=New-Ops
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
  if ($result.Result -ne 'Deployed') { throw "unexpected result $($result.Result)" }
  if (($script:order -join ',') -ne 'preflight,build,plan,backup,rehearse,quiesce,cubi,verify-cubi,restore-discovery,ha-file-transfer,ha-restart,health-check') { throw "wrong order: $($script:order -join ',')" }
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
  if ($script:order -contains 'quiesce' -or $script:order -contains 'restore-discovery') { throw 'discovery was paused for an update that does not stop the service' }
}
foreach ($failure in @('preflight','build','plan','backup','rehearse','quiesce','cubi','verify-cubi','restore-discovery','ha-file-transfer','ha-restart','health-check')) {
  Check "failure at $failure is surfaced" {
    $ops=New-Ops -FailAt $failure
    $threw=$false
    try { Invoke-AreaEventsDeploymentEngine -Operations $ops | Out-Null } catch { $threw=$_.Exception.Message -match "$failure simulated failure" }
    if (-not $threw) { throw 'failure was swallowed or replaced' }
    $restores=@($script:order | Where-Object { $_ -eq 'restore-discovery' }).Count
    if ($failure -in @('preflight','build','plan','backup','rehearse','quiesce','restore-discovery') -and $script:recovered) { throw 'code recovery ran although the service was never changed or was already verified' }
    if ($failure -in @('cubi','verify-cubi','ha-file-transfer','ha-restart','health-check') -and -not $script:recovered) { throw 'recovery was not attempted after production modification was attempted' }
    if ($failure -in @('preflight','build','plan','backup','rehearse') -and $restores) { throw 'the discovery state was changed before the maintenance window opened' }
    if ($failure -in @('quiesce','cubi','verify-cubi','restore-discovery','ha-file-transfer','ha-restart','health-check') -and $restores -ne 1) { throw "the discovery state was restored $restores times" }
    if ($failure -in @('cubi','verify-cubi') -and ($script:order -join ',') -notmatch "$failure,recover,restore-discovery$") { throw "discovery must be restored after the code recovery: $($script:order -join ',')" }
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
Check 'DPAPI credential file round-trips despite Set-Content line ending' {
  $path=Join-Path ([IO.Path]::GetTempPath()) ('ae-token-'+[guid]::NewGuid().ToString('N')+'.dpapi')
  $expected='local-only-credential-roundtrip-test'
  $plain=ConvertTo-SecureString $expected -AsPlainText -Force
  $loaded=$null; $ptr=[IntPtr]::Zero
  try {
    Set-Content -LiteralPath $path -Value (ConvertFrom-SecureString $plain) -Encoding ASCII
    $loaded=Import-AreaEventsDpapiCredential -Path $path
    $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($loaded)
    if ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) -ne $expected) { throw 'credential value changed during encrypted-file read' }
  } finally {
    if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    if ($loaded) { $loaded.Dispose() }; $plain.Dispose()
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  }
}
Check 'credential creation and replacement use current-user DACL and never write plaintext' {
  $root=Join-Path ([IO.Path]::GetTempPath()) ('ae-token-acl-'+[guid]::NewGuid().ToString('N'))
  $path=Join-Path $root 'ha-token.dpapi'
  $tokens=@('synthetic-initial-token-for-acl-test','synthetic-replacement-token-for-acl-test')
  $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
  function Assert-CredentialFile([string]$Expected) {
    $secure=ConvertTo-SecureString $Expected -AsPlainText -Force
    try {
      $savedOutput=@(Save-HaCredentialFile -Secure $secure -Path $path)
      if ($savedOutput.Count) { throw 'credential helper emitted unexpected output' }
    } finally { $secure.Dispose() }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'credential file was not created' }
    $stored=Get-Content -Raw -LiteralPath $path
    if ($stored.Contains($Expected)) { throw 'plaintext token was written to the credential file' }
    $acl=Get-Acl -LiteralPath $path
    $rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 1 -or
        $rules[0].IdentityReference.Value -ne $sid.Value -or
        $rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $rules[0].FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
        $rules[0].IsInherited) { throw 'credential DACL is not restricted to current-user full control' }
    $loaded=Import-AreaEventsDpapiCredential -Path $path
    $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($loaded)
    try { if ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) -ne $Expected) { throw 'DPAPI token decryption did not match the input' } }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $loaded.Dispose() }
  }
  try {
    Assert-CredentialFile $tokens[0]
    Assert-CredentialFile $tokens[1]
  } finally { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
Check 'DACL repair preserves an existing encrypted credential file' {
  $root=Join-Path ([IO.Path]::GetTempPath()) ('ae-token-preserve-'+[guid]::NewGuid().ToString('N'))
  $path=Join-Path $root 'ha-token.dpapi'; New-Item -ItemType Directory -Path $root | Out-Null
  $token='synthetic-preserved-token-for-acl-test'; $secure=ConvertTo-SecureString $token -AsPlainText -Force
  try {
    Set-Content -LiteralPath $path -Value (ConvertFrom-SecureString $secure) -Encoding ASCII
    $before=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    Set-HaCredentialFileDacl -Path $path
    $after=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($before -ne $after) { throw 'DACL repair changed the encrypted credential file contents' }
    $loaded=Import-AreaEventsDpapiCredential -Path $path; $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($loaded)
    try { if ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) -ne $token) { throw 'preserved credential did not decrypt after DACL repair' } }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $loaded.Dispose() }
  } finally { $secure.Dispose(); Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
Check 'remote Python transport preserves source without shell quoting' {
  $command=New-AreaEventsRemotePythonCommand -Code 'print("quoted-value")' -Variables @{probe='value'} -TimeoutSeconds 17
  if ($command -match '["'']' -or $command -notmatch '^printf %s [A-Za-z0-9+/=]+ \| base64 -d \| timeout --signal=TERM --kill-after=5s 17s python3 -$') { throw 'remote command is not shell quote-free or bounded' }
  $encoded=[regex]::Match($command,'^printf %s ([A-Za-z0-9+/=]+)').Groups[1].Value
  $wrapper=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
  if ($wrapper -notmatch 'base64\.b64decode\("' -or $wrapper -notmatch '<ae>') { throw 'encoded Python wrapper lost its quoting or source framing' }
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
    Set-Content (Join-Path $root 'manager.js') 'manager'
    $names=@('manifest.js','registry.js','community.js','generic.js','nasa-mission.js','new-renderer.js')
    foreach($name in $names) { Set-Content (Join-Path $presentation $name) 'module' }
    Set-Content (Join-Path $presentation 'manifest.js') @'
import { communityRenderer } from "./community.js";
import { newRenderer } from "./new-renderer.js";
'@
    $assets=@(Get-AreaEventsRequiredFrontendAssets $root)
    if ($assets.Count -ne 5 -or $assets -notcontains (Join-Path $root 'manager.js') -or $assets -notcontains (Join-Path $presentation 'new-renderer.js')) { throw "new renderer not represented in required assets: $($assets.Count)" }
  } finally { Remove-Item -LiteralPath $root -Recurse -Force }
}
Check 'modules the card imports are required assets' {
  $root=Join-Path ([IO.Path]::GetTempPath()) ('ae-card-imports-'+[guid]::NewGuid().ToString('N'))
  $presentation=Join-Path $root 'presentation'; $filters=Join-Path $root 'filters'
  New-Item -ItemType Directory -Path $presentation,$filters | Out-Null
  try {
    Set-Content (Join-Path $root 'manager.js') 'manager'
    Set-Content (Join-Path $presentation 'generic.js') 'module'
    Set-Content (Join-Path $presentation 'manifest.js') 'import { genericRenderer } from "./generic.js";'
    Set-Content (Join-Path $root 'area-events-card.js') @'
import { PRESENTATIONS } from "./presentation/manifest.js";
import { resolveWindow } from "./filters/date-windows.js";
'@
    $threw=$false; try { Get-AreaEventsRequiredFrontendAssets $root | Out-Null } catch { $threw=$true }
    if (-not $threw) { throw 'a missing card module was not reported' }
    Set-Content (Join-Path $filters 'date-windows.js') 'module'
    $assets=@(Get-AreaEventsRequiredFrontendAssets $root)
    if ($assets.Count -ne 5 -or $assets -notcontains (Join-Path $filters 'date-windows.js')) { throw "card module not represented in required assets: $($assets.Count)" }
  } finally { Remove-Item -LiteralPath $root -Recurse -Force }
}
Check 'the whole fingerprinted module graph is required and every fingerprint must match its module' {
  $repo=Join-Path $PSScriptRoot '..\custom_components\area_events\frontend'
  $assets=@(Get-AreaEventsRequiredFrontendAssets $repo | ForEach-Object { $_.Substring([IO.Path]::GetFullPath($repo).Length+1).Replace('\','/') })
  foreach ($name in 'area-events-card.js','manager.js','presentation/manifest.js','presentation/registry.js','presentation/community.js','presentation/generic.js',
                     'presentation/nasa-mission.js','presentation/contract.js','filters/date-windows.js') {
    if ($assets -notcontains $name) { throw "the built frontend's $name is not a required asset: $($assets -join ', ')" }
  }
  if ($assets[0] -ne 'area-events-card.js') { throw 'the card must stay the first asset (the readiness probe)' }
  $root=Join-Path ([IO.Path]::GetTempPath()) ('ae-fingerprints-'+[guid]::NewGuid().ToString('N'))
  $presentation=Join-Path $root 'presentation'; New-Item -ItemType Directory -Path $presentation | Out-Null
  try {
    Set-Content (Join-Path $root 'manager.js') 'manager'
    Set-Content (Join-Path $presentation 'contract.js') 'export const contract = 1;'
    $contract=Get-AreaEventsModuleFingerprint (Join-Path $presentation 'contract.js')
    Set-Content (Join-Path $presentation 'community.js') "import { contract } from `"./contract.js?v=$contract`";"
    $community=Get-AreaEventsModuleFingerprint (Join-Path $presentation 'community.js')
    Set-Content (Join-Path $presentation 'manifest.js') "import { communityRenderer } from `"./community.js?v=$community`";"
    $manifest=Get-AreaEventsModuleFingerprint (Join-Path $presentation 'manifest.js')
    Set-Content (Join-Path $root 'area-events-card.js') "import { PRESENTATIONS } from `"./presentation/manifest.js?v=$manifest`";"
    $assets=@(Get-AreaEventsRequiredFrontendAssets $root)
    if ($assets.Count -ne 5 -or $assets -notcontains (Join-Path $presentation 'contract.js')) { throw "a module two imports deep is not required: $($assets.Count)" }
    [IO.File]::WriteAllText((Join-Path $presentation 'contract.js'), "export const contract = 1;`r`n".Replace("`r`n", "`n"))
    $lf=Get-AreaEventsModuleFingerprint (Join-Path $presentation 'contract.js')
    [IO.File]::WriteAllText((Join-Path $presentation 'contract.js'), "export const contract = 1;`r`n")
    if ((Get-AreaEventsModuleFingerprint (Join-Path $presentation 'contract.js')) -ne $lf) { throw 'a CRLF checkout must have the fingerprint of its LF text' }
    Set-Content (Join-Path $presentation 'contract.js') 'export const contract = 2;'   # deployed without rebuilding the modules that import it
    $message=''; try { Get-AreaEventsRequiredFrontendAssets $root | Out-Null } catch { $message=$_.Exception.Message }
    if ($message -notmatch 'Module version mismatch: presentation/community.js imports \./contract\.js\?v=') { throw "a stale fingerprint was not reported: '$message'" }
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
Check 'Goose3 installation and self-check cover rehearsal, deployment, recovery and verification' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  function Get-Section([string]$From,[string]$To) {
    $a=$text.IndexOf($From); if ($a -lt 0) { throw "section not found: $From" }
    $b=$text.IndexOf($To,$a+1); if ($b -le $a) { throw "section end not found: $To" }
    $text.Substring($a,$b-$a)
  }
  $rehearsal=Get-Section 'function Invoke-Rehearsal(' 'function Invoke-CubiDeploy('
  $deploy=Get-Section 'function Invoke-CubiDeploy(' 'function Test-CubiHealth {'
  $health=Get-Section 'function Test-CubiHealth {' 'function Install-Ha {'
  $source=Get-Section 'function Test-CubiInstalledSource {' 'function Invoke-DeploymentPlan('
  $recovery=Get-Section "Write-Warning 'Restoring Cubi code only" "elseif (`$phase -eq 'Home Assistant installation')"
  foreach($case in @(@{N='rehearsal';T=$rehearsal},@{N='deployment';T=$deploy},@{N='recovery';T=$recovery},@{N='verification';T=$health},@{N='installed-source check';T=$source})) {
    if ($case.T -notmatch '\$script:ImageDepsPy\+"`n"\+@') { throw "$($case.N) does not include the image dependency helper" }
  }
  foreach($case in @(@{N='rehearsal';T=$rehearsal},@{N='deployment';T=$deploy},@{N='recovery';T=$recovery})) {
    if ($case.T -notmatch 'ae_install_images\(') { throw "$($case.N) does not install the pinned image dependencies" }
  }
  foreach($case in @(@{N='rehearsal';T=$rehearsal},@{N='deployment';T=$deploy},@{N='recovery';T=$recovery},@{N='verification';T=$health},@{N='installed-source check';T=$source})) {
    if ($case.T -notmatch 'ae_image_smoke\(') { throw "$($case.N) does not run the image extraction self-check" }
  }
  if ($rehearsal -match 'schema -ge \$script:TargetSchema\) \{ return') { throw 'rehearsal is skipped when the schema is current, so dependencies would go unrehearsed' }
  if ($deploy.IndexOf('ae_image_smoke(') -gt $deploy.IndexOf("with open(unit,'w'")) { throw 'deployment writes the service unit before the image self-check' }
  if ($health.IndexOf('ae_image_smoke(') -gt $health.IndexOf('$deadline=')) { throw 'image verification must precede the API wait' }
  if ($text -notmatch "--only-binary=:all:" -or $text -notmatch "optional-dependencies") { throw 'dependencies must come from the pinned pyproject extra as wheels' }
  if ($text -match "ae_image_smoke\([^)]*\)\s+or True|except Exception:\s*pass") { throw 'a failed image self-check may be swallowed' }
}
Check 'embedded Python helper installs from the images extra and fails on a missing or broken engine' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  $a=$text.IndexOf("`$script:ImageDepsPy=@'"); $b=$text.IndexOf("'@",$a+10)
  if ($a -lt 0 -or $b -lt 0) { throw 'helper not found' }
  $helper=$text.Substring($a,$b-$a); $helper=$helper.Substring($helper.IndexOf("`n")+1)
  $dir=Join-Path ([IO.Path]::GetTempPath()) ('ae-helper-'+[guid]::NewGuid().ToString('N')); New-Item -ItemType Directory $dir | Out-Null
  try {
    Set-Content -LiteralPath (Join-Path $dir 'helper.py') -Value $helper -Encoding UTF8
        $out=& python (Join-Path $PSScriptRoot 'test-deploy-image-helper.py') (Join-Path $dir 'helper.py') $dir 2>&1
    if ($LASTEXITCODE -ne 0 -or "$out" -notmatch 'helper ok') { throw "helper behavior check failed: $out" }
  } finally { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue }
}
Check 'every Invoke-CubiPython timeout is accepted by the real remote-command builder' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  $timeouts=@([regex]::Matches($text,'(?m)Invoke-CubiPython\s+\S+[^
]*?\s(\d+)\s*(?:\||$)') | ForEach-Object { [int]$_.Groups[1].Value })
  $calls=@([regex]::Matches($text,'Invoke-CubiPython\s+[$"]')).Count
  if ($timeouts.Count -lt 11 -or $timeouts.Count -ne $calls) { throw "found $($timeouts.Count) timeouts for $calls calls" }
  foreach($t in $timeouts) {
    # Invoke-CubiPython hands the builder Max(5, timeout-10); the real ValidateRange runs here.
    $command=New-AreaEventsRemotePythonCommand -Code 'print(1)' -TimeoutSeconds ([Math]::Max(5,$t-10))
    if ($command -notmatch "timeout --signal=TERM --kill-after=5s $([Math]::Max(5,$t-10))s python3 -") { throw "unexpected remote command for $t" }
  }
  $rejected=$false; try { $null=New-AreaEventsRemotePythonCommand -Code 'print(1)' -TimeoutSeconds 601 } catch { $rejected=$true }
  if (-not $rejected) { throw 'the builder no longer enforces its 600 s limit; this check is stale' }
}
function New-FakeHa([string[]]$Answers, [int]$SecondsPerAttempt) {
  # A fake HA whose attempts each take $SecondsPerAttempt on a fake clock; the last answer repeats.
  $script:haNow = [DateTime]'2026-10-29T00:00:00Z'; $script:haAnswers = $Answers; $script:haCalls = 0; $script:haLimits = @(); $script:haCost = $SecondsPerAttempt
  return @{
    Fetch = { param($t) $script:haLimits += $t; $answer = $script:haAnswers[[Math]::Min($script:haCalls, $script:haAnswers.Count - 1)]; $script:haCalls++
              $script:haNow = $script:haNow.AddSeconds([Math]::Min($script:haCost, $t)); if ($answer -eq 'throw') { throw 'curl.exe failed: Operation timed out' }; $answer }
    Clock = { $script:haNow }
    Sleep = { param($s) $script:haNow = $script:haNow.AddSeconds($s) }
  }
}
Check 'HA readiness: a first answer after about 21 s (a Core restart) is waited for, within 60 s' {
  $ha = New-FakeHa @('throw', '200') 12   # the first attempt hits its 12 s limit, the next is served
  $attempts = Wait-AreaEventsHaAsset -Fetch $ha.Fetch -Clock $ha.Clock -Sleep $ha.Sleep -DeadlineSeconds 60 -AttemptSeconds 12 -PauseSeconds 3
  if ($attempts -ne 2) { throw "expected 2 attempts, got $attempts" }
  if (($script:haNow - [DateTime]'2026-10-29T00:00:00Z').TotalSeconds -gt 60) { throw 'waited beyond the deadline' }
}
Check 'HA readiness: a slow start answering 502 and 404 is retried until it serves' {
  $ha = New-FakeHa @('000', '502', '404', '200') 2
  if ((Wait-AreaEventsHaAsset -Fetch $ha.Fetch -Clock $ha.Clock -Sleep $ha.Sleep) -ne 4) { throw 'not retried to success' }
}
Check 'HA readiness: an asset served at once costs one fast attempt and no pause' {
  $ha = New-FakeHa @('200') 0
  if ((Wait-AreaEventsHaAsset -Fetch $ha.Fetch -Clock $ha.Clock -Sleep $ha.Sleep) -ne 1) { throw 'more than one attempt' }
  if ($script:haNow -ne [DateTime]'2026-10-29T00:00:00Z') { throw 'paused although HA answered' }
}
Check 'HA readiness: an asset that is never served fails at the 60 s deadline, with the last answer' {
  $ha = New-FakeHa @('throw') 12
  $message = $null
  try { $null = Wait-AreaEventsHaAsset -Fetch $ha.Fetch -Clock $ha.Clock -Sleep $ha.Sleep -DeadlineSeconds 60 -AttemptSeconds 12 -PauseSeconds 3 } catch { $message = $_.Exception.Message }
  if ($message -notmatch 'within 60 seconds.*Operation timed out') { throw "unexpected outcome: $message" }
  $elapsed = ($script:haNow - [DateTime]'2026-10-29T00:00:00Z').TotalSeconds
  if ($elapsed -gt 60 -or $elapsed -lt 55) { throw "gave up after $elapsed s" }
  if (@($script:haLimits | Where-Object { $_ -gt 12 -or $_ -lt 1 }).Count) { throw "an attempt exceeded its limit: $($script:haLimits -join ',')" }
}
Check 'HA readiness is polled before the asset downloads, which keep their 12 s limit and hash comparison' {
  $body = (Get-Command Test-HaAssets).Definition
  if ($body -notmatch 'Wait-AreaEventsHaAsset -DeadlineSeconds 60') { throw 'readiness polling is missing' }
  if ($body.IndexOf('Wait-AreaEventsHaAsset') -gt $body.IndexOf('foreach ($f in $assets)')) { throw 'readiness must come before the downloads' }
  if ($body -notmatch "'--max-time','12'" -or $body -notmatch 'Served asset differs from source') { throw 'the per-asset limit or the hash check changed' }
}
Check 'the readiness probe runs through the script''s bounded runner with the caller''s variables' {
  function Probe-Scope { $expected = '200'; Wait-AreaEventsHaAsset -DeadlineSeconds 30 -PauseSeconds 0 -Fetch { param($s) (Invoke-Bounded 'cmd.exe' @('/c', "echo $expected") ($s + 8)).Trim() } }
  if ((Probe-Scope) -ne 1) { throw 'the probe did not see the caller scope or the bounded runner' }
}
# --- the maintenance window: the deployer's own pause, drain, stop and restore functions against a fake Cubi on a fake clock ---------------
Check 'discovery states: running and paused are healthy; drained needs the pause, an idle host loop and no lease' {
  $s={ param($state,$paused,$leased) Resolve-AreaEventsDiscoveryState -Status ([pscustomobject]@{state=$state;paused=$paused;leased=$leased}) }
  $running=& $s 'running' $false 0; $busy=& $s 'running' $true 1; $unseen=& $s 'running' $true 0; $leasedPaused=& $s 'paused' $true 1; $drained=& $s 'paused' $true 0
  if (-not $running.Healthy -or $running.Drained) { throw 'a running service is healthy and not drained' }
  if (-not $busy.Healthy -or $busy.Drained -or $unseen.Drained -or $leasedPaused.Drained) { throw 'a scan still running, or a pause the host has not seen, counted as drained' }
  if (-not $drained.Healthy -or -not $drained.Drained) { throw 'a paused, idle service without leases is drained' }
  if ((& $s 'stopped' $false 0).Healthy) { throw 'a stopped host loop passed as healthy' }
  foreach ($bad in @(@{state='running';paused=$null;leased=0},@{state='running';paused=$false;leased=$null},@{state='running';paused=$false;leased=-1},@{state=$null;paused=$false;leased=0})) {
    $threw=$false; try { Resolve-AreaEventsDiscoveryState -Status ([pscustomobject]$bad) | Out-Null } catch { $threw=$true }
    if (-not $threw) { throw "an invalid discovery status was accepted: $($bad | ConvertTo-Json -Compress)" }
  }
  if (-not (Test-AreaEventsDiscoveryRestored -OriginalPaused $false -Current $running) -or (Test-AreaEventsDiscoveryRestored -OriginalPaused $false -Current $drained)) { throw 'running restore check is wrong' }
  if (-not (Test-AreaEventsDiscoveryRestored -OriginalPaused $true -Current $drained) -or (Test-AreaEventsDiscoveryRestored -OriginalPaused $true -Current $running)) { throw 'paused restore check is wrong' }
}
function New-FakeCubi([bool]$Paused, [int[]]$Leased=@(0), [int]$HostLag=1) {
  # One Discovery service: its pause setting, the leased jobs each probe sees (the last repeats), and a host loop that observes a changed setting
  # $HostLag probes later. Every deployer action is recorded in Calls.
  $script:cubi=[pscustomobject]@{Paused=$Paused;Host=$(if ($Paused) { 'paused' } else { 'running' });Leased=$Leased;Probes=0;Lag=0;HostLag=$HostLag;Active=$true
                                 Calls=[System.Collections.Generic.List[string]]::new()}
  $script:fakeNow=[DateTime]'2026-11-01T00:00:00Z'
  $script:DiscoveryClock={ $script:fakeNow }
  $script:DiscoverySleep={ param($Seconds) $script:fakeNow=$script:fakeNow.AddSeconds($Seconds) }
  $script:OriginalDiscoveryPaused=$Paused; $script:DiscoveryQuiesced=$false; $script:SchemaChanged=$false
}
function Get-CubiDiscoveryState {
  $c=$script:cubi; $c.Probes++; $c.Calls.Add('probe')
  if (-not $c.Active) { throw 'connection refused' }
  $leased=$c.Leased[[Math]::Min($c.Probes-1,$c.Leased.Count-1)]
  if ($c.Lag -gt 0) { $c.Lag-- } else { $c.Host=$(if ($c.Paused -and $leased -eq 0) { 'paused' } else { 'running' }) }
  Resolve-AreaEventsDiscoveryState -Status ([pscustomobject]@{state=$c.Host;paused=$c.Paused;leased=$leased})
}
function Set-CubiDiscoveryPaused([bool]$Paused,[switch]$AllowStopped) {
  $c=$script:cubi; $action=$(if ($Paused) { 'pause' } else { 'resume' })
  if (-not $c.Active) { if (-not $AllowStopped) { throw 'the Discovery service is inactive' }; $c.Paused=$Paused; $c.Calls.Add("cli-$action"); return [pscustomobject]@{via='cli'} }
  if ($c.Paused -ne $Paused) { $c.Lag=$c.HostLag }
  $c.Paused=$Paused; $c.Calls.Add($action); [pscustomobject]@{via='api'}
}
function Stop-CubiService([switch]$RequireDrained) {
  $c=$script:cubi
  if ($RequireDrained -and -not ($c.Paused -and $c.Host -eq 'paused')) { throw 'discovery is not drained; the service was not stopped' }
  $c.Calls.Add($(if ($RequireDrained) { 'stop-drained' } else { 'stop' })); $c.Active=$false
}
function Invoke-CubiPython { $script:cubi.Calls.Add('remote-code-restore'); throw 'offline test: no remote command' }
function New-MaintenanceOps([scriptblock]$DeployCubi) {
  $ops=New-Ops -HANeeded $false
  $ops.Quiesce={ param($s) $script:cubi.Calls.Add('quiesce'); Invoke-DiscoveryQuiesce }
  $ops.DeployCubi=$DeployCubi
  $ops.VerifyCubi={ param($s) $script:cubi.Calls.Add('verify'); if ((Get-CubiDiscoveryState).State -notin @('running','paused')) { throw 'unhealthy' } }
  $ops.RestoreDiscovery={ param($s) Restore-DiscoveryState }
  $ops.Recover={ param($phase,$s,$schema,$ha) $script:cubi.Calls.Add("recover:$phase"); Invoke-Recovery $phase $null $schema $ha }
  return $ops
}
$DrainTimeoutSeconds=420
Check 'normal running deployment: pauses, waits for the running scan, stops drained, then restores running' {
  New-FakeCubi -Paused $false -Leased @(1,1,1,0)
  $ops=New-MaintenanceOps { param($s,$r) Stop-CubiService -RequireDrained; $script:cubi.Calls.Add('install-migrate'); $script:cubi.Active=$true; [pscustomobject]@{Changed=$true;SchemaChanged=$true} }
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
  $calls=@($script:cubi.Calls | Where-Object { $_ -ne 'probe' }) -join ','
  if ($result.Result -ne 'Deployed' -or $calls -ne 'quiesce,pause,stop-drained,install-migrate,verify,resume') { throw "unexpected maintenance sequence: $calls" }
  if ($script:cubi.Paused -or $script:cubi.Host -ne 'running' -or $script:DiscoveryQuiesced) { throw 'discovery was not left running' }
  if (($script:fakeNow - [DateTime]'2026-11-01T00:00:00Z').TotalSeconds -ge $DrainTimeoutSeconds) { throw 'the drain took the whole deadline' }
}
Check 'paused deployment: an originally paused service is drained, deployed and left paused' {
  New-FakeCubi -Paused $true -Leased @(0)
  $ops=New-MaintenanceOps { param($s,$r) Stop-CubiService -RequireDrained; $script:cubi.Active=$true; [pscustomobject]@{Changed=$true;SchemaChanged=$false} }
  $result=Invoke-AreaEventsDeploymentEngine -Operations $ops
  if ($result.Result -ne 'Deployed' -or -not $script:cubi.Paused -or $script:cubi.Host -ne 'paused') { throw 'an originally paused service did not stay paused' }
  if ($script:cubi.Calls -contains 'resume') { throw 'a paused service was resumed' }
  if ($script:cubi.Calls -notcontains 'stop-drained') { throw 'the paused service was not stopped through the drain check' }
}
Check 'drain timeout: gives up at the deadline without stopping anything and restores running discovery' {
  New-FakeCubi -Paused $false -Leased @(1)
  $script:DrainTimeoutSeconds=60
  $ops=New-MaintenanceOps { param($s,$r) $script:cubi.Calls.Add('cutover'); [pscustomobject]@{Changed=$true;SchemaChanged=$false} }
  $message=''; try { Invoke-AreaEventsDeploymentEngine -Operations $ops 3>$null | Out-Null } catch { $message=$_.Exception.Message } finally { $script:DrainTimeoutSeconds=420 }
  if ($message -notmatch 'did not drain within 60 seconds.*leased jobs 1') { throw "unexpected outcome: $message" }
  if ($script:cubi.Calls -contains 'cutover' -or @($script:cubi.Calls | Where-Object { $_ -like 'stop*' -or $_ -like 'recover*' }).Count) { throw 'the service was touched after a failed drain' }
  $elapsed=($script:fakeNow - [DateTime]'2026-11-01T00:00:00Z').TotalSeconds
  if ($elapsed -lt 55 -or $elapsed -gt 60) { throw "gave up after $elapsed s" }
  if ($script:cubi.Paused -or $script:cubi.Host -ne 'running') { throw 'discovery was not restored after the failed drain' }
}
Check 'deployment failure: a failed cutover is recovered first, then the discovery state is restored' {
  New-FakeCubi -Paused $false -Leased @(0)
  $ops=New-MaintenanceOps { param($s,$r) Stop-CubiService -RequireDrained; throw 'cutover simulated failure' }
  $ops.Recover={ param($phase,$s,$schema,$ha) $script:cubi.Calls.Add("recover:$phase"); $script:cubi.Active=$true }   # the code-only rollback restarted the service
  $message=''; try { Invoke-AreaEventsDeploymentEngine -Operations $ops | Out-Null } catch { $message=$_.Exception.Message }
  $calls=@($script:cubi.Calls | Where-Object { $_ -ne 'probe' }) -join ','
  if ($message -notmatch 'cutover simulated failure') { throw "the original failure was lost: $message" }
  if ($calls -ne 'quiesce,pause,stop-drained,recover:Cubi cutover,resume') { throw "unexpected recovery sequence: $calls" }
  if ($script:cubi.Paused -or $script:cubi.Host -ne 'running') { throw 'discovery was not restored after recovery' }
}
Check 'schema migration failure: the service stays stopped with its new code, and only the persisted setting is restored' {
  New-FakeCubi -Paused $false -Leased @(0)
  $ops=New-MaintenanceOps { param($s,$r) Stop-CubiService -RequireDrained; $script:SchemaChanged=$true; throw 'migration simulated failure' }
  $message=''; try { Invoke-AreaEventsDeploymentEngine -Operations $ops 3>$null | Out-Null } catch { $message=$_.Exception.Message }
  $calls=@($script:cubi.Calls | Where-Object { $_ -ne 'probe' }) -join ','
  if ($message -notmatch 'migration simulated failure') { throw "the original failure was lost: $message" }
  if ($script:cubi.Calls -contains 'remote-code-restore') { throw 'older code was restored over an advanced schema' }
  if ($calls -ne 'quiesce,pause,stop-drained,recover:Cubi cutover,stop,cli-resume') { throw "unexpected sequence: $calls" }
  if ($script:cubi.Active -or $script:cubi.Paused) { throw 'the service must stay stopped with its original (running) setting persisted' }
}
Check 'code-only rollback stops the service cleanly before it restores any code' {
  New-FakeCubi -Paused $false -Leased @(0)
  $threw=$false; try { Invoke-Recovery 'Cubi cutover' $null $false $false 3>$null } catch { $threw=$true }
  if (-not $threw -or (@($script:cubi.Calls) -join ',') -ne 'stop,remote-code-restore') { throw "unexpected rollback order: $(@($script:cubi.Calls) -join ',')" }
}
Check 'successful restart: restore waits until the host loop runs again, and fails at its deadline if it never does' {
  New-FakeCubi -Paused $false -Leased @(0) -HostLag 3
  $script:DiscoveryQuiesced=$true; $script:cubi.Paused=$true; $script:cubi.Host='paused'
  Restore-DiscoveryState
  if ($script:cubi.Host -ne 'running' -or $script:DiscoveryQuiesced) { throw 'the restored state was not confirmed' }
  if (@($script:cubi.Calls | Where-Object { $_ -eq 'probe' }).Count -lt 4) { throw 'restore did not wait for the host loop' }
  New-FakeCubi -Paused $false -Leased @(0) -HostLag 1000
  $script:DiscoveryQuiesced=$true; $script:cubi.Paused=$true; $script:cubi.Host='paused'
  $message=''; try { Restore-DiscoveryState } catch { $message=$_.Exception.Message }
  if ($message -notmatch 'did not return to its original state within 60 seconds') { throw "unexpected outcome: $message" }
}
Check 'no maintenance window opens when the pause setting changed after preflight' {
  New-FakeCubi -Paused $false -Leased @(0)
  $script:OriginalDiscoveryPaused=$true
  $threw=$false; try { Invoke-DiscoveryQuiesce } catch { $threw=$true }
  Restore-DiscoveryState
  if (-not $threw -or $script:cubi.Calls -contains 'pause' -or $script:cubi.Calls -contains 'resume') { throw 'the setting was changed although its original value was unclear' }
}
Check 'the service is stopped only by the clean-stop helper, and code changes only while it is stopped' {
  $text=Get-Content -Raw (Join-Path $PSScriptRoot 'deploy-area-events.ps1')
  if ([regex]::Matches($text,"'systemctl','--user','stop'").Count -ne 1 -or $text -match 'systemctl --user stop|systemctl.{0,20}kill|SIGKILL|kill -9') { throw 'a stop or kill bypasses Stop-CubiService' }
  $deploy=$text.Substring($text.IndexOf('function Invoke-CubiDeploy('),$text.IndexOf('function Test-CubiHealth {')-$text.IndexOf('function Invoke-CubiDeploy('))
  $stop=$deploy.IndexOf('Stop-CubiService -RequireDrained'); $stage2=$deploy.IndexOf('$py=$script:ImageDepsPy')
  if ($stop -lt 0 -or $stage2 -lt $stop -or $deploy.IndexOf('$installPy @{') -gt $stop) { throw 'the cutover does not stop the drained service between dependency staging and code installation' }
  foreach ($section in @($deploy.Substring($stage2), $text.Substring($text.IndexOf("Write-Warning 'Restoring Cubi code only")))) {
    if ($section -notmatch "not in \('inactive','failed'\): raise RuntimeError") { throw 'code is changed without confirming the service is stopped' }
  }
  if ($text -notmatch 'Quiesce=\{ param\(\$s\) Invoke-DiscoveryQuiesce \}' -or $text -notmatch 'RestoreDiscovery=\{ param\(\$s\) Restore-DiscoveryState \}') { throw 'the maintenance window is not part of the normal deployment operations' }
}
if ($script:failures) { Write-Error "$script:failures deployment tests failed"; exit 1 }
Write-Host 'All deployment engine checks passed.'
