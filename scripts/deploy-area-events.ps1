<#
.SYNOPSIS
  Build, back up, rehearse and deploy the Area Events service and HA integration.
.PARAMETER DryRun
  Build and perform read-only preflight, then stop before production writes.
.PARAMETER SetupCredentials
  Prompt once and store an HA token encrypted with Windows DPAPI for this user.
#>
[CmdletBinding()]
param(
  [switch]$DryRun,
  [switch]$SetupCredentials,
  [string]$HaUrl = $(if ($env:HA_URL) { $env:HA_URL } else { 'http://192.168.1.48:8123' }),
  [string]$ConfigShare = $(if ($env:HA_CONFIG_SHARE) { $env:HA_CONFIG_SHARE } else { 'Z:\' }),
  [int]$RestartTimeoutSeconds = 240
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'area-events-deploy-hardening.psm1') -Force
$script:Repo = Split-Path -Parent $PSScriptRoot
$script:Cubi = 'robohobo@192.168.1.88'
$script:RemoteRoot = '/home/robohobo/area-events-discovery'
$script:RemoteBackup = $null
$script:CredentialPath = Join-Path $env:LOCALAPPDATA 'AreaEvents\ha-token.dpapi'
$script:LockPath = Join-Path ([IO.Path]::GetTempPath()) 'area-events-deploy.lock'
$script:LocalBackupRoot = Join-Path $env:LOCALAPPDATA 'AreaEventsBackups'
$schemaText = Get-Content -Raw (Join-Path $script:Repo 'area_events\discovery\service\persistence\schema.py')
$schemaVersions=[regex]::Matches($schemaText,'(?m)^\s*\((\d+),\s*"')
if (-not $schemaVersions.Count) { throw 'Could not read the migration versions from schema.py.' }
$script:TargetSchema = [int]$schemaVersions[$schemaVersions.Count-1].Groups[1].Value

function Write-Step([string]$Text) { Write-Host ([Environment]::NewLine + '== ' + $Text) -ForegroundColor Cyan }
function Set-HaCredentialFileDacl([Parameter(Mandatory)][string]$Path) {
  $fullPath = [IO.Path]::GetFullPath($Path)
  if (-not [IO.File]::Exists($fullPath)) { throw 'Encrypted Home Assistant credential file is missing.' }
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
  $icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
  $null = & $icacls $fullPath '/inheritance:r' '/grant:r' "*$($sid.Value):(F)" 2>&1
  if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the encrypted credential file DACL to the current Windows user.' }
  $acl = Get-Acl -LiteralPath $fullPath
  if (-not $acl.AreAccessRulesProtected) { throw 'Credential file DACL still inherits permissions.' }
  $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid.Value -or
      $rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
      $rules[0].FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
      $rules[0].IsInherited) {
    throw 'Credential file DACL does not grant only current-user full control.'
  }
}
function Save-HaCredentialFile([Parameter(Mandatory)][Security.SecureString]$Secure,[Parameter(Mandatory)][string]$Path) {
  $fullPath = [IO.Path]::GetFullPath($Path)
  $dir = Split-Path -Parent $fullPath
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $tempPath = Join-Path $dir ('.ha-token-' + [guid]::NewGuid().ToString('N') + '.tmp')
  $backupPath = $tempPath + '.bak'
  try {
    $ciphertext = ConvertFrom-SecureString $Secure
    [IO.File]::WriteAllText($tempPath, $ciphertext, [Text.Encoding]::ASCII)

    Set-HaCredentialFileDacl -Path $tempPath

    if ([IO.File]::Exists($fullPath)) { [IO.File]::Replace($tempPath, $fullPath, $backupPath) }
    else { [IO.File]::Move($tempPath, $fullPath) }
  } finally {
    if ([IO.File]::Exists($tempPath)) { [IO.File]::Delete($tempPath) }
    if ([IO.File]::Exists($backupPath)) { [IO.File]::Delete($backupPath) }
  }
}
function Save-HaCredential {
  if ($env:OS -ne 'Windows_NT') { throw 'Credential setup requires Windows DPAPI.' }
  $secure = Read-Host 'Paste an administrator long-lived HA token once (input is hidden)' -AsSecureString
  try {
    if ($secure.Length -lt 20) { throw 'Token input looks too short.' }
    Save-HaCredentialFile -Secure $secure -Path $script:CredentialPath
    Write-Host "Credential encrypted for this Windows account at $script:CredentialPath"
  } finally { $secure.Dispose() }
}
function Get-HaToken {
  if (-not (Test-Path -LiteralPath $script:CredentialPath)) { throw 'Run .\scripts\deploy-area-events.ps1 -SetupCredentials once first.' }
  $secure = Import-AreaEventsDpapiCredential -Path $script:CredentialPath
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $secure.Dispose() }
}
function Invoke-Bounded([string]$File,[string[]]$Arguments,[int]$TimeoutSeconds=60,[string]$WorkingDirectory=(Get-Location).Path) {
  $job=Start-Job -ArgumentList $File,(,$Arguments),$WorkingDirectory -ScriptBlock {
    param($exe,$argv,$cwd)
    Set-Location -LiteralPath $cwd
    $output=@(& $exe @argv 2>&1 | ForEach-Object { "$_" }) -join [Environment]::NewLine
    [pscustomobject]@{Code=$LASTEXITCODE;Output=$output}
  }
  try {
    if (-not (Wait-Job -Job $job -Timeout $TimeoutSeconds)) { Stop-Job $job -ErrorAction SilentlyContinue; throw "Timed out after $TimeoutSeconds seconds running $File." }
    $result=Receive-Job $job
    if ($job.State -ne 'Completed' -or $result.Code -ne 0) { throw "$File failed: $($result.Output)" }
    return $result.Output
  } finally { Remove-Job $job -Force -ErrorAction SilentlyContinue }
}
function Invoke-BoundedBlock([scriptblock]$Block,[object[]]$Arguments,[int]$TimeoutSeconds=60) {
  $job=Start-Job -ScriptBlock $Block -ArgumentList $Arguments
  try {
    if (-not (Wait-Job -Job $job -Timeout $TimeoutSeconds)) { Stop-Job $job -ErrorAction SilentlyContinue; throw "Timed out after $TimeoutSeconds seconds during a file operation." }
    $result=Receive-Job $job
    if ($job.State -ne 'Completed') { throw 'Bounded file operation failed.' }
    return $result
  } finally { Remove-Job $job -Force -ErrorAction SilentlyContinue }
}
function Get-SshBase { @('-o','BatchMode=yes','-o','ConnectTimeout=8','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=3') }
function Invoke-Cubi([string]$Command,[int]$Timeout=90) { Invoke-Bounded 'ssh.exe' (@(Get-SshBase)+@($script:Cubi,$Command)) $Timeout }
function Invoke-CubiPython([string]$Code,[hashtable]$Variables=@{},[int]$Timeout=90) {
  $remote=New-AreaEventsRemotePythonCommand -Code $Code -Variables $Variables -TimeoutSeconds ([Math]::Max(5,$Timeout-10))
  Invoke-Cubi $remote $Timeout
}
function Invoke-Build {
  $npm=(Get-Command npm.cmd -ErrorAction Stop).Source
  $null=Invoke-Bounded $npm @('run','build','--silent') 240 $script:Repo
  $card=Join-Path $script:Repo 'custom_components\area_events\frontend\area-events-card.js'
  if ((Get-Content -Raw $card) -match '__AREA_EVENTS_CARD_BUILD__') { throw 'Frontend build left an unresolved build placeholder.' }
  $script:Archive=Join-Path ([IO.Path]::GetTempPath()) ('area-events-'+[guid]::NewGuid().ToString('N')+'.tar')
  $null=Invoke-Bounded 'git.exe' @('-C',$script:Repo,'archive','--format=tar',"--output=$script:Archive",'HEAD','area_events','scripts/discovery.py') 45
}
function Test-FolderMatches([string]$Source,[string]$Destination) {
  return [bool](Invoke-BoundedBlock {
    param($src,$dst)
    $ErrorActionPreference='Stop'
    if (-not (Test-Path -LiteralPath $dst)) { return $false }
    foreach ($f in Get-ChildItem -LiteralPath $src -Recurse -File | Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' }) {
      $rel=$f.FullName.Substring($src.Length).TrimStart('\'); $target=Join-Path $dst $rel
      if (-not (Test-Path -LiteralPath $target) -or (Get-FileHash $f.FullName).Hash -ne (Get-FileHash $target).Hash) { return $false }
    }
    return $true
  } @($Source,$Destination) 90)
}
function Get-FolderFingerprint([string]$Root) {
  $lines=@(Get-ChildItem -LiteralPath $Root -Recurse -File | Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } | Sort-Object FullName | ForEach-Object {
    ($_.FullName.Substring($Root.Length).TrimStart('\').Replace('\','/'))+':'+(Get-FileHash -Algorithm SHA256 $_.FullName).Hash
  })
  $bytes=[Text.Encoding]::UTF8.GetBytes(($lines -join [Environment]::NewLine)); $sha=[Security.Cryptography.SHA256]::Create()
  try { [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
}
function Get-CubiFingerprint {
  $area=(Invoke-Bounded 'git.exe' @('-C',$script:Repo,'rev-parse','HEAD:area_events') 10).Trim()
  $entry=(Invoke-Bounded 'git.exe' @('-C',$script:Repo,'rev-parse','HEAD:scripts/discovery.py') 10).Trim()
  $bytes=[Text.Encoding]::UTF8.GetBytes($area+':'+$entry); $sha=[Security.Cryptography.SHA256]::Create()
  try { [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
}
function Get-HaCurrentUser {
  $wsUrl=$script:BaseUrl -replace '^http','ws'
  $socket=New-Object Net.WebSockets.ClientWebSocket
  $cancel=New-Object Threading.CancellationTokenSource
  $cancel.CancelAfter([TimeSpan]::FromSeconds(15))
  $buffer=New-Object byte[] 16384
  function Receive-PreflightMessage {
    $text=''
    do {
      $segment=New-Object ArraySegment[byte] -ArgumentList @(,$buffer)
      $part=$socket.ReceiveAsync($segment,$cancel.Token).GetAwaiter().GetResult()
      $text+=[Text.Encoding]::UTF8.GetString($buffer,0,$part.Count)
    } while (-not $part.EndOfMessage)
    $text | ConvertFrom-Json
  }
  function Send-PreflightMessage($message) {
    $bytes=[Text.Encoding]::UTF8.GetBytes(($message | ConvertTo-Json -Compress -Depth 4))
    $segment=New-Object ArraySegment[byte] -ArgumentList @(,$bytes)
    $socket.SendAsync($segment,[Net.WebSockets.WebSocketMessageType]::Text,$true,$cancel.Token).GetAwaiter().GetResult() | Out-Null
  }
  try {
    $socket.ConnectAsync([Uri]($wsUrl+'/api/websocket'),$cancel.Token).GetAwaiter().GetResult() | Out-Null
    if ((Receive-PreflightMessage).type -ne 'auth_required') { throw 'Unexpected HA WebSocket greeting during preflight.' }
    Send-PreflightMessage @{type='auth';access_token=$script:Token}
    if ((Receive-PreflightMessage).type -ne 'auth_ok') { throw 'HA rejected the stored token during WebSocket preflight.' }
    Send-PreflightMessage @{id=1;type='auth/current_user'}
    $result=Receive-PreflightMessage
    if (-not $result.success) { throw "HA current-user permission check failed: $($result.error.code)" }
    return $result.result
  } finally { $cancel.Cancel(); $socket.Dispose(); $cancel.Dispose() }
}
function Invoke-Preflight {
  Write-Step 'Preflight'
  $branch=(Invoke-Bounded 'git.exe' @('-C',$script:Repo,'branch','--show-current') 10).Trim()
  if ($branch -ne 'ha-native-refactor') { throw "Expected ha-native-refactor, found $branch." }
  $dirty=Invoke-Bounded 'git.exe' @('-C',$script:Repo,'status','--porcelain') 10
  $unexpected=@($dirty -split [Environment]::NewLine | Where-Object { $_ })
  if ($unexpected.Count) { throw "Resolve local changes before deployment: $($unexpected -join ', ')" }
  if (-not (Test-Path -LiteralPath (Join-Path $ConfigShare 'configuration.yaml'))) { throw "HA config share unavailable: $ConfigShare" }
  $script:Token=Get-HaToken; $script:BaseUrl=$HaUrl.TrimEnd('/')
  if ($script:BaseUrl -notmatch '^https?://[^/]+$') { throw 'HA URL must be an HTTP(S) origin.' }
  $haApi=Invoke-RestMethod -Method Get -Uri "$script:BaseUrl/api/" -Headers @{Authorization="Bearer $script:Token"} -TimeoutSec 12
  if ($haApi.message -notmatch 'API running') { throw 'HA REST API did not return its expected healthy response.' }
  $haConfig=Invoke-RestMethod -Method Get -Uri "$script:BaseUrl/api/config" -Headers @{Authorization="Bearer $script:Token"} -TimeoutSec 12
  $haUser=Get-HaCurrentUser
  Assert-AreaEventsHaPrerequisites -Config $haConfig -CurrentUser $haUser
  $shareProbeModule=Join-Path $PSScriptRoot 'area-events-deploy-hardening.psm1'
  $shareDestinations=@((Join-Path $ConfigShare 'custom_components'))
  $integrationDestination=Join-Path $ConfigShare 'custom_components\area_events'
  if (Test-Path -LiteralPath $integrationDestination -PathType Container) { $shareDestinations+= $integrationDestination }
  foreach ($destination in $shareDestinations) {
    try { $null=Invoke-BoundedBlock { param($path,$root) Import-Module $path -Force; Test-AreaEventsShareCapabilities -Root $root } @($shareProbeModule,$destination) 25 }
    catch { throw "HA Samba destination failed temporary write/rename/remove preflight at '$destination': $($_.Exception.Message)" }
  }
  # The documented current-user API confirms administrator role. No read-only HA API can prove a future restart request will be accepted without issuing that mutating request.
  $script:HaRestartPermissionEvidence='authenticated administrator confirmed; restart request itself is intentionally not probed'
  $script:HaLoaded=($haConfig.components -contains 'area_events')
  $py=@'
import json,os,shutil,sqlite3,subprocess,ssl,urllib.parse,urllib.request
root='/home/robohobo/area-events-discovery'; db=root+'/data/discovery/discovery.db'
unit=subprocess.run(['systemctl','--user','show','area-events-discovery.service','--property=ActiveState','--property=WorkingDirectory','--property=ExecStart'],capture_output=True,text=True,timeout=12,check=True).stdout
def can_create(path):
 p=path
 while not os.path.exists(p):
  parent=os.path.dirname(p)
  if parent==p: return False
  p=parent
 return os.access(p,os.W_OK|os.X_OK)
backup_root=os.path.expanduser('~/backups/area-events-deploy')
tools={name:shutil.which(name) is not None for name in ('python3','timeout','systemctl')}
c=sqlite3.connect('file:'+db+'?mode=ro',uri=True,timeout=5)
try:
 v=[r[0] for r in c.execute('select version from schema_migrations')]
 z={'unit':unit,'schema':max(v),'events':c.execute('select count(*) from events').fetchone()[0],'associations':c.execute('select count(*) from event_candidates').fetchone()[0],'integrity':c.execute('pragma quick_check').fetchone()[0],'foreign_keys':len(list(c.execute('pragma foreign_key_check'))),'code_writable':os.access(root,os.W_OK|os.X_OK) and os.access(root+'/scripts',os.W_OK|os.X_OK),'backup_writable':can_create(backup_root),'required_tools':tools}
finally: c.close()
token=open(root+'/data/discovery/api-read-token',encoding='utf-8').read().strip(); context=ssl.create_default_context(cafile=root+'/certs/cert.pem')
def api(path):
 req=urllib.request.Request('https://192.168.1.88:8780/v1/'+path,headers={'Authorization':'Bearer '+token})
 with urllib.request.urlopen(req,context=context,timeout=8) as r: return json.load(r)
status=api('discovery/status'); status_data=status.get('status',status); z['api_state']=status_data.get('state') or status_data.get('runtime',{}).get('state'); z['api_samples']={}
areas_page=api('areas')
if not isinstance(areas_page,dict) or not isinstance(areas_page.get('areas'),list): raise RuntimeError('invalid area feed schema')
area_keys={x.get('name'):x.get('area_key') for x in areas_page['areas'] if isinstance(x,dict)}
for area in ('San Diego','Austin'):
 area_key=area_keys.get(area)
 if not area_key: raise RuntimeError('configured area missing: '+area)
 page=api('events?area='+urllib.parse.quote(area_key)+'&limit=1')
 if not isinstance(page,dict) or not isinstance(page.get('events'),list): raise RuntimeError('invalid event feed schema for '+area)
 z['api_samples'][area]=len(page['events'])
p=os.path.expanduser('~/.local/state/area-events-deploy.json'); z['receipt']=json.load(open(p)) if os.path.isfile(p) else None
print(json.dumps(z))
'@
  $remote=Invoke-CubiPython $py @{} 40 | ConvertFrom-Json
  if ($remote.integrity -ne 'ok' -or $remote.foreign_keys -ne 0) { throw 'Cubi database integrity check failed.' }
  if (-not $remote.code_writable -or -not $remote.backup_writable -or @($remote.required_tools.PSObject.Properties | Where-Object { -not $_.Value }).Count) { throw 'Cubi code/backup write access or a required remote command is unavailable.' }
  if ($remote.schema -gt $script:TargetSchema) { throw "Production schema $($remote.schema) is newer than this source ($script:TargetSchema); refusing to deploy older code." }
  if ($remote.api_state -ne 'running') { throw 'Cubi discovery service API is not healthy.' }
  if ($remote.unit -notmatch 'ActiveState=active' -or $remote.unit -notmatch '/home/robohobo/area-events-discovery' -or $remote.unit -notmatch 'port 8780') { throw 'Expected Discovery service is not active on port 8780; refusing to act.' }
  $script:Head=(Invoke-Bounded 'git.exe' @('-C',$script:Repo,'rev-parse','HEAD') 10).Trim()
  $script:PreflightRemote=$remote
  $script:PreflightHaConfig=$haConfig
  $script:State=[pscustomobject]@{Remote=$remote;Head=$script:Head;HAPath=(Join-Path $ConfigShare 'custom_components\area_events');HaLoaded=[bool]$script:HaLoaded}
  Write-Host "HEAD $script:Head; schema $($remote.schema); events $($remote.events); HA administrator verified; restart permission not mutation-tested."
  [pscustomobject]@{State=$script:State}
}
function Invoke-DeploymentPlan($preflight) {
  $currentHead=(Invoke-Bounded 'git.exe' @('-C',$script:Repo,'rev-parse','HEAD') 10).Trim()
  if ($currentHead -ne $preflight.Head) { throw 'Git HEAD changed between preflight and deployment planning.' }
  $dirty=Invoke-Bounded 'git.exe' @('-C',$script:Repo,'status','--porcelain') 10
  $unexpected=@($dirty -split [Environment]::NewLine | Where-Object { $_ -and $_ -notmatch '^.. custom_components/area_events/frontend/area-events-card\.js\s*$' })
  if ($unexpected.Count) { throw "Build changed unexpected source files; refusing inconsistent source/artifact deployment: $($unexpected -join ', ')" }
  $script:CubiFingerprint=Get-CubiFingerprint
  $haSource=Join-Path $script:Repo 'custom_components\area_events'
  $script:HAFingerprint=Get-FolderFingerprint $haSource
  $remote=$preflight.Remote
  $ha=Test-FolderMatches $haSource (Join-Path $ConfigShare 'custom_components\area_events')
  $cubiNeeded=(-not $remote.receipt -or $remote.receipt.cubi_hash -ne $script:CubiFingerprint -or $remote.schema -lt $script:TargetSchema)
  $haNeeded=(-not $remote.receipt -or $remote.receipt.ha_hash -ne $script:HAFingerprint -or -not $ha -or -not $script:HaLoaded)
  $script:State=[pscustomobject]@{Remote=$remote;Head=$script:Head;HAPath=(Join-Path $ConfigShare 'custom_components\area_events');CubiNeeded=[bool]$cubiNeeded;HANeeded=[bool]$haNeeded;HaLoaded=[bool]$preflight.HaLoaded;CubiFingerprint=$script:CubiFingerprint;HAFingerprint=$script:HAFingerprint;RequiredAssets=@(Get-AreaEventsRequiredFrontendAssets (Join-Path $haSource 'frontend'))}
  $noChange=(-not $cubiNeeded -and -not $haNeeded)
  Write-Host "Built artifacts fingerprinted; Cubi update $cubiNeeded; HA update $haNeeded"
  [pscustomobject]@{NoChanges=[bool]$noChange;State=$script:State}
}
function Invoke-Backups($state) {
  $stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
  $script:RemoteBackup="/home/robohobo/backups/area-events-deploy/$stamp"
  $script:LocalBackup=Join-Path $script:LocalBackupRoot $stamp
  New-Item -ItemType Directory -Force $script:LocalBackup | Out-Null
  $py=@'
import hashlib,json,os,sqlite3,tarfile
root='/home/robohobo/area-events-discovery'; out=os.environ['AE_BACKUP']; os.makedirs(out,mode=0o700,exist_ok=False)
s=sqlite3.connect('file:'+root+'/data/discovery/discovery.db?mode=ro',uri=True,timeout=10); d=sqlite3.connect(out+'/discovery.db')
try:
 s.backup(d); d.commit()
 if d.execute('pragma quick_check').fetchone()[0]!='ok' or list(d.execute('pragma foreign_key_check')): raise RuntimeError('backup integrity failed')
 r={'schema':max(x[0] for x in d.execute('select version from schema_migrations')),'events':d.execute('select count(*) from events').fetchone()[0],'associations':d.execute('select count(*) from event_candidates').fetchone()[0],'sha256':hashlib.sha256(open(out+'/discovery.db','rb').read()).hexdigest()}
finally: s.close(); d.close()
with tarfile.open(out+'/accepted-files.tar.gz','w:gz') as t:
 t.add(root+'/area_events',arcname='area_events'); t.add(root+'/scripts/discovery.py',arcname='scripts/discovery.py')
 unit=os.path.expanduser('~/.config/systemd/user/area-events-discovery.service')
 if os.path.isfile(unit): t.add(unit,arcname='systemd/area-events-discovery.service')
 for name in os.listdir(root+'/data/discovery'):
  if name.startswith('discovery.db'): continue
  t.add(root+'/data/discovery/'+name,arcname='data/discovery/'+name)
r['archive_sha256']=hashlib.sha256(open(out+'/accepted-files.tar.gz','rb').read()).hexdigest()
with tarfile.open(out+'/accepted-files.tar.gz','r:gz') as t:
 members=t.getmembers(); names={m.name for m in members}
 if 'scripts/discovery.py' not in names or 'area_events' not in names: raise RuntimeError('code backup contents are incomplete')
 for m in members:
  if m.isfile():
   f=t.extractfile(m)
   if f is None or len(f.read())!=m.size: raise RuntimeError('code/media/config backup member failed verification')
r['archive_members']=len(members); r['backup']=out
open(out+'/verification.json','w').write(json.dumps(r,sort_keys=True)); print(json.dumps(r))
'@
  $result=Invoke-CubiPython $py @{AE_BACKUP=$script:RemoteBackup} 180 | ConvertFrom-Json
  if ($result.schema -ne $state.Remote.schema -or $result.events -ne $state.Remote.events -or $result.associations -ne $state.Remote.associations) { throw 'Cubi backup did not match preflight counts.' }
  $null=Invoke-BoundedBlock {
    param($source,$backup,$share)
    $ErrorActionPreference='Stop'
    $integration=Join-Path $backup 'area_events'; Copy-Item -LiteralPath $source -Destination $integration -Recurse -Force
    foreach ($f in Get-ChildItem -LiteralPath $source -Recurse -File | Where-Object { $_.Extension -ne '.pyc' -and $_.FullName -notmatch '\\__pycache__\\' }) {
      $rel=$f.FullName.Substring($source.Length).TrimStart('\'); $copy=Join-Path $integration $rel
      if ((Get-FileHash $f.FullName).Hash -ne (Get-FileHash $copy).Hash) { throw 'HA integration backup hash mismatch.' }
    }
    $dest=Join-Path $backup 'ha-config'; New-Item -ItemType Directory -Force -Path (Join-Path $dest '.storage') | Out-Null
    $config=Join-Path $share 'configuration.yaml'; Copy-Item -LiteralPath $config -Destination $dest -Force
    $storage=Join-Path $share '.storage'; $files=@((Join-Path $storage 'core.config_entries'))+@(Get-ChildItem $storage -Filter 'lovelace*' -File | ForEach-Object FullName)
    foreach ($f in $files) { if (Test-Path -LiteralPath $f) { Copy-Item -LiteralPath $f -Destination (Join-Path $dest ('.storage\'+(Split-Path -Leaf $f))) -Force } }
    foreach ($f in @($config)+@($files | Where-Object { Test-Path -LiteralPath $_ })) {
      $name=Split-Path -Leaf $f; $copy=if($name -eq 'configuration.yaml'){Join-Path $dest $name}else{Join-Path $dest ('.storage\'+$name)}
      if ((Get-FileHash $f).Hash -ne (Get-FileHash $copy).Hash) { throw "HA configuration backup hash mismatch: $name" }
    }
  } @($state.HAPath,$script:LocalBackup,$ConfigShare) 120
  $files=Get-ChildItem $script:LocalBackup -Recurse -File | ForEach-Object { (Get-FileHash $_.FullName).Hash+' '+$_.FullName.Substring($script:LocalBackup.Length+1) }
  Set-Content (Join-Path $script:LocalBackup 'sha256.txt') $files -Encoding ASCII
  $script:BackupCounts=$result
  Write-Host "Backups verified: $($result.backup); $script:LocalBackup"
}
function Invoke-Rehearsal($state) {
  if ($state.Remote.schema -ge $script:TargetSchema) { return [pscustomobject]@{Passed=$true;Skipped=$true} }
  $archive="/home/robohobo/.cache/ae-rehearsal-$([guid]::NewGuid().ToString('N')).tar"
  $null=Invoke-Bounded 'scp.exe' (@(Get-SshBase)+@($script:Archive,"$($script:Cubi):$archive")) 120
  $py=@'
import json,os,sqlite3,subprocess,tarfile,tempfile,shutil
stage=tempfile.mkdtemp(prefix='ae-rehearsal-'); snap=os.environ['AE_BACKUP']+'/discovery.db'
s=sqlite3.connect('file:'+snap+'?mode=ro',uri=True); d=sqlite3.connect(stage+'/rehearsal.db'); s.backup(d); d.close(); s.close()
with tarfile.open(os.environ['AE_ARCHIVE']) as t:
 members=t.getmembers()
 if any(os.path.isabs(m.name) or '..' in m.name.split('/') or m.issym() or m.islnk() for m in members): raise RuntimeError('unsafe deployment archive')
 t.extractall(stage,filter='data')
r=subprocess.run(['python3',stage+'/scripts/discovery.py','status','--db',stage+'/rehearsal.db'],cwd=stage,timeout=90,capture_output=True,text=True)
if r.returncode: raise RuntimeError('migration command failed: '+r.stderr[-1000:])
a=sqlite3.connect('file:'+stage+'/rehearsal.db?mode=ro',uri=True); b=sqlite3.connect('file:'+snap+'?mode=ro',uri=True)
try:
 keys={x[0] for x in a.execute('select event_key from events')}; old={x[0] for x in b.execute('select event_key from events')}
 q='select c.candidate_key,e.event_key,ec.role from event_candidates ec join candidates c using(candidate_id) join events e using(event_id)'
 links=set(a.execute(q)); oldlinks=set(b.execute(q)); v=max(x[0] for x in a.execute('select version from schema_migrations'))
 if v!=int(os.environ['AE_TARGET']) or not old<=keys or not oldlinks<=links or a.execute('pragma quick_check').fetchone()[0]!='ok' or list(a.execute('pragma foreign_key_check')): raise RuntimeError('schema or identity checks failed')
 print(json.dumps({'passed':True,'schema':v,'events':len(keys),'associations':len(links)}))
finally: a.close(); b.close(); shutil.rmtree(stage,ignore_errors=True); os.remove(os.environ['AE_ARCHIVE'])
'@
  $res=Invoke-CubiPython $py @{AE_ARCHIVE=$archive;AE_BACKUP=$script:RemoteBackup;AE_TARGET="$script:TargetSchema"} 180 | ConvertFrom-Json
  if (-not $res.passed) { throw 'Migration rehearsal failed.' }; return $res
}
function Invoke-CubiDeploy($state) {
  $archive="/home/robohobo/.cache/ae-deploy-$([guid]::NewGuid().ToString('N')).tar"
  $null=Invoke-Bounded 'scp.exe' (@(Get-SshBase)+@($script:Archive,"$($script:Cubi):$archive")) 120
  $py=@'
import json,os,shutil,sqlite3,subprocess,tarfile,tempfile
root='/home/robohobo/area-events-discovery'; stage=tempfile.mkdtemp(prefix='.ae-stage-',dir=root)
subprocess.run(['systemctl','--user','stop','area-events-discovery.service'],timeout=45,check=True)
with tarfile.open(os.environ['AE_ARCHIVE']) as t:
 members=t.getmembers()
 if any(os.path.isabs(m.name) or '..' in m.name.split('/') or m.issym() or m.islnk() for m in members): raise RuntimeError('unsafe deployment archive')
 t.extractall(stage,filter='data')
run=os.path.basename(os.environ['AE_ARCHIVE']).replace('.tar',''); old=root+'/.area-events-old-'+run
os.rename(root+'/area_events',old); os.rename(stage+'/area_events',root+'/area_events')
scriptold=root+'/scripts/discovery.py.old-'+run; os.rename(root+'/scripts/discovery.py',scriptold); shutil.copy2(stage+'/scripts/discovery.py',root+'/scripts/discovery.py')
try:
 r=subprocess.run(['python3',root+'/scripts/discovery.py','status','--db',root+'/data/discovery/discovery.db'],cwd=root,timeout=120,capture_output=True,text=True)
 if r.returncode: raise RuntimeError('migration failed: '+r.stderr[-1000:])
 a=sqlite3.connect('file:'+root+'/data/discovery/discovery.db?mode=ro',uri=True); b=sqlite3.connect('file:'+os.environ['AE_BACKUP']+'/discovery.db?mode=ro',uri=True)
 try:
  keys={x[0] for x in a.execute('select event_key from events')}; oldkeys={x[0] for x in b.execute('select event_key from events')}
  q='select c.candidate_key,e.event_key,ec.role from event_candidates ec join candidates c using(candidate_id) join events e using(event_id)'
  links=set(a.execute(q)); oldlinks=set(b.execute(q)); versions=[x[0] for x in a.execute('select version from schema_migrations')]
  if max(versions)!=int(os.environ['AE_TARGET']) or not oldkeys<=keys or not oldlinks<=links: raise RuntimeError('identity/schema check failed')
  if a.execute('pragma quick_check').fetchone()[0]!='ok' or list(a.execute('pragma foreign_key_check')): raise RuntimeError('database integrity failed')
  changed=max(versions)>int(os.environ['AE_OLD'])
 finally: a.close(); b.close()
 subprocess.run(['systemctl','--user','start','area-events-discovery.service'],timeout=45,check=True)
 shutil.rmtree(old,ignore_errors=True); os.remove(scriptold)
 print(json.dumps({'changed':True,'schema_changed':changed,'schema':max(versions),'events':len(keys)}))
finally: shutil.rmtree(stage,ignore_errors=True)
'@
  try {
    $res=Invoke-CubiPython $py @{AE_ARCHIVE=$archive;AE_BACKUP=$script:RemoteBackup;AE_OLD="$($state.Remote.schema)";AE_TARGET="$script:TargetSchema"} 300 | ConvertFrom-Json
    $script:SchemaChanged=[bool]$res.schema_changed
    ConvertTo-CubiDeployResult $res
  } catch {
    try {
      $check=Invoke-CubiPython "import sqlite3,json; c=sqlite3.connect('file:$script:RemoteRoot/data/discovery/discovery.db?mode=ro',uri=True); print(json.dumps({'schema':c.execute('select max(version) from schema_migrations').fetchone()[0]}))" @{} 15 | ConvertFrom-Json
      $script:SchemaChanged=($check.schema -gt $state.Remote.schema)
    } catch { $script:SchemaChanged=$true }
    throw
  }
}
function Test-CubiHealth {
  $py=@'
import json,ssl,urllib.parse,urllib.request
root='/home/robohobo/area-events-discovery'; token=open(root+'/data/discovery/api-read-token').read().strip(); ctx=ssl.create_default_context(cafile=root+'/certs/cert.pem')
def get(path):
 q=urllib.request.Request('https://192.168.1.88:8780/v1/'+path,headers={'Authorization':'Bearer '+token})
 with urllib.request.urlopen(q,context=ctx,timeout=3) as r: return json.load(r)
s=get('discovery/status'); s=s.get('status',s); state=s.get('state') or s.get('runtime',{}).get('state'); samples={}
areas_page=get('areas')
if not isinstance(areas_page,dict) or not isinstance(areas_page.get('areas'),list): raise RuntimeError('invalid area feed schema')
area_keys={x.get('name'):x.get('area_key') for x in areas_page['areas'] if isinstance(x,dict)}
for area in ('San Diego','Austin'):
 area_key=area_keys.get(area)
 if not area_key: raise RuntimeError('configured area missing: '+area)
 page=get('events?area='+urllib.parse.quote(area_key)+'&limit=1')
 if not isinstance(page,dict) or not isinstance(page.get('events'),list): raise RuntimeError('invalid event feed schema for '+area)
 samples[area]=len(page['events'])
print(json.dumps({'state':state,'samples':samples}))
'@
  $deadline=(Get-Date).AddSeconds(90); $h=$null; $last='no response'
  while ((Get-Date) -lt $deadline) {
    try { $h=Invoke-CubiPython $py @{} 25 | ConvertFrom-Json; if ($h.state -eq 'running' -and $h.samples.'San Diego' -gt 0 -and $h.samples.Austin -gt 0) { break } }
    catch { $last=$_.Exception.Message }
    Start-Sleep -Seconds 4
  }
  if ($null -eq $h -or $h.state -ne 'running' -or $null -eq $h.samples.'San Diego' -or $null -eq $h.samples.Austin) { throw "Cubi API did not become healthy within 90 seconds. Last check: $last" }
  Write-Host "API verified; valid event page lengths San Diego/Austin: $($h.samples.'San Diego')/$($h.samples.Austin)"
}
function Install-Ha {
  $old=$env:HA_TOKEN; $env:HA_TOKEN=$script:Token
  try {
    $ps=(Get-Command powershell.exe -ErrorAction Stop).Source
    $args=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',(Join-Path $PSScriptRoot 'deploy-ha.ps1'),'-SkipBuild','-SkipApp','-ConfigShare',$ConfigShare,'-Url',$script:BaseUrl,'-CoreRecoveryTimeoutSeconds',"$RestartTimeoutSeconds")
    if (-not $script:DeploymentState.HaLoaded) { $args+= '-Force' }
    $output=Invoke-Bounded $ps $args ($RestartTimeoutSeconds+240)
    if ($output) { Write-Host $output }
  } finally { $env:HA_TOKEN=$old }
  [pscustomobject]@{Changed=$true}
}
function Test-HaWebSocket {
  $wsUrl=$script:BaseUrl -replace '^http','ws'
  $socket=New-Object Net.WebSockets.ClientWebSocket
  $cancel=New-Object Threading.CancellationTokenSource
  $cancel.CancelAfter([TimeSpan]::FromSeconds(25))
  $buffer=New-Object byte[] 262144
  function Receive-HaMessage {
    $text=''
    do {
      $segment=New-Object ArraySegment[byte] -ArgumentList @(,$buffer)
      $part=$socket.ReceiveAsync($segment,$cancel.Token).GetAwaiter().GetResult()
      $text+=[Text.Encoding]::UTF8.GetString($buffer,0,$part.Count)
    } while (-not $part.EndOfMessage)
    $text | ConvertFrom-Json
  }
  function Send-HaMessage($message) {
    $bytes=[Text.Encoding]::UTF8.GetBytes(($message | ConvertTo-Json -Compress -Depth 8))
    $segment=New-Object ArraySegment[byte] -ArgumentList @(,$bytes)
    $socket.SendAsync($segment,[Net.WebSockets.WebSocketMessageType]::Text,$true,$cancel.Token).GetAwaiter().GetResult() | Out-Null
  }
  try {
    $socket.ConnectAsync([Uri]($wsUrl+'/api/websocket'),$cancel.Token).GetAwaiter().GetResult() | Out-Null
    if ((Receive-HaMessage).type -ne 'auth_required') { throw 'Unexpected HA WebSocket greeting.' }
    Send-HaMessage @{type='auth';access_token=$script:Token}
    if ((Receive-HaMessage).type -ne 'auth_ok') { throw 'HA rejected the stored access token over WebSocket.' }
    Send-HaMessage @{id=1;type='area_events/events';area='San Diego';limit=20}
    Send-HaMessage @{id=2;type='area_events/events';area='Austin';limit=20}
    $pages=@{}
    for ($i=0;$i -lt 8 -and $pages.Count -lt 2;$i++) {
      $m=Receive-HaMessage
      if ($m.id -in @(1,2)) { if (-not $m.success) { throw "HA event command failed: $($m.error.code)" }; $pages[[int]$m.id]=$m.result }
    }
    $page=$pages[1]; $austin=$pages[2]
    Assert-AreaEventsFeedResponse -Response $page -Area 'San Diego' | Out-Null
    Assert-AreaEventsFeedResponse -Response $austin -Area 'Austin' | Out-Null
    Send-HaMessage @{id=3;type='area_events/subscribe';area='San Diego'}
    $subscribed=$false; $view=$null
    for ($i=0;$i -lt 8;$i++) {
      $m=Receive-HaMessage
      if ($m.id -eq 3 -and $m.type -eq 'result') { if (-not $m.success) { throw "HA subscription failed: $($m.error.code)" }; $subscribed=$true }
      if ($m.id -eq 3 -and $m.type -eq 'event') { $view=$m.event; if ($subscribed) { break } }
    }
    if (-not $subscribed -or $null -eq $view) { throw 'HA subscription did not deliver its initial view.' }
    Assert-AreaEventsFeedResponse -Response $view -Area 'San Diego subscription' | Out-Null
    Send-HaMessage @{type='unsubscribe_events';subscription=3}
    $script:WsEventCount=@($page.events).Count; $script:WsAustinCount=@($austin.events).Count; $script:WsViewCount=@($view.events).Count
  } finally { $cancel.Cancel(); $socket.Dispose(); $cancel.Dispose() }
}
function Test-HaAssets {
  $front=Join-Path $script:Repo 'custom_components\area_events\frontend'
  $assets=@(Get-AreaEventsRequiredFrontendAssets $front)
  $servedDir=Join-Path ([IO.Path]::GetTempPath()) ('area-events-served-'+[guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $servedDir | Out-Null
  try {
    foreach ($f in $assets) {
      $rel=$f.Substring($front.Length+1).Replace('\','/')
      $served=Join-Path $servedDir ([guid]::NewGuid().ToString('N')+'.asset')
      $status=Invoke-Bounded 'curl.exe' @('--silent','--show-error','--max-time','12','--output',$served,'--write-out','%{http_code}',"$script:BaseUrl/area_events/$rel") 20
      if ($status -ne '200') { throw "HA did not serve $rel (HTTP $status)." }
      $hash=(Get-FileHash -Algorithm SHA256 $served).Hash
      if ($hash -ne (Get-FileHash $f).Hash) { throw "Served asset differs from source: $rel" }
    }
  } finally { Remove-Item -LiteralPath $servedDir -Recurse -Force -ErrorAction SilentlyContinue }
  $cfg=Invoke-RestMethod -Method Get -Uri "$script:BaseUrl/api/config" -Headers @{Authorization="Bearer $script:Token"} -TimeoutSec 15
  if ($cfg.state -ne 'RUNNING' -or $cfg.components -notcontains 'area_events') { throw 'HA or Area Events integration is not loaded.' }
  Test-HaWebSocket
  $card=Get-Content -Raw (Join-Path $front 'area-events-card.js')
  $script:BuildId=[regex]::Match($card,'AREA-EVENTS \d+(\.\d+)*-[0-9a-f]{8}').Value
  if (-not $script:BuildId) { throw 'Build identifier missing.' }
  $script:AssetCount=$assets.Count
}
function Get-HaRecoveryPaths([string]$BackupRoot,[string]$ComponentsRoot) {
  $backup=[IO.Path]::GetFullPath($BackupRoot).TrimEnd('\')
  $components=[IO.Path]::GetFullPath($ComponentsRoot).TrimEnd('\')
  $allowedRoot=[IO.Path]::GetFullPath($script:LocalBackupRoot).TrimEnd('\')
  if (-not $backup.StartsWith($allowedRoot+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'HA recovery backups must remain under the Area Events backup root.'
  }
  $cursor=$backup
  while ($cursor -and (Test-Path -LiteralPath $cursor)) {
    if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw 'HA recovery backup path contains a reparse point.'
    }
    $parent=Split-Path -Parent $cursor
    if (-not $parent -or $parent -eq $cursor) { break }
    $cursor=$parent
  }
  if ($backup.Equals($components,[StringComparison]::OrdinalIgnoreCase) -or
      $backup.StartsWith($components+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'HA recovery backups must be outside custom_components.'
  }
  $root=[IO.Path]::GetFullPath((Join-Path $backup 'ha-recovery'))
  if (-not $root.StartsWith($backup+'\',[StringComparison]::OrdinalIgnoreCase) -or
      $root.StartsWith($components+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'HA recovery backup path escaped its protected Area Events backup root.'
  }
  if (Test-Path -LiteralPath $root) {
    if ((Get-Item -LiteralPath $root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw 'HA recovery destination is a reparse point.'
    }
  }
  $id=[guid]::NewGuid().ToString('N')
  return [pscustomobject]@{
    Root=$root
    Failed=(Join-Path $root ('area_events_failed_'+$id))
    Partial=(Join-Path $root ('area_events_partial_'+$id))
  }
}
function Protect-HaRecoveryRoot([string]$Path) {
  New-Item -ItemType Directory -Path $Path -Force | Out-Null
  $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
  $icacls=Join-Path $env:SystemRoot 'System32\icacls.exe'
  $null=& $icacls $Path '/inheritance:r' '/grant:r' "*$($sid.Value):(OI)(CI)(F)" 2>&1
  if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the HA recovery backup DACL to the current Windows user.' }
  $acl=Get-Acl -LiteralPath $Path
  $rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
  if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 1 -or
      $rules[0].IdentityReference.Value -ne $sid.Value -or
      $rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
      $rules[0].FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
      $rules[0].IsInherited) { throw 'HA recovery backup DACL is not restricted to the current Windows user.' }
}
function Restore-HaBackup($state) {
  $saved=Join-Path $script:LocalBackup 'area_events'
  $componentsRoot=[IO.Path]::GetFullPath((Join-Path $ConfigShare 'custom_components')).TrimEnd('\')
  $liveResolved=[IO.Path]::GetFullPath($state.HAPath)
  $expectedLive=[IO.Path]::GetFullPath((Join-Path $componentsRoot 'area_events'))
  if (-not $liveResolved.Equals($expectedLive,[StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing an unexpected HA integration recovery path.' }
  if (-not (Test-Path -LiteralPath $saved)) { throw 'HA rollback copy is missing.' }
  if (Test-FolderMatches $saved $state.HAPath) { Write-Host 'HA files already match rollback copy; no restart needed.'; return }
  $recovery=Get-HaRecoveryPaths -BackupRoot $script:LocalBackup -ComponentsRoot $componentsRoot
  Protect-HaRecoveryRoot -Path $recovery.Root
  $failed=$recovery.Failed
  $partial=$recovery.Partial
  $null=Invoke-BoundedBlock {
    param($live,$source,$old,$partialPath)
    $ErrorActionPreference='Stop'
    if (Test-Path -LiteralPath $live) { Move-Item -LiteralPath $live -Destination $old }
    try { Copy-Item -LiteralPath $source -Destination $live -Recurse -Force }
    catch {
      if (Test-Path -LiteralPath $live) { Move-Item -LiteralPath $live -Destination $partialPath }
      if (Test-Path -LiteralPath $old) { Move-Item -LiteralPath $old -Destination $live }
      throw
    }
  } @($state.HAPath,$saved,$failed,$partial) 120
  if (-not (Test-FolderMatches $saved $state.HAPath)) { throw 'Restored HA integration did not pass hash verification.' }
  $headers=@{Authorization="Bearer $script:Token"}
  try { $null=Invoke-RestMethod -Method Post -Uri "$script:BaseUrl/api/services/homeassistant/restart" -Headers $headers -TimeoutSec 20 }
  catch {
    $status=$null
    try { $status=[int]$_.Exception.Response.StatusCode } catch { }
    if ($status -and $status -notin @(502,503,504)) { throw "HA rejected the rollback restart request (HTTP $status)." }
  }
  $deadline=(Get-Date).AddSeconds($RestartTimeoutSeconds); $sawUnavailable=$false; $healthy=$false
  while ((Get-Date) -lt $deadline) {
    try {
      $config=Invoke-RestMethod -Method Get -Uri "$script:BaseUrl/api/config" -Headers $headers -TimeoutSec 6
      if ($config.state -eq 'RUNNING' -and $config.components -contains 'area_events') {
        if ($sawUnavailable) { $healthy=$true; break }
      }
    } catch { $sawUnavailable=$true }
    Start-Sleep -Seconds 3
  }
  if (-not $healthy) { throw "Restored files are in place, but HA restart/recovery was not confirmed within $RestartTimeoutSeconds seconds. Check HA before retrying." }
  Write-Host "HA rollback verified; the displaced integration copy is retained at $failed."
}
function Write-Receipt($state) {
  $py=@'
import json,os,tempfile
p=os.path.expanduser('~/.local/state/area-events-deploy.json'); os.makedirs(os.path.dirname(p),mode=0o700,exist_ok=True)
fd,t=tempfile.mkstemp(dir=os.path.dirname(p),prefix='.ae-',text=True)
with os.fdopen(fd,'w') as f: json.dump({'commit':os.environ['AE_COMMIT'],'schema':int(os.environ['AE_SCHEMA']),'cubi_hash':os.environ['AE_CUBI_HASH'],'ha_hash':os.environ['AE_HA_HASH']},f); f.flush(); os.fsync(f.fileno())
os.replace(t,p)
'@
  $null=Invoke-CubiPython $py @{AE_COMMIT=$state.Head;AE_SCHEMA="$script:TargetSchema";AE_CUBI_HASH=$state.CubiFingerprint;AE_HA_HASH=$state.HAFingerprint} 30
}
function ConvertTo-CubiDeployResult($Result) {
  if ($null -eq $Result -or $null -eq $Result.PSObject.Properties['changed'] -or $null -eq $Result.PSObject.Properties['schema_changed']) {
    throw 'Cubi deployment returned an invalid result schema.'
  }
  return [pscustomobject]@{Changed=[bool]$Result.changed;SchemaChanged=[bool]$Result.schema_changed}
}
function Invoke-Recovery($phase,$state,$schemaChanged,$haChanged) {
  if ($phase -eq 'Cubi cutover') {
    if ($script:SchemaChanged -or $schemaChanged) {
      Write-Warning "DB schema advanced; stopping the intended service without restoring or downgrading data. Snapshot retained at $script:RemoteBackup."
      try { $null=Invoke-Cubi "systemctl --user stop area-events-discovery.service" 60 } catch { Write-Warning 'Could not confirm the service stopped; inspect it before retrying.' }
      return
    }
    Write-Warning 'Restoring Cubi code only; database is untouched.'
    $py=@'
import json,os,shutil,sqlite3,ssl,subprocess,tarfile,tempfile,time,urllib.request
root='/home/robohobo/area-events-discovery'; t=tempfile.mkdtemp(dir=root,prefix='.ae-restore-')
subprocess.run(['systemctl','--user','stop','area-events-discovery.service'],timeout=45,check=True)
with tarfile.open(os.environ['AE_BACKUP']+'/accepted-files.tar.gz') as z:
 members=z.getmembers()
 if any(os.path.isabs(m.name) or '..' in m.name.split('/') or m.issym() or m.islnk() for m in members): raise RuntimeError('unsafe rollback archive')
 z.extractall(t,filter='data')
failed=root+'/.area-events-failed'
if os.path.exists(root+'/area_events'): os.rename(root+'/area_events',failed)
os.rename(t+'/area_events',root+'/area_events'); shutil.copy2(t+'/scripts/discovery.py',root+'/scripts/discovery.py')
shutil.rmtree(t,ignore_errors=True); shutil.rmtree(failed,ignore_errors=True)
subprocess.run(['systemctl','--user','start','area-events-discovery.service'],timeout=45,check=True)
subprocess.run(['systemctl','--user','is-active','--quiet','area-events-discovery.service'],timeout=12,check=True)
token=open(root+'/data/discovery/api-read-token').read().strip(); ctx=ssl.create_default_context(cafile=root+'/certs/cert.pem')
req=urllib.request.Request('https://192.168.1.88:8780/v1/discovery/status',headers={'Authorization':'Bearer '+token})
status=None
for attempt in range(30):
 try:
  with urllib.request.urlopen(req,context=ctx,timeout=1) as response: status=json.load(response)
  break
 except Exception:
  if attempt==29: raise RuntimeError('restored service API did not become ready within 60 seconds')
  time.sleep(1)
status_data=status.get('status',status) if isinstance(status,dict) else {}
state=status_data.get('state') or status_data.get('runtime',{}).get('state')
if state!='running': raise RuntimeError('accepted Cubi service failed rollback health check')
'@
    $null=Invoke-CubiPython $py @{AE_BACKUP=$script:RemoteBackup} 120
  } elseif ($phase -eq 'Home Assistant installation') {
    Write-Warning 'Restoring the backed-up HA integration and requesting one bounded HA restart.'
    Restore-HaBackup $state
  }
}
function Invoke-Main {
  if ($SetupCredentials) { Save-HaCredential; return }
  $script:OriginalHaToken=$env:HA_TOKEN
  Import-Module (Join-Path $PSScriptRoot 'area-events-deploy-engine.psm1') -Force
  $ops=@{
    Preflight={ $p=Invoke-Preflight; $script:DeploymentState=$p.State; $p.State }
    Build={ Invoke-Build }
    Plan={ param($p) $plan=Invoke-DeploymentPlan $p; $script:DeploymentState=$plan.State; $plan.State | Add-Member -NotePropertyName NoChanges -NotePropertyValue $plan.NoChanges; $plan.State }
    Backup={ param($s) Invoke-Backups $script:DeploymentState }
    Rehearse={ param($s) Invoke-Rehearsal $script:DeploymentState }
    DeployCubi={ param($s,$r) Invoke-CubiDeploy $script:DeploymentState }
    VerifyCubi={ param($s) Test-CubiHealth }
    DeployHA={ param($s) Install-Ha }
    VerifyHA={ param($s) Test-HaAssets }
    Recover={ param($phase,$s,$schema,$ha) Invoke-Recovery $phase $script:DeploymentState $schema $ha }
  }
  $lock=$null
  try {
    $lock=[IO.File]::Open($script:LockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $result=Invoke-AreaEventsDeploymentEngine -Operations $ops -DryRun:$DryRun
    if ($result.Result -eq 'Deployed') {
      Write-Receipt $script:DeploymentState
      Write-Host "Deployment complete: schema $script:TargetSchema; $script:BuildId; $script:AssetCount assets; WebSocket San Diego/Austin $script:WsEventCount/$script:WsAustinCount, subscription $script:WsViewCount; backups $script:RemoteBackup and $script:LocalBackup"
    }
    if ($script:Archive -and (Test-Path $script:Archive)) { Remove-Item $script:Archive -Force }
  } finally {
    if ($lock) { $lock.Dispose() }
    Remove-Item $script:LockPath -Force -ErrorAction SilentlyContinue
    $script:Token=$null
    if ($null -eq $script:OriginalHaToken) { Remove-Item Env:\HA_TOKEN -ErrorAction SilentlyContinue } else { $env:HA_TOKEN=$script:OriginalHaToken }
  }
}
if ($SetupCredentials) { Save-HaCredential }
elseif ($MyInvocation.InvocationName -ne '.') { try { Invoke-Main } catch { Write-Host ('Deployment stopped: '+$_.Exception.Message) -ForegroundColor Red; exit 1 } }
