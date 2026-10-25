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
  if ($text -notmatch "if legacy and updated==unit_text" -or $text -notmatch "if modern and updated!=unit_text") { throw 'remote deployment does not preserve modern module startup' }
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
  if ($text -notmatch 'CoreRecoveryTimeoutSeconds' -or $text -notmatch 'Test-HaWebSocket' -or $text -notmatch 'Get-AreaEventsRequiredFrontendAssets') { throw 'HA restart, websocket or manifest-driven module checks are missing' }
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
