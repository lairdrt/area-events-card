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


// --- location from YAML: area + radius_miles, applied through the Home Assistant scripts --------------------------

const { haHomeArea, parsePlace, extractServiceResponse } = CardClass._internals;
const HOME = { latitude: 30.267153, longitude: -97.743061, time_zone: "America/Chicago", location_name: "Home" };

/** A stand-in for Home Assistant plus the discovery server's /api/v1/location, reached only through scripts. */
function createServer() {
  const server = { calls: [], wsCalls: [], change: { state: "idle" }, rejectSet: undefined, syncFails: false,
    active: { name: "San Diego, CA", latitude: 32.7157, longitude: -117.1611, radius_miles: 25 }, feedLocation: "San Diego, CA" };
  const handlers = {
    location: () => ({ status: 200, content: { active: { source: "configuration", location: server.active }, feed: { location: server.feedLocation }, change: server.change } }),
    set_location: ({ spec }) => {
      if (server.rejectSet) return { status: server.rejectSet.status, content: { error: { message: server.rejectSet.message } } };
      if (spec.query === "Nowhereville") return { status: 422, content: { error: { message: "could not resolve location 'Nowhereville'" } } };
      if (server.change.state === "queued" || server.change.state === "running") return { status: 409, content: { error: { message: "a location change is already in progress" } } };
      server.change = { state: "queued", requested: spec };
      return { status: 202, content: { change: server.change } };
    },
    sync_now: () => (server.syncFails ? { status: 500, content: "sync failed" } : { status: 200, content: "published" }),
  };
  server.hass = (config = HOME) => ({
    states: {}, config: { country: "US", ...config },
    callWS: async (message) => {
      server.wsCalls.push(message);
      if (message.type !== "call_service") return message.type === "frontend/get_user_data" ? { value: null } : undefined;
      assert.equal(message.domain, "script");
      assert.equal(message.return_response, true);
      const name = message.service.replace(/^area_events_/, "");
      server.calls.push([name, message.service_data]);
      return { context: {}, response: handlers[name](message.service_data) };
    },
  });
  server.count = (name) => server.calls.filter(([n]) => n === name).length;
  server.setSpec = () => server.calls.find(([n]) => n === "set_location")[1].spec;
  return server;
}

const open = [];
async function makeControlled(config, server, hass, samePage = false) {
  if (!samePage) delete globalThis.__areaEventsWantedPlaces; // each card is on its own page unless a test says otherwise
  let feedLoads = 0;
  globalThis.fetch = async () => { feedLoads++; return { ok: true, json: async () => makeFeed({ location: { display_name: "San Diego, CA", latitude: 32.7157, longitude: -117.1611, radius_miles: 25 } }) }; };
  const card = new CardClass();
  open.push(card);
  card.setConfig({ feed: "/f.json", location_control: true, ...config });
  card.hass = hass || server.hass();
  await wait(40);
  card.feedLoads = () => feedLoads;
  return card;
}
test.afterEach(() => { open.splice(0).forEach((c) => c.disconnectedCallback()); delete globalThis.__areaEventsWantedPlaces; });
const noticeText = (card) => (byClass(card.shadowRoot, "notice")[0].hidden ? "" : text(byClass(card.shadowRoot, "notice")[0]));
const watchOnce = (card, spec) => card._controlWatch(card._control.run, spec);

test("feed-only cards are unchanged: no script calls and no geographic controls", async () => {
  const server = createServer();
  const card = await make({ feed: "/f.json" }, { hass: server.hass() });
  openFilters(card.shadowRoot);
  assert.equal(server.calls.length, 0);
  assert.equal(byClass(card.shadowRoot, "event").length, 3);
  assert.equal(noticeText(card), "");
});

test("no interactive geographic controls remain, and the browser location is never requested", async () => {
  const server = createServer();
  let asked = 0;
  Object.defineProperty(globalThis, "navigator", { value: { geolocation: { getCurrentPosition: () => { asked++; } } }, configurable: true, writable: true });
  const card = await makeControlled({ area: "San Diego, CA" }, server);
  openFilters(card.shadowRoot);
  for (const cls of ["area", "use-home", "use-mine", "clear-area", "search-area", "apply-area", "place-input", "saved-area", "sync-now"]) {
    assert.equal(byClass(card.shadowRoot, cls).length, 0, cls);
  }
  assert.equal(asked, 0);
  assert.equal(byClass(card.shadowRoot, "chip").length > 0, true); // the category filters are still there
});

