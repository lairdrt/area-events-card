# Cutover checklist: legacy Area Events app to the Discovery Service integration

Status: **prepared, not executed.** Nothing here has been run against the live Home Assistant. Every step that changes it is marked **[LIVE]** and waits for approval.

## State when this was written (2026-10-23)

| Item | State |
|---|---|
| Cubi `192.168.1.88` | Debian 13, Python 3.13.5. Discovery + Extraction run from `~/area-events-discovery` as the systemd **user** unit `area-events-discovery` (enabled, restarts on failure, 50% CPU / 512 MB cap). One SQLite database, 23 real San Diego events, HTTPS with a certificate for `192.168.1.88`. Frigate and the legacy `area-events` container are untouched. |
| Cubi lingering | **Disabled.** The account may not enable it. The user unit stops when the last login session ends and does not start at boot. |
| Home Assistant `192.168.1.48` | Core 2026.10.2. Entry `area_events` (source `hassio`, version 1) is loaded; the legacy integration and app are live. |
| Live card | Dashboard `test_2`: `area: "San Diego, CA"`, `radius_miles: 20`, two `sources`. |
| Backups | Home Assistant backup `7e845601` (partial, `/config`, 82 MB). Local copy of the integration and dashboard files in `data/ha-backup-2026-10-23` (git-ignored). `install-ha.ps1` also copies the old integration to `..\area-events-card-ha-backups\integration.<stamp>` before it overwrites anything. |

## Before the cutover (user, once)

1. **Cubi lingering**, so the service survives logout and reboot. Needs root, which the service account does not have:
   `sudo loginctl enable-linger robohobo`
   Check: `loginctl show-user robohobo --property=Linger` prints `Linger=yes`. Until then the service runs only while some login session of `robohobo` exists (the acceptance run held one open with a long `ssh ... sleep`). Do not call reboot persistence verified before a reboot test: after enabling linger, run `systemctl --user restart area-events-discovery`, log out of all sessions, and check `systemctl --user is-active area-events-discovery` from a new session; reboot only when Frigate can tolerate it.
2. Confirm the certificate and tokens are where the form expects them (the Cubi service prints their paths at start):
   - certificate: `~/area-events-discovery/certs/cert.pem` (public part; copy to Home Assistant as `/config/area_events_cert.pem`, for example with the Samba `config` share: `Copy-Item <cert.pem> Z:\area_events_cert.pem`)
   - read token: `~/area-events-discovery/data/discovery/api-read-token`
   - control token (administrators only, optional): `.../api-token`
   Do not paste tokens into chat or commit them.

## Area name

The live card says `San Diego, CA`; the service area is `San Diego`. Areas are matched by name, so the card must change. **Chosen: change the card to `area: San Diego`** (no change on the Cubi). Do it in the dashboard editor *after* the new integration is running, so the old card keeps working until then. `radius_miles` and `sources` in that card are ignored by the new card and can be deleted.

## Cutover **[LIVE]** (needs approval)

1. Confirm the Cubi service: `systemctl --user is-active area-events-discovery` is `active`, and the read-token check in `docs/ha-integration-2d.md` passes.
2. Confirm the Home Assistant backup `7e845601` exists (Settings, System, Backups) and the local backup folder is present.
3. **Deploy, without rebuilding the legacy app:**
   `.\scripts\deploy-ha.ps1 -SkipApp` (never `-Force`). It builds the card, backs up the old integration (`install-ha.ps1`), copies the new integration and card, removes only `client.py`, `profile.py` and `media_view.py` from the integration folder, restarts Home Assistant Core once, and checks the integration is loaded and the served card build matches.
   Expected: Home Assistant is unavailable for about 90 s; the old entry shows a migration error afterwards (it is refused with a clear log line). The Area Events card shows an error until step 6.
