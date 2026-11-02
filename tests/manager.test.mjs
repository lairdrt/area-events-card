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
  manager._tab = "sources";
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
  manager._tab = "sources";
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

const clickTab = (manager, value) => manager.handleClick({ target: { closest: () => ({ disabled: false, dataset: { action: "tab", value } }) } });
const withCardLoad = (marker, run) => {
  const saved = globalThis.__AREA_EVENTS_CARD_LOAD__;
  if (marker === undefined) delete globalThis.__AREA_EVENTS_CARD_LOAD__; else globalThis.__AREA_EVENTS_CARD_LOAD__ = marker;
  try { return run(); } finally {
    if (saved === undefined) delete globalThis.__AREA_EVENTS_CARD_LOAD__; else globalThis.__AREA_EVENTS_CARD_LOAD__ = saved;
  }
};

test("a new dashboard session opens on the Overview", () => {
  const manager = new Registered();
  assert.equal(manager._tab, "overview");
  manager._snapshot = snapshot();
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /data-value='overview' class='active'>Overview/);
  assert.match(manager.shadowRoot.innerHTML, /Service overview/);
});

test("Sources, Areas, Trashcan and Overview navigation still works", () => {
  const manager = setup();
  for (const [tab, marker] of [["sources", /Calendar URL/], ["areas", /Example Area/], ["trashcan", /does not report a trashcan/], ["overview", /Service overview/]]) {
    clickTab(manager, tab);
    assert.equal(manager._tab, tab);
    assert.match(manager.shadowRoot.innerHTML, new RegExp(`data-value='${tab}' class='active'`));
    assert.match(manager.shadowRoot.innerHTML, marker, tab);
  }
});

test("refreshing the data keeps the selected tab", async () => {
  const manager = setup();
  manager._hass = { callWS: async () => snapshot() };
  clickTab(manager, "areas");
  await manager.refresh();
  assert.equal(manager._tab, "areas");
  assert.match(manager.shadowRoot.innerHTML, /data-value='areas' class='active'/);
});

test("the Overview shows the card build the page actually loaded and the backend build the service reports", () => {
  const manager = setup();
  manager._snapshot.status.build = "AREA-EVENTS-BACKEND 0.2.0-0123abcd";
  withCardLoad({ build: "AREA-EVENTS 0.2.0-89abcdef", defined: true }, () => {
    const html = manager.renderOverview();
    assert.match(html, /Card build: <strong class='build-card'>AREA-EVENTS 0\.2\.0-89abcdef<\/strong>/);
    assert.match(html, /Backend build: <strong class='build-backend'>AREA-EVENTS-BACKEND 0\.2\.0-0123abcd<\/strong>/);
  });
});

test("a card that has not loaded, or a backend that reports no build, is shown as Unavailable, never guessed", () => {
  const manager = setup();
  for (const marker of [undefined, {}, { build: "" }, { build: "__AREA_EVENTS_CARD_BUILD__" }]) {
    withCardLoad(marker, () => assert.match(manager.renderOverview(), /Card build: <strong class='build-card'>Unavailable</));
  }
  assert.match(manager.renderOverview(), /Backend build: <strong class='build-backend'>Unavailable</);
});

test("build identifiers are HTML escaped", () => {
  const manager = setup();
  manager._snapshot.status.build = "<img src=x onerror=alert(1)>";
  withCardLoad({ build: "<script>alert(2)</script>" }, () => {
    const html = manager.renderOverview();
    assert.doesNotMatch(html, /<img|<script/);
    assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(html, /&lt;script&gt;alert\(2\)&lt;\/script&gt;/);
  });
});

// --- SerpApi usage in the existing producer panel --------------------------------------------------------------------

