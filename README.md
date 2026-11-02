# Area Events for Home Assistant

Discover local events and show them on your Home Assistant (HA) dashboard. Area Events includes a **Discovery Service** that searches event sources and stores events, an **HA integration and administrator control panel**, and a **dashboard card**. The browser never holds API keys.

```text
Event websites / Google Events
          ↓
Discovery Service (Python, scheduled producers, one SQLite database)
          ↓ HTTPS + read/control tokens
Home Assistant integration → Area Events dashboard card
                           → Area Events management panel (HA admins)
```

**Start here:** Follow steps 1–10 once. You can get working events from a public calendar **without buying or obtaining any search API key**. Add Google Events and Tavily later if you want wider coverage. This is a community/custom integration, **not a one-click HA add-on**.

## 1. Choose where to run the Discovery Service

The service must stay running even when the dashboard is closed. It needs Python **3.11 or newer**, internet access, and a persistent local disk for SQLite.

| Your Home Assistant installation | Recommended Discovery Service location |
|---|---|
| **Home Assistant OS** (typical HA Green, Raspberry Pi, or HA OS mini-PC) | **Another Linux computer** or VM (an always-on mini-PC, Debian/Ubuntu PC, or Raspberry Pi 4/5 for modest workloads). Do not attempt to install pip packages into the managed HA OS host. |
| **HA Container on Linux** | A separate Linux host/VM, or the same physical Linux computer **outside** the HA container; container networking must allow HA to reach the service. |
| **HA Core on a self-managed Linux machine** | The same machine in a separate Python virtual environment, or another Linux host. Loopback HTTP is acceptable only if HA and the service really share the same network namespace. |

A small, always-on **Debian/Ubuntu machine** is the easiest documented path. Keep its LAN IP stable (ideally reserve it in your router). The steps below assume a *separate Linux host* and a private home LAN. The Discovery Service does **not** need Home Assistant installed on that host. **Do not store or share its SQLite database on SMB/NFS.**

## 2. Install Python and the Discovery Service (on the Linux host)

Connect to that machine's terminal/SSH as your ordinary user (not root), then run:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv openssl curl
git clone https://github.com/lairdrt/area-events-card.git
cd area-events-card
python3 -m venv .venv
.venv/bin/python -m pip install ".[images]"
mkdir -p "$HOME/area-events-data" "$HOME/.config/area-events"
```

The `[images]` extra enables richer event-image extraction. If you only need basic event discovery, `.venv/bin/python -m pip install .` is sufficient. No Node.js, HACS, or database server is required for an end-user installation.

**Check:** `.venv/bin/area-events --help` should display available commands. Keep the cloned directory and `area-events-data` on persistent storage. Updates should back up the database before changing installed service code.

## 3. Configure secure access from Home Assistant

These commands assume the Discovery Service runs on **a different machine**. Replace `192.168.1.88` with **your Linux host's actual LAN IP**; use the same IP everywhere, and keep it stable.

```bash
export SERVER_IP=192.168.1.88
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$HOME/.config/area-events/key.pem" \
  -out "$HOME/.config/area-events/cert.pem" \
  -subj "/CN=area-events" -addext "subjectAltName=IP:$SERVER_IP"
chmod 600 "$HOME/.config/area-events/key.pem"
```

The **private key** stays on the Linux host. Copy **only** `cert.pem` into Home Assistant's configuration directory as `/config/area_events_cert.pem` (using a Samba share, SCP, or an HA file-editor upload). Home Assistant will use that public certificate to trust the service's self-signed HTTPS connection. If you use an already-trusted HTTPS reverse proxy, use its HTTPS address instead and omit the certificate-file field during HA setup.

**Same-computer exception:** For a self-managed HA Core installation sharing the service's network namespace, you may instead bind the service to `127.0.0.1`, omit `--allow-remote` and TLS flags, and configure HA with `http://127.0.0.1:8780`. **`localhost` inside an HA container is not the Linux host.** Never use plain HTTP with tokens across a normal LAN.

## 4. Optional API keys: where to get them and how to install them

Skip this step for a no-key **Website**, **Localist**, or **City of San Diego** producer. Keys are configured **on the Discovery Service host**, not in the HA dashboard or a producer form.

