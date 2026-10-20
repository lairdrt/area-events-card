# EDIT THIS VALUE before first use if your Home Assistant mapped drive differs.
# The Samba share exposes Home Assistant's /config directory directly.
$HaConfigShare = "Z:"

# This script only ever writes into www\area-events-card on the share.
$CardDirectoryName = "area-events-card"

$SourceFile = Join-Path $PSScriptRoot "dist\area-events-card.js"
$LoaderSourceFile = Join-Path $PSScriptRoot "loader.js"
$HaWwwDirectory = Join-Path $HaConfigShare "www\$CardDirectoryName"
$DestinationFile = Join-Path $HaWwwDirectory "area-events-card.js"
$LoaderDestinationFile = Join-Path $HaWwwDirectory "loader.js"

# Guard against an edit above pointing this script at another card's
# directory (e.g. www\yardian-card), whose loader.js it would overwrite.
if ((Split-Path -Leaf $HaWwwDirectory) -ne $CardDirectoryName) {
    Write-Error "Refusing to deploy: destination directory is not '$CardDirectoryName': $HaWwwDirectory"
    exit 1
}

Write-Host "Local source: $SourceFile"
Write-Host "HA destination: $DestinationFile"
Write-Host "Loader source: $LoaderSourceFile"
Write-Host "Loader destination: $LoaderDestinationFile"

# The deployable module is compiled from TypeScript, so build it first.
# Output is only shown if the build fails.
Push-Location -LiteralPath $PSScriptRoot
try { $BuildOutput = & npm run build 2>&1 }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) {
    $BuildOutput | ForEach-Object { Write-Host $_ }
    Write-Error "Failed to build the area events card: npm run build exited with code $LASTEXITCODE"
    exit 1
}

if (-not (Test-Path -LiteralPath $SourceFile -PathType Leaf)) {
    Write-Error "Local source file does not exist: $SourceFile"
    exit 1
}

if (-not (Test-Path -LiteralPath $LoaderSourceFile -PathType Leaf)) {
    Write-Error "Local loader file does not exist: $LoaderSourceFile"
    exit 1
}

if (-not (Test-Path -LiteralPath $HaConfigShare -PathType Container)) {
    Write-Error "Home Assistant Samba share is not accessible: $HaConfigShare"
    exit 1
}

# Deliberately not created here: making this directory is the one-time,
# conscious step that enables deployment.
if (-not (Test-Path -LiteralPath $HaWwwDirectory -PathType Container)) {
    Write-Error "Home Assistant www directory does not exist: $HaWwwDirectory"
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "Git is not available. Install Git or add it to PATH before deploying."
    exit 1
}

$ShortHash = & git -C $PSScriptRoot rev-parse --short HEAD 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to obtain the Git commit hash (the repository needs at least one commit): $ShortHash"
    exit 1
}
$ShortHash = ($ShortHash | Out-String).Trim()

$Placeholder = "__AREA_EVENTS_CARD_BUILD__"

try {
    # -Encoding UTF8: the bundle may contain non-ASCII characters, which
    # Windows PowerShell 5.1 would otherwise misread as the ANSI code page.
    $SourceContent = Get-Content -LiteralPath $SourceFile -Raw -Encoding UTF8 -ErrorAction Stop

    $DeployedSourceFiles = @(
        Get-Item -LiteralPath $SourceFile -ErrorAction Stop
        Get-Item -LiteralPath $LoaderSourceFile -ErrorAction Stop
    ) | Sort-Object FullName

    $SourceRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path.TrimEnd("\") + "\"
    $ManifestLines = $DeployedSourceFiles | ForEach-Object {
        $RelativePath = $_.FullName.Substring($SourceRoot.Length).Replace("\", "/")
        $FileHash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        "$RelativePath|$FileHash"
    }
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $ManifestBytes = $Utf8NoBom.GetBytes(($ManifestLines -join "`n"))
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()

    try {
        $ManifestHash = ([BitConverter]::ToString($Sha256.ComputeHash($ManifestBytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $Sha256.Dispose()
    }

    $WorkingTreeHash = $ManifestHash.Substring(0, 6)
    $BuildIdentifier = "AREA-EVENTS $ShortHash-$WorkingTreeHash"

    Write-Host "Deploying build: $BuildIdentifier"

    if (-not $SourceContent.Contains($Placeholder)) {
        Write-Error "Build placeholder was not found in the source file: $Placeholder"
        exit 1
    }

    $DeployedContent = $SourceContent.Replace($Placeholder, $BuildIdentifier)
    $DeployedBytes = $Utf8NoBom.GetBytes($DeployedContent)
    [System.IO.File]::WriteAllBytes($DestinationFile, $DeployedBytes)
    Copy-Item -LiteralPath $LoaderSourceFile -Destination $LoaderDestinationFile -Force -ErrorAction Stop
}
catch {
    Write-Error "Failed to deploy area events card files: $($_.Exception.Message)"
    exit 1
}

if (-not (Test-Path -LiteralPath $DestinationFile -PathType Leaf)) {
    Write-Error "Destination file does not exist after deployment: $DestinationFile"
    exit 1
}

if (-not (Test-Path -LiteralPath $LoaderDestinationFile -PathType Leaf)) {
    Write-Error "Loader destination file does not exist after deployment: $LoaderDestinationFile"
    exit 1
}

$DestinationSize = (Get-Item -LiteralPath $DestinationFile).Length

if ($DeployedBytes.Length -ne $DestinationSize) {
    Write-Error "File size verification failed. Expected: $($DeployedBytes.Length) bytes; destination: $DestinationSize bytes."
    exit 1
}

if ($DestinationSize -eq 0) {
    Write-Error "Deployed area-events-card.js is empty: $DestinationFile"
    exit 1
}

$LoaderSourceSize = (Get-Item -LiteralPath $LoaderSourceFile).Length
$LoaderDestinationSize = (Get-Item -LiteralPath $LoaderDestinationFile).Length

if ($LoaderSourceSize -eq 0 -or $LoaderDestinationSize -eq 0) {
    Write-Error "Loader file size verification failed because the source or destination is empty."
    exit 1
}

if ($LoaderSourceSize -ne $LoaderDestinationSize) {
    Write-Error "Loader file size verification failed. Source: $LoaderSourceSize bytes; destination: $LoaderDestinationSize bytes."
    exit 1
}

$DestinationContent = Get-Content -LiteralPath $DestinationFile -Raw -Encoding UTF8
$ExpectedBuildLine = 'const AREA_EVENTS_CARD_BUILD = "' + $BuildIdentifier + '";'

if (-not $DestinationContent.Contains($ExpectedBuildLine)) {
    Write-Error "Build identifier verification failed. Expected to find: $ExpectedBuildLine"
    exit 1
}

Write-Host "Deployment succeeded with build identifier: $BuildIdentifier"