const serpapiSnapshot = (usage = {}) => {
  const s = snapshot();
  s.sources.push({ id: "serpapi_sd", type: "serpapi_google_events", name: "SerpApi San Diego", enabled: true, interval_minutes: 720, revision: 2,
    options: { query: "events in San Diego", location: "San Diego, California, United States", timezone: "America/Los_Angeles" } });
  s.producer_types.types.push({ id: "serpapi_google_events", name: "SerpApi Google Events", fields: [
    { name: "query", label: "Event search query", kind: "text", required: true },
    { name: "location", label: "Search location", kind: "text", required: true },
    { name: "timezone", label: "Event time zone", kind: "text", required: true },
    { name: "max_pages", label: "Result pages per scan", kind: "integer", default: 1, min: 1, max: 3 },
    { name: "lookahead_days", label: "Event look-ahead (days)", kind: "integer", default: 90, min: 30, max: 365 }] });
  s.status.settings = { revision: 9 };
  s.status.sources.push({ source_key: "serpapi_sd", eligible: true });
  s.usage = { schema: "discovery.usage.v1", serpapi: {
    configured: true, stale: false, error: null, state: "active", reason: null, resumes: null,
    account: { plan_name: "Free Plan", monthly_price: 0, searches_per_month: 250, used: 124, remaining: 126, renewal: "2026-11-20", checked_at: 1793462400 },
    policy: { auto_pause: true, pause_at: 200, free_tier_reserve: 5 },
    sources: { serpapi_sd: { state: "active", reason: null } }, ...usage } };
  return s;
};
const serpapiManager = (usage) => {
  const manager = new Registered();
  manager._snapshot = serpapiSnapshot(usage);
  manager._selectedSource = "serpapi_sd";
  manager._tab = "sources";
  return manager;
};

test("a SerpApi producer's panel shows the account's real usage, the shared policy and the effective status, and never a key", () => {
  const manager = serpapiManager();
  manager.render();
  const html = manager.shadowRoot.innerHTML;
  for (const shown of [/SerpApi account usage/i, /Plan:<\/span> <strong>Free Plan \(\$0\)/, /Monthly allowance:<\/span> <strong>250/, /Searches used:<\/span> <strong>124/,
    /Searches remaining:<\/span> <strong>126/, /Next renewal:<\/span> <strong>2026-11-20/, /Effective status:<\/span> <strong>Active/,
    /name='usage:auto_pause' checked/, /name='usage:pause_at' type='number' min='1' max='10000' required value='200'/, /free-tier safety stop is always on/,
    /Shared by every SerpApi producer/, /Save changes/, /data-action='toggle'/,
    /Event look-ahead \(days\)<input name='opt:lookahead_days' type='number' value='90' min='30' max='365'>/]) assert.match(html, shown);
  assert.doesNotMatch(html, /api_key|serpapi-test-key/i);
  assert.doesNotMatch(manager.sourceForm(manager._snapshot.sources[0]), /SerpApi account usage/, "other producers have no usage section");
});

test("why searches stopped, when they can resume, and stale or unavailable readings are said plainly", () => {
  let manager = serpapiManager({ state: "paused_threshold", reason: "200 searches used this cycle; automatic pause stops scheduled searches at 200", resumes: "2026-11-20",
    sources: { serpapi_sd: { state: "paused_threshold", reason: "200 searches used this cycle; automatic pause stops scheduled searches at 200" } } });
  manager.render();
  let html = manager.shadowRoot.innerHTML;
  assert.match(html, /Effective status:<\/span> <strong>Paused by usage threshold/);
  assert.match(html, /automatic pause stops scheduled searches at 200\. Searches can resume when SerpApi reports a new allowance \(renewal 2026-11-20\)/);
  assert.match(html, /Paused · usage threshold/, "the list row says so too");
  manager = serpapiManager({ stale: true, error: "the SerpApi account could not be read (FetchError)", state: "usage_unavailable",
    sources: { serpapi_sd: { state: "usage_unavailable", reason: "the SerpApi account could not be read (FetchError)" } } });
  manager.render();
  html = manager.shadowRoot.innerHTML;
  assert.match(html, /usage-stale'>Account information is unavailable: the SerpApi account could not be read/);
  assert.match(html, /Effective status:<\/span> <strong>Usage unavailable/);
  manager = serpapiManager({ account: null, stale: true, error: "no SerpApi key is configured (SERPAPI_API_KEY)", state: "usage_unavailable", sources: {} });
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /Monthly allowance:<\/span> <strong>—/);
  const disabled = serpapiManager({ sources: { serpapi_sd: { state: "manually_disabled", reason: "disabled by an operator" } } });
  disabled._snapshot.sources[1].enabled = false;
  disabled.render();
  assert.match(disabled.shadowRoot.innerHTML, /Effective status:<\/span> <strong>Manually disabled/);
  assert.match(disabled.shadowRoot.innerHTML, /data-action='toggle'[^>]*>Enable</, "the existing enable control");
  const old = serpapiManager();
  delete old._snapshot.usage;
  old.render();
  assert.match(old.shadowRoot.innerHTML, /does not report SerpApi usage/);
});

