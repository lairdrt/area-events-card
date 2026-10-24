# Automated deployment

## One-time setup

1. Ensure Windows OpenSSH can connect to `robohobo@192.168.1.88` using the existing key and host-key entry. That account must be able to control its `area-events-discovery.service` and write the service code and backup directories. The deployment refuses password prompts and does not change SSH host-key policy.
2. Map the Home Assistant config Samba share (default `Z:\`) and confirm it contains `configuration.yaml`. The mapped account needs write and rename permission under `custom_components` for the `area_events` integration.
3. In Home Assistant, create a long-lived token for an administrator. Run `.\scripts\deploy-area-events.ps1 -SetupCredentials` once and paste the token at the hidden prompt. Windows DPAPI encrypts it for the current Windows user; the file ACL is restricted to that user. The token is never stored in Git or passed on a command line. Revoke it in Home Assistant if the Windows account is retired.
4. Optionally set `HA_URL` and `HA_CONFIG_SHARE` for a different HA address or share mapping.

## Deploy

Run:

```powershell
.\scripts\deploy-area-events.ps1
```

The deployment checks the branch and source state, validates SSH and authenticated HA access, checks Cubi code/backup write prerequisites and required remote commands, inspects the intended systemd service, database, and event API, and checks the mapped Samba share with a temporary write/rename/remove probe in `custom_components` (and the existing integration directory when present). It confirms the HA token's current user is an administrator through the read-only WebSocket `auth/current_user` command. That proves the user's role; the deployer does not send a restart request during preflight, so acceptance of a future restart request cannot be proven without a mutation.

The frontend is built before final fingerprints and component decisions. The HA fingerprint uses the generated working-tree assets that are copied to the share; unexpected build changes to tracked files stop planning. Cubi and HA updates are planned independently, and a no-change result is based on the built artifacts. Required frontend files are derived from the presentation manifest's imports, so registering another renderer does not require changing deployment code. Empty but schema-valid event pages pass health checks; malformed API responses still fail.

Before changing either installation, it makes a SQLite API backup and a private Cubi archive of the accepted code, media, and service configuration. It also saves the HA integration, configuration, and Lovelace storage locally. If migrations are pending, it applies them to an isolated copy and checks the schema, integrity, event keys, and candidate associations. The Cubi cutover stops only `area-events-discovery.service`; port 8765 is not addressed. HA files are copied through the existing Samba share and restarted through the authenticated HA deployment helper.

## Dry run and local tests

```powershell
.\scripts\deploy-area-events.ps1 -DryRun
.\scripts\test-deploy-area-events.ps1
```

Dry run may build local frontend files and performs preflight, including a temporary file capability check on the configured HA share. It stops before creating backups or making persistent writes to Cubi or HA. The test script uses mocked operations and isolated state; it does not contact either host.

## Backups and recovery

- Cubi backups: `/home/robohobo/backups/area-events-deploy/<timestamp>`
- Windows backups: `%LOCALAPPDATA%\AreaEventsBackups\<timestamp>`

The deployer never restores a database snapshot over live data. If code deployment fails before the schema changes, it attempts a code-only rollback and restarts the intended service. If the schema advanced, it stops the intended service and preserves the snapshot for diagnosis. On HA failure it attempts to restore the saved integration and performs one bounded authenticated restart. Any recovery failure is reported and the backup is retained. Inspect HA and Cubi state before retrying a failed deployment.
