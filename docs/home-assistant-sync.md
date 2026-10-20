# Syncing the event feed into Home Assistant

Copies the Cubi's validated feed (`http://192.168.1.88:8765/events.json`) to `/config/www/area-events-card/events.json` every 30 minutes (and at Home Assistant startup), so the existing card (`feed: /local/area-events-card/events.json`) stays current with no Windows machine or manual deploy.

## Mechanism

- A standard Home Assistant `shell_command` calls a small Python script, and an automation runs it on a schedule. No add-on, custom component or cloud service.
- Script: [homeassistant/sync_area_events.py](../homeassistant/sync_area_events.py), standard library only. It downloads the feed (20 s timeout, 8 MB cap), rejects anything that is not valid JSON with a non-empty `events` list whose events have `id`, `title`, `start` and `url`, writes a temporary file inside `/config/www/area-events-card/`, re-reads and re-validates it, then renames it over `events.json`. If the content is identical it does nothing. On any failure it prints the reason to stderr, exits 1 and leaves the previous feed in place.
- Dependency: a `python3` on the `PATH` of the container where Home Assistant runs shell commands. On Home Assistant OS that is the Home Assistant Core container, which ships Python 3; step 3 below confirms it before anything else.
- Examples (copy, do not edit live config from this repo): [configuration.example.yaml](../homeassistant/configuration.example.yaml) and [automations.example.yaml](../homeassistant/automations.example.yaml). The automation uses Home Assistant 2024.10+ syntax and logs a warning (`system_log.write`) when the script fails.

## Install (Home Assistant OS)

1. **Copy the script** to `/config/scripts/sync_area_events.py` (create the `scripts` folder). Any of: the Samba share (`\\<ha>\config\scripts`, which is your `Z:\scripts`), the File editor add-on, or the Terminal & SSH add-on. The `homeassistant/sync_area_events.py` file is the only thing to copy.
2. **Add the shell commands** from `homeassistant/configuration.example.yaml` to `/config/configuration.yaml` (merge into an existing `shell_command:` block). Check the URL still points at the Cubi. Then Developer tools > YAML > **Check configuration**, and **restart** Home Assistant (shell commands are loaded at startup).
3. **Confirm Python exists.** Developer tools > Actions > `shell_command.area_events_python_check` > Perform action. The response should show `returncode: 0` and `stdout: Python 3.x`. If it fails, stop here (see Blockers).
4. **Run the sync once by hand.** Developer tools > Actions > `shell_command.sync_area_events` > Perform action. Expect `returncode: 0` and `stdout: published: NNN events, generated_at ...` (or `unchanged: ...` on later runs).
5. **Check the file** at `/config/www/area-events-card/events.json` (timestamp updated, no `.events.json.*.tmp` left behind) and reload the dashboard; the card's footer shows the new "feed updated" time.
6. **Add the automation** from `homeassistant/automations.example.yaml` (Settings > Automations & scenes > Create > Edit in YAML, or append to `automations.yaml` and reload automations).
7. Remove the `area_events_python_check` command once step 3 has passed (optional).

## Test failure handling (in Home Assistant)

- Temporarily change the URL in `shell_command.sync_area_events` to an unused port (for example `:8799`), restart, run the action: expect `returncode: 1`, a message on stderr, and `events.json` unchanged. Restore the URL afterwards.
- Or stop the container on the Cubi briefly (`docker compose stop`) and run the action; the previous feed must remain. Start it again afterwards.
- Failed scheduled runs appear in Settings > System > Logs as warnings from `area_events_sync`.

## What was verified where

Local (development machine, `python -m unittest tests.test_ha_sync`): valid feed published, identical feed not rewritten, rejected cases leave the previous file byte-identical (empty, malformed, `[]`, `null`, empty `events`, events missing fields, no `generated_at`, HTTP 503, connection refused, oversized body), missing destination folder is not created, no temp file left, and the script's validator agrees with the discovery service's. The script was also run from the development machine against the live Cubi endpoint into a temporary folder and published 227 events.

**Not verified until you do steps 3 to 6:** that `python3` exists in the Home Assistant shell_command environment, that Home Assistant can reach `192.168.1.88:8765` (firewall, VLAN), write permission on `/config/www/area-events-card`, the automation's trigger syntax on your Home Assistant version, and the 30-minute schedule itself.

## Blockers and alternatives

If step 3 fails (no `python3`), do not build workarounds in the container. The supported alternatives are:

1. Let the card read the Cubi directly: `feed: http://192.168.1.88:8765/events.json`. The service already sends CORS headers. This works when the dashboard is served over `http://` (not `https://`) and needs no copy at all.
2. Install the Terminal & SSH add-on or the Python-capable "Advanced SSH & Web Terminal" add-on and run the same script from an add-on-side schedule.

## Rollback

Delete the automation and the `sync_area_events` / `area_events_python_check` entries from `configuration.yaml`, restart, and optionally delete `/config/scripts/sync_area_events.py`. The last copied `events.json` stays in place and the card keeps working; `.\refresh-events.ps1` or `.\deploy.ps1` from Windows still work as before.