test("saving a SerpApi producer saves a changed policy through the existing settings command, then the producer", async () => {
  const manager = serpapiManager();
  const calls = [];
  manager.call = async (type, params) => { calls.push([type, params]); return type === "management" ? serpapiSnapshot() : {}; };
  const previous = globalThis.FormData;
  globalThis.FormData = class { constructor(form) { this.form = form; } get(key) { return this.form.values[key] ?? null; } };
  try {
    const fields = { "opt:query": { value: "events in San Diego" }, "opt:location": { value: "San Diego, California, United States" },
      "opt:timezone": { value: "America/Los_Angeles" }, "opt:max_pages": { value: "1" }, "opt:lookahead_days": { value: "120" },
      "usage:auto_pause": { checked: false }, "usage:pause_at": { value: "150" } };
    const form = { values: { source_id: "serpapi_sd", name: "SerpApi San Diego", producer_type: "serpapi_google_events", interval_minutes: "720" },
      elements: { namedItem(name) { return fields[name]; } } };
    await manager.saveSource(form);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(calls[0], ["settings", { expected_revision: 9, serpapi_auto_pause: false, serpapi_pause_at: 150 }]);
    assert.equal(calls[1][0], "source_command");
    assert.equal(calls[1][1].action, "update");
    assert.deepEqual(calls[1][1].options, { query: "events in San Diego", location: "San Diego, California, United States", timezone: "America/Los_Angeles", max_pages: 1,
      lookahead_days: 120 }, "the look-ahead is saved as a number with the producer's options");
    calls.length = 0;
    fields["usage:auto_pause"].checked = true;
    fields["usage:pause_at"].value = "200";
    await manager.saveSource(form);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(calls.map(([type]) => type).filter((type) => type !== "management"), ["source_command"], "an unchanged policy is not saved again");
    fields["usage:pause_at"].value = "0";
    calls.length = 0;
    await manager.saveSource(form);
    assert.equal(calls.length, 0);
    assert.match(manager._error, /Pause at must be a whole number/);
  } finally {
    globalThis.FormData = previous;
  }
});

// --- Active issues on the Overview, and the Sources status --------------------------------------------------------------

const issue = (id, extra = {}) => ({ id, severity: "error", kind: "source_failing", area_keys: ["abcdefghi1"], source_key: "test-source", component: "Example Campus",
  summary: "Example Campus — last scan failed", detail: "HTTP 503 from https://example.org", last_success_at: 1793000000,
  action: "Open Sources and check this producer's settings and the site it reads.", ...extra });
