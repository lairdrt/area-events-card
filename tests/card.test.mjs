// Run: npm test   (no dependencies; a tiny DOM stub stands in for the browser)
import test from "node:test";
import assert from "node:assert/strict";

class El {
  constructor(tag) { Object.assign(this, { tag, children: [], className: "", textContent: "", attrs: {}, listeners: {}, hidden: false, value: "" }); }
  append(...n) { this.children.push(...n); }
  prepend(...n) { this.children.unshift(...n); }
  remove() { this.removed = true; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(e, f) { this.listeners[e] = f; }
  replaceChildren() { this.children = []; }
  attachShadow() { this.shadowRoot = new El("root"); return this.shadowRoot; }
  get isConnected() { return true; }
}
class Storage {
  constructor() { this.map = new Map(); }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
}
let CardClass;
const created = { img: 0 };
globalThis.HTMLElement = El;
globalThis.document = {
  createElement: (t) => { if (t === "img") created.img++; return new El(t); },
  createTextNode: (text) => ({ text }),
};
globalThis.customElements = { get: () => undefined, define: (_n, c) => { CardClass = c; } };
globalThis.window = { location: { pathname: "/lovelace/events" }, localStorage: new Storage() };
await import("../src/area-events-card.js");
const { categoriesFor, normalizePrefs, prefsKey } = CardClass._internals;

const day = 24 * 3600 * 1000;
const iso = (offsetMs) => new Date(Date.now() + offsetMs).toISOString();
const event = (title, offsetDays, extra) => ({
  id: title, title, start: iso(offsetDays * day), url: "https://www.kpbs.org/events/x",
  source: { name: "KPBS" }, verified: false, verification: { status: "unverified", evidence: [] }, ...extra,
});
const baseEvents = () => [
  event("Confirmed Jazz Night", 1, { verified: true, verification: { status: "verified", evidence: ["two publishers agree"] }, categories: ["Music"] }),
  event("Corroborated Farmers Market", 2, { verification: { status: "corroborated", evidence: ["dates agree"] }, description: "Fresh produce and food vendors." }),
  event("Mystery Gathering", 3),
  event("Past Event", -3),
];
const makeFeed = (overrides = {}) => ({
  generated_at: iso(-35 * 60 * 1000),
  sources: { kpbs: { ok: true }, dosd: { ok: true } },
  events: baseEvents(),
  ...overrides,
});

const text = (el) => [el.textContent, el.text, ...(el.children || []).map(text)].filter(Boolean).join(" | ");
const walk = (el, out = []) => { out.push(el); (el.children || []).forEach((c) => c.children && walk(c, out)); return out; };
const byClass = (root, cls) => walk(root).filter((e) => e.className && e.className.split(" ").includes(cls));
const click = (el) => el.listeners.click({});
const type = (el, value) => { el.value = value; el.listeners.input({}); };
const wait = (ms = 10) => new Promise((r) => setTimeout(r, ms));
const chip = (root, name) => byClass(root, "chip").find((c) => text(c).includes(name));
const visibleTitles = (root) => byClass(root, "event").filter((e) => !e.hidden).map((e) => text(byClass(e, "title")[0]));
const openFilters = (root) => click(byClass(root, "filter-toggle")[0]);

const okFetch = (feed) => async () => ({ ok: true, json: async () => feed });
async function make(config, { feed = makeFeed(), fetchImpl, hass } = {}) {
  globalThis.fetch = fetchImpl || okFetch(feed);
  const card = new CardClass();
  card.setConfig(config);
  if (hass) card.hass = hass;
  await wait();
  card.disconnectedCallback();
  return card;
}
test.beforeEach(() => { window.localStorage = new Storage(); created.img = 0; });

// --- existing behavior ------------------------------------------------------------

test("feed renders upcoming events with honest verification labels and source links", async () => {
  const { shadowRoot: root } = await make({ feed: "/local/area-events-card/events.json" });
  assert.equal(byClass(root, "event").length, 3); // the past event is hidden
  const labels = byClass(root, "verify").map((v) => text(v).replace(/\|/g, "").replace(/\s+/g, " ").trim());
  assert.deepEqual(labels, ["Verified", "Source corroborated", "Unverified"]);
  assert.doesNotMatch(text(root), /DEMONSTRATION/);
  assert.ok(byClass(root, "source").every((a) => a.href.startsWith("https://www.kpbs.org/")));
});

test("feed fetch bypasses the cache", async () => {
  let seen;
  await make({ feed: "/f.json" }, { fetchImpl: async (url, opts) => { seen = [url, opts]; return okFetch(makeFeed())(); } });
  assert.match(seen[0], /^\/f\.json\?ts=\d+$/);
  assert.equal(seen[1].cache, "no-store");
});

test("feed failure shows an error, never demo events", async () => {
  const { shadowRoot: root } = await make({ feed: "/f.json" }, { fetchImpl: async () => { throw new Error("Failed to fetch"); } });
  assert.equal(byClass(root, "event").length, 0);
  assert.match(text(root), /Could not load events feed: Failed to fetch/);
  assert.match(text(byClass(root, "sticky-header")[0]), /Feed unavailable/);
});

test("inline events keep working and Verified only when verified is true", async () => {
  const { shadowRoot: root } = await make({ demo: true, events: [{ title: "A", verified: true }, { title: "B" }] });
  const out = text(root);
  assert.match(out, /DEMONSTRATION DATA/);
  assert.match(out, /Verified/);
  assert.match(out, /Unverified/);
});

test("entity attribute events still render, with the legacy single category field", async () => {
  const hass = { states: { "sensor.e": { attributes: { events: [{ title: "Concert", category: "Music", verified: false }] } } } };
  const { shadowRoot: root } = await make({ entity: "sensor.e" }, { hass });
  assert.equal(byClass(root, "event").length, 1);
  assert.match(text(root), /Music/);
});

test("build identifier stays visible", async () => {
  const { shadowRoot: root } = await make({ feed: "/f.json" });
  assert.match(text(byClass(root, "footer")[0]), /__AREA_EVENTS_CARD_BUILD__/);
});

// --- category matching ----------------------------------------------------------------

test("categories match title, description and source categories; events may match several", () => {
  const m = (e, custom) => [...categoriesFor(e, custom)].sort();
  assert.deepEqual(m({ title: "Jazz in the Park" }), ["music", "outdoors"]);
  assert.deepEqual(m({ title: "Gallery night", description: "wine tasting and live band" }), ["arts", "food-drink", "music"]);
  assert.deepEqual(m({ title: "Open day", categories: ["Family"] }), ["family"]);
  assert.deepEqual(m({ title: "Food / Drinks", categories: ["Food / Drinks"] }), ["food-drink"]);
  assert.deepEqual(m({ title: "Quarterly meeting" }), ["other"]);
});

test("keywords match whole words only", () => {
  assert.deepEqual([...categoriesFor({ title: "Parking lot sale" })], ["other"]);
  assert.deepEqual([...categoriesFor({ title: "Farmers Markets" })], ["markets"]);
});

// --- filtering ------------------------------------------------------------------------

test("default is Show All and unmatched events are never hidden", async () => {
  const { shadowRoot: root } = await make({ feed: "/f.json" });
  assert.deepEqual(visibleTitles(root), ["Confirmed Jazz Night", "Corroborated Farmers Market", "Mystery Gathering"]);
  assert.match(text(byClass(root, "filter-summary")[0]), /Showing all categories/);
});

test("selecting categories filters client side; multiple selection; Other; Show All; no new images or tiles", async () => {
  const card = await make({ feed: "/f.json" });
  const root = card.shadowRoot;
  const tiles = byClass(root, "event");
  const images = created.img;
  openFilters(root);

  click(chip(root, "Music"));
  assert.deepEqual(visibleTitles(root), ["Confirmed Jazz Night"]);
  click(chip(root, "Markets"));
  assert.deepEqual(visibleTitles(root), ["Confirmed Jazz Night", "Corroborated Farmers Market"]);
  assert.match(text(byClass(root, "counts")[0]), /2 of 3 events/);
  click(chip(root, "Music")); // deselect
  click(chip(root, "Markets"));
  click(chip(root, "Other"));
  assert.deepEqual(visibleTitles(root), ["Mystery Gathering"]);

  click(chip(root, "Music"));
  click(byClass(root, "show-all")[0]);
  assert.equal(visibleTitles(root).length, 3);
  click(chip(root, "Music"));
  click(chip(root, "All"));
  assert.equal(visibleTitles(root).length, 3);

  assert.deepEqual(byClass(root, "event"), tiles); // same tile objects, nothing rebuilt
  assert.equal(created.img, images); // no image elements were created
});

test("Show all comes first, before Filters; the header timestamp has no icon", async () => {
  const card = await make({ feed: "/f.json" });
  const root = card.shadowRoot;
  const bar = byClass(root, "filter-bar")[0];
  assert.deepEqual(bar.children.slice(0, 2).map((c) => c.className), ["show-all", "filter-toggle"]);
  assert.equal(byClass(root, "show-all")[0].attrs["aria-pressed"], "true");
  openFilters(root);
  click(chip(root, "Music"));
  assert.equal(byClass(root, "show-all")[0].attrs["aria-pressed"], "false");
  assert.equal(byClass(root, "filter-bar")[0].children[0].className, "show-all");
  const updated = byClass(root, "updated")[0];
  assert.ok(!updated.children.some((c) => c.tag === "ha-icon"));
  assert.match(text(updated), /^Updated /);
});

test("a selection matching nothing shows an explanatory message", async () => {
  const card = await make({ feed: "/f.json" });
  openFilters(card.shadowRoot);
  click(chip(card.shadowRoot, "Comedy"));
  assert.equal(visibleTitles(card.shadowRoot).length, 0);
  assert.equal(byClass(card.shadowRoot, "no-match")[0].hidden, false);
});

test("filtering makes no network requests", async () => {
  let calls = 0;
  const card = await make({ feed: "/f.json" }, { fetchImpl: async () => { calls++; return okFetch(makeFeed())(); } });
  const before = calls;
  openFilters(card.shadowRoot);
  click(chip(card.shadowRoot, "Music"));
  click(chip(card.shadowRoot, "Markets"));
  assert.equal(calls, before);
});

// --- custom categories and persistence -----------------------------------------------

function createCustom(root, name, keywords) {
  click(byClass(root, "new-category")[0]);
  type(byClass(root, "name-input")[0], name);
  type(byClass(root, "keywords-input")[0], keywords);
  click(byClass(root, "save")[0]);
}
const stored = (card) => JSON.parse(window.localStorage.getItem(card._prefsKey));

test("custom categories: create with keywords, filter by them, rename, delete; persisted", async () => {
  const card = await make({ feed: "/f.json" });
  const root = card.shadowRoot;
  openFilters(root);
  createCustom(root, "Weekend", "mystery, gathering");
  assert.ok(chip(root, "Weekend"));
  click(chip(root, "Weekend"));
  assert.deepEqual(visibleTitles(root), ["Mystery Gathering"]);
  assert.equal(stored(card).custom[0].name, "Weekend");
  assert.deepEqual(stored(card).custom[0].keywords, ["mystery", "gathering"]);
  assert.deepEqual(stored(card).selected, [stored(card).custom[0].id]);

  click(byClass(root, "edit")[0]);
  type(byClass(root, "name-input")[0], "Odd Things");
  type(byClass(root, "keywords-input")[0], "jazz");
  click(byClass(root, "save")[0]);
  assert.ok(chip(root, "Odd Things"));
  assert.deepEqual(visibleTitles(root), ["Confirmed Jazz Night"]); // selection kept, new keywords applied

  click(byClass(root, "delete")[0]); // first click asks for confirmation
  assert.equal(stored(card).custom.length, 1);
  click(byClass(root, "delete")[0]);
  assert.equal(stored(card).custom.length, 0);
  assert.deepEqual(stored(card).selected, []);
  assert.equal(visibleTitles(root).length, 3);
});

test("a custom category with no keywords matches its own name; duplicate and empty names are refused", async () => {
  const card = await make({ feed: "/f.json" });
  const root = card.shadowRoot;
  openFilters(root);
  createCustom(root, "Gathering", "");
  click(chip(root, "Gathering"));
  assert.deepEqual(visibleTitles(root), ["Mystery Gathering"]);
  createCustom(root, "music", "x");
  assert.match(text(byClass(root, "editor")[0]), /already used/);
  type(byClass(root, "name-input")[0], "  ");
  click(byClass(root, "save")[0]);
  assert.match(text(byClass(root, "editor")[0]), /Enter a name/);
  assert.equal(stored(card).custom.length, 1);
});

test("saved preferences restore in a new card instance", async () => {
  const first = await make({ feed: "/f.json", title: "Events" });
  openFilters(first.shadowRoot);
  createCustom(first.shadowRoot, "Weekend", "mystery");
  click(chip(first.shadowRoot, "Weekend"));
  click(chip(first.shadowRoot, "Music"));

  const second = await make({ feed: "/f.json", title: "Events" });
  assert.deepEqual(visibleTitles(second.shadowRoot), ["Confirmed Jazz Night", "Mystery Gathering"]);
  assert.match(text(byClass(second.shadowRoot, "filter-summary")[0]), /Music, Weekend/);
});

test("preferences are isolated per card instance and per dashboard path", async () => {
  const a = await make({ feed: "/f.json", title: "A" });
  const b = await make({ feed: "/f.json", title: "B" });
  const c = await make({ feed: "/f.json", title: "A", id: "left" });
  const d = await make({ feed: "/f.json", title: "A", id: "right" });
  assert.equal(new Set([a, b, c, d].map((card) => card._prefsKey)).size, 4);

  openFilters(a.shadowRoot);
  click(chip(a.shadowRoot, "Music"));
  const a2 = await make({ feed: "/f.json", title: "A" });
  const b2 = await make({ feed: "/f.json", title: "B" });
  assert.equal(visibleTitles(a2.shadowRoot).length, 1);
  assert.equal(visibleTitles(b2.shadowRoot).length, 3);

  const before = prefsKey({ feed: "/f.json", title: "A" });
  window.location.pathname = "/other-dashboard/view";
  assert.notEqual(prefsKey({ feed: "/f.json", title: "A" }), before);
  window.location.pathname = "/lovelace/events";
});

test("corrupt or hostile stored preferences are ignored", async () => {
  const key = prefsKey({ feed: "/f.json" });
  for (const bad of ["{oops", "[]", "null", JSON.stringify({ selected: "music" })]) {
    window.localStorage.setItem(key, bad);
    const card = await make({ feed: "/f.json" });
    assert.equal(visibleTitles(card.shadowRoot).length, 3);
  }
  assert.deepEqual(normalizePrefs({ selected: ["music", "nope", "music"], custom: [{ id: "bad id", name: "x" }, { id: "c_abc", name: " Ok ", keywords: [" A ", 5] }] }),
    { v: 1, selected: ["music"], custom: [{ id: "c_abc", name: "Ok", keywords: ["a"] }] });
});

test("preferences sync to Home Assistant user data and load from it", async () => {
  const calls = [];
  const remote = new Map();
  const hass = {
    states: {},
    callWS: async (msg) => {
      calls.push(msg);
      if (msg.type === "frontend/get_user_data") return { value: remote.get(msg.key) ?? null };
      remote.set(msg.key, msg.value);
    },
  };
  const card = await make({ feed: "/f.json" }, { hass });
  openFilters(card.shadowRoot);
  click(chip(card.shadowRoot, "Music"));
  await wait(400);
  const save = calls.find((c) => c.type === "frontend/set_user_data");
  assert.ok(save && save.key.startsWith("area-events-card:prefs:v1:"));
  assert.deepEqual(save.value.selected, ["music"]);

  // another browser (empty localStorage) gets the saved choice from the HA user data
  window.localStorage = new Storage();
  const other = await make({ feed: "/f.json" }, { hass });
  assert.deepEqual(visibleTitles(other.shadowRoot), ["Confirmed Jazz Night"]);

  // HA unreachable: local storage still works
  window.localStorage = new Storage();
  const offline = await make({ feed: "/f.json" }, { hass: { states: {}, callWS: async () => { throw new Error("down"); } } });
  openFilters(offline.shadowRoot);
  click(chip(offline.shadowRoot, "Markets"));
  assert.deepEqual(stored(offline).selected, ["markets"]);
});

test("existing local preferences migrate to HA user data when none exist there", async () => {
  const key = prefsKey({ feed: "/f.json" });
  window.localStorage.setItem(key, JSON.stringify({ v: 1, selected: ["music"], custom: [] }));
  const saved = [];
  const hass = { states: {}, callWS: async (m) => { if (m.type === "frontend/set_user_data") saved.push(m); return m.type === "frontend/get_user_data" ? null : undefined; } };
  await make({ feed: "/f.json" }, { hass });
  await wait(20);
  assert.equal(saved.length, 1);
  assert.deepEqual(saved[0].value.selected, ["music"]);
});

// --- header ---------------------------------------------------------------------------

test("header shows title, the feed's own update time, counts and source count", async () => {
  const generated = iso(-35 * 60 * 1000);
  const card = await make({ feed: "/f.json", title: "San Diego Events" }, { feed: makeFeed({ generated_at: generated }) });
  const header = text(byClass(card.shadowRoot, "sticky-header")[0]);
  assert.match(header, /San Diego Events/);
  assert.match(header, /Updated .*\(35 min ago\)/);
  assert.ok(header.includes(card._formatDate(generated)));
  assert.match(header, /3 events/);
  assert.match(header, /2 sources/);
  assert.doesNotMatch(header, /Stale|failed|next|Next/);
});

test("header time comes from generated_at, never the page-load time", async () => {
  const old = new Date("2026-01-02T03:04:00-08:00").toISOString();
  const card = await make({ feed: "/f.json" }, { feed: makeFeed({ generated_at: old, events: [event("Future", 5)] }) });
  const header = text(byClass(card.shadowRoot, "sticky-header")[0]);
  assert.ok(header.includes(card._formatDate(old)));
  assert.match(header, /Stale/);
  assert.match(header, /d ago/);
});

test("header marks stale feeds, failing sources and refresh errors", async () => {
  const stale = await make({ feed: "/f.json" }, { feed: makeFeed({ generated_at: iso(-4 * 3600 * 1000), sources: { a: { ok: true }, b: { ok: false } } }) });
  const header = text(byClass(stale.shadowRoot, "sticky-header")[0]);
  assert.match(header, /Stale/);
  assert.match(header, /1 of 2 sources ok/);
  assert.match(header, /1 source failing/);

  // a later refresh failure keeps the old data but flags it
  const card = await make({ feed: "/f.json" });
  globalThis.fetch = async () => { throw new Error("offline"); };
  await card._loadFeed();
  assert.match(text(byClass(card.shadowRoot, "sticky-header")[0]), /Update failed/);
  assert.equal(byClass(card.shadowRoot, "event").length, 3);
  assert.match(text(card.shadowRoot), /Showing stale data/);
});

test("an identical refreshed feed does not rebuild tiles or images", async () => {
  const feed = makeFeed();
  const card = await make({ feed: "/f.json" }, { feed });
  const tiles = byClass(card.shadowRoot, "event");
  const images = created.img;
  await card._loadFeed();
  assert.deepEqual(byClass(card.shadowRoot, "event"), tiles);
  assert.equal(created.img, images);
});

test("inline events show a header without a fabricated update time", async () => {
  const { shadowRoot: root } = await make({ title: "Demo", demo: true, events: [{ title: "A" }] });
  const header = text(byClass(root, "sticky-header")[0]);
  assert.match(header, /Demo/);
  assert.match(header, /1 event$/);
  assert.doesNotMatch(header, /Updated/);
});


// --- area: configured / Home Assistant home / browser location -------------------------------

const { haHomeArea, normalizeArea, resolveArea, backendHintCommand } = CardClass._internals;
const HOME = { latitude: 30.2672, longitude: -97.7431, time_zone: "America/Chicago", location_name: "Home" };
const hassWith = (config) => ({ states: {}, config });
const SAN_DIEGO_FEED_LOCATION = { display_name: "San Diego, CA", latitude: 32.7157, longitude: -117.1611, radius_miles: 25 };
const openArea = (root) => { openFilters(root); return text(byClass(root, "area")[0]); };

function geolocationSpy(behavior) {
  const spy = { calls: 0, options: undefined, getCurrentPosition(success, failure, options) { spy.calls++; spy.options = options; behavior(success, failure); } };
  Object.defineProperty(globalThis, "navigator", { value: { geolocation: spy }, configurable: true, writable: true });
  return spy;
}
const noGeolocation = () => Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true, writable: true });

