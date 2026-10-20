# Area Events Card

A vanilla JavaScript custom Lovelace card for Home Assistant that shows local area events as a responsive, theme-aware grid. Each event shows an image (placeholder if none), category, date, location and a verification indicator. The footer shows the build version.

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

Reads `src/area-events-card.js`, stamps the version (`package.json` version + git short hash), and writes `dist/area-events-card.js`. `dist/` is committed so the card can be installed without building.

## Local installation in Home Assistant

1. Run `npm run build`.
2. Copy `dist/area-events-card.js` to `<HA config>/www/area-events-card/area-events-card.js` (create the folder; `www` maps to `/local/`).
3. In HA: **Settings > Dashboards > ⋮ > Resources > Add resource**
   (enable Advanced Mode in your user profile if Resources is missing).
   - URL: `/local/area-events-card/area-events-card.js?v=0.1.0`
   - Type: **JavaScript module**
4. Hard-refresh the browser (Ctrl+Shift+R). Bump the `?v=` value after each rebuild to bypass caching.
5. Edit a dashboard > **Add card > Manual**, paste the contents of [examples/demo-card.yaml](examples/demo-card.yaml).

Testing: confirm the demo banner, three event tiles, placeholder images, verified/unverified indicators, and `Area Events Card v0.1.0+<hash>` in the footer. Check a light and a dark theme and a narrow (phone) width. The browser console should show an `AREA-EVENTS-CARD` version line.

## Planned: Python integration (not implemented)

A separate Home Assistant custom integration (own repository/component, Python) is planned to supply the events. Intended shape:

- Config flow for choosing an area/location.
- A coordinator that fetches and verifies events, and exposes them as an entity with an `events` attribute in the format above.
- The card stays unchanged: it reads that entity via the `entity` option.

Nothing of this exists yet.

## License

MIT