test("YAML area and radius that match the server's active place make no change request", async () => {
  const server = createServer();
  const card = await makeControlled({ area: "San Diego, CA", radius_miles: 25 }, server);
  assert.equal(server.count("location"), 1);
  assert.equal(server.count("set_location"), 0);
  assert.equal(noticeText(card), "");
  card.hass = server.hass(); card.hass = server.hass(); // Home Assistant state updates never repeat the check
  await wait(20);
  assert.equal(server.count("location"), 1);
});

test("a different area is applied once, the old events stay, and the new feed is fetched when discovery succeeds", async () => {
  const server = createServer();
  const card = await makeControlled({ area: "Austin, TX", radius_miles: 30 }, server);
  assert.deepEqual(server.setSpec(), { query: "Austin, TX", country: "US", radius_miles: 30, name: "Austin, TX" });
  assert.equal(server.count("set_location"), 1);
  assert.match(noticeText(card), /Discovery for Austin, TX is waiting to start; the current events stay until it succeeds/);
  assert.equal(byClass(card.shadowRoot, "event").length, 3); // still the previous place's events
  server.change = { ...server.change, state: "running" };
  await watchOnce(card, server.setSpec());
  assert.match(noticeText(card), /is running/);
  const loads = card.feedLoads();
  server.change = { ...server.change, state: "succeeded" };
  await watchOnce(card, server.setSpec());
  assert.equal(server.count("sync_now"), 1); // the existing sync script copies the new feed to Home Assistant now
  assert.equal(card.feedLoads(), loads + 1); // and the card reloads it
  assert.equal(noticeText(card), "");
  assert.equal(server.count("set_location"), 1); // never repeated
});

test("a different radius counts as a different place", async () => {
  const server = createServer();
  await makeControlled({ area: "San Diego, CA", radius_miles: 40 }, server);
  assert.equal(server.setSpec().radius_miles, 40);
});

test("coordinates in YAML are compared by position; ZIP codes use the country", async () => {
  const server = createServer();
  server.active = { name: "Somewhere", latitude: 30.2672, longitude: -97.7431, radius_miles: 25 };
  await makeControlled({ area: "30.2672, -97.7431" }, server);
  assert.equal(server.count("set_location"), 0);
  const other = createServer();
  await makeControlled({ area: "78701", title: "zip" }, other);
  assert.deepEqual(other.setSpec(), { zip: "78701", country: "US", radius_miles: 25, name: "78701" });
});

test("without an area the Home Assistant home location is used, rounded, with its time zone", async () => {
  const server = createServer();
  await makeControlled({}, server);
  assert.deepEqual(server.setSpec(), { latitude: 30.2672, longitude: -97.7431, radius_miles: 25, name: "Home", timezone: "America/Chicago" });
  const matching = createServer();
  matching.active = { name: "Home", latitude: 30.2672, longitude: -97.7431, radius_miles: 25 };
  await makeControlled({ title: "home match" }, matching);
  assert.equal(matching.count("set_location"), 0);
});

test("a missing Home Assistant home location is a clear error and nothing is sent", async () => {
  const server = createServer();
  const card = await makeControlled({}, server, server.hass({ latitude: 0, longitude: 0 }));
  assert.equal(server.calls.length, 0);
  assert.match(noticeText(card), /Location could not be applied: No area is set in the card and Home Assistant has no home location/);
});

