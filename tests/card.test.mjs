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
globalThis.customElements = { get: () => undefined, define: (name, c) => { if (name === "area-events-card") CardClass = c; } };
globalThis.window = { location: { pathname: "/lovelace/events" }, localStorage: new Storage() };
await import("../src/area-events-card.js");
const { categoriesFor, normalizePrefs, prefsKey, readSettings, editorToConfig, configToEditor, EDITOR_SCHEMA } = CardClass._internals;

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

/** What the integration's area_events/events command answers for a feed. */
const answer = (feed, extra = {}) => ({
  state: "ready", connected: true, refreshing: false, message: null, report: [],
  generated_at: feed.generated_at, location: feed.location || { display_name: "San Diego, CA" }, sources: feed.sources, events: feed.events, ...extra,
});

/** A Home Assistant stand-in: answers area_events/events, and the user-data commands the filters use. */
function fakeHass({ feed = makeFeed(), reply, onWS } = {}) {
  const hass = {
    states: {}, config: {}, calls: [], eventsCalls: () => hass.calls.filter((m) => m.type === "area_events/events"),
    callWS: async (message) => {
      hass.calls.push(message);
      if (message.type === "area_events/events") {
        if (hass.fail) throw hass.fail;
        if (reply) return reply(message);
        return message.since && message.since === feed.generated_at ? answer(feed, { events: undefined, unchanged: true }) : answer(feed);
      }
      if (onWS) {
        const result = await onWS(message);
        if (result !== undefined) return result;
      }
      return message.type === "frontend/get_user_data" ? { value: null } : undefined;
    },
  };
  return hass;
}

async function make(config, { feed = makeFeed(), hass, reply } = {}) {
  globalThis.fetch = async () => { throw new Error("the card must not use fetch"); };
  if (hass && !hass.eventsCalls) { // a custom hass from a persistence test: add the events command to it
    const inner = hass.callWS;
    hass.callWS = async (m) => (m.type === "area_events/events" ? answer(feed) : inner(m));
  }
  hass = hass || fakeHass({ feed, reply });
  const card = new CardClass();
  card.setConfig(config);
  card.hass = hass;
  await wait();
  card.disconnectedCallback();
  card.hassStub = hass;
  return card;
}
/** Poll once more, as the card's timer would, without leaving a timer behind. */
async function pollAgain(card) {
  await card._loadFeed(card._feedRun);
  card.disconnectedCallback();
}
test.beforeEach(() => { window.localStorage = new Storage(); created.img = 0; });

// --- events from the integration -----------------------------------------------------------

test("events render upcoming events with honest verification labels and source links", async () => {
  const { shadowRoot: root } = await make({});
  assert.equal(byClass(root, "event").length, 3); // the past event is hidden
  const labels = byClass(root, "verify").map((v) => text(v).replace(/\|/g, "").replace(/\s+/g, " ").trim());
  assert.deepEqual(labels, ["Verified", "Source corroborated", "Unverified"]);
  assert.doesNotMatch(text(root), /DEMONSTRATION/);
  assert.ok(byClass(root, "source").every((a) => a.href.startsWith("https://www.kpbs.org/")));
});

test("the card asks only Home Assistant, with its profile; later polls send `since`", async () => {
  const card = await make({ area: "Austin, TX", radius_miles: 30, sources: ["https://calendar.utexas.edu"] });
  const [first] = card.hassStub.eventsCalls();
  assert.deepEqual(first, { type: "area_events/events", radius_miles: 30, area: "Austin, TX", sources: ["https://calendar.utexas.edu"] });
  await pollAgain(card);
  assert.equal(card.hassStub.eventsCalls()[1].since, card._feed.generatedAt);
  assert.ok(card.hassStub.calls.every((m) => /^(area_events\/events|frontend\/)/.test(m.type)), "no other Home Assistant commands");
  assert.equal(byClass(card.shadowRoot, "event").length, 3); // the unchanged answer kept the events
});

test("without an area the card asks for the Home Assistant home location", async () => {
  const card = await make({});
  assert.deepEqual(card.hassStub.eventsCalls()[0], { type: "area_events/events", radius_miles: 25 });
  const pending = await make({}, { reply: () => ({ state: "pending", connected: true, report: [], sources: {} }) });
  assert.match(text(byClass(pending.shadowRoot, "notice")[0]), /Finding events near your Home Assistant home location/);
});

