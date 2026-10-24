Set-StrictMode -Version Latest

function Invoke-AreaEventsDeploymentEngine {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][hashtable]$Operations,
    [switch]$DryRun
  )

  $required = @('Preflight', 'Build', 'Backup', 'Rehearse', 'DeployCubi', 'VerifyCubi', 'DeployHA', 'VerifyHA', 'Recover')
  foreach ($name in $required) {
    if (-not $Operations.ContainsKey($name) -or $Operations[$name] -isnot [scriptblock]) {
      throw "Deployment operation '$name' is missing."
    }
  }

  $phase = 'Preflight'
  $cubiChanged = $false
  $schemaChanged = $false
  $haChanged = $false
  $cubiAttempted = $false
  $haAttempted = $false
  try {
    Write-Host '[1/8] Read-only preflight'
    $state = & ($Operations.Preflight)
    if ($state.NoChanges) {
      Write-Host 'Deployment already matches this source; no production changes are needed.'
      return [pscustomobject]@{ Result = 'NoChange'; Phase = $phase }
    }

    Write-Host '[2/8] Build frontend'
    & ($Operations.Build)
    if ($DryRun) {
      Write-Host '[dry run] Stopping before backups or any production write.'
      return [pscustomobject]@{ Result = 'DryRun'; Phase = $phase }
    }

    Write-Host '[3/8] Create and verify backups'
    & ($Operations.Backup) $state
    if ($state.CubiNeeded) {
      Write-Host '[4/8] Rehearse pending migrations on an isolated database copy'
      $rehearsal = & ($Operations.Rehearse) $state
    } else { $rehearsal = [pscustomobject]@{ Passed = $true; Skipped = $true }; Write-Host '[4/8] Cubi is current; migration rehearsal is skipped.' }
    if (-not $rehearsal.Passed) { throw 'Migration rehearsal did not pass all invariants.' }

    if ($state.CubiNeeded) {
      $phase = 'Cubi cutover'
      Write-Host '[5/8] Deploy Cubi service and database'
      $cubiAttempted = $true
      $deployed = & ($Operations.DeployCubi) $state $rehearsal
      $cubiChanged = [bool]$deployed.Changed
      $schemaChanged = [bool]$deployed.SchemaChanged
      Write-Host '[6/8] Verify Cubi service and event API'
      & ($Operations.VerifyCubi) $state
    } else { Write-Host '[5/8] Cubi code and schema already match; skipping Cubi restart.' }

    if ($state.HANeeded) {
      $phase = 'Home Assistant installation'
      Write-Host '[7/8] Install Home Assistant integration and restart Core'
      $haAttempted = $true
      $ha = & ($Operations.DeployHA) $state
      $haChanged = [bool]$ha.Changed
      Write-Host '[8/8] Verify Home Assistant integration and assets'
      & ($Operations.VerifyHA) $state
    } else { Write-Host '[7/8] Home Assistant files already match; skipping copy and restart.' }
    Write-Host 'Deployment completed successfully.'
    return [pscustomobject]@{ Result = 'Deployed'; Phase = 'Complete'; CubiChanged = $cubiChanged; SchemaChanged = $schemaChanged; HAChanged = $haChanged }
  } catch {
    $original = $_
    if ($cubiAttempted -or $haAttempted) {
      Write-Warning "Deployment failed in '$phase'; attempting only recovery the operation layer reports safe."
      try { & ($Operations.Recover) $phase $state $schemaChanged $haChanged }
      catch { Write-Warning "Automatic recovery could not be completed: $($_.Exception.Message)" }
    }
    throw $original
  }
}

Export-ModuleMember -Function Invoke-AreaEventsDeploymentEngine
