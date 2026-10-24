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
function Save-HaCredential {
  if ($env:OS -ne 'Windows_NT') { throw 'Credential setup requires Windows DPAPI.' }
  $secure = Read-Host 'Paste an administrator long-lived HA token once (input is hidden)' -AsSecureString
  if ($secure.Length -lt 20) { throw 'Token input looks too short.' }
  $dir = Split-Path -Parent $script:CredentialPath
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Set-Content -LiteralPath $script:CredentialPath -Value (ConvertFrom-SecureString $secure) -Encoding ASCII
  $acl = Get-Acl -LiteralPath $script:CredentialPath
  $acl.SetAccessRuleProtection($true, $false)
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
  $acl.SetAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')))
  Set-Acl -LiteralPath $script:CredentialPath -AclObject $acl
  $secure.Dispose()
  Write-Host "Credential encrypted for this Windows account at $script:CredentialPath"
}
function Get-HaToken {
  if (-not (Test-Path -LiteralPath $script:CredentialPath)) { throw 'Run .\scripts\deploy-area-events.ps1 -SetupCredentials once first.' }
  $secure = ConvertTo-SecureString (Get-Content -Raw -LiteralPath $script:CredentialPath)
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $secure.Dispose() }
}
function Invoke-Bounded([string]$File,[string[]]$Arguments,[int]$TimeoutSeconds=60) {
  $job=Start-Job -ArgumentList $File,(,$Arguments) -ScriptBlock {
    param($exe,$argv)
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
  $json=ConvertTo-Json -InputObject $Variables -Compress -Depth 4
  $env64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
  $code64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
  $q=[string][char]39
  $remote='timeout --signal=TERM --kill-after=5s '+[Math]::Max(5,$Timeout-10)+'s python3 -c '+$q+'import base64,json,os;os.environ.update(json.loads(base64.b64decode("'+$env64+'")));exec(compile(base64.b64decode("'+$code64+'"),"<ae>","exec"))'+$q
  Invoke-Cubi $remote $Timeout
}
function Invoke-Build {
  $npm=(Get-Command npm.cmd -ErrorAction Stop).Source
  $null=Invoke-Bounded $npm @('run','build','--silent') 240
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
function Invoke-Preflight {
  Write-Step 'Read-only preflight'
  $branch=(Invoke-Bounded 'git.exe' @('-C',$script:Repo,'branch','--show-current') 10).Trim()
  if ($branch -ne 'ha-native-refactor') { throw "Expected ha-native-refactor, found $branch." }
  $dirty=Invoke-Bounded 'git.exe' @('-C',$script:Repo,'status','--porcelain') 10
  $unexpected=@($dirty -split [Environment]::NewLine | Where-Object { $_ -and $_ -notmatch '^\?\? scripts/deploy-cubi-2f-display\.ps1\s*$' })
  if ($unexpected.Count) { throw "Resolve local changes before deployment: $($unexpected -join ', ')" }
  if (-not (Test-Path -LiteralPath (Join-Path $ConfigShare 'configuration.yaml'))) { throw "HA config share unavailable: $ConfigShare" }
  $script:Token=Get-HaToken; $script:BaseUrl=$HaUrl.TrimEnd('/')
  if ($script:BaseUrl -notmatch '^https?://[^/]+$') { throw 'HA URL must be an HTTP(S) origin.' }
  $haApi=Invoke-RestMethod -Method Get -Uri "$script:BaseUrl/api/" -Headers @{Authorization="Bearer $script:Token"} -TimeoutSec 12
  $haConfig=Invoke-RestMethod -Method Get -Uri "$script:BaseUrl/api/config" -Headers @{Authorization="Bearer $script:Token"} -TimeoutSec 12
  if ($haConfig.state -ne 'RUNNING') { throw 'Home Assistant Core is not RUNNING; refusing to start a deployment.' }
  $script:HaLoaded=($haConfig.components -contains 'area_events')
  $py=@'
import json,os,sqlite3,subprocess,ssl,urllib.parse,urllib.request
root='/home/robohobo/area-events-discovery'; db=root+'/data/discovery/discovery.db'
unit=subprocess.run(['systemctl','--user','show','area-events-discovery.service','--property=ActiveState','--property=WorkingDirectory','--property=ExecStart'],capture_output=True,text=True,timeout=12,check=True).stdout
c=sqlite3.connect('file:'+db+'?mode=ro',uri=True,timeout=5)
try:
 v=[r[0] for r in c.execute('select version from schema_migrations')]
 z={'unit':unit,'schema':max(v),'events':c.execute('select count(*) from events').fetchone()[0],'associations':c.execute('select count(*) from event_candidates').fetchone()[0],'integrity':c.execute('pragma quick_check').fetchone()[0],'foreign_keys':len(list(c.execute('pragma foreign_key_check')))}
finally: c.close()
token=open(root+'/data/discovery/api-read-token',encoding='utf-8').read().strip(); context=ssl.create_default_context(cafile=root+'/certs/cert.pem')
def api(path):
 req=urllib.request.Request('https://192.168.1.88:8780/v1/'+path,headers={'Authorization':'Bearer '+token})
 with urllib.request.urlopen(req,context=context,timeout=8) as r: return json.load(r)
status=api('discovery/status'); z['api_state']=status.get('state') or status.get('runtime',{}).get('state'); z['api_samples']={}
for area in ('San Diego','Austin'):
 z['api_samples'][area]=len(api('events?area='+urllib.parse.quote(area)+'&limit=1').get('events',[]))
p=os.path.expanduser('~/.local/state/area-events-deploy.json'); z['receipt']=json.load(open(p)) if os.path.isfile(p) else None
print(json.dumps(z))
'@
  $remote=Invoke-CubiPython $py @{} 40 | ConvertFrom-Json
  if ($remote.integrity -ne 'ok' -or $remote.foreign_keys -ne 0) { throw 'Cubi database integrity check failed.' }
  if ($remote.schema -gt $script:TargetSchema) { throw "Production schema $($remote.schema) is newer than this source ($script:TargetSchema); refusing to deploy older code." }
  if ($remote.api_state -ne 'running' -or $remote.api_samples.'San Diego' -lt 1 -or $remote.api_samples.Austin -lt 1) { throw 'Cubi read API or representative area event pages failed preflight.' }
  if ($remote.unit -notmatch 'ActiveState=active' -or $remote.unit -notmatch '/home/robohobo/area-events-discovery' -or $remote.unit -notmatch 'port 8780') { throw 'Expected Discovery service is not active on port 8780; refusing to act.' }
  $script:Head=(Invoke-Bounded 'git.exe' @('-C',$script:Repo,'rev-parse','HEAD') 10).Trim()
  $script:CubiFingerprint=Get-CubiFingerprint
  $haSource=Join-Path $script:Repo 'custom_components\area_events'
  $script:HAFingerprint=Get-FolderFingerprint $haSource
  $ha=Test-FolderMatches $haSource (Join-Path $ConfigShare 'custom_components\area_events')
  $cubiNeeded=(-not $remote.receipt -or $remote.receipt.cubi_hash -ne $script:CubiFingerprint -or $remote.schema -lt $script:TargetSchema)
  $haNeeded=(-not $remote.receipt -or $remote.receipt.ha_hash -ne $script:HAFingerprint -or -not $ha -or -not $script:HaLoaded)
  $script:State=[pscustomobject]@{Remote=$remote;Head=$script:Head;HAPath=(Join-Path $ConfigShare 'custom_components\area_events');CubiNeeded=[bool]$cubiNeeded;HANeeded=[bool]$haNeeded;HaLoaded=[bool]$script:HaLoaded;CubiFingerprint=$script:CubiFingerprint;HAFingerprint=$script:HAFingerprint}
  $noChange=(-not $cubiNeeded -and -not $haNeeded)
  Write-Host "HEAD $script:Head; schema $($remote.schema); events $($remote.events); Cubi update $cubiNeeded; HA update $haNeeded"
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
 t.extractall(stage)
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
 t.extractall(stage)
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
    $res
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
s=get('discovery/status'); samples={}
for area in ('San Diego','Austin'):
 samples[area]=len(get('events?area='+urllib.parse.quote(area)+'&limit=1').get('events',[]))
 if not samples[area]: raise RuntimeError('No current events for '+area)
print(json.dumps({'state':s.get('state'),'samples':samples}))
'@
  $deadline=(Get-Date).AddSeconds(90); $h=$null; $last='no response'
  while ((Get-Date) -lt $deadline) {
    try { $h=Invoke-CubiPython $py @{} 25 | ConvertFrom-Json; if ($h.state -eq 'running' -and $h.samples.'San Diego' -gt 0 -and $h.samples.Austin -gt 0) { break } }
    catch { $last=$_.Exception.Message }
    Start-Sleep -Seconds 4
  }
  if ($null -eq $h -or $h.state -ne 'running' -or $h.samples.'San Diego' -lt 1 -or $h.samples.Austin -lt 1) { throw "Cubi API did not become healthy within 90 seconds. Last check: $last" }
  Write-Host "API verified; event samples San Diego/Austin: $($h.samples.'San Diego')/$($h.samples.Austin)"
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
    Send-HaMessage @{id=3;type='area_events/events';area='Austin';limit=20}
    $pages=@{}
    for ($i=0;$i -lt 8 -and $pages.Count -lt 2;$i++) {
      $m=Receive-HaMessage
      if ($m.id -in @(1,3)) { if (-not $m.success) { throw "HA event command failed: $($m.error.code)" }; $pages[[int]$m.id]=$m.result }
    }
    $page=$pages[1]; $austin=$pages[3]
    if ($null -eq $page -or @($page.events).Count -eq 0 -or -not $page.events[0].id -or -not $page.events[0].title) { throw 'HA San Diego event page was empty or lacked expected event fields.' }
    if ($null -eq $austin -or @($austin.events).Count -eq 0 -or -not $austin.events[0].id -or -not $austin.events[0].title) { throw 'HA Austin event page was empty or lacked expected event fields.' }
    Send-HaMessage @{id=2;type='area_events/subscribe';area='San Diego'}
    $subscribed=$false; $view=$null
    for ($i=0;$i -lt 8;$i++) {
      $m=Receive-HaMessage
      if ($m.id -eq 2 -and $m.type -eq 'result') { if (-not $m.success) { throw "HA subscription failed: $($m.error.code)" }; $subscribed=$true }
      if ($m.id -eq 2 -and $m.type -eq 'event') { $view=$m.event; if ($subscribed) { break } }
    }
    if (-not $subscribed -or $null -eq $view -or @($view.events).Count -eq 0 -or -not $view.events[0].id -or -not $view.events[0].title) { throw 'HA subscription did not deliver its initial view with expected event fields.' }
    Send-HaMessage @{type='unsubscribe_events';subscription=2}
    $script:WsEventCount=@($page.events).Count; $script:WsAustinCount=@($austin.events).Count; $script:WsViewCount=@($view.events).Count
  } finally { $cancel.Cancel(); $socket.Dispose(); $cancel.Dispose() }
}
function Test-HaAssets {
  $front=Join-Path $script:Repo 'custom_components\area_events\frontend'
  $modules=@(Get-ChildItem (Join-Path $front 'presentation') -Filter '*.js' -File | ForEach-Object FullName)
  if ($modules.Count -ne 6) { throw "Expected six presentation modules; found $($modules.Count)." }
  $assets=@((Join-Path $front 'area-events-card.js'))+$modules
  $servedDir=Join-Path ([IO.Path]::GetTempPath()) ('area-events-served-'+[guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $servedDir | Out-Null
  foreach ($f in $assets) {
    $rel=$f.Substring($front.Length+1).Replace('\','/')
    $served=Join-Path $servedDir ([IO.Path]::GetFileName($f))
    $status=Invoke-Bounded 'curl.exe' @('--silent','--show-error','--max-time','12','--output',$served,'--write-out','%{http_code}',"$script:BaseUrl/area_events/$rel") 20
    if ($status -ne '200') { throw "HA did not serve $rel (HTTP $status)." }
    $hash=(Get-FileHash -Algorithm SHA256 $served).Hash
    if ($hash -ne (Get-FileHash $f).Hash) { throw "Served asset differs from source: $rel" }
  }
  Remove-Item -LiteralPath $servedDir -Recurse -Force
  $cfg=Invoke-RestMethod -Method Get -Uri "$script:BaseUrl/api/config" -Headers @{Authorization="Bearer $script:Token"} -TimeoutSec 15
  if ($cfg.state -ne 'RUNNING' -or $cfg.components -notcontains 'area_events') { throw 'HA or Area Events integration is not loaded.' }
  Test-HaWebSocket
  $card=Get-Content -Raw (Join-Path $front 'area-events-card.js')
  $script:BuildId=[regex]::Match($card,'AREA-EVENTS \d+(\.\d+)*-[0-9a-f]{8}').Value
  if (-not $script:BuildId) { throw 'Build identifier missing.' }
  $script:AssetCount=$assets.Count
}
function Restore-HaBackup($state) {
  $saved=Join-Path $script:LocalBackup 'area_events'
  $parent=Split-Path -Parent $state.HAPath
  $failed=Join-Path $parent ('.area_events_failed_'+[guid]::NewGuid().ToString('N'))
  $partial=Join-Path $parent ('.area_events_partial_'+[guid]::NewGuid().ToString('N'))
  $componentsRoot=[IO.Path]::GetFullPath((Join-Path $ConfigShare 'custom_components')).TrimEnd('\')
  $liveResolved=[IO.Path]::GetFullPath($state.HAPath)
  foreach ($candidate in @($liveResolved,[IO.Path]::GetFullPath($failed),[IO.Path]::GetFullPath($partial))) {
    if (-not $candidate.StartsWith($componentsRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing HA recovery path outside custom_components.' }
  }
  if (-not (Test-Path -LiteralPath $saved)) { throw 'HA rollback copy is missing.' }
  if (Test-FolderMatches $saved $state.HAPath) { Write-Host 'HA files already match rollback copy; no restart needed.'; return }
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
function Invoke-Recovery($phase,$state,$schemaChanged,$haChanged) {
  if ($phase -eq 'Cubi cutover') {
    if ($script:SchemaChanged -or $schemaChanged) {
      Write-Warning "DB schema advanced; stopping the intended service without restoring or downgrading data. Snapshot retained at $script:RemoteBackup."
      try { $null=Invoke-Cubi "systemctl --user stop area-events-discovery.service" 60 } catch { Write-Warning 'Could not confirm the service stopped; inspect it before retrying.' }
      return
    }
    Write-Warning 'Restoring Cubi code only; database is untouched.'
    $py=@'
import json,os,shutil,sqlite3,ssl,subprocess,tarfile,tempfile,urllib.request
root='/home/robohobo/area-events-discovery'; t=tempfile.mkdtemp(dir=root,prefix='.ae-restore-')
subprocess.run(['systemctl','--user','stop','area-events-discovery.service'],timeout=45,check=True)
with tarfile.open(os.environ['AE_BACKUP']+'/accepted-files.tar.gz') as z:
 members=z.getmembers()
 if any(os.path.isabs(m.name) or '..' in m.name.split('/') or m.issym() or m.islnk() for m in members): raise RuntimeError('unsafe rollback archive')
 z.extractall(t)
failed=root+'/.area-events-failed'
if os.path.exists(root+'/area_events'): os.rename(root+'/area_events',failed)
os.rename(t+'/area_events',root+'/area_events'); shutil.copy2(t+'/scripts/discovery.py',root+'/scripts/discovery.py')
shutil.rmtree(t,ignore_errors=True); shutil.rmtree(failed,ignore_errors=True)
subprocess.run(['systemctl','--user','start','area-events-discovery.service'],timeout=45,check=True)
subprocess.run(['systemctl','--user','is-active','--quiet','area-events-discovery.service'],timeout=12,check=True)
token=open(root+'/data/discovery/api-read-token').read().strip(); ctx=ssl.create_default_context(cafile=root+'/certs/cert.pem')
req=urllib.request.Request('https://192.168.1.88:8780/v1/discovery/status',headers={'Authorization':'Bearer '+token})
with urllib.request.urlopen(req,context=ctx,timeout=8) as response: status=json.load(response)
if status.get('state')!='running': raise RuntimeError('accepted Cubi service failed rollback health check')
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
    Preflight={ $p=Invoke-Preflight; $script:DeploymentState=$p.State; [pscustomobject]@{NoChanges=$p.NoChanges;CubiNeeded=$p.State.CubiNeeded;HANeeded=$p.State.HANeeded} }
    Build={ Invoke-Build }
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