test("area priority: chosen in the card, then card configuration, then Home Assistant home, then unavailable", () => {
  const hass = hassWith(HOME);
  const configured = normalizeArea({ kind: "configured", name: "Office", latitude: 40.7, longitude: -74.0 });
  const browser = normalizeArea({ kind: "browser", latitude: 51.5, longitude: -0.12 });
  assert.equal(resolveArea({ saved: browser, configured, hass }).source, "selected_browser");
  assert.equal(resolveArea({ saved: { kind: "ha_home" }, configured, hass }).source, "selected_home_assistant");
  assert.equal(resolveArea({ configured, hass }).source, "configured");
  const home = resolveArea({ hass });
  assert.deepEqual([home.source, home.area.latitude, home.area.timezone], ["home_assistant", HOME.latitude, "America/Chicago"]);
  assert.equal(resolveArea({ hass: hassWith({}) }).source, "unavailable");
  assert.equal(resolveArea({ saved: { kind: "ha_home" }, hass: hassWith({}) }).source, "unavailable"); // chosen home, but HA has none
});

test("a missing or invalid Home Assistant location is treated as unavailable, never guessed", () => {
  const bad = [undefined, {}, { latitude: 0, longitude: 0 }, { latitude: "30", longitude: "-97" }, { latitude: NaN, longitude: 1 },
    { latitude: 91, longitude: 0 }, { latitude: 30, longitude: 181 }, { latitude: null, longitude: null }];
  for (const config of bad) assert.equal(haHomeArea(hassWith(config)), undefined, JSON.stringify(config));
  assert.equal(haHomeArea(undefined), undefined);
  assert.equal(haHomeArea(hassWith({ latitude: 30, longitude: -97, time_zone: "Central" })).timezone, undefined); // no timezone is made up
});

