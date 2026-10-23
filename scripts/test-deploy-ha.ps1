<#
.SYNOPSIS
  Offline checks for scripts\deploy-ha.ps1: syntax, safety guards, bounded waits, locking, change detection. Changes nothing and contacts nothing.
#>
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "deploy-ha.ps1"
$failures = 0
function Check([string]$Name, [scriptblock]$Test) {
  try { & $Test; Write-Host "ok   $Name" } catch { $script:failures++; Write-Host "FAIL $Name : $($_.Exception.Message)" -ForegroundColor Red }
}
function Expect-Throw([scriptblock]$Block, [string]$Pattern) {
  $threw = $false
  try { & $Block } catch { $threw = $true; if ($_.Exception.Message -notmatch $Pattern) { throw "wrong error: $($_.Exception.Message)" } }
  if (-not $threw) { throw "expected an error matching '$Pattern'" }
}

Check "both scripts parse" {
  foreach ($file in @($script, (Join-Path $PSScriptRoot "install-ha.ps1"))) {
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$null, [ref]$errors) | Out-Null
    if ($errors.Count) { throw "$file : $($errors[0].Message)" }
  }
}

. $script -AppsShare "X:\" 2>$null   # dot-sourcing defines the functions without running a deployment

Check "only the Area Events app is accepted" {
  Assert-AppIdentity ([pscustomobject]@{ slug = "local_area_events"; name = "Area Events"; repository = "local" }) "local_area_events" "Area Events"
  Expect-Throw { Assert-AppIdentity ([pscustomobject]@{ slug = "ccab4aaf_frigate"; name = "Frigate"; repository = "x" }) "local_area_events" "Area Events" } "Refusing to act"
  Expect-Throw { Assert-AppIdentity ([pscustomobject]@{ slug = "local_area_events"; name = "Something else" }) "local_area_events" "Area Events" } "Refusing to act"
  Expect-Throw { Assert-AppIdentity ([pscustomobject]@{ slug = "local_area_events"; name = "Area Events"; repository = "core" }) "local_area_events" "Area Events" } "not the local apps"
  Expect-Throw { Assert-AppIdentity $null "local_area_events" "Area Events" } "no information"
  if ($script:AppSlug -ne "local_area_events") { throw "the app slug is not fixed" }
}
Check "build identifiers are parsed" {
  if ((Get-BuildId 'console.info("AREA-EVENTS 0.2.0-be2eea40")') -ne "AREA-EVENTS 0.2.0-be2eea40") { throw "not parsed" }
  if (Get-BuildId '__AREA_EVENTS_CARD_BUILD__') { throw "the placeholder must not count as a build" }
}
Check "waits are bounded and report what they waited for" {
  $watch = [Diagnostics.Stopwatch]::StartNew()
  Expect-Throw { Wait-Until "never" { $false } 2 1 } "Timed out after 2 s waiting for: never"
  if ($watch.Elapsed.TotalSeconds -gt 6) { throw "took too long: $($watch.Elapsed)" }
  Wait-Until "immediately" { $true } 5 1
  $script:n = 0
  Wait-Until "third try" { $script:n++; $script:n -ge 3 } 10 1
}
Check "a second deployment cannot start while one holds the lock" {
  $path = Join-Path ([IO.Path]::GetTempPath()) "area-events-deploy-test.lock"
  $first = Enter-DeployLock $path
  try { Expect-Throw { Enter-DeployLock $path } "already running" } finally { Exit-DeployLock $first $path }
  $again = Enter-DeployLock $path   # released cleanly
  Exit-DeployLock $again $path
}
Check "change detection compares content, with shell scripts normalised to Unix line endings" {
  $root = Join-Path ([IO.Path]::GetTempPath()) ("aed-" + [guid]::NewGuid().ToString("N"))
  $a = Join-Path $root "a"; $b = Join-Path $root "b"
  New-Item -ItemType Directory -Force -Path $a, $b | Out-Null
  try {
    [IO.File]::WriteAllText((Join-Path $a "x.py"), "print(1)`n"); [IO.File]::WriteAllText((Join-Path $b "x.py"), "print(1)`n")
    [IO.File]::WriteAllText((Join-Path $a "run.sh"), "echo hi`r`n"); [IO.File]::WriteAllText((Join-Path $b "run.sh"), "echo hi`n")
    if (-not (Test-FolderCurrent $a $b)) { throw "identical content reported as different" }
    [IO.File]::WriteAllText((Join-Path $b "x.py"), "print(2)`n")
    if (Test-FolderCurrent $a $b) { throw "a changed file was not noticed" }
    [IO.File]::WriteAllText((Join-Path $b "x.py"), "print(1)`n"); Remove-Item (Join-Path $b "run.sh")
    if (Test-FolderCurrent $a $b) { throw "a missing file was not noticed" }
    if (Test-FolderCurrent $a (Join-Path $root "nothing")) { throw "a missing folder is current" }
  } finally { Remove-Item -Recurse -Force $root }
}
Check "the token comes from a file, trimmed, and a missing file is an error" {
  $file = Join-Path ([IO.Path]::GetTempPath()) ("aed-token-" + [guid]::NewGuid().ToString("N"))
  [IO.File]::WriteAllText($file, "  abc.def`r`n")
  try { if ((Get-Token $file) -ne "abc.def") { throw "not trimmed" } } finally { Remove-Item $file }
  Expect-Throw { Get-Token (Join-Path ([IO.Path]::GetTempPath()) "does-not-exist-aed") } "not found"
}
Check "the script never contains a token or a hard-coded address" {
  $text = Get-Content -Raw $script
  if ($text -match "eyJ[A-Za-z0-9_-]{20,}") { throw "looks like a JWT" }
  if ($text -match "\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}") { throw "contains an IP address" }
}
Check "only the Area Events app is ever the target of a Supervisor write" {
  $text = Get-Content -Raw $script
  $writes = [regex]::Matches($text, 'Invoke-Supervisor\s+"([^"]+)"\s+"post"') | ForEach-Object { $_.Groups[1].Value }
  foreach ($endpoint in $writes) { if ($endpoint -ne "/store/reload" -and $endpoint -notmatch '^/addons/\$script:AppSlug/(rebuild|update|start)$') { throw "unexpected write: $endpoint" } }
  if ($text -match '/addons/(?!\$script:AppSlug)[a-z0-9_]+/(rebuild|restart|stop|start|update)') { throw "an app other than the Area Events app is addressed" }
}