test("a failure shows an error, never demo events", async () => {
  const hass = fakeHass();
  hass.fail = { code: "not_configured", message: "The Area Events integration is not set up" };
  const { shadowRoot: root } = await make({}, { hass });
  assert.equal(byClass(root, "event").length, 0);
  assert.match(text(root), /Events are not available: The Area Events integration is not set up/);
  assert.match(text(byClass(root, "sticky-header")[0]), /Events unavailable/);
});

test("discovery states are told apart: finding, not reachable, nothing found, unsupported sources", async () => {
  const pending = await make({ area: "Austin, TX" }, { reply: () => ({ state: "discovering", connected: true, report: [], sources: {} }) });
  assert.match(text(byClass(pending.shadowRoot, "notice")[0]), /Finding events near Austin, TX/);

  const down = await make({}, { reply: () => ({ state: "unavailable", connected: false, message: "connection refused", report: [], sources: {} }) });
  assert.match(text(byClass(down.shadowRoot, "notice")[0]), /app is not reachable \(connection refused\)/);

  const failed = await make({ area: "Nowhere" }, { reply: () => ({ state: "error", connected: true, message: "could not resolve location 'Nowhere'", report: [], sources: {} }) });
  assert.match(text(byClass(failed.shadowRoot, "notice")[0]), /could not be found for Nowhere: could not resolve location/);

  const report = [{ url: "https://blog.example", status: "unsupported", reason: "no adapter reads this website" }, { url: "https://calendar.utexas.edu", status: "supported" }];
  const partial = await make({ sources: ["https://blog.example"] }, { reply: () => answer(makeFeed(), { report }) });
  const notice = text(byClass(partial.shadowRoot, "notice")[0]);
  assert.match(notice, /Source not supported: https:\/\/blog\.example \(no adapter reads this website\)/);
  assert.doesNotMatch(notice, /utexas/);
  assert.equal(byClass(partial.shadowRoot, "event").length, 3);
});

test("a refresh failure keeps the earlier events and says so", async () => {
  const card = await make({});
  card.hassStub.fail = { code: "unknown_error", message: "offline" };
  await pollAgain(card);
  assert.match(text(byClass(card.shadowRoot, "sticky-header")[0]), /Update failed/);
  assert.equal(byClass(card.shadowRoot, "event").length, 3);
  assert.match(text(byClass(card.shadowRoot, "notice")[0]), /Showing earlier data/);
});

