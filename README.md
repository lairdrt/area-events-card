# Area Events Card

A vanilla JavaScript custom Lovelace card for Home Assistant that shows local area events as a responsive, theme-aware grid. Each event shows an image (placeholder if none), category, date, location and a verification indicator. The footer shows the build identifier.

**Status:** initial foundation (v0.1.0). The card renders events supplied to it; there is no event discovery yet.

## Card configuration

Registers as `custom:area-events-card`.

| Option   | Description |
|----------|-------------|
| `title`  | Optional header text. |
| `entity` | Entity whose `events` attribute is a list of events. |
| `events` | Inline list of events (takes precedence over `entity`). |
| `demo`   | If `true`, shows a "demonstration data" banner. |

Event fields: `title`, `start` (ISO 8601), `location`, `category`, `image` (URL, optional), `verified` (boolean).

The production card contains no sample data. A fictional demo configuration is in [examples/demo-card.yaml](examples/demo-card.yaml).

## Build

Requires Node.js 18+; no dependencies.

```
npm run build
```

Copies `src/area-events-card.js` to `dist/area-events-card.js` (generated, not stored in Git). The source keeps a `__AREA_EVENTS_CARD_BUILD__` placeholder, which the deploy script replaces in the deployed copy only. The identifier looks like `AREA-EVENTS <git short hash>-<file hash>` and is shown in the card footer and logged to the browser console.

## Local installation in Home Assistant

This uses the same mechanism as the sibling `temperature-card` project: a small `loader.js` is the only registered resource, and it imports the card with a fresh `?ts=` timestamp on every page load, so a redeployed card is never served from the browser cache.

One-time setup:

1. Map the Home Assistant `config` share (Samba) as drive `Z:`, or edit `$HaConfigShare` at the top of [deploy.ps1](deploy.ps1).
2. Create the folder `www\area-events-card` on the share. The script deliberately does not create it.
3. Run the deploy once (below), then add the dashboard resource: **Settings > Dashboards > ⋮ > Resources > Add resource** (turn on Advanced Mode in your user profile if missing).
   - URL: `/local/area-events-card/loader.js`
   - Type: **JavaScript module**
   - Register only `loader.js`, never `area-events-card.js`.
4. Edit a dashboard > **Add card > Manual**, paste [examples/demo-card.yaml](examples/demo-card.yaml).

Deploy (build, stamp, copy, verify):

```
npm run deploy
```

The script builds the card, replaces the placeholder, writes `area-events-card.js` and `loader.js` to `Z:\www\area-events-card\`, and verifies file sizes and that the build identifier is present in the deployed file. After deploying, reload the dashboard page and confirm the footer shows the identifier the script printed. The resource itself never needs to change.

Testing: confirm the demo banner, three event tiles, placeholder images, verified/unverified indicators and the build identifier in the footer. Check a light and a dark theme and a narrow (phone) width.

## Planned: Python integration (not implemented)

A separate Home Assistant custom integration (own repository/component, Python) is planned to supply the events. Intended shape:

- Config flow for choosing an area/location.
- A coordinator that fetches and verifies events, and exposes them as an entity with an `events` attribute in the format above.
- The card stays unchanged: it reads that entity via the `entity` option.

Nothing of this exists yet.

## License

MIT