4. In Settings, Devices & services, **remove the old Area Events entry** (the one with the migration error). Do not edit `.storage`.
5. **Add the integration**: Add integration, Area Events. Address `https://192.168.1.88:8780`, read token, optional control token, certificate file `/config/area_events_cert.pem`.
   Expected errors if wrong: "rejected" (token), "could not be reached" (address or untrusted certificate), "certificate file could not be read" (path).
6. Edit the `test_2` card: `area: San Diego`. Add a second card `area: Austin`.
7. Verify (see `docs/ha-integration-2d.md`, "Verification on the device"): San Diego shows real City events with unverified locations marked; Austin says no sources are attached; stop and start the service for the unavailable and recovery states; an administrator can call `area_events/pause` and `area_events/resume`, a non-administrator cannot.

## Rollback **[LIVE]**

Use whichever matches how far the cutover got.

- **The new integration misbehaves but Home Assistant is healthy:** Settings, Devices & services, Area Events, Delete. Then restore the old files (below) and restart.
- **Restore the old integration files** (stop at "restart" if Home Assistant is down for another reason):
  1. Copy the backup back over the share: `Remove-Item Z:\custom_components\area_events -Recurse -Force; Copy-Item ..\area-events-card-ha-backups\integration.<stamp> Z:\custom_components\area_events -Recurse` (the stamped folder `install-ha.ps1` made), or `data\ha-backup-2026-10-23\integration`.
  2. Restart Home Assistant (Settings, System, Restart, or `deploy-ha.ps1`'s restart). The legacy entry no longer exists, so add Area Events again through the discovered app (the legacy app was never stopped or rebuilt by `-SkipApp`).
  3. Restore the dashboard card only if it was edited: put back `area: "San Diego, CA"` with its `radius_miles` and `sources` (copy in `data/ha-backup-2026-10-23/dashboards/lovelace.dashboard_test_2`, read-only reference; edit through the dashboard editor, not `.storage`).
- **Home Assistant will not start after the restart:** restore the backup from the Home Assistant recovery screen or Settings, System, Backups, slug `7e845601` (config only). Home Assistant tolerates a failing custom integration without stopping, so this is unlikely.
- **Git baseline:** the previous integration also exists in the protected tag `area-events-imagery-runtime-accepted-2026-10-22` (`git show <tag>:custom_components/area_events/...`); do not move the tag.

The Cubi service is independent of all of this: it keeps running (or stopped) regardless of what happens in Home Assistant, and rolling back Home Assistant does not touch its database.

## Executed 2026-10-23

Done with approval: certificate placed at `/config/area_events_cert.pem` (public part only), `deploy-ha.ps1 -SkipApp` (Core restart, back online after 85 s, the three obsolete modules removed after the backup), the old `hassio` entry (it was in `migration_error`) removed through the REST API, the new entry created through the real config flow (HTTPS, certificate file, read and control tokens; state `loaded`), and the `test_2` dashboard saved through `lovelace/config/save` with a San Diego card (`area: San Diego`) and a second view with an Austin card.

Checked on the live system: the integration's WebSocket commands, 20 simultaneous card subscriptions, no card-triggered crawl, administrator pause/resume, refusal of a temporary non-administrator (removed afterwards), a stale revision, an outage (Discovery stopped with `systemctl --user stop`) with an honest unavailable view and automatic recovery, an entry reload with Discovery's process unchanged, and no tokens in the Home Assistant log or in the served card.

Not verified: how the card looks in a browser, a real event revision (the City listing did not change), and a reboot of the Cubi.

Defect found and fixed on the way (commit `96cad6e`): a crawl of a page whose markup changed but whose text did not, together with a changed event, failed with a foreign-key error and rolled back. Evidence now names the stored snapshot, and an unexpected storage error releases the job with backoff.

Unrelated, pre-existing and left alone: Home Assistant's own configuration still has the legacy `area_events_sync` shell command and automation, which fail every 30 minutes (`destination folder does not exist: /config/www/area-events-card`). They belong to the earlier bridge and can be removed by the owner.
