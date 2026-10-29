import assert from "node:assert/strict";
import test from "node:test";

let Registered;
globalThis.HTMLElement = class {
  constructor() { this.shadowRoot = null; }
  attachShadow() {
    this.shadowRoot = { innerHTML: "", addEventListener() {} };
    return this.shadowRoot;
  }
};
globalThis.customElements = { define(name, ctor) {
  assert.equal(name, "area-events-manager");
  Registered = ctor;
} };
await import("../custom_components/area_events/frontend/manager.js");

const snapshot = () => ({
  schema: "area_events.management.v1",
  sources: [{ id: "test-source", type: "localist", name: "Example Campus", enabled: true, interval_minutes: 30,
    revision: 4, options: { url: "https://example.org", days: 14 } }],
  areas: [{ area_key: "abcdefghi1", name: "Example Area", enabled: true, sources: ["test-source"], revision: 3 }],
  status: { sources: [{ source_key: "test-source", eligible: true }] },
  producer_types: { schema: "area_events.producer_forms.v1", types: [
    { id: "localist", name: "Localist Calendar", fields: [
      { name: "url", label: "Calendar URL", kind: "url", required: true },
      { name: "days", label: "Look ahead", kind: "integer", default: 14, min: 1, max: 365 }
    ] }
  ] },
  can_control: true
});
const setup = () => {
  const manager = new Registered();
  manager._snapshot = snapshot();
  manager._selectedSource = "test-source";
  manager._selectedArea = "abcdefghi1";
  return manager;
};

test("panel renders inventory and the real source configuration instead of raw JSON", () => {
  const manager = setup();
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /Example Campus/);
  assert.match(manager.shadowRoot.innerHTML, /Ready/);
  assert.match(manager.shadowRoot.innerHTML, /Calendar URL/);
  assert.match(manager.shadowRoot.innerHTML, /Save changes/);
  assert.doesNotMatch(manager.shadowRoot.innerHTML, /options_json/);
});

test("creating a producer includes exactly one source_id field", () => {
  const manager = setup();
  const form = manager.sourceForm(null);
  assert.equal((form.match(/name='source_id'/g) || []).length, 1);
  assert.match(form, /Create disabled producer/);
  assert.match(form, /name='producer_type'/);
});

test("editing a producer includes exactly one immutable source_id field", () => {
  const form = setup().sourceForm(snapshot().sources[0]);
  assert.equal((form.match(/name='source_id'/g) || []).length, 1);
  assert.match(form, /type='hidden' name='source_id'/);
  assert.match(form, /max='365'/);
});

test("user-supplied names are HTML escaped and never inserted as executable content", () => {
  const manager = setup();
  manager._snapshot.sources[0].name = "<img src=x onerror=alert(1)>";
  const html = manager.renderSources();
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
});

test("retired producers have disable-only controls and cannot be re-enabled", () => {
  const manager = setup();
  manager._snapshot.sources[0].type = "uninstalled_adapter";
  const html = manager.renderSources();
  assert.match(html, /Enabled · Retired adapter/);
  assert.match(html, /Disable source/);
  assert.doesNotMatch(html, /Save changes/);
});

test("retired producer status explicitly shows disabled state after save", () => {
  const manager = setup();
  manager._snapshot.sources[0].type = "uninstalled_adapter";
  manager._snapshot.sources[0].enabled = false;
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /Disabled · Retired adapter/);
  assert.match(manager.shadowRoot.innerHTML, /data-action='toggle' disabled/);
});

test("producer options are assembled from adapter contracts with correct numeric types", () => {
  const manager = setup();
  const elements = { "opt:url": { value: "https://example.org" }, "opt:days": { value: "30" } };
  const form = { elements: { namedItem(key) { return elements[key]; } } };
  assert.deepEqual(manager.optionsFrom(form, "localist"), { url: "https://example.org", days: 30 });
});

test("creation calls the versioned WebSocket API using the user's actual source ID", () => {
  const manager = setup();
  manager._creating = true;
  let called;
  manager.mutate = (type, payload) => { called = { type, payload }; };
  const previous = globalThis.FormData;
  globalThis.FormData = class {
    constructor(form) { this.form = form; }
    get(key) { return this.form.values[key] ?? null; }
  };
  try {
    const fields = { "opt:url": { value: "https://new.example" }, "opt:days": { value: "7" } };
    const form = { values: { source_id: "new-campus", name: "New Campus", producer_type: "localist", interval_minutes: "60" },
      elements: { namedItem(name) { return fields[name]; } } };
    manager.saveSource(form);
    assert.equal(called.type, "source_command");
    assert.equal(called.payload.source_id, "new-campus");
    assert.equal(called.payload.enabled, false);
    assert.deepEqual(called.payload.options, { url: "https://new.example", days: 7 });
  } finally {
    globalThis.FormData = previous;
  }
});


test("SourcesProducer URL list uses multiline native editor, not JSON", () => {
  const manager = setup();
  manager._snapshot.producer_types.types.push({
    id: "sources_producer", name: "Website event sources (generic)",
    fields: [{ name: "urls", kind: "url_list", label: "Website URLs (one per line)", required: true }]
  });
  manager._snapshot.sources.push({
    id: "websites", type: "sources_producer", name: "My websites", enabled: false,
    interval_minutes: 60, revision: 1,
    options: { urls: ["https://a.example/events", "https://b.example/calendar"] }
  });
  const html = manager.sourceForm(manager._snapshot.sources[1]);
  assert.match(html, /<textarea name='opt:urls'/);
  assert.match(html, /https:\/\/a.example\/events\nhttps:\/\/b.example\/calendar/);
  const input = { elements: { namedItem() { return { value: " https://a.example/events\n\nhttps://b.example/calendar \n" }; } } };
  assert.deepEqual(manager.optionsFrom(input, "sources_producer"),
    { urls: ["https://a.example/events", "https://b.example/calendar"] });
});

test("the Trashcan section lists deleted events with their reason, date, source, totals and suppressed rediscoveries", () => {
  const manager = setup();
  manager._snapshot.trashcan = { schema: "discovery.trashcan.v1", total: 2, suppressed: 5,
    by_reason: { duplicate: 1, not_relevant: 0, incorrect: 0, expired_or_canceled: 0, advertisement: 1, other: 0 },
    events: [{ event_key: "ev-0123456789abcdef0123", title: "Harvest <Craft> Fair", starts_at: 1794600000, sources: [{ key: "fairs", name: "Local Fairs" }],
      reason: "advertisement", reason_label: "Advertisement / not a real event", deleted_at: 1793300000, suppressed_count: 5 }] };
  manager._tab = "trashcan";
  manager.render();
  const html = manager.shadowRoot.innerHTML;
  assert.match(html, /data-value='trashcan' class='active'>Trashcan/);
  assert.match(html, /Harvest &lt;Craft&gt; Fair/, "titles are escaped");
  assert.match(html, /Local Fairs/);
  assert.match(html, /Advertisement \/ not a real event/);
  assert.match(html, /Deleted events<\/div><div class='value'>2/);
  assert.match(html, /Rediscoveries suppressed<\/div><div class='value'>5/);
  assert.match(html, /Duplicate event: <strong>1/);
  assert.doesNotMatch(html, /restore|Restore/, "no restore in V1");
});

test("the Trashcan section says so when the service has no trashcan, and when it is empty", () => {
  const manager = setup();
  manager._tab = "trashcan";
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /does not report a trashcan/);
  manager._snapshot.trashcan = { schema: "discovery.trashcan.v1", total: 0, suppressed: 0, by_reason: {}, events: [] };
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /The trashcan is empty/);
});