test("a rejected or failed change shows why and keeps the previous events labeled as they were", async () => {
  const server = createServer();
  const card = await makeControlled({ area: "Nowhereville" }, server);
  assert.match(noticeText(card), /Location could not be applied: could not resolve location 'Nowhereville'/);
  assert.equal(byClass(card.shadowRoot, "event").length, 3);
  const limited = createServer();
  limited.rejectSet = { status: 429, message: "the location was changed a moment ago" };
  assert.match(noticeText(await makeControlled({ area: "Austin, TX", title: "limited" }, limited)), /changed a moment ago/);
  const failing = createServer();
  const failed = await makeControlled({ area: "Austin, TX", title: "failing" }, failing);
  failing.change = { state: "failed", requested: failing.setSpec(), error: "no enabled source applies to Austin" };
  await watchOnce(failed, failing.setSpec());
  assert.match(noticeText(failed), /Location could not be applied: no enabled source applies to Austin \(the events shown are still for San Diego, CA\)/);
  assert.equal(failing.count("sync_now"), 0);
  assert.equal(byClass(failed.shadowRoot, "event").length, 3);
});

test("a change already in progress for the same place is waited for, not repeated", async () => {
  const server = createServer();
  server.change = { state: "running", requested: { query: "Austin, TX", radius_miles: 25, name: "Austin, TX" } };
  const card = await makeControlled({ area: "Austin, TX" }, server);
  assert.equal(server.count("set_location"), 0);
  assert.match(noticeText(card), /is running/);
});

test("cards that want different places on one page are a conflict and none of them changes the location", async () => {
  const server = createServer();
  const first = await makeControlled({ area: "Austin, TX", title: "A" }, server);
  const second = await makeControlled({ area: "Boston, MA", title: "B" }, server, undefined, true);
  assert.equal(server.count("set_location"), 1); // only the first card asked; the second saw the conflict and did not
  for (const card of [first, second]) assert.match(noticeText(card), /Another Area Events card on this page wants a different place/);
  const same = createServer();
  const a = await makeControlled({ area: "San Diego, CA", title: "C" }, same);
  const b = await makeControlled({ area: "san diego, ca", title: "D" }, same, undefined, true);
  assert.equal(same.count("set_location"), 0);
  assert.equal(noticeText(a) + noticeText(b), "");
});

test("only Home Assistant scripts are used: no network for control, no credential, nothing stored", async () => {
  const server = createServer();
  await makeControlled({ area: "Austin, TX", script_prefix: "my_events" }, server);
  const services = server.wsCalls.filter((m) => m.type === "call_service");
  assert.ok(services.length > 0 && services.every((m) => m.domain === "script" && /^my_events_(location|set_location|sync_now)$/.test(m.service)));
  const everything = JSON.stringify(server.wsCalls) + JSON.stringify(Object.fromEntries(window.localStorage.map));
  assert.doesNotMatch(everything, /Bearer|authorization|api[_-]?key|https?:\/\/192/i);
});

test("configuration is validated", () => {
  for (const bad of [{ area: 5 }, { area: "  " }, { area: { latitude: 1, longitude: 2 } }, { radius_miles: 0 }, { radius_miles: 300 }, { radius_miles: "far" }, { script_prefix: "bad prefix; rm" }]) {
    assert.throws(() => new CardClass().setConfig({ feed: "/f.json", ...bad }), /area|radius_miles|script_prefix/, JSON.stringify(bad));
  }
  const valid = new CardClass();
  valid.setConfig({ feed: "/f.json", area: "San Diego, CA", radius_miles: 25 });
  valid.disconnectedCallback();
});

test("place and response parsing", () => {
  assert.deepEqual(parsePlace("Austin, TX", "US", 30), { spec: { query: "Austin, TX", country: "US", radius_miles: 30 } });
  assert.deepEqual(parsePlace("78701", "US", 25), { spec: { zip: "78701", country: "US", radius_miles: 25 } });
  assert.deepEqual(parsePlace("30.27, -97.74", "US", 10), { spec: { latitude: 30.27, longitude: -97.74, radius_miles: 10 } });
  assert.match(parsePlace("78701", undefined, 25).error, /needs a country/);
  assert.match(parsePlace("95, 10", "US", 25).error, /not valid/);
  assert.equal(haHomeArea({ config: { latitude: 0, longitude: 0 } }), undefined);
  assert.deepEqual(extractServiceResponse({ response: { status: 200, content: '{"a": 1}' } }), { status: 200, content: { a: 1 } });
  assert.deepEqual(extractServiceResponse({ response: { "script.x": { status: 404, content: "t" } } }), { status: 404, content: "t" });
  assert.throws(() => extractServiceResponse({ response: null }), /returned no response/);
});
