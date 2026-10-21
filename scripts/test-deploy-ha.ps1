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

if ($failures) { Write-Host "$failures check(s) failed." -ForegroundColor Red; exit 1 }
Write-Host "All checks passed."