test("without any location the panel says so and offers choices; the browser is not asked on load", async () => {
  const spy = geolocationSpy(() => {});
  const card = await make({ feed: "/f.json" }, { hass: hassWith({}) });
  const out = openArea(card.shadowRoot);
  assert.match(out, /No location is available/);
  assert.equal(byClass(card.shadowRoot, "use-home").length, 0); // HA has no home location to offer
  assert.equal(byClass(card.shadowRoot, "use-mine").length, 1);
  assert.equal(spy.calls, 0);
});

test("the Home Assistant home location is used when nothing else is set, and is not stored", async () => {
  const spy = geolocationSpy(() => {});
  const card = await make({ feed: "/f.json" }, { hass: hassWith(HOME) });
  const out = openArea(card.shadowRoot);
  assert.match(out, /Area: Home - Home Assistant home location/);
  assert.match(out, /America\/Chicago/);
  assert.equal(spy.calls, 0);
  assert.equal(window.localStorage.getItem(card._prefsKey), null); // read live from hass, never saved
});

test("choosing Home Assistant home saves a reference, not coordinates, and follows hass", async () => {
  const hass = hassWith(HOME);
  const card = await make({ feed: "/f.json" }, { hass });
  openFilters(card.shadowRoot);
  click(byClass(card.shadowRoot, "use-home")[0]);
  assert.deepEqual(JSON.parse(window.localStorage.getItem(card._prefsKey)).area, { kind: "ha_home" });
  hass.config = { ...HOME, latitude: 48.8566, longitude: 2.3522, location_name: "Paris", time_zone: "Europe/Paris" };
  card.hass = hass;
  card._renderFilterUI();
  assert.match(text(byClass(card.shadowRoot, "area")[0]), /Area: Paris - Home Assistant home location \(selected\)/);
});