| Service | What it adds | Getting the key | Free allowance and built-in safeguards |
|---|---|---|---|
| **[SerpAPI](https://serpapi.com/)** | Searches **Google Events** for events; the shared **Google Maps venue resolver** may fill missing addresses automatically. | Sign up, then open [Manage API Key](https://serpapi.com/manage-api-key) and copy the API key. | As of Nov 2026: **250 searches/month** on the free plan, shared by Google Events and Maps. Our service refuses paid-plan searches, reserves the last **5** free searches and defaults to an earlier **200-search** automatic pause. Maps also has a separate **20/month and 3/new lookups per scan** cap. Check your actual plan and usage in the control panel. |
| **[Tavily](https://www.tavily.com/)** | Searches for an event's **actual webpage** when its producer did not supply a valid event link; can also recover an address stated on that page. It does not discover Google Events by itself. | Create a free account at [Tavily](https://app.tavily.com/) and copy your API key from its dashboard. | As of Nov 2026: **1,000 credits/month free**, no card required; the service uses **basic search (1 credit)** and caps itself at **40/day, 900/month**. |

Create the following files (for example, with `nano`); each contains **one line** with your own secret value:

```text
~/.config/area-events/serpapi.env     → SERPAPI_API_KEY=your_key_here
~/.config/area-events/tavily.env      → TAVILY_API_KEY=your_key_here
```

Then secure them:

```bash
chmod 600 "$HOME/.config/area-events/"*.env
```

Those environment files will be loaded by the service definition in Step 5. If you add keys later, restart **only the Discovery Service**. Do not commit keys, paste them into event-source settings, or expose them in screenshots/logs. Provider limits/prices can change; see [SerpAPI pricing](https://serpapi.com/pricing) and [Tavily pricing](https://www.tavily.com/pricing).

**Budget tip:** One Google Events page costs one SerpAPI search per scan. At a **12-hour interval**, one page for each of two Google Events producers is about **120 searches over 30 days**, before Maps lookups. Start with one page, one or two producers, and a 12-hour interval; the panel shows remaining usage. An hourly scan will exhaust a free account quickly.

## 5. Run the Discovery Service automatically (on Linux)

Create `~/.config/systemd/user/area-events-discovery.service` using a text editor. Replace **192.168.1.88** below with the same IP used in Step 3.

```ini
[Unit]
Description=Area Events Discovery Service

[Service]
Type=simple
WorkingDirectory=%h/area-events-card
EnvironmentFile=-%h/.config/area-events/serpapi.env
EnvironmentFile=-%h/.config/area-events/tavily.env
ExecStart=%h/area-events-card/.venv/bin/area-events serve --db %h/area-events-data/discovery.db --bind 192.168.1.88 --allow-remote --tls-cert %h/.config/area-events/cert.pem --tls-key %h/.config/area-events/key.pem
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Run:

```bash
mkdir -p "$HOME/.config/systemd/user"
systemctl --user daemon-reload
systemctl --user enable --now area-events-discovery
systemctl --user status area-events-discovery --no-pager
```

To keep it running after logout and start it at boot, run `sudo loginctl enable-linger "$USER"` once (on a systemd Linux host). **Check:** status says `active (running)`; `journalctl --user -u area-events-discovery -n 30 --no-pager` should show the service listening without errors. The first start creates the database and private token files alongside it: `api-read-token`, `api-token` (administrator control), and `api-process-token` (advanced remote extraction only).

From the Linux host, an unauthenticated connection check is:

```bash
curl --cacert "$HOME/.config/area-events/cert.pem" \
  -o /dev/null -sS -w 'HTTP %{http_code}\n' \
  "https://$SERVER_IP:8780/v1/discovery/status"
```

**HTTP 401 is expected**: the HTTPS server is reachable but correctly requires a token. A connection or certificate error is **not** success. Do not forward port 8780 to the public internet.

For service troubleshooting, use `journalctl --user -u area-events-discovery -e`; after editing either `.env` file, run `systemctl --user restart area-events-discovery`.

## 6. Install the Home Assistant integration and card

1. Download this repository ([Code → Download ZIP](https://github.com/lairdrt/area-events-card)) to your computer and extract it, or use an existing checkout.
2. Copy the **entire** `custom_components/area_events` folder into HA's `/config/custom_components/area_events`. Keep its bundled `frontend` directory and files. Create `custom_components` if missing; do not copy `src` or `tests` to HA.
3. In Home Assistant, go to **Settings → System → Restart** and restart HA.
4. Go to **Settings → Devices & services → Add integration**, search **Area Events**, and select it. No separate Lovelace JavaScript resource registration or HACS installation is needed.
5. Enter:
   - **Service address:** `https://192.168.1.88:8780` (use your actual service IP).
   - **Read token:** securely copy the text in `~/area-events-data/api-read-token` from the Linux host.
   - **Control token:** securely copy `~/area-events-data/api-token` to enable the administrator control panel; omit it for a read-only installation.
   - **Certificate file:** `/config/area_events_cert.pem` if following Step 3; otherwise leave empty for already-trusted TLS.
6. Submit. The setup form validates the service connection and tokens. If it says **cannot connect**, check address, port, service status, firewall, and certificate IP; **invalid auth** means a wrong token. Keep tokens private.

**Check:** The HA sidebar should now include **Area Events**, its management panel. Open it as an **HA administrator**. You should see **Connected** and **Control: Ready** rather than **Read-only**. If control is read-only, verify you entered the **control** token and are an administrator. Home Assistant stores tokens server-side; cards access the integration using the user's normal HA login.

## 7. Create a discovery area

In HA's **Area Events** sidebar panel:

1. Open **Areas → + Add area**.
2. Name it, for example **Cookeville** (or **Austin** if you want to test a keyless university calendar).
3. Enter the area's approximate **center latitude/longitude**, **radius**, and **Miles/Kilometers**; leave **Area enabled** checked.
4. Choose any producers already created, if present, and select **Create area**. Otherwise attach the producer in Step 9.

**Important:** Currently, an area's sources determine which events it displays. **The radius is descriptive configuration, not a precise distance filter.** Choose sources appropriate to the area; don't assume the card discards events beyond the radius.

## 8. Create an event source (called a *producer*)

Open **Sources → + Add producer**. Enter a unique **Source ID** (lowercase letters, numbers, `-`, `_`; e.g. `cookeville_google`), a readable name, and a **scan interval in minutes**. Pick the producer type and complete the fields that appear. The form creates the producer **disabled**, intentionally.

| Producer type in Sources | Configuration for a first setup |
|---|---|
| **Website event sources (generic)** — no key | Supply **public event website URLs, one per line** (1–12); set site timezone, page budget (e.g. 12), link depth (1) and event cap (e.g. 75). Handles ordinary HTML, embedded structured Event data and ICS. Pages needing a login, JavaScript-only rendering or blocking crawlers may not work. |
| **Localist events calendar** — no key | Enter a Localist calendar address such as `https://calendar.utexas.edu`, timezone `America/Chicago`, look-ahead 14 days and max 5 pages. This is a good first test with an **Austin** area. |
| **City of San Diego events** — no key | Keep the offered city events URL; start with 2 listing pages and 10 event-detail pages. Intended for San Diego, not arbitrary cities. |
| **SerpApi Google Events** — SerpAPI key required | Example: query `events in Cookeville Tennessee`; location `Cookeville, Tennessee, United States`; timezone `America/Chicago`; **max pages 1**, look-ahead 90 days. Start with an interval of **720 minutes** (12 hours) to control credits. |

**Google Maps** is **not another producer to create**: it's a shared address resolver used automatically for Google Events candidates missing street addresses. **Tavily** likewise runs as a shared event-link helper when needed, not a separately configured producer. Both use the credentials and safeguards from Step 4.

Select **Create disabled producer**. No event search starts simply because the form was saved.

## 9. Connect the source to the area, then enable it

1. Open **Areas**, select the area you created, check the new source under **Assigned producers**, and select **Save area**.
2. Open **Sources**, select your producer, verify its settings, then select **Enable** and confirm.
3. The list should report **Ready** when the producer is enabled, its adapter exists, and it is assigned to an enabled area. **Idle** usually means it lacks an eligible area. Disabled and retired sources cannot discover new events.
4. Let the normal scheduled scan run. The control panel's **Refresh** button reloads status; **it does not start a scan**. A fresh source may take until its next eligible scan and processing to show events.

## 10. Add the card to an HA dashboard and check your results

1. Open your HA dashboard, choose **Edit dashboard → Add card**, and select **Area Events** if offered, or use a **Manual** card with YAML below.
2. Enter the **exact area name** you created, save, and exit edit mode.

```yaml
type: custom:area-events-card
title: Cookeville Events
area: Cookeville
days: 30
max_events: 30
show_images: true
show_filters: true
```

3. After the producer completes a scheduled scan, confirm the **Sources** panel shows it Ready, the event tiles appear, and the **Source:** link opens the event page when one was found. Unverified labels and missing links/addresses mean the source did not supply enough evidence; they are not necessarily connection failures.
4. If a card says **unknown area**, correct its `area:` spelling or enable the area. If no events appear, check that the area is enabled, its source is **enabled and assigned**, its date range includes future events, and the service is running. A valid source can legitimately return zero events.

You can create additional areas/cards that reuse a source or use different sources. Each card's display filters are independent. The integration's **Configure/Options → Change scan interval** controls how often HA *checks for changed data* (5–300 seconds, default 10); **it is not the producer's discovery interval**.

## Control panel: every available function

Open **Area Events** in HA's sidebar as an administrator. The panel communicates through HA with the Discovery Service. It does not directly access SQLite. Changes require a configured control token.

| Control / screen | How to use it |
|---|---|
| **Refresh** (top right) | Reload management status and counts; **does not run a producer scan**. |
| **HA Menu** | Open/close the Home Assistant navigation sidebar. |
| **Top counters** | **Producers** = configured instances; **Discovery areas** = collections; **Queued jobs** = ready for discovery; **Control** = Ready or Read-only. These are indicators, not buttons that run jobs. |
| **Overview** | View service connection, running/paused state, database schema, control access, and loaded frontend/backend build identifiers. It is read-only, not a pause/resume control. |
| **Sources → select producer** | See adapter, enabled/idle/ready state and schedule. Edit its display name, interval and producer-specific fields, then **Save changes**. A name edit does not change its permanent Source ID. |
| **Sources → + Add producer** | Select type, enter unique Source ID, name, interval and the type's form fields; select **Create disabled producer**. Then attach and enable it (Steps 8–9). **Cancel** discards the unsaved form. |
| **Sources → Enable / Disable** | Confirm the change. A disabled source stops future discovery but **does not erase stored events**. Retired adapters cannot be enabled or edited; they can be disabled. |
| **Sources → SerpAPI account usage** | For a Google Events producer, view the shared account plan, searches used/remaining, renewal and effective status. **Automatic pause** and **Pause at** set the common search threshold; save with **Save changes**. The free-tier reserve always remains active. A spent threshold or missing key can block searching without changing the source's manual Enabled switch. |
| **Areas → + Add area / select area** | Create or edit name, latitude/longitude, radius and units; check/uncheck **Area enabled**; check producer names under **Assigned producers**; **Save area**. This changes which sources feed the area, not the card's display filters. |
| **Trashcan** | View events deleted by an HA administrator, deletion reasons and dates, and how often rediscovery was suppressed. **No restore or bulk-delete function** exists in this panel. |
| **Delete event** (on an event tile, admins only) | Hover the tile's lower-right corner (or use its trash icon on touch devices); select a deletion reason and confirm. This **permanently suppresses rediscovery** of matching occurrences. Use carefully; this is not a hide-for-now control. |

Other HA configuration: **Settings → Devices & services → Area Events → Configure** provides integration Options, including the HA data-refresh interval and an advanced producer/area management flow. The sidebar panel is the easier interface for normal setup. The panel currently has **no Run Now button**, global Pause/Resume switch, key-entry field, restore action or SQL editor.

### Common checks and useful links

- **Service won't start:** verify Python 3.11+, virtual environment, absolute data path, TLS certificate/private-key paths, and `journalctl --user -u area-events-discovery -e`. If credentials are absent, keyless sources still work.
- **HA can't connect:** confirm the host IP/port and LAN/firewall, correct HTTPS certificate, and tokens; an HTTP 401 from the no-token check in Step 5 proves basic reachability.
- **Source says Idle:** enable it **and** attach it to an enabled area. **No events:** allow a scan interval, check the source's actual public event data and card filters. **Google Events stops:** check the shared SerpAPI quota in **Sources**.
- **Backups/upgrades:** preserve `area-events-data/discovery.db` and service credentials; see [deployment and recovery](docs/automated-deployment.md). That deployment script is tailored to the maintainers' Windows/Cubi/HA setup; a new user's installation should follow Steps 1–6 rather than assume their hosts or addresses match it.
- More detail: [HA integration and TLS](docs/ha-integration-2d.md), [management console](docs/ha-management-console.md), [generic website sources](docs/ha-management-console.md#generic-website-sourcesproducer-ae-10b4), [event deletion](docs/trashcan.md), [deduplication/persistence](docs/deduplication-and-persistence.md), [producer contract](docs/producer-contract.md).

## Card display options (optional)

Use the visual editor or YAML. `area` is required; the rest are optional.

| Option | Purpose |
|---|---|
| `title`, `id` | Header and optional stable card ID (helps keep viewer preferences separate across otherwise identical cards). |
| `area`, `producers` | Area name/key; optional list of source IDs **already assigned** to the area to show only those events. |
| `categories`, `interests`, `exclude` | Built-in categories; custom keyword interests (e.g. `"Board games: tabletop, board game"`); topics to hide. |
| `days`, `max_events`, `sort` | Look-ahead display window, maximum events, order (`date`, `title`, `verified`). |
| `columns`, `show_images`, `show_filters` | Tile layout and controls. |
| `events`, `demo` | Optional static demo events; in this mode **no Discovery Service is queried** (see [demo example](examples/demo-card.yaml)). |

The dashboard's date/category filter chips (including **All**, **This Week**, **This Weekend**, **This Month**) are viewer preferences, not shared source controls. They do **not** change when or what producers crawl. Event photos use available event imagery or a labelled category illustration; see [images](docs/images.md).

## Development (contributors)

```bash
npm test
python -m unittest discover -s tests -t .
npm run build
```

Building generates the bundled integration frontend in `custom_components/area_events/frontend`; those generated files are committed so ordinary HA users do not need a build environment. Architecture: [presentation](docs/presentation-architecture.md), [producer input](docs/producer-contract.md), [one-database processing](docs/deduplication-and-persistence.md).