test("polling is quick while discovering and relaxed once ready", async () => {
  const delays = [];
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => { delays.push(ms); return realSetTimeout(fn, 0 && ms, ...rest); };
  try {
    const hass = fakeHass({ reply: () => ({ state: "discovering", connected: true, report: [], sources: {} }) });
    const card = new CardClass();
    card.setConfig({});
    card.hass = hass;
    await wait();
    hass.fail = { code: "invalid_profile", message: "bad" };
    await wait();
    card.disconnectedCallback();
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
  assert.ok(delays.includes(10000), "10 s while discovering");
  assert.ok(delays.includes(300000), "5 min after a permanent error");
});

test("inline events keep working and Verified only when verified is true", async () => {
  const { shadowRoot: root } = await make({ demo: true, events: [{ title: "A", verified: true }, { title: "B" }] });
  const out = text(root);
  assert.match(out, /DEMONSTRATION DATA/);
  assert.match(out, /Verified/);
  assert.match(out, /Unverified/);
});

test("inline events never ask the integration", async () => {
  const card = await make({ events: [{ title: "A" }] });
  assert.equal(card.hassStub.eventsCalls().length, 0);
});

test("build identifier stays visible", async () => {
  const { shadowRoot: root } = await make({});
  assert.match(text(byClass(root, "footer")[0]), /__AREA_EVENTS_CARD_BUILD__/);
});

// --- configuration owned by the card ---------------------------------------------------------

const titles = (card) => byClass(card.shadowRoot, "event").map((e) => text(byClass(e, "title")[0]));

test("the configuration is validated with clear messages", () => {
  const bad = [
    [{ feed: "/f.json" }, /no longer supported/], [{ entity: "sensor.e" }, /no longer supported/],
    [{ area: 5 }, /'area'/], [{ area: " " }, /'area'/], [{ radius_miles: 0 }, /radius_miles/], [{ radius_miles: 300 }, /radius_miles/],
    [{ sources: ["ftp://x"] }, /sources/], [{ sources: "nonsense" }, /sources/], [{ categories: ["nonsense"] }, /Unknown category/],
    [{ interests: [5] }, /interest/], [{ interests: ["Music: jazz"] }, /duplicates/], [{ sort: "random" }, /sort/],
    [{ days: 0 }, /days/], [{ max_events: 1.5 }, /max_events/], [{ columns: 9 }, /columns/], [{ show_images: "yes" }, /show_images/],
  ];
  for (const [config, message] of bad) assert.throws(() => new CardClass().setConfig(config), message, JSON.stringify(config));
  const settings = readSettings({ area: " Austin, TX ", radius_miles: "40", sources: "https://a.example\nhttps://b.example", categories: ["Music", "food-drink"],
    interests: ["Board games: board game, tabletop"], exclude: "Politics\n Gala", days: 14, max_events: 12, sort: "verified", columns: 2, show_images: false });
  assert.equal(settings.area, "Austin, TX");
  assert.equal(settings.radius, 40);
  assert.deepEqual(settings.sources, ["https://a.example", "https://b.example"]);
  assert.deepEqual([...settings.categories], ["music", "food-drink"]);
  assert.deepEqual(settings.interests.map((c) => [c.name, c.keywords]), [["Board games", ["board game", "tabletop"]]]);
  assert.deepEqual(settings.exclude, ["politics", "gala"]);
});

test("exclude, date range, maximum and sorting apply to the shown events", async () => {
  const feed = makeFeed({ events: [event("Zeta Concert", 1, { categories: ["Music"] }), event("Alpha Politics Rally", 2), event("Mid Market", 4, { verified: true }), event("Far Away Fair", 30)] });
  assert.deepEqual(titles(await make({}, { feed })), ["Zeta Concert", "Alpha Politics Rally", "Mid Market", "Far Away Fair"]);
  assert.deepEqual(titles(await make({ days: 7 }, { feed })), ["Zeta Concert", "Alpha Politics Rally", "Mid Market"]);
  assert.deepEqual(titles(await make({ exclude: ["politics"] }, { feed })), ["Zeta Concert", "Mid Market", "Far Away Fair"]);
  assert.deepEqual(titles(await make({ max_events: 2 }, { feed })), ["Zeta Concert", "Alpha Politics Rally"]);
  assert.deepEqual(titles(await make({ sort: "title" }, { feed })), ["Alpha Politics Rally", "Far Away Fair", "Mid Market", "Zeta Concert"]);
  assert.deepEqual(titles(await make({ sort: "verified", max_events: 2 }, { feed })), ["Mid Market", "Zeta Concert"]);
});

test("categories limit the events and the chips; interests add topics that work as filters", async () => {
  const feed = makeFeed({ events: [event("Jazz Night", 1), event("Board Game Cafe", 2, { description: "tabletop evening" }), event("Quarterly meeting", 3)] });
  assert.deepEqual(titles(await make({ categories: ["music"] }, { feed })), ["Jazz Night"]);
  const both = await make({ categories: ["music"], interests: ["Board games: tabletop"] }, { feed });
  assert.deepEqual(titles(both), ["Jazz Night", "Board Game Cafe"]);
  openFilters(both.shadowRoot);
  const names = byClass(both.shadowRoot, "chip").map((c) => text(c).replace(/[\s|\d]+$/g, "").replace(/^✓ /, "").trim());
  assert.deepEqual(names, ["All", "Music", "Board games"]);
  click(chip(both.shadowRoot, "Board games"));
  assert.deepEqual(visibleTitles(both.shadowRoot), ["Board Game Cafe"]);
  assert.deepEqual(stored(both).selected.length, 1); // a YAML interest can be chosen and is remembered
});

test("layout settings: images, filters and columns", async () => {
  const feed = makeFeed({ events: [event("With image", 1, { image: "https://img.example/a.jpg" })] });
  assert.equal((await make({}, { feed })).shadowRoot.children.length > 0, true);
  const before = created.img;
  await make({}, { feed });
  assert.equal(created.img - before, 1);
  const noImages = created.img;
  await make({ show_images: false }, { feed });
  assert.equal(created.img, noImages);
  assert.equal(byClass((await make({ show_filters: false }, { feed })).shadowRoot, "filter-bar").length, 0);
  assert.equal(byClass((await make({}, { feed })).shadowRoot, "filter-bar").length, 1);
  const styled = await make({ columns: 2 }, { feed });
  assert.match(styled.shadowRoot.children[0].textContent, /\(100% - 8px\) \/ 2/);
});

test("the visual editor round-trips the configuration", () => {
  const config = { type: "custom:area-events-card", title: "Events", area: "Austin, TX", radius_miles: 30, sources: ["https://a.example", "https://b.example"],
    interests: ["Board games: tabletop"], exclude: ["politics"], categories: ["music"], days: 7, max_events: 10, sort: "title", show_images: false };
  const form = configToEditor(config);
  assert.equal(form.sources, "https://a.example\nhttps://b.example");
  assert.equal(form.interests, "Board games: tabletop");
  assert.deepEqual(editorToConfig(config, form), config);
  const edited = editorToConfig(config, { ...form, area: "", sources: "", exclude: "a\n\n b ", days: undefined });
  assert.equal("area" in edited, false);
  assert.equal("sources" in edited, false);
  assert.equal("days" in edited, false);
  assert.deepEqual(edited.exclude, ["a", "b"]);
  assert.equal(edited.type, "custom:area-events-card");
  readSettings(edited); // what the editor produces is always a valid configuration
  const names = EDITOR_SCHEMA.flatMap((item) => item.schema ? item.schema.flatMap((inner) => inner.name) : item.name);
  for (const key of ["title", "area", "radius_miles", "sources", "categories", "interests", "exclude", "days", "max_events", "sort", "columns", "show_images", "show_filters"]) {
    assert.ok(names.includes(key), key);
  }
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
  const { shadowRoot: root } = await make({});
  assert.deepEqual(visibleTitles(root), ["Confirmed Jazz Night", "Corroborated Farmers Market", "Mystery Gathering"]);
  assert.match(text(byClass(root, "filter-summary")[0]), /Showing all categories/);
});

test("selecting categories filters client side; multiple selection; Other; Show All; no new images or tiles", async () => {
  const card = await make({});
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
  const card = await make({});
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
  const card = await make({});
  openFilters(card.shadowRoot);
  click(chip(card.shadowRoot, "Comedy"));
  assert.equal(visibleTitles(card.shadowRoot).length, 0);
  assert.equal(byClass(card.shadowRoot, "no-match")[0].hidden, false);
});

test("filtering makes no requests", async () => {
  const card = await make({});
  const before = card.hassStub.calls.length;
  openFilters(card.shadowRoot);
  click(chip(card.shadowRoot, "Music"));
  click(chip(card.shadowRoot, "Markets"));
  assert.equal(card.hassStub.calls.filter((m) => m.type === "area_events/events").length, 1);
  assert.ok(card.hassStub.calls.length >= before);
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
  const card = await make({});
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
  const card = await make({});
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
  const first = await make({ title: "Events" });
  openFilters(first.shadowRoot);
  createCustom(first.shadowRoot, "Weekend", "mystery");
  click(chip(first.shadowRoot, "Weekend"));
  click(chip(first.shadowRoot, "Music"));

  const second = await make({ title: "Events" });
  assert.deepEqual(visibleTitles(second.shadowRoot), ["Confirmed Jazz Night", "Mystery Gathering"]);
  assert.match(text(byClass(second.shadowRoot, "filter-summary")[0]), /Music, Weekend/);
});

test("preferences are isolated per card instance and per dashboard path", async () => {
  const a = await make({ title: "A" });
  const b = await make({ title: "B" });
  const c = await make({ title: "A", id: "left" });
  const d = await make({ title: "A", id: "right" });
  assert.equal(new Set([a, b, c, d].map((card) => card._prefsKey)).size, 4);

  openFilters(a.shadowRoot);
  click(chip(a.shadowRoot, "Music"));
  const a2 = await make({ title: "A" });
  const b2 = await make({ title: "B" });
  assert.equal(visibleTitles(a2.shadowRoot).length, 1);
  assert.equal(visibleTitles(b2.shadowRoot).length, 3);

  const before = prefsKey({ title: "A" });
  window.location.pathname = "/other-dashboard/view";
  assert.notEqual(prefsKey({ title: "A" }), before);
  window.location.pathname = "/lovelace/events";
});

test("corrupt or hostile stored preferences are ignored", async () => {
  const key = prefsKey({});
  for (const bad of ["{oops", "[]", "null", JSON.stringify({ selected: "music" })]) {
    window.localStorage.setItem(key, bad);
    const card = await make({});
    assert.equal(visibleTitles(card.shadowRoot).length, 3);
  }
  assert.deepEqual(normalizePrefs({ selected: ["music", "nope", "music"], custom: [{ id: "bad id", name: "x" }, { id: "c_abc", name: " Ok ", keywords: [" A ", 5] }] }),
    { v: 1, selected: ["music"], custom: [{ id: "c_abc", name: "Ok", keywords: ["a"] }] });
});

test("preferences sync to Home Assistant user data and load from it", async () => {
  const calls = [];
  const remote = new Map();
  const hass = fakeHass({
    onWS: (msg) => {
      calls.push(msg);
      if (msg.type === "frontend/get_user_data") return { value: remote.get(msg.key) ?? null };
      remote.set(msg.key, msg.value);
      return null;
    },
  });
  const card = await make({}, { hass });
  openFilters(card.shadowRoot);
  click(chip(card.shadowRoot, "Music"));
  await wait(400);
  const save = calls.find((c) => c.type === "frontend/set_user_data");
  assert.ok(save && save.key.startsWith("area-events-card:prefs:v1:"));
  assert.deepEqual(save.value.selected, ["music"]);

  // another browser (empty localStorage) gets the saved choice from the HA user data
  window.localStorage = new Storage();
  const other = await make({}, { hass });
  assert.deepEqual(visibleTitles(other.shadowRoot), ["Confirmed Jazz Night"]);

  // HA unreachable: local storage still works
  window.localStorage = new Storage();
  const offline = await make({}, { hass: fakeHass({ onWS: () => { throw new Error("down"); } }) });
  openFilters(offline.shadowRoot);
  click(chip(offline.shadowRoot, "Markets"));
  assert.deepEqual(stored(offline).selected, ["markets"]);
});

test("existing local preferences migrate to HA user data when none exist there", async () => {
  const key = prefsKey({});
  window.localStorage.setItem(key, JSON.stringify({ v: 1, selected: ["music"], custom: [] }));
  const saved = [];
  const hass = fakeHass({ onWS: (m) => { if (m.type === "frontend/set_user_data") saved.push(m); return m.type === "frontend/get_user_data" ? { value: null } : null; } });
  await make({}, { hass });
  await wait(20);
  assert.equal(saved.length, 1);
  assert.deepEqual(saved[0].value.selected, ["music"]);
});

// --- header ---------------------------------------------------------------------------

test("header shows title, the app's own update time, the place, counts and source count", async () => {
  const generated = iso(-35 * 60 * 1000);
  const card = await make({ title: "San Diego Events", radius_miles: 25 }, { feed: makeFeed({ generated_at: generated }) });
  const header = text(byClass(card.shadowRoot, "sticky-header")[0]);
  assert.match(header, /San Diego Events/);
  assert.match(header, /Updated .*\(35 min ago\)/);
  assert.ok(header.includes(card._formatDate(generated)));
  assert.match(header, /San Diego, CA · 25 mi/);
  assert.match(header, /3 events/);
  assert.match(header, /2 sources/);
  assert.doesNotMatch(header, /Stale|failed|next|Next/);
});

test("header time comes from generated_at, never the page-load time", async () => {
  const old = new Date("2026-01-02T03:04:00-08:00").toISOString();
  const card = await make({}, { feed: makeFeed({ generated_at: old, events: [event("Future", 5)] }) });
  const header = text(byClass(card.shadowRoot, "sticky-header")[0]);
  assert.ok(header.includes(card._formatDate(old)));
  assert.match(header, /Stale/);
  assert.match(header, /d ago/);
});

test("header marks stale data and failing sources", async () => {
  const stale = await make({}, { feed: makeFeed({ generated_at: iso(-4 * 3600 * 1000), sources: { a: { ok: true }, b: { ok: false } } }) });
  const header = text(byClass(stale.shadowRoot, "sticky-header")[0]);
  assert.match(header, /Stale/);
  assert.match(header, /1 of 2 sources ok/);
  assert.match(header, /1 source failing/);
});

test("an unchanged answer does not rebuild tiles or images", async () => {
  const card = await make({});
  const tiles = byClass(card.shadowRoot, "event");
  const images = created.img;
  await pollAgain(card);
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

// --- build -------------------------------------------------------------------------------

test("the card the integration serves is the current source, stamped", async () => {
  const { readFileSync } = await import("node:fs");
  const { createHash } = await import("node:crypto");
  const source = readFileSync(new URL("../src/area-events-card.js", import.meta.url), "utf8");
  const { version } = JSON.parse(readFileSync(new URL("../custom_components/area_events/manifest.json", import.meta.url), "utf8"));
  const identifier = `AREA-EVENTS ${version}-${createHash("sha1").update(source).digest("hex").slice(0, 8)}`;
  const served = readFileSync(new URL("../custom_components/area_events/frontend/area-events-card.js", import.meta.url), "utf8");
  assert.equal(served, source.replaceAll("__AREA_EVENTS_CARD_BUILD__", identifier), "run npm run build and commit the result");
});