# --- Core restart handling (a simulated clock: no real waiting, no Home Assistant) -------------------------------

function Invoke-Sim($sim, [hashtable]$Extra = @{}) { $all = @{}; foreach ($k in $sim.Splat.Keys) { $all[$k] = $sim.Splat[$k] }; foreach ($k in $Extra.Keys) { $all[$k] = $Extra[$k] }; Wait-CoreRestart @all }

function New-Sim([scriptblock]$Request, [scriptblock]$Poll) {
  $sim = [pscustomobject]@{ T = 0; Requests = 0; Polls = 0; Reports = @() }
  $sim | Add-Member -NotePropertyName Splat -NotePropertyValue @{
    Request = { $sim.Requests++; & $Request $sim }.GetNewClosure()
    Poll = { $sim.Polls++; & $Poll $sim }.GetNewClosure()
    Sleep = { param($Seconds) $sim.T += $Seconds }.GetNewClosure()
    Now = { ([datetime]"2030-01-01").AddSeconds($sim.T) }.GetNewClosure()
    Report = { param($Text) $sim.Reports += $Text }.GetNewClosure()
  }
  return $sim
}
$accepted = { param($sim) [pscustomobject]@{ Outcome = "accepted"; Status = $null; Detail = "" } }
$dropped = { param($sim) [pscustomobject]@{ Outcome = "dropped"; Status = $null; Detail = "The underlying connection was closed" } }
$up = [pscustomobject]@{ Healthy = $true; Detail = "state RUNNING" }
$down = [pscustomobject]@{ Healthy = $false; Detail = "connection refused" }

