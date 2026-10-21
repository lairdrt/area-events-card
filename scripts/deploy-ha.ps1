<#
.SYNOPSIS
  One command to deploy Area Events to Home Assistant: install the files, rebuild the Area Events app, restart Home Assistant Core.

.DESCRIPTION
  A thin wrapper around scripts\install-ha.ps1 (which still works on its own):

    1. builds the card and compares this checkout with the Home Assistant shares (nothing changed = nothing restarted);
    2. runs install-ha.ps1 (backup, copy, hash verification);
    3. rebuilds ONLY the Area Events app (Supervisor "local_area_events") and waits until it is started;
    4. restarts Home Assistant Core once, only if the integration changed, and waits for the API;
    5. verifies the result (app started, integration loaded, the served card carries this build's identifier).

  It talks to Home Assistant with a long-lived access token of an administrator through Home Assistant's own WebSocket API
  (the "supervisor/api" command, which Home Assistant forwards to the Supervisor) and its REST API. No SSH, and no other
  credential, is used. The token is never written anywhere by this script; see "Authentication" below.

  Safety: only the app whose slug is exactly local_area_events and whose name is "Area Events" is ever rebuilt or started
  (checked against the Supervisor before acting); Frigate and every other app are never touched. If the install or the
  rebuild fails, Home Assistant Core is NOT restarted. Every wait is bounded. A lock stops two deployments running at once.
  The browser is never automated: refresh the dashboard yourself when the script says so.

.PARAMETER AppsShare
  The Home Assistant local apps share (called "addons" or "local_apps"), for example X:\ . Required.

.PARAMETER ConfigShare
  The Home Assistant config share (the folder containing configuration.yaml). Default Z:\

.PARAMETER Url
  Home Assistant address, for example http://homeassistant.local:8123 . Default: the HA_URL environment variable.

.PARAMETER TokenFile
  A file (outside the repository, or git-ignored) containing only a long-lived access token. Default: the HA_TOKEN environment variable.

.PARAMETER DryRun
  Report what would happen. Reads (never changes) the shares and, when credentials are available, asks Home Assistant to confirm the app.

.PARAMETER Force
  Redeploy even when the shares already match this checkout.

.PARAMETER SkipBuild
  Do not run "npm run build" first.

.PARAMETER SkipCoreRestart
  Install and rebuild the app but leave Home Assistant Core running (the integration then loads at its next restart).

.NOTES
  Authentication (one time):
    1. In Home Assistant open your profile (bottom left) > Security > Long-lived access tokens > Create token. The user must be an administrator.
    2. Store it for your Windows account only, then open a new terminal:
         setx HA_URL "http://homeassistant.local:8123"
         setx HA_TOKEN "<paste the token>"
       or save the token alone in a file kept out of Git (for example %USERPROFILE%\.ha-area-events-token) and pass -TokenFile.
    Revoke the token in the same profile page if it is ever exposed.
#>
[CmdletBinding()]
param(
  [string]$AppsShare,
  [string]$ConfigShare = "Z:\",
  [string]$Url = $env:HA_URL,
  [string]$TokenFile,
  [switch]$DryRun,
  [switch]$Force,
  [switch]$SkipBuild,
  [switch]$SkipCoreRestart,
  [int]$AppTimeoutSeconds = 900,
  [int]$CoreTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$script:Repo = Split-Path -Parent $PSScriptRoot
$script:AppSlug = "local_area_events"
$script:AppName = "Area Events"
$script:LockPath = Join-Path ([IO.Path]::GetTempPath()) "area-events-deploy-ha.lock"

# --- helpers (dot-source this file to test them; see scripts\test-deploy-ha.ps1) ---------------------------------

function Write-Step([string]$Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Plan([string]$Text) { Write-Host "   [dry run] would: $Text" -ForegroundColor Yellow }

function Get-Token([string]$File) {
  $value = if ($File) { if (-not (Test-Path -LiteralPath $File)) { throw "Token file not found: $File" }; (Get-Content -Raw -LiteralPath $File) } else { $env:HA_TOKEN }
  if ($value) { $value = $value.Trim() }
  return $value
}

function Wait-Until([string]$Description, [scriptblock]$Test, [int]$TimeoutSeconds, [int]$IntervalSeconds = 3) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ($true) {
    $result = $false
    try { $result = & $Test } catch { $result = $false }
    if ($result) { return }
    if ((Get-Date) -ge $deadline) { throw "Timed out after $TimeoutSeconds s waiting for: $Description" }
    Start-Sleep -Seconds $IntervalSeconds
  }
}

function Enter-DeployLock([string]$Path) {
  try { return [IO.File]::Open($Path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
  catch [IO.IOException] { throw "Another Area Events deployment is already running on this computer (lock: $Path). Wait for it to finish." }
}

function Exit-DeployLock($Handle, [string]$Path) {
  if ($Handle) { $Handle.Dispose() }
  Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

function Assert-AppIdentity($Info, [string]$Slug, [string]$Name) {
  if ($null -eq $Info) { throw "The Supervisor returned no information for app '$Slug'." }
  if ($Info.slug -ne $Slug -or $Info.name -ne $Name) {
    throw "Refusing to act: expected app '$Slug' named '$Name' but the Supervisor reports slug '$($Info.slug)' named '$($Info.name)'."
  }
  if ($Info.PSObject.Properties.Name -contains "repository" -and $Info.repository -and $Info.repository -ne "local") {
    throw "Refusing to act: app '$Slug' comes from repository '$($Info.repository)', not the local apps folder."
  }
}

function Get-BuildId([string]$Text) {
  $match = [regex]::Match($Text, "AREA-EVENTS \d+(\.\d+)*-[0-9a-f]{8}")
  if ($match.Success) { return $match.Value }
  return $null
}

function Get-FolderFiles([string]$Root) {
  Get-ChildItem -LiteralPath $Root -Recurse -File |
    Where-Object { $_.FullName -notmatch "\\__pycache__\\" -and $_.Extension -ne ".pyc" } |
    ForEach-Object { $_.FullName.Substring($Root.Length).TrimStart("\") }
}

function Get-NormalizedHash([string]$File) {
  if ($File -like "*.sh") {  # the installer writes shell scripts with Unix line endings
    $text = [IO.File]::ReadAllText($File).Replace("`r`n", "`n")
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($bytes)) } finally { $sha.Dispose() }
  }
  return (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash
}

function Test-FolderCurrent([string]$Source, [string]$Destination) {
  <# True when every file of $Source exists unchanged in $Destination. Reads only. #>
  if (-not (Test-Path -LiteralPath $Destination)) { return $false }
  foreach ($relative in Get-FolderFiles $Source) {
    $target = Join-Path $Destination $relative
    if (-not (Test-Path -LiteralPath $target)) { return $false }
    if ((Get-NormalizedHash (Join-Path $Source $relative)) -ne (Get-NormalizedHash $target)) { return $false }
  }
  return $true
}

# --- Home Assistant access ------------------------------------------------------------------------------------

function Invoke-HaRest([string]$Method, [string]$Path, [int]$TimeoutSeconds = 20) {
  $headers = @{ Authorization = "Bearer $script:Token" }
  Invoke-RestMethod -Method $Method -Uri ($script:BaseUrl + $Path) -Headers $headers -TimeoutSec $TimeoutSeconds
}

function Invoke-Supervisor([string]$Endpoint, [string]$Method = "get", [int]$TimeoutSeconds = 30, $Data = $null) {
  <# One Supervisor API call, authenticated as the token's user, through Home Assistant's WebSocket "supervisor/api" command. #>
  $socket = New-Object Net.WebSockets.ClientWebSocket
  $cancel = New-Object Threading.CancellationTokenSource
  $cancel.CancelAfter([TimeSpan]::FromSeconds($TimeoutSeconds + 30))
  $buffer = New-Object byte[] 262144
  function Receive() {
    $text = ""
    do {
      $segment = New-Object ArraySegment[byte] -ArgumentList @(, $buffer)
      $result = $socket.ReceiveAsync($segment, $cancel.Token).GetAwaiter().GetResult()
      $text += [Text.Encoding]::UTF8.GetString($buffer, 0, $result.Count)
    } while (-not $result.EndOfMessage)
    return ($text | ConvertFrom-Json)
  }
  function Send($Object) {
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Object | ConvertTo-Json -Compress -Depth 6))
    $segment = New-Object ArraySegment[byte] -ArgumentList @(, $bytes)
    $socket.SendAsync($segment, [Net.WebSockets.WebSocketMessageType]::Text, $true, $cancel.Token).GetAwaiter().GetResult() | Out-Null
  }
  try {
    $socketUrl = ($script:BaseUrl -replace "^http", "ws") + "/api/websocket"
    $socket.ConnectAsync([Uri]$socketUrl, $cancel.Token).GetAwaiter().GetResult() | Out-Null
    $hello = Receive
    if ($hello.type -ne "auth_required") { throw "unexpected WebSocket greeting '$($hello.type)'" }
    Send @{ type = "auth"; access_token = $script:Token }
    $auth = Receive
    if ($auth.type -ne "auth_ok") { throw "Home Assistant rejected the access token (revoked, mistyped or expired?). Create a new long-lived token and update HA_TOKEN." }
    $message = @{ id = 1; type = "supervisor/api"; endpoint = $Endpoint; method = $Method; timeout = $TimeoutSeconds }
    if ($null -ne $Data) { $message.data = $Data }
    Send $message
    $reply = Receive
    if (-not $reply.success) {
      $code = if ($reply.error) { "$($reply.error.code): $($reply.error.message)" } else { "unknown error" }
      if ($reply.error.code -eq "unauthorized") { $code += " (the token's user must be a Home Assistant administrator)" }
      throw "Supervisor $Method $Endpoint failed: $code"
    }
    return $reply.result
  } finally {
    $socket.Dispose()
    $cancel.Dispose()
  }
}

function Get-AppInfo { Invoke-Supervisor "/addons/$script:AppSlug/info" "get" 30 }

function Get-CardBuildFromHa {
  try { $card = Invoke-WebRequest -UseBasicParsing -Uri "$script:BaseUrl/area_events/area-events-card.js" -TimeoutSec 20; return (Get-BuildId $card.Content) } catch { return $null }
}

# --- main -----------------------------------------------------------------------------------------------------

function Invoke-Deployment {
  if (-not $AppsShare) { throw "Pass -AppsShare, the Home Assistant local apps share (for example .\scripts\deploy-ha.ps1 -AppsShare X:\)." }
  foreach ($share in @($AppsShare, $ConfigShare)) { if (-not (Test-Path -LiteralPath $share)) { throw "$share is not reachable. Map the Home Assistant share first." } }
  if (-not (Test-Path -LiteralPath (Join-Path $ConfigShare "configuration.yaml"))) { throw "$ConfigShare does not look like the Home Assistant config share (no configuration.yaml). Pass -ConfigShare." }

  $script:Token = Get-Token $TokenFile
  $script:BaseUrl = if ($Url) { $Url.TrimEnd("/") } else { $null }
  $haReady = [bool]($script:Token -and $script:BaseUrl)
  if (-not $haReady -and -not $DryRun) {
    throw ("Home Assistant access is not configured. One-time setup: in Home Assistant open your profile > Security > Long-lived access tokens > Create token (administrator account), then run " +
           "setx HA_URL ""http://homeassistant.local:8123"" and setx HA_TOKEN ""<token>"" and open a new terminal (or use -TokenFile). See the help: Get-Help .\scripts\deploy-ha.ps1 -Full")
  }

  Write-Step "Build and compare"
  if (-not $SkipBuild) {
    Push-Location $script:Repo
    try { npm run build --silent; if ($LASTEXITCODE) { throw "npm run build failed" } } finally { Pop-Location }
  }
  $cardFile = Join-Path $script:Repo "custom_components\area_events\frontend\area-events-card.js"
  $expected = Get-BuildId (Get-Content -Raw -LiteralPath $cardFile)
  if (-not $expected) { throw "The card in the integration is not built (no build identifier). Run npm run build." }
  Write-Host "   card build: $expected"
  $integrationCurrent = Test-FolderCurrent (Join-Path $script:Repo "custom_components\area_events") (Join-Path $ConfigShare "custom_components\area_events")
  $appCurrent = Test-FolderCurrent (Join-Path $script:Repo "area_events") (Join-Path $AppsShare "area_events")
  $needIntegration = $Force -or -not $integrationCurrent
  $needApp = $Force -or -not $appCurrent
  Write-Host ("   integration on the share: {0}; app on the share: {1}" -f $(if ($integrationCurrent) { "up to date" } else { "differs" }), $(if ($appCurrent) { "up to date" } else { "differs" }))

  $info = $null
  if ($haReady) {
    Write-Step "Check Home Assistant and the app"
    try { $api = Invoke-HaRest GET "/api/" }
    catch { throw "Could not reach Home Assistant at $script:BaseUrl with the access token: $($_.Exception.Message). Check HA_URL, that Home Assistant is running, and that the token is valid (Profile > Security)." }
    Write-Host "   Home Assistant API: $($api.message)"
    $info = Get-AppInfo
    Assert-AppIdentity $info $script:AppSlug $script:AppName
    Write-Host "   app $($info.slug) ('$($info.name)'): state $($info.state), version $($info.version), update available: $($info.update_available)"
  } else {
    Write-Host "   (Home Assistant access is not configured; the app and Core are not checked in this dry run.)" -ForegroundColor Yellow
  }

  Write-Step "Plan"
  $plan = @()
  if ($needIntegration -or $needApp) { $plan += "run install-ha.ps1 (backup, copy, verify)" }
  if ($needApp) { $plan += "reload the local apps and rebuild/update ONLY app $script:AppSlug, then wait for it to start" }
  if ($needIntegration -and -not $SkipCoreRestart) { $plan += "restart Home Assistant Core once and wait for the API" }
  if (-not $plan) { $plan += "nothing to install: the shares already match this checkout (use -Force to redeploy)" }
  $plan += "verify the app, the integration and the served card build $expected"
  foreach ($item in $plan) { if ($DryRun) { Write-Plan $item } else { Write-Host "   - $item" } }
  if ($DryRun) {
    if ($needIntegration -or $needApp) { & (Join-Path $PSScriptRoot "install-ha.ps1") -ConfigShare $ConfigShare -AppsShare $AppsShare -DryRun -SkipBuild }
    Write-Host ""; Write-Host "Dry run: Home Assistant and the shares were not changed."
    return
  }

  $lock = Enter-DeployLock $script:LockPath
  try {
    if ($needIntegration -or $needApp) {
      Write-Step "Install files"
      & (Join-Path $PSScriptRoot "install-ha.ps1") -ConfigShare $ConfigShare -AppsShare $AppsShare -SkipBuild
      if (-not $?) { throw "install-ha.ps1 failed" }
    }

    if ($needApp) {
      Write-Step "Rebuild the Area Events app ($script:AppSlug)"
      Assert-AppIdentity (Get-AppInfo) $script:AppSlug $script:AppName   # re-checked immediately before acting
      Invoke-Supervisor "/store/reload" "post" 120 | Out-Null            # lets the Supervisor see the files just copied (store metadata only)
      $info = Get-AppInfo
      Assert-AppIdentity $info $script:AppSlug $script:AppName
      if ($info.update_available) {
        Write-Host "   a newer version is on the share: updating (this rebuilds the image)"
        Invoke-Supervisor "/addons/$script:AppSlug/update" "post" $AppTimeoutSeconds @{ backup = $false } | Out-Null
      } else {
        Write-Host "   rebuilding the image (a running app is restarted by the Supervisor)"
        Invoke-Supervisor "/addons/$script:AppSlug/rebuild" "post" $AppTimeoutSeconds | Out-Null
      }
      Wait-Until "the app to report 'started' (after a rebuild the Supervisor starts it again)" { (Get-AppInfo).state -eq "started" } 120
      $info = Get-AppInfo
      if ($info.state -ne "started") {
        Write-Host "   starting the app"
        Invoke-Supervisor "/addons/$script:AppSlug/start" "post" 120 | Out-Null
        Wait-Until "the app to start" { (Get-AppInfo).state -eq "started" } 180
      }
      Start-Sleep -Seconds 5
      if ((Get-AppInfo).state -ne "started") { throw "The app stopped again right after starting. Check its log under Settings > Apps > Area Events > Log. Home Assistant Core was NOT restarted." }
      Write-Host "   app started"
    }

    if ($needIntegration -and -not $SkipCoreRestart) {
      Write-Step "Restart Home Assistant Core"
      Invoke-HaRest POST "/api/services/homeassistant/restart" 60 | Out-Null   # checks the configuration first; refuses to restart if it is invalid
      Write-Host "   restart requested; waiting for the API to go down and come back"
      try { Wait-Until "Home Assistant to stop answering" { try { Invoke-HaRest GET "/api/" 5 | Out-Null; $false } catch { $true } } 90 2 }
      catch { Write-Host "   (the API never went down; checking that it is running the new build anyway)" -ForegroundColor Yellow }
      Wait-Until "the Home Assistant API to answer again and report RUNNING" { (Invoke-HaRest GET "/api/config" 10).state -eq "RUNNING" } $CoreTimeoutSeconds 5
      Write-Host "   Home Assistant is running again"
    }

    Write-Step "Verify"
    $config = Invoke-HaRest GET "/api/config" 20
    if ($config.state -ne "RUNNING") { throw "Home Assistant reports state $($config.state), not RUNNING." }
    if ($config.components -notcontains "area_events") { throw "The Area Events integration is not loaded in Home Assistant (check Settings > Devices & services and the Core log)." }
    $final = Get-AppInfo
    Assert-AppIdentity $final $script:AppSlug $script:AppName
    if ($final.state -ne "started") { throw "The Area Events app is '$($final.state)', not started." }
    Wait-Until "the served card to carry build $expected" { $script:Served = Get-CardBuildFromHa; $script:Served -eq $expected } 60 5
    Write-Host "   Home Assistant $($config.version): RUNNING; integration loaded; app $($final.state); card build $expected served"
    Write-Host ""
    Write-Host ("Deployment complete {0} refresh the HA dashboard." -f [char]0x2014) -ForegroundColor Green
    if ($SkipCoreRestart -and $needIntegration) { Write-Host "(Core was not restarted; restart it to load the new integration code.)" -ForegroundColor Yellow }
  } finally {
    Exit-DeployLock $lock $script:LockPath
  }
}

if ($MyInvocation.InvocationName -ne ".") {
  try { Invoke-Deployment }
  catch { Write-Host ""; Write-Host "Deployment failed: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
}