const withIssues = (issues, upcoming = []) => {
  const manager = setup();
  manager._snapshot.areas.push({ area_key: "otherarea01", name: "Other Area", enabled: true, sources: [], revision: 1 });
  manager._snapshot.diagnostics = { schema: "area_events.diagnostics.v1", issues, upcoming };
  return manager;
};
const rows = (html) => (html.match(/class='issue-row /g) || []).length;
const withLocation = (search, run) => {
  const saved = globalThis.window;
  globalThis.window = { location: { search } };
  try { return run(); } finally { if (saved === undefined) delete globalThis.window; else globalThis.window = saved; }
};

test("the Overview keeps its service summary and lists each active issue with its evidence, last success and next step", () => {
  const stale = issue("area:abcdefghi1:stale", { severity: "warning", kind: "area_stale", source_key: null, component: "Example Area",
    summary: "Example Area — event information is stale", detail: "No source of this area has completed a scan in the last 12 hours.", action: "Check that discovery is running." });
  const manager = withIssues([issue("source:test-source:failing"), stale]);
  const html = manager.renderOverview();
  assert.match(html, /Service overview/);
  assert.match(html, /Backend build:/);
  assert.ok(html.indexOf("Service overview") < html.indexOf("Active issues"), "below the existing service summary");
  assert.match(html, /<h2>Active issues — All areas<\/h2>/);
  assert.equal(rows(html), 2);
  assert.match(html, /Error · Example Campus/);
  assert.match(html, /Example Campus — last scan failed/);
  assert.match(html, /Evidence:<\/span> HTTP 503 from https:\/\/example.org/);
  assert.match(html, /Last successful scan:<\/span> /);
  assert.match(html, /Next step:<\/strong> Open Sources and check this producer/);
  assert.match(html, /Warning · Example Area/);
  assert.match(html, /mdi:alert-circle/);
  manager._snapshot.diagnostics.issues = [];
  assert.match(manager.renderOverview(), /No active issues\./);
  assert.doesNotMatch(manager.render() || manager.shadowRoot.innerHTML, /Issues<\/button>/, "no separate Issues tab");
});

test("K, J: the badge's address opens the Overview on that area, which lists exactly the card's issues", () => {
  const global = issue("discovery:paused", { severity: "warning", kind: "discovery_paused", area_keys: [], component: "Discovery", summary: "Discovery is paused" });
  const elsewhere = issue("source:other:failing", { area_keys: ["otherarea01"], summary: "Other source — last scan failed" });
  const card = [issue("source:test-source:failing"), global];   // what the card's own view (for_area) holds for abcdefghi1
  const manager = withLocation("?tab=overview&area=abcdefghi1", () => {
    const m = new Registered();
    m._snapshot = withIssues([...card, elsewhere])._snapshot;
    return m;
  });
  assert.equal(manager._tab, "overview");
  manager.render();
  const html = manager.shadowRoot.innerHTML;
  assert.match(html, /<h2>Active issues — Example Area<\/h2>/);
  assert.equal(rows(html), card.length, "the same count as the card's badge");
  assert.doesNotMatch(html, /Other source/);
  assert.match(html, /<option value='abcdefghi1' selected>Example Area/);
  manager.handleChange({ target: { name: "issue-area", value: "" } });
  assert.equal(rows(manager.shadowRoot.innerHTML), 3, "every area");
  manager.handleChange({ target: { name: "issue-area", value: "otherarea01" } });
  assert.equal(rows(manager.shadowRoot.innerHTML), 2, "Other Area: its own source and the shared pause");
});

test("an address that names no known area, or something that is not an identifier, falls back to all areas", () => {
  for (const search of ["?tab=overview&area=%3Cscript%3E", "?tab=overview&area=unknownarea", "?tab=nonsense"]) {
    const manager = withLocation(search, () => new Registered());
    manager._snapshot = withIssues([issue("source:test-source:failing")])._snapshot;
    assert.equal(manager._tab, "overview");
    manager.render();
    assert.match(manager.shadowRoot.innerHTML, /Active issues — All areas/, search);
    assert.doesNotMatch(manager.shadowRoot.innerHTML, /<script/);
  }
});

test("an unreachable Discovery Service is explained on the Overview, never shown as no issues", async () => {
  const manager = new Registered();
  manager._hass = { callWS: async () => { throw new Error("the Discovery Service is not reachable (connection refused)"); } };
  await manager.refresh();
  const html = manager.shadowRoot.innerHTML;
  assert.match(html, /Area Events could not read the Discovery Service/);
  assert.match(html, /Evidence:<\/span> the Discovery Service is not reachable \(connection refused\)/);
  assert.match(html, /Next step:<\/strong> Check that the Discovery Service host is running/);
  assert.doesNotMatch(html, /No active issues/);
});

test("a stale area that became due since the snapshot was read is listed, as the card counts it", () => {
  const stale = issue("area:abcdefghi1:stale", { severity: "warning", kind: "area_stale", summary: "Example Area — event information is stale" });
  const manager = withIssues([], [{ ...stale, active_from: Date.now() / 1000 - 60 }, { ...stale, id: "later", summary: "Later", active_from: Date.now() / 1000 + 3600 }]);
  const html = manager.renderOverview();
  assert.equal(rows(html), 1);
  assert.match(html, /event information is stale/);
});

test("issue text is HTML escaped", () => {
  const manager = withIssues([issue("x", { summary: "<img src=x onerror=alert(1)>", detail: "<script>alert(2)</script>", component: "<b>c</b>" })]);
  const html = manager.renderOverview();
  assert.doesNotMatch(html, /<img|<script|<b>c/);
  assert.match(html, /&lt;img src=x/);
});

test("Sources shows a recorded scan error as Failing, a usage hold distinctly, and the last error and success of the selected producer", () => {
  const manager = serpapiManager({ sources: { serpapi_sd: { state: "blocked_free_tier", reason: "5 free searches remain" } } });
  manager._snapshot.status.sources = [
    { source_key: "test-source", eligible: true, last_error: "HTTP 503 from https://example.org", last_success_at: 1793000000 },
    { source_key: "serpapi_sd", eligible: true, last_error: "SerpApi search not made: 5 free searches remain", last_success_at: 1793000000 }];
  manager.render();
  let html = manager.shadowRoot.innerHTML;
  assert.match(html, /<span class='state bad'>Failing<\/span>/, "eligible, but its last scan failed: not Ready");
  assert.match(html, /<span class='state warn'>Blocked · free-tier limit<\/span>/, "a usage hold is not shown as a plain failure");
  manager._selectedSource = "test-source";
  manager.render();
  html = manager.shadowRoot.innerHTML;
  assert.match(html, /Last scan error:<\/span> <strong class='state bad'>HTTP 503 from https:\/\/example.org/);
  assert.match(html, /Last successful scan:<\/span> /);
  manager._snapshot.status.sources[0].last_error = null;
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /<span class='state good'>Ready<\/span>/, "no known failure: Ready");
  assert.doesNotMatch(manager.shadowRoot.innerHTML, /Last scan error/);
  manager._snapshot.sources[0].enabled = false;
  manager._snapshot.status.sources[0].last_error = "an old failure";
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /<span class='state '>Disabled<\/span>/, "deliberately disabled wins over an old error");
  manager._snapshot.sources[0].enabled = true;
  manager._snapshot.status.sources[0] = { source_key: "test-source", eligible: false };
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /<span class='state '>Idle<\/span>/);
});

test("Sources and Areas share one 50/50 split that still stacks on narrow screens", () => {
  const manager = serpapiManager();
  manager.render();
  const css = manager.shadowRoot.innerHTML;
  assert.match(css, /\.split\{display:grid;grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\);gap:14px\}/);
  assert.equal((css.match(/\.split\{display:grid/g) || []).length, 1, "one shared layout rule for both pages");
  assert.match(css, /@media\(max-width:900px\)\{[^\n]*?\.split\{grid-template-columns:1fr\}/, "stacked below 900px, as before");
  assert.match(css, /<div class='split'><div class='list'>/);
  manager._tab = "areas";
  manager.render();
  assert.match(manager.shadowRoot.innerHTML, /<div class='split'><div class='list'>/);
});
