> **Superseded for the integration (Milestone 2D).** The integration no longer discovers events through a Home Assistant app; it reads them from the Discovery Service. Use [ha-integration-2d.md](ha-integration-2d.md). The steps below describe the earlier arrangement and are kept for reference.

# Installing on Home Assistant OS

Written for Home Assistant OS on a Raspberry Pi 4 (aarch64). Nothing here restarts Home Assistant for you, and nothing needs another machine once it is installed.

## 1. The integration (serves the card)

On the PC with this repository and the Samba `config` share mapped (for example as `Z:`):

```
.\scripts\install-ha.ps1 -DryRun      # shows what would be copied
.\scripts\install-ha.ps1              # builds the card, backs up any earlier copy, copies, verifies by hash
```

This writes only `config\custom_components\area_events`. Without the script, copy that folder by hand. Then restart Home Assistant once (Settings > System > Restart); Home Assistant only loads a new custom integration at startup.

## 2. The app (finds the events)

Pick one:

- **From the repository.** Settings > Apps > App store > ⋮ > Repositories, add `https://github.com/lairdrt/area-events-card` (use `https://github.com/lairdrt/area-events-card#ha-native-refactor` to try this branch before it is merged). Open **Area Events** in the store and install it.
- **From this PC, before anything is merged.** Map the Home Assistant local apps share (named `addons` or `local_apps`, depending on the version) and run `.\scripts\install-ha.ps1 -AppsShare <that share>`. Choose Check for updates in the App store, then install **Area Events** from the *Local apps* section.

Start the app. The first start builds the image on the Pi (a minute or two; it needs no package downloads). The Log tab should show `Area Events engine listening` and `announced <hostname>:8765 to Home Assistant`. **There is no token to create or copy:** the app generates one and hands it to Home Assistant through Supervisor discovery.

## 3. Connect the integration

Settings > Devices & services shows a discovered **Area Events**. Select it and Submit. If it does not appear (for example the app runs on another machine), Add integration > **Area Events** and enter the app's address and a token (set the app's optional *Token* option to 32 or more characters of your choosing). An app that restarts or gets a new hostname updates the existing entry by itself; a rejected token starts Home Assistant's re-authentication.

## 4. The card

No dashboard resource and no file copying: the integration registers the card itself. Reload the browser page once after the integration is first set up. Then edit a dashboard (for example `test_2`) > Add card > **Area Events Card**, or use the YAML editor:

```yaml
type: custom:area-events-card
title: Austin Events
area: Austin, TX
```

Leave out `area` to use the Home Assistant home location. All options: [README](../README.md#card-configuration).

## Checking it works

1. App log: `listening` and `announced`.
2. Settings > Devices & services > Area Events is *Connected*.
3. The card shows "Finding events near ..." and, after the first search (minutes for a new area), the events. The header shows the place, radius and update time.
4. Change the card's area in the editor: a second search starts, the first one's events are not replaced by it.

## Updating and rolling back

Update: run the script again (it backs up the earlier copy first to `area-events-card-ha-backups` next to the repository), restart Home Assistant if the integration changed, and update the app from the store (or re-copy it and Check for updates, bumping `version` in `area_events/config.yaml`). Roll back: restore the backed-up folder; the previous design is tag `area-events-pre-ha-refactor-2026-10-20`. Removing the integration entry and uninstalling the app leaves no other trace; the app's data is deleted with the app.

## If something is off

| Symptom | Check |
|---|---|
| No "Area Events Card" in the picker | The integration is not loaded (restart done? any error in Settings > System > Logs) or the page needs a reload. |
| Card says the app is not reachable | The app is stopped, or still building; see its Log tab. |
| Integration asks for re-authentication | The app's token option changed; restart the app so it announces itself again, or enter the token. |
| "Source not supported" | The website has no adapter; see the README's *Source websites*. |
| "no enabled source applies" | No built-in source covers that area yet. |

## One-command deployment

For repeated updates from this PC, `scripts\deploy-ha.ps1` wraps the installer: it builds the card, installs the files (`install-ha.ps1`), rebuilds only the Area Events app, restarts Home Assistant Core once and checks the result.

```powershell
.\scripts\deploy-ha.ps1 -AppsShare X:\ -DryRun   # shows the plan, changes nothing
.\scripts\deploy-ha.ps1 -AppsShare X:\            # deploy; then refresh the HA dashboard
```

It restarts nothing that did not change (the app is rebuilt only when its files differ, Core is restarted only when the integration differs; `-Force` overrides), never touches any other app, does not restart Core if the install or the app rebuild fails, uses bounded waits, and refuses to run twice at once. It never automates the browser.

**One-time setup.** The script uses Home Assistant's own API with an administrator's long-lived token (no SSH, nothing stored in the repository): Profile > Security > Long-lived access tokens > Create token, then in a terminal run `setx HA_URL "http://homeassistant.local:8123"` and `setx HA_TOKEN "<token>"` and open a new terminal. Alternatively keep the token alone in a file outside the repository and pass `-TokenFile <path>`. Revoke the token on the same profile page if it is ever exposed. `scripts	est-deploy-ha.ps1` runs the script's offline checks.