Check "request failures are classified: no HTTP answer or a proxy 5xx is indeterminate, other answers are rejections" {
  foreach ($status in @($null, 502, 503, 504)) { if ((Get-RestartRequestOutcome $status) -ne "dropped") { throw "status $status should be indeterminate" } }
  foreach ($status in @(400, 401, 403, 404, 500)) { if ((Get-RestartRequestOutcome $status) -ne "rejected") { throw "status $status should be a rejection" } }
  $error1 = try { Invoke-RestMethod -Uri "http://127.0.0.1:9/" -TimeoutSec 2 } catch { $_ }
  if ($null -ne (Get-ErrorStatusCode $error1)) { throw "a refused connection has no HTTP status" }
}
Check "the connection closing during the restart request, then about 90 s of outage, is a confirmed restart (request sent once)" {
  $sim = New-Sim $dropped { param($sim) if ($sim.T -ge 4 -and $sim.T -lt 94) { $down } else { $up } }
  $r = Invoke-Sim $sim @{ RecoveryTimeoutSeconds = 180 }
  if (-not $r.Confirmed) { throw "not confirmed" }
  if ($sim.Requests -ne 1) { throw "the restart was requested $($sim.Requests) times" }
  if ($r.DownAfterSeconds -gt 6 -or $r.RecoveredAfterSeconds -lt 90 -or $r.RecoveredAfterSeconds -gt 100) { throw "implausible timings: $($r.DownAfterSeconds) / $($r.RecoveredAfterSeconds)" }
}
Check "an accepted request, temporary HTTP errors and a state other than RUNNING all count as the restart in progress" {
  $states = @("down", "starting", "up")
  $sim = New-Sim $accepted { param($sim) if ($sim.T -lt 6) { $up } elseif ($sim.T -lt 30) { [pscustomobject]@{ Healthy = $false; Detail = "HTTP 503" } } elseif ($sim.T -lt 60) { [pscustomobject]@{ Healthy = $false; Detail = "state NOT_RUNNING" } } else { $up } }
  $r = Invoke-Sim $sim
  if (-not $r.Confirmed -or $sim.Requests -ne 1) { throw "not confirmed once" }
}
Check "Home Assistant that never recovers fails clearly at the timeout, without more requests" {
  $sim = New-Sim $dropped { param($sim) if ($sim.T -lt 3) { $up } else { $down } }
  Expect-Throw { Invoke-Sim $sim @{ RecoveryTimeoutSeconds = 180 } } "did not become healthy again within 180 s"
  if ($sim.T -gt 190 -or $sim.Requests -ne 1) { throw "unbounded or repeated: T=$($sim.T) requests=$($sim.Requests)" }
}
Check "a request that Home Assistant explicitly rejects fails at once and polls nothing" {
  $sim = New-Sim { param($sim) [pscustomobject]@{ Outcome = "rejected"; Status = 400; Detail = "Invalid config" } } { param($sim) $up }
  Expect-Throw { Invoke-Sim $sim } "rejected the restart request \(HTTP 400\)"
  if ($sim.Polls -ne 0 -or $sim.Requests -ne 1) { throw "polled or retried after a rejection" }
}
Check "an API that never goes down is not reported as a restart" {
  foreach ($request in @($accepted, $dropped)) {
    $sim = New-Sim $request { param($sim) $up }
    Expect-Throw { Invoke-Sim $sim @{ ObserveSeconds = 45 } } "could not be confirmed"
    if ($sim.T -gt 50 -or $sim.Requests -ne 1) { throw "took too long or repeated: T=$($sim.T)" }
  }
}
Check "a recovery that arrives right at the timeout is still accepted, not turned into a failure" {
  $sim = New-Sim $dropped { param($sim) if ($sim.T -lt 3) { $up } elseif ($sim.T -lt 177) { $down } else { $up } }
  if (-not (Invoke-Sim $sim @{ RecoveryTimeoutSeconds = 180 }).Confirmed) { throw "not confirmed" }
}
Check "final verification fails clearly after recovery when something is wrong" {
  $config = [pscustomobject]@{ state = "RUNNING"; version = "2026.10.2"; components = @("area_events", "frontend") }
  $app = [pscustomobject]@{ slug = "local_area_events"; name = "Area Events"; state = "started" }
  $good = Test-FinalState -GetConfig { $config } -GetApp { $app } -GetServedBuild { "AREA-EVENTS 0.2.0-aaaaaaaa" } -Expected "AREA-EVENTS 0.2.0-aaaaaaaa" -BuildWaitSeconds 2 -BuildPollSeconds 1
  if ($good.Build -ne "AREA-EVENTS 0.2.0-aaaaaaaa") { throw "a good state was rejected" }
  Expect-Throw { Test-FinalState -GetConfig { [pscustomobject]@{ state = "NOT_RUNNING"; components = @() } } -GetApp { $app } -GetServedBuild { "x" } -Expected "x" } "not RUNNING"
  Expect-Throw { Test-FinalState -GetConfig { [pscustomobject]@{ state = "RUNNING"; components = @("frontend") } } -GetApp { $app } -GetServedBuild { "x" } -Expected "x" } "integration is not loaded"
  Expect-Throw { Test-FinalState -GetConfig { $config } -GetApp { [pscustomobject]@{ slug = "local_area_events"; name = "Area Events"; state = "stopped" } } -GetServedBuild { "x" } -Expected "x" } "not started"
  Expect-Throw { Test-FinalState -GetConfig { $config } -GetApp { [pscustomobject]@{ slug = "frigate"; name = "Frigate"; state = "started" } } -GetServedBuild { "x" } -Expected "x" } "Refusing to act"
  Expect-Throw { Test-FinalState -GetConfig { $config } -GetApp { $app } -GetServedBuild { "AREA-EVENTS 0.2.0-bbbbbbbb" } -Expected "AREA-EVENTS 0.2.0-aaaaaaaa" -BuildWaitSeconds 2 -BuildPollSeconds 1 } "Timed out.*served card to carry build"
}
Check "the restart is requested from exactly one place, with the recovery timeout defaulting to 180 s" {
  $text = Get-Content -Raw $script
  if ([regex]::Matches($text, "homeassistant/restart").Count -ne 1) { throw "the restart command appears more than once" }
  if ($text -notmatch "CoreRecoveryTimeoutSeconds = 180") { throw "default timeout is not 180" }
}

