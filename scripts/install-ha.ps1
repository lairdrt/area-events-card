<#
.SYNOPSIS
  Copies the Area Events integration (and optionally the app) to a Home Assistant OS machine over its Samba shares.

.DESCRIPTION
  Builds the card, then copies
    custom_components\area_events   ->  <ConfigShare>\custom_components\area_events   (the integration, which also serves the card)
    area_events                     ->  <AppsShare>\area_events                       (the app, only when -AppsShare is given)
  An existing copy of either is backed up first (next to this repository, in area-events-card-ha-backups). Every copied file
  is verified by hash. Nothing else on the share is read, changed or deleted, and Home Assistant is never restarted.

  The app can alternatively be installed from the repository (see docs/installation.md); -AppsShare is for trying a branch
  before it is merged.

.PARAMETER ConfigShare
  The Home Assistant "config" share (the folder that contains configuration.yaml). Default Z:\

.PARAMETER AppsShare
  The Home Assistant local apps share (called "addons" or "local_apps", depending on the Home Assistant version).

.PARAMETER DryRun
  Show what would be copied and change nothing on the share.

.PARAMETER SkipBuild
  Do not run "npm run build" first.
#>
[CmdletBinding()]
param(
  [string]$ConfigShare = "Z:\",
  [string]$AppsShare,
  [switch]$DryRun,
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$backups = Join-Path (Split-Path -Parent $repo) "area-events-card-ha-backups"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Get-RelativeFiles([string]$root) {
  Get-ChildItem -LiteralPath $root -Recurse -File |
    Where-Object { $_.FullName -notmatch "\\__pycache__\\" -and $_.Extension -ne ".pyc" } |
    ForEach-Object { $_.FullName.Substring($root.Length).TrimStart("\") }
}

function Copy-Folder([string]$source, [string]$destination, [string]$label) {
  $files = @(Get-RelativeFiles $source)
  Write-Host ("{0}: {1} files  {2}  ->  {3}" -f $label, $files.Count, $source, $destination)
  if ($DryRun) { return }
  if (Test-Path -LiteralPath $destination) {
    $backup = Join-Path $backups ("{0}.{1}" -f $label, $stamp)
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    Copy-Item -LiteralPath $destination -Destination $backup -Recurse -Force
    Write-Host "  existing copy backed up to $backup"
  }
  foreach ($relative in $files) {
    $target = Join-Path $destination $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    if ($relative -like "*.sh") {
      # the Supervisor cannot start a script with Windows line endings
      $text = [System.IO.File]::ReadAllText((Join-Path $source $relative)).Replace("`r`n", "`n")
      [System.IO.File]::WriteAllText($target, $text, (New-Object System.Text.UTF8Encoding($false)))
    } else {
      Copy-Item -LiteralPath (Join-Path $source $relative) -Destination $target -Force
    }
  }
  $bad = @()
  foreach ($relative in $files) {
    $a = Join-Path $source $relative
    $b = Join-Path $destination $relative
    if ($relative -like "*.sh") {
      if ([System.IO.File]::ReadAllBytes($b) -contains 13) { $bad += $relative }
    } elseif ((Get-FileHash -LiteralPath $a).Hash -ne (Get-FileHash -LiteralPath $b).Hash) { $bad += $relative }
  }
  if ($bad.Count) { throw "$label verification failed for: $($bad -join ', ')" }
  $stale = @(Get-RelativeFiles $destination | Where-Object { $files -notcontains $_ })
  if ($stale.Count) { Write-Host "  note: files on the share that are not part of this version (left alone): $($stale -join ', ')" }
  Write-Host "  verified"
}

if (-not $SkipBuild) {
  Push-Location $repo
  try { npm run build --silent; if ($LASTEXITCODE) { throw "npm run build failed" } } finally { Pop-Location }
}
$card = Join-Path $repo "custom_components\area_events\frontend\area-events-card.js"
if ((Get-Content -Raw -LiteralPath $card) -match "__AREA_EVENTS_CARD_BUILD__") { throw "the card in the integration is not built; run npm run build" }

if (-not (Test-Path -LiteralPath (Join-Path $ConfigShare "configuration.yaml"))) {
  throw "$ConfigShare does not look like the Home Assistant config share (no configuration.yaml). Pass -ConfigShare."
}
Copy-Folder (Join-Path $repo "custom_components\area_events") (Join-Path $ConfigShare "custom_components\area_events") "integration"

if ($AppsShare) {
  if (-not (Test-Path -LiteralPath $AppsShare)) { throw "$AppsShare does not exist. Pass the Home Assistant local apps share." }
  Copy-Folder (Join-Path $repo "area_events") (Join-Path $AppsShare "area_events") "app"
}

Write-Host ""
if ($DryRun) { Write-Host "Dry run: nothing was changed." } else { Write-Host "Copied. Home Assistant was not restarted." }
Write-Host "Next (see docs/installation.md): restart Home Assistant once so it loads the integration, install and start the app, then add the integration."
