Set-StrictMode -Version Latest

function Import-AreaEventsDpapiCredential {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Encrypted Home Assistant credential file is missing.' }
  $ciphertext=(Get-Content -Raw -LiteralPath $Path).Trim()
  if ([string]::IsNullOrWhiteSpace($ciphertext)) { throw 'Encrypted Home Assistant credential file is empty.' }
  return ConvertTo-SecureString $ciphertext
}

function New-AreaEventsRemotePythonCommand {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$Code, [hashtable]$Variables=@{}, [ValidateRange(5,600)][int]$TimeoutSeconds=60)
  $json=ConvertTo-Json -InputObject $Variables -Compress -Depth 4
  $env64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
  $code64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
  $wrapper='import base64,json,os;os.environ.update(json.loads(base64.b64decode("'+$env64+'")));exec(compile(base64.b64decode("'+$code64+'"),"<ae>","exec"))'
  $wrapper64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($wrapper))
  $timeoutText="${TimeoutSeconds}s"
  return "printf %s $wrapper64 | base64 -d | timeout --signal=TERM --kill-after=5s $timeoutText python3 -"
}

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

function Test-AreaEventsCodeBackupMembers {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][ValidateSet('legacy','modern')][string]$Layout,
    [Parameter(Mandatory)][string[]]$Required,
    [Parameter(Mandatory)][string[]]$Archived
  )
  $requiredNames = @($Required | ForEach-Object { $_.Replace('\','/') } | Where-Object { $_ })
  $archivedNames = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  foreach ($name in $Archived) { [void]$archivedNames.Add($name.Replace('\','/')) }
  if ($Layout -eq 'legacy') {
    $packageCode = @($requiredNames | Where-Object { $_ -like 'area_events/*' })
    if ($requiredNames -notcontains 'scripts/discovery.py' -or $packageCode.Count -eq 0) { return $false }
  } else {
    $packageCode = @($requiredNames | Where-Object { $_ -like 'src/area_events/*' })
    if ($requiredNames -notcontains 'pyproject.toml' -or $packageCode.Count -eq 0) { return $false }
  }
  if (-not $requiredNames.Count) { return $false }
  foreach ($name in $requiredNames) { if (-not $archivedNames.Contains($name)) { return $false } }
  return $true
}

function Resolve-AreaEventsSystemdLayout {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][string]$WorkingDirectory,
    [Parameter(Mandatory)][string]$ExecStart,
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][bool]$LegacyEntryExists,
    [Parameter(Mandatory)][bool]$ModernPackageExists
  )
  $expected = $Root.Replace([string][char]92,'/').TrimEnd('/')
  $actual = $WorkingDirectory.Replace([string][char]92,'/').TrimEnd('/')
  if (-not $actual.Equals($expected,[StringComparison]::Ordinal)) { throw 'Discovery Service WorkingDirectory does not match its deployment root.' }
  $argvMatch = [regex]::Match($ExecStart,'(?:^|[;{])\s*argv\[\]=([^;}]+)')
  if (-not $argvMatch.Success) { throw 'Discovery Service ExecStart could not be parsed safely.' }
  $argv = @($argvMatch.Groups[1].Value.Trim() -split '\s+')
  if ($argv.Count -lt 2 -or $argv[0] -notmatch '(^|/)python3?$') { throw 'Discovery Service ExecStart does not use a supported Python command.' }
  if ($argv.Count -ge 3 -and $argv[1] -eq '-m' -and $argv[2] -eq 'area_events') {
    if (-not $ModernPackageExists) { throw 'Active modular Discovery Service package is incomplete.' }
    return 'modern'
  }
  if ($argv[1] -in @('scripts/discovery.py',($expected+'/scripts/discovery.py')) -and $LegacyEntryExists) { return 'legacy' }
  throw 'Discovery Service ExecStart uses an unknown or unsupported code layout.'
}

function Convert-AreaEventsSystemdExecStart {
  [CmdletBinding()]
  param([Parameter(Mandatory)][string]$UnitText,[Parameter(Mandatory)][string]$Root)
  $absolute = [regex]::Escape(($Root.Replace([string][char]92,'/').TrimEnd('/') + '/scripts/discovery.py'))
  $entry = '(?:scripts/discovery\.py|' + $absolute + ')'
  $pattern = '(?m)^(ExecStart=\S+)\s+' + $entry + '(?=\s|$)'
  $updated = [regex]::Replace($UnitText,$pattern,'$1 -m area_events',1)
  if ($updated -eq $UnitText) { throw 'systemd unit does not use a supported legacy Python discovery entry point' }
  return $updated
}

Export-ModuleMember -Function Import-AreaEventsDpapiCredential, New-AreaEventsRemotePythonCommand, Test-AreaEventsShareCapabilities, Assert-AreaEventsHaPrerequisites, Get-AreaEventsRequiredFrontendAssets, Assert-AreaEventsFeedResponse, Test-AreaEventsCodeBackupMembers, Resolve-AreaEventsSystemdLayout, Convert-AreaEventsSystemdExecStart
