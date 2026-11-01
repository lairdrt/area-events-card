Set-StrictMode -Version Latest

function Invoke-AreaEventsDeploymentEngine {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)][hashtable]$Operations,
    [switch]$DryRun
  )

  $required = @('Preflight', 'Build', 'Plan', 'Backup', 'Rehearse', 'Quiesce', 'DeployCubi', 'VerifyCubi', 'RestoreDiscovery', 'DeployHA', 'VerifyHA', 'Recover')
  foreach ($name in $required) {
    if (-not $Operations.ContainsKey($name) -or $Operations[$name] -isnot [scriptblock]) {
      throw "Deployment operation '$name' is missing."
    }
  }

  # A Cubi update is a maintenance window: new scans are paused and the running one drains before the service stops, and the discovery
  # state found at preflight is restored once the new service is verified, or after recovery when anything fails from the drain onwards.
  $phase = 'Preflight'
  $cubiChanged = $false
  $schemaChanged = $false
  $haChanged = $false
  $cubiAttempted = $false
  $haAttempted = $false
  $quiesceAttempted = $false
  $discoveryRestored = $false
  try {
    Write-Host '[1/9] Read-only preflight'
    $state = & ($Operations.Preflight)
    Write-Host '[2/9] Build frontend'
    & ($Operations.Build)
    $state = & ($Operations.Plan) $state
    if ($state.NoChanges) {
      Write-Host 'Deployment already matches this built source; no production changes are needed.'
      return [pscustomobject]@{ Result = 'NoChange'; Phase = $phase }
    }
    if ($DryRun) {
      Write-Host '[dry run] Stopping before backups or any production write.'
      return [pscustomobject]@{ Result = 'DryRun'; Phase = $phase }
    }

    Write-Host '[3/9] Create and verify backups'
    & ($Operations.Backup) $state
    if ($state.CubiNeeded) {
      Write-Host '[4/9] Rehearse pending migrations on an isolated database copy'
      $rehearsal = & ($Operations.Rehearse) $state
    } else { $rehearsal = [pscustomobject]@{ Passed = $true; Skipped = $true }; Write-Host '[4/9] Cubi is current; migration rehearsal is skipped.' }
    if (-not $rehearsal.Passed) { throw 'Migration rehearsal did not pass all invariants.' }

    if ($state.CubiNeeded) {
      $phase = 'Discovery drain'
      Write-Host '[5/9] Pause new discovery scans and drain the current one'
      $quiesceAttempted = $true
      & ($Operations.Quiesce) $state
      $phase = 'Cubi cutover'
      Write-Host '[6/9] Stop the drained service, install and migrate, start it'
      $cubiAttempted = $true
      $deployed = & ($Operations.DeployCubi) $state $rehearsal
      $cubiChanged = [bool]$deployed.Changed
      $schemaChanged = [bool]$deployed.SchemaChanged
      Write-Host '[7/9] Verify Cubi service and event API, then restore the discovery state'
      & ($Operations.VerifyCubi) $state
      $phase = 'Discovery restore'
      & ($Operations.RestoreDiscovery) $state
      $discoveryRestored = $true
    } else { Write-Host '[5/9] Cubi code and schema already match; no drain or restart.' }

    if ($state.HANeeded) {
      $phase = 'Home Assistant installation'
      Write-Host '[8/9] Install Home Assistant integration and restart Core'
      $haAttempted = $true
      $ha = & ($Operations.DeployHA) $state
      $haChanged = [bool]$ha.Changed
      Write-Host '[9/9] Verify Home Assistant integration and assets'
      & ($Operations.VerifyHA) $state
    } else { Write-Host '[8/9] Home Assistant files already match; skipping copy and restart.' }
    Write-Host 'Deployment completed successfully.'
    return [pscustomobject]@{ Result = 'Deployed'; Phase = 'Complete'; CubiChanged = $cubiChanged; SchemaChanged = $schemaChanged; HAChanged = $haChanged }
  } catch {
    $original = $_
    # A verified service whose discovery state could not be restored needs no code recovery; that failure is reported as it is.
    if (($cubiAttempted -or $haAttempted) -and $phase -ne 'Discovery restore') {
      Write-Warning "Deployment failed in '$phase'; attempting only recovery the operation layer reports safe."
      try { & ($Operations.Recover) $phase $state $schemaChanged $haChanged }
      catch { Write-Warning "Automatic recovery could not be completed: $($_.Exception.Message)" }
    }
    if ($quiesceAttempted -and -not $discoveryRestored -and $phase -ne 'Discovery restore') {
      Write-Warning 'Restoring the discovery state found at preflight.'
      try { & ($Operations.RestoreDiscovery) $state }
      catch { Write-Warning "The discovery state could not be restored: $($_.Exception.Message)" }
    }
    throw $original
  }
}

Export-ModuleMember -Function Invoke-AreaEventsDeploymentEngine
