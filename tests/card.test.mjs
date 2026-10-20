// Run: node --test tests/   (no dependencies; a tiny DOM stub stands in for the browser)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

class El {
  constructor(tag) { Object.assign(this, { tag, children: [], className: "", textContent: "", attrs: {}, listeners: {} }); }
  append(...n) { this.children.push(...n); }
  prepend(...n) { this.children.unshift(...n); }
  remove() { this.removed = true; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(e, f) { this.listeners[e] = f; }
  replaceChildren() { this.children = []; }
  attachShadow() { this.shadowRoot = new El("root"); return this.shadowRoot; }
  get isConnected() { return true; }
}
let CardClass;
globalThis.HTMLElement = El;
globalThis.document = { createElement: (t) => new El(t), createTextNode: (text) => ({ text }) };
globalThis.customElements = { get: () => undefined, define: (_n, c) => { CardClass = c; } };
globalThis.window = {};
await import("../src/area-events-card.js");

const feed = JSON.parse(readFileSync(new URL("../data/events.json", import.meta.url), "utf8"));
const text = (el) => [el.textContent, el.text, ...(el.children || []).map(text)].filter(Boolean).join(" | ");
const find = (el, cls) => (el.className.split(" ").includes(cls) ? [el] : []).concat(...el.children.map((c) => (c.children ? find(c, cls) : [])));
const make = async (config, fetchImpl) => {
  globalThis.fetch = fetchImpl;
  const card = new CardClass();
  card.setConfig(config);
  await new Promise((r) => setTimeout(r, 10));
  card.disconnectedCallback();
  return card.shadowRoot;
};
const okFetch = async () => ({ ok: true, json: async () => feed });

test("feed renders real events, corroborated not verified, with source links", async () => {
  const root = await make({ feed: "/local/area-events-card/events.json" }, okFetch);
  const tiles = find(root, "event");
  assert.ok(tiles.length > 0);
  const out = text(root);
  assert.match(out, /Source corroborated/);
  assert.doesNotMatch(out, /\bVerified\b/);
  assert.doesNotMatch(out, /DEMONSTRATION/);
  assert.ok(find(root, "source").every((a) => a.href.startsWith("https://www.kpbs.org/")));
});

test("feed fetch bypasses the cache", async () => {
  let seen;
  await make({ feed: "/f.json" }, async (url, opts) => { seen = [url, opts]; return okFetch(); });
  assert.match(seen[0], /^\/f\.json\?ts=\d+$/);
  assert.equal(seen[1].cache, "no-store");
});

test("feed failure shows an error, never demo events", async () => {
  const root = await make({ feed: "/f.json" }, async () => { throw new Error("Failed to fetch"); });
  assert.equal(find(root, "event").length, 0);
  assert.match(text(root), /Could not load events feed: Failed to fetch/);
});

test("inline events keep working and Verified only when verified is true", async () => {
  const root = await make({ demo: true, events: [{ title: "A", verified: true }, { title: "B" }] }, okFetch);
  const out = text(root);
  assert.match(out, /DEMONSTRATION DATA/);
  assert.match(out, /Verified/);
  assert.match(out, /Unverified/);
});
