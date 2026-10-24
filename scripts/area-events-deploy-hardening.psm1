Set-StrictMode -Version Latest

function Test-AreaEventsShareCapabilities {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$Root)
  $rootPath = [IO.Path]::GetFullPath($Root)
  if (-not (Test-Path -LiteralPath $rootPath -PathType Container)) { throw "HA Samba destination is unavailable: $rootPath" }
  $source = Join-Path $rootPath ('.ae-preflight-' + [guid]::NewGuid().ToString('N') + '.tmp')
  $renamed = $source + '.renamed'
  try {
    $bytes = [Text.Encoding]::UTF8.GetBytes('area-events deployment preflight')
    $stream = [IO.File]::Open($source, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    [IO.File]::Move($source, $renamed)
    if (-not (Test-Path -LiteralPath $renamed -PathType Leaf)) { throw 'HA Samba rename verification failed.' }
    [IO.File]::Delete($renamed)
    if (Test-Path -LiteralPath $renamed) { throw 'HA Samba removal verification failed.' }
  } finally {
    if (Test-Path -LiteralPath $source) { Remove-Item -LiteralPath $source -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $renamed) { Remove-Item -LiteralPath $renamed -Force -ErrorAction SilentlyContinue }
  }
  return $true
}

function Assert-AreaEventsHaPrerequisites {
  [CmdletBinding()]
  param([Parameter(Mandatory)]$Config, [Parameter(Mandatory)]$CurrentUser)
  if ($Config.state -ne 'RUNNING') { throw 'Home Assistant Core is not RUNNING.' }
  if ($CurrentUser.is_admin -ne $true) { throw 'Stored HA token is not authenticated as an administrator; deployment restart prerequisite is not met.' }
  return $true
}

function Get-AreaEventsRequiredFrontendAssets {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$FrontendRoot)
  $root = [IO.Path]::GetFullPath($FrontendRoot)
  $manifest = Join-Path $root 'presentation\manifest.js'
  if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw 'Presentation manifest is missing.' }
  $text = Get-Content -Raw -LiteralPath $manifest
  $imports = [regex]::Matches($text, '(?m)^\s*import\s+.+?\s+from\s+["''](\./[^"'']+\.js)["'']\s*;?')
  if (-not $imports.Count) { throw 'Presentation manifest has no statically registered module imports.' }
  $assets = [System.Collections.Generic.List[string]]::new()
  $assets.Add((Join-Path $root 'area-events-card.js'))
  $assets.Add($manifest)
  foreach ($match in $imports) {
    $relative = $match.Groups[1].Value.Substring(2).Replace('/', '\')
    $asset = Join-Path (Split-Path -Parent $manifest) $relative
    if (-not (Test-Path -LiteralPath $asset -PathType Leaf)) { throw "Manifest asset is missing: $relative" }
    if (-not $assets.Contains($asset)) { $assets.Add($asset) }
  }
  foreach ($asset in $assets) { if (-not (Test-Path -LiteralPath $asset -PathType Leaf)) { throw "Required frontend asset is missing: $asset" } }
  return $assets.ToArray()
}

function Assert-AreaEventsFeedResponse {
  [CmdletBinding()]
  param([Parameter(Mandatory)]$Response, [Parameter(Mandatory)][string]$Area)
  if ($null -eq $Response -or $Response.events -isnot [System.Array]) { throw "Event API response for '$Area' has an invalid schema; expected an events array." }
  foreach ($event in $Response.events) {
    if ($null -eq $event -or [string]::IsNullOrWhiteSpace([string]$event.id) -or [string]::IsNullOrWhiteSpace([string]$event.title)) {
      throw "Event API response for '$Area' contains an event without required id/title fields."
    }
  }
  return $true
}

Export-ModuleMember -Function Test-AreaEventsShareCapabilities, Assert-AreaEventsHaPrerequisites, Get-AreaEventsRequiredFrontendAssets, Assert-AreaEventsFeedResponse