test("browser location is requested only by clicking, then saved for this card", async () => {
  const spy = geolocationSpy((ok) => ok({ coords: { latitude: 51.5072, longitude: -0.1276, accuracy: 42.4 } }));
  const card = await make({ feed: "/f.json" }, { hass: hassWith(HOME) });
  openFilters(card.shadowRoot);
  assert.equal(spy.calls, 0);
  click(byClass(card.shadowRoot, "use-mine")[0]);
  assert.equal(spy.calls, 1);
  assert.equal(spy.options.timeout, 10000);
  const saved = JSON.parse(window.localStorage.getItem(card._prefsKey)).area;
  assert.deepEqual([saved.kind, saved.latitude, saved.longitude, saved.accuracy_m], ["browser", 51.5072, -0.1276, 42.4]);
  const out = text(byClass(card.shadowRoot, "area")[0]);
  assert.match(out, /Area: Browser location - your browser location \(saved for this card\)/);
  assert.match(out, /accurate to about 42 m/);
  const fresh = await make({ feed: "/f.json" }, { hass: hassWith(HOME) });
  assert.match(openArea(fresh.shadowRoot), /Browser location/); // restored later, without asking the browser again
  assert.equal(spy.calls, 1);
});

test("denied, timed-out, unavailable, unusable and unsupported browser locations save nothing", async () => {
  const cases = [
    [(ok, fail) => fail({ code: 1 }), /permission was denied/],
    [(ok, fail) => fail({ code: 3 }), /took too long/],
    [(ok, fail) => fail({ code: 2 }), /could not determine/],
    [(ok, fail) => fail({}), /could not be read/],
    [(ok) => ok({ coords: { latitude: 0, longitude: 0, accuracy: 1 } }), /unusable position/],
    [(ok) => ok({ coords: { latitude: 200, longitude: 0 } }), /unusable position/],
    [() => { throw new Error("blocked"); }, /could not be read/],
  ];
  for (const [behavior, expected] of cases) {
    geolocationSpy(behavior);
    const card = await make({ feed: "/f.json" }, { hass: hassWith({}) });
    openFilters(card.shadowRoot);
    click(byClass(card.shadowRoot, "use-mine")[0]);
    assert.match(text(byClass(card.shadowRoot, "area-message")[0]), expected);
    assert.equal(window.localStorage.getItem(card._prefsKey), null);
    assert.match(text(byClass(card.shadowRoot, "area")[0]), /No location is available/); // nothing substituted
  }
  noGeolocation();
  const card = await make({ feed: "/f.json" }, { hass: hassWith({}) });
  openFilters(card.shadowRoot);
  click(byClass(card.shadowRoot, "use-mine")[0]);
  assert.match(text(byClass(card.shadowRoot, "area-message")[0]), /does not support location/);
});

