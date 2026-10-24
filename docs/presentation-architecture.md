# Modular presentation

The frontend presentation contract is `area-events.presentation.v1`. A renderer registers a unique `id`, this contract version, an explicit `eventTypes` list, a `render(event, context)` entry point, a `styles` string (empty when it needs no CSS), and `cleanup(tile)` behavior when it creates resources or listeners. Renderers receive normalized event data and small host capabilities for safe element creation, dates, icons, theme state, and the existing community image setting. Renderers do not receive Home Assistant, networking, service, producer, or persistence objects. Renderer output must be one DOM element; exceptions and invalid output use the generic renderer. The registry returns the displayed element with its actual renderer so cleanup follows the fallback when used. Renderer CSS should be scoped beneath its tile root class (for example, `.community-event`) because module styles share the card's stylesheet.

Event types and renderer IDs are separate. Card YAML may select a `presentation.default_renderer` and map event types with `presentation.event_types`:

```yaml
type: custom:area-events-card
area: San Diego
presentation:
  default_renderer: community
  event_types:
    nasa.mission: nasa-mission
```

Untyped `discovery.events.v1` events retain the community presentation by default. An explicitly typed event uses its configured mapping, otherwise the default only when it declares support for that type, then the generic fallback. Unknown renderer IDs, incompatible mappings, and malformed config fail with a specific configuration error. Renderer choice changes presentation only; event IDs and all data remain unchanged. Source names and URLs never select a renderer.

The initial modules are `community` (the extracted existing tile) and `nasa-mission` (a synthetic contract proof). The mission renderer reads optional typed `event_type: nasa.mission` and `details` fields such as `name`, `launch_window`, `launch_date`, `launch_vehicle`, `status`, and `operator`. It displays only values supplied. The current `discovery.events.v1` contract does not define these fields; the feed adapter forwards them only if a later service record supplies them, and never infers an event type from a source. To add a renderer, implement a module in `src/presentation`, add one import/registration to `src/presentation/manifest.js`, configure it, and add tests. The build automatically packages each JavaScript module in that directory.

The build emits the card and renderer modules under `custom_components/area_events/frontend`. Home Assistant serves the modules from `/area_events/presentation/`; the card uses relative ES module imports.

## Future event data contracts

`discovery.events.v1` currently represents scheduled local occurrences and requires `starts_at`. Presentation cannot make that contract sufficient for every event category. A separately reviewed upstream contract should represent location kind (`physical`, `online`, or `none`), optional confirmed start and end instants, a launch window with its own bounds/precision, and announcements that do not yet have an occurrence. It should carry type-specific structured data under a namespaced typed extension and define validation and versioning for those fields.

Until then, do not manufacture timestamps, venues, addresses, or online locations to satisfy `discovery.events.v1`. Do not change event identity or persistence schema as part of presentation selection.
