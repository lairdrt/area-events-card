# Refreshes the event feed and publishes it to Home Assistant. Event data only:
# card software is still deployed with .\deploy.ps1.
#
#   .\refresh-events.ps1                      refresh and publish
#   .\refresh-events.ps1 -DryRun              fetch and report, write nothing
#   .\refresh-events.ps1 -NoPublish           update data\events.json only
#   .\refresh-events.ps1 -DestinationDirectory \\host\share\www\area-events-card
#
# Exit code: 0 = all sources refreshed, 2 = some sources failed (previous data
# kept), 1 = nothing refreshed or publishing failed (feed left unchanged).
param(
    [string]$DestinationDirectory,
    [string[]]$Only,
    [switch]$DryRun,
    [switch]$NoPublish
)

# EDIT THESE if your Home Assistant share differs (same share as deploy.ps1).
# Mapped drives belong to a logon session, so a scheduled task may not see Z:;
# the UNC path of the same share is the fallback.
$HaConfigShare = "Z:"
$HaConfigUnc = "\\192.168.1.48\config"

# This script only ever writes events.json into www\area-events-card.
$CardDirectoryName = "area-events-card"

$LogFile = Join-Path $PSScriptRoot "data\refresh.log"
$MaxLogLines = 2000

function Write-Log([string]$Message) {
    $Line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $Line
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
    Add-Content -LiteralPath $LogFile -Value $Line -Encoding UTF8
}

function Find-Python {
    # Take the first python that really runs; the Microsoft Store stub in
    # WindowsApps exists on machines without Python and fails when run.
    foreach ($Command in @(Get-Command python, py -All -ErrorAction SilentlyContinue)) {
        $Version = & $Command.Source --version 2>$null
        if ($LASTEXITCODE -eq 0 -and "$Version" -match "^Python 3") {
            return $Command.Source
        }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Log "ERROR: Python was not found on PATH for user $env:USERNAME."
    exit 1
}

$PublishArgument = @()
if (-not $NoPublish -and -not $DryRun) {
    if (-not $DestinationDirectory) {
        $Candidates = @((Join-Path $HaConfigShare "www\$CardDirectoryName"), (Join-Path $HaConfigUnc "www\$CardDirectoryName"))
        $DestinationDirectory = $Candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -First 1
        if (-not $DestinationDirectory) {
            Write-Log "ERROR: no Home Assistant destination reachable as user $env:USERNAME. Tried: $($Candidates -join ', ')"
            Write-Log "       Mapped drives are per logon session; see README (Hourly refresh > Troubleshooting)."
            exit 1
        }
    }
    if ((Split-Path -Leaf $DestinationDirectory) -ne $CardDirectoryName) {
        Write-Log "ERROR: refusing to publish: destination directory is not '$CardDirectoryName': $DestinationDirectory"
        exit 1
    }
    if (-not (Test-Path -LiteralPath $DestinationDirectory -PathType Container)) {
        Write-Log "ERROR: destination directory does not exist (run .\deploy.ps1 once first): $DestinationDirectory"
        exit 1
    }
    $PublishArgument = @("--publish", (Join-Path $DestinationDirectory "events.json"))
}

$Arguments = @("-m", "discovery") + $PublishArgument
if ($DryRun) { $Arguments += "--dry-run" }
if ($Only) { $Arguments += @("--only") + $Only }

Write-Log "Refresh starting (python: $Python; user: $env:USERNAME; publish: $(if ($PublishArgument) { $PublishArgument[1] } else { 'no' }))"
$Started = Get-Date
$env:PYTHONIOENCODING = "utf-8"
$OutFile = [System.IO.Path]::GetTempFileName()
$ErrFile = [System.IO.Path]::GetTempFileName()
try {
    $Process = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $PSScriptRoot `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
    $ExitCode = $Process.ExitCode
    foreach ($File in $OutFile, $ErrFile) {
        Get-Content -LiteralPath $File -Encoding UTF8 | ForEach-Object { Write-Log "  $_" }
    }
}
finally {
    Remove-Item -LiteralPath $OutFile, $ErrFile -ErrorAction SilentlyContinue
}

$Seconds = [int]((Get-Date) - $Started).TotalSeconds
Write-Log "Refresh finished with exit code $ExitCode in ${Seconds}s"

# Keep the log bounded.
$Lines = @(Get-Content -LiteralPath $LogFile -Encoding UTF8)
if ($Lines.Count -gt $MaxLogLines) {
    Set-Content -LiteralPath $LogFile -Value $Lines[-$MaxLogLines..-1] -Encoding UTF8
}
exit $ExitCode