test("a pending browser request that is superseded does not overwrite the newer choice", async () => {
  let respond;
  geolocationSpy((ok) => { respond = ok; });
  const card = await make({ feed: "/f.json" }, { hass: hassWith(HOME) });
  openFilters(card.shadowRoot);
  click(byClass(card.shadowRoot, "use-mine")[0]);
  click(byClass(card.shadowRoot, "use-home")[0]); // the user changed their mind before the browser answered
  respond({ coords: { latitude: 51.5, longitude: -0.12, accuracy: 10 } });
  assert.equal(JSON.parse(window.localStorage.getItem(card._prefsKey)).area.kind, "ha_home");
});

test("area choices are isolated per card instance and can be cleared", async () => {
  geolocationSpy((ok) => ok({ coords: { latitude: 51.5, longitude: -0.12, accuracy: 10 } }));
  const a = await make({ feed: "/f.json", title: "A" }, { hass: hassWith({}) });
  openFilters(a.shadowRoot);
  click(byClass(a.shadowRoot, "use-mine")[0]);
  const b = await make({ feed: "/f.json", title: "B" }, { hass: hassWith({}) });
  assert.match(openArea(b.shadowRoot), /No location is available/);
  click(byClass(a.shadowRoot, "clear-area")[0]);
  assert.equal(JSON.parse(window.localStorage.getItem(a._prefsKey)).area, undefined);
  assert.match(text(byClass(a.shadowRoot, "area")[0]), /No location is available/);
});