Check "-SkipApp deploys the integration without the app: no app share, no Supervisor app call, no app check" {
  $text = Get-Content -Raw -LiteralPath $script
  foreach ($needle in @('[switch]$SkipApp', 'if ($SkipApp) { $AppsShare = $null }', '$needApp = -not $SkipApp -and', '-GetApp $(if ($SkipApp) { $null }')) {
    if (-not $text.Contains($needle)) { throw "missing: $needle" }
  }
  $result = Test-FinalState -GetConfig { [pscustomobject]@{ state = "RUNNING"; components = @("area_events"); version = "2026.10.2" } } -GetApp $null -GetServedBuild { "AREA-EVENTS x" } -Expected "AREA-EVENTS x"
  if ($result.AppState -ne "not checked") { throw "the app was checked" }
  Expect-Throw { Test-FinalState -GetConfig { [pscustomobject]@{ state = "RUNNING"; components = @("area_events") } } -GetApp $null -GetServedBuild { "other" } -Expected "AREA-EVENTS x" -BuildWaitSeconds 1 -BuildPollSeconds 1 } "Timed out"
}

Check "obsolete integration modules are removed by name, only when the checkout lacks them, and nothing else" {
  $root = Join-Path ([IO.Path]::GetTempPath()) ("aed-obsolete-" + [guid]::NewGuid().ToString("N"))
  $source = Join-Path $root "source"; $share = Join-Path $root "share"
  New-Item -ItemType Directory -Force -Path $source, (Join-Path $share "frontend") | Out-Null
  try {
    foreach ($name in @("__init__.py", "hub.py", "client.py", "profile.py", "media_view.py", "keep-me.txt")) { [IO.File]::WriteAllText((Join-Path $share $name), "x") }
    [IO.File]::WriteAllText((Join-Path $share "frontend\client.py"), "x")   # same name in a subfolder: not touched
    [IO.File]::WriteAllText((Join-Path $source "__init__.py"), "x")
    $found = @(Get-ObsoleteFiles $source $share)
    if (($found -join ",") -ne "client.py,profile.py,media_view.py") { throw "found: $($found -join ',')" }
    if (-not (Test-Path (Join-Path $share "client.py"))) { throw "finding must not delete" }
    [IO.File]::WriteAllText((Join-Path $source "client.py"), "x")   # a module the checkout still has is never removed
    if ((@(Get-ObsoleteFiles $source $share) -join ",") -ne "profile.py,media_view.py") { throw "a module still in the checkout was offered for removal" }
    Remove-Item (Join-Path $source "client.py")
    $removed = @(Remove-ObsoleteFiles $source $share)
    if ($removed.Count -ne 3) { throw "removed $($removed.Count)" }
    foreach ($kept in @("__init__.py", "hub.py", "keep-me.txt", "frontend\client.py")) { if (-not (Test-Path (Join-Path $share $kept))) { throw "$kept was removed" } }
    if (@(Get-ObsoleteFiles $source $share).Count) { throw "still offered after removal" }
  } finally { Remove-Item -Recurse -Force $root }
}

Check "private category photographs are copied with the integration only when they exist (and are never special-cased)" {
  $root = Join-Path ([IO.Path]::GetTempPath()) ("aed-photos-" + [guid]::NewGuid().ToString("N"))
  $integration = Join-Path $root "area_events"
  New-Item -ItemType Directory -Force -Path (Join-Path $integration "frontend") | Out-Null
  try {
    [IO.File]::WriteAllText((Join-Path $integration "__init__.py"), "x")
    [IO.File]::WriteAllText((Join-Path (Join-Path $integration "frontend") "area-events-card.js"), "x")
    $without = @(Get-FolderFiles $integration)
    if ($without -match "photos") { throw "photographs listed although there are none" }
    New-Item -ItemType Directory -Force -Path (Join-Path $integration "frontend\photos") | Out-Null
    [IO.File]::WriteAllText((Join-Path $integration "frontend\photos\music-1.webp"), "x")
    [IO.File]::WriteAllText((Join-Path $integration "frontend\photos\index.json"), "{}")
    $with = @(Get-FolderFiles $integration)
    if (-not ($with -contains "frontend\photos\music-1.webp" -and $with -contains "frontend\photos\index.json")) { throw "present photographs are not part of the copy" }
    if ((Test-FolderCurrent $integration (Join-Path $root "share"))) { throw "an empty share is current" }
  } finally { Remove-Item -Recurse -Force $root }
  $installer = Get-Content -Raw (Join-Path $PSScriptRoot "install-ha.ps1")
  if ($installer -match "photos|webp") { throw "the installer must stay generic: it copies whatever the integration folder contains" }
}

if ($failures) { Write-Host "$failures check(s) failed." -ForegroundColor Red; exit 1 }
Write-Host "All checks passed."