test("an area in the card configuration is used and validated", async () => {
  const card = await make({ feed: "/f.json", area: { name: "Office", latitude: 40.7, longitude: -74.0, radius_miles: 10, timezone: "America/New_York" } }, { hass: hassWith(HOME) });
  assert.match(openArea(card.shadowRoot), /Area: Office - set in the card configuration/);
  for (const area of [{ latitude: 100, longitude: 0 }, { latitude: 1 }, { latitude: 0, longitude: 0 }, "x"]) {
    assert.throws(() => new CardClass().setConfig({ feed: "/f.json", area }), /'area' needs valid latitude and longitude/);
  }
});

test("the card never claims the chosen area reaches the discovery service", async () => {
  const feed = makeFeed({ location: SAN_DIEGO_FEED_LOCATION });
  const near = await make({ feed: "/f.json", area: { latitude: 32.8, longitude: -117.1 } }, { feed });
  const nearText = openArea(near.shadowRoot);
  assert.match(nearText, /Events were discovered for: San Diego, CA, 25 mi radius/);
  assert.match(nearText, /inside the discovery area/);
  assert.match(nearText, /nothing here is sent to it/);
  const far = await make({ feed: "/f.json", area: { name: "Austin", latitude: 30.2672, longitude: -97.7431 } }, { feed });
  assert.match(openArea(far.shadowRoot), /Your area is \d+ mi from the discovery area, so these events may not be near you/);
  assert.match(text(byClass(far.shadowRoot, "hint-command")[0]), /^python -m discovery.hint --source home_assistant --latitude 30.26720 --longitude -97.74310/);
  const unlabeled = await make({ feed: "/f.json", area: { latitude: 30.27, longitude: -97.74 } });
  assert.match(openArea(unlabeled.shadowRoot), /does not say which area/);
});

test("backend hint command and stored areas are validated", () => {
  assert.equal(backendHintCommand({ source: "browser", latitude: 51.5, longitude: -0.12, accuracy_m: 41.6, timezone: "Europe/London" }),
    "python -m discovery.hint --source browser --latitude 51.50000 --longitude -0.12000 --timezone Europe/London --accuracy-m 42");
  assert.deepEqual(normalizePrefs({ selected: [], custom: [], area: { kind: "ha_home" } }).area, { kind: "ha_home" });
  assert.equal(normalizePrefs({ selected: [], custom: [], area: { kind: "browser", latitude: 0, longitude: 0 } }).area, undefined);
  assert.equal(normalizePrefs({ selected: [], custom: [], area: { kind: "configured", latitude: 1, longitude: 2 } }).area, undefined); // configuration is not stored
  assert.equal(normalizeArea({ kind: "browser", latitude: null, longitude: 5 }), undefined);
  assert.equal(normalizeArea({ kind: "browser", latitude: "", longitude: 5 }), undefined);
});
