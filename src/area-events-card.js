/**
 * Area Events Card - Home Assistant custom Lovelace card.
 *
 * Shows the events the Discovery Service has found for the discovery area this card's `area` names. The card talks only to
 * Home Assistant, over the signed-in user's own WebSocket session (`area_events/subscribe`: the current view of the area, then a
 * new one whenever it changes); it holds no secret and contacts no other server. Which areas and sources exist is decided in the
 * Discovery Service; this card only chooses what to show of one area.
 * `events` (an inline list) shows fixed events instead, for demonstrations; see examples/demo-card.yaml.
 * Everything about what is shown (categories, interests, exclusions, date range, sorting, layout) is YAML or the
 * visual editor; the filter chips are the viewer's own choices, saved per card instance (see "Preferences").
 */

const AREA_EVENTS_CARD_BUILD = "__AREA_EVENTS_CARD_BUILD__";
const CARD_TAG = "area-events-card";

// Diagnostics for "Custom element doesn't exist": window.__AREA_EVENTS_CARD_LOAD__ is missing when this module was never
// evaluated (the request failed or was never made), has defined: false until the card is registered (that waits for
// <home-assistant>, see "Registration" below) and defined: true once it is.
globalThis.__AREA_EVENTS_CARD_LOAD__ = { build: AREA_EVENTS_CARD_BUILD, started: Date.now(), defined: false };

/*
 * Theme: colors come only from Home Assistant theme variables, so the card
 * follows light/dark theme changes through CSS inheritance (no JS needed).
 * Tiles are a tint of the primary text color over the card background, which
 * stays visible even in themes (e.g. Graphite E-ink Dark) where card, page
 * and secondary backgrounds are identical. Verification, category and filter
 * state are always conveyed by icon or text, never by color alone.
 *
 * Layout: auto-fill columns (empty columns stay empty, so a tile never grows when there are few events), 220px minimum tile width, capped at four, so the
 * column count follows the card width (not the viewport). No breakpoints and
 * no font scaling; the header only rearranges using a container query on the
 * card's own width.
 *
 * Sticky header: `overflow: clip` (not hidden) on ha-card keeps the card from
 * becoming a scroll container, so the header sticks to the page's own scroll.
 */
const STYLES = `
  :host { display: block; }
  [hidden] { display: none !important; }
  ha-card { height: 100%; box-sizing: border-box; padding: 0 12px 8px; overflow: clip; }
  .wrap { container-type: inline-size; }

  .sticky-header {
    position: sticky; top: var(--header-height, 0px); z-index: 3;
    display: flex; flex-direction: column; gap: 2px;
    margin: 0 -12px 8px; padding: 10px 16px 8px;
    background: var(--ha-card-background, var(--card-background-color, var(--primary-background-color)));
    border-bottom: 1px solid var(--divider-color);
  }
  .head-title {
    font-size: var(--ha-card-header-font-size, 24px); line-height: 1.2; overflow-wrap: anywhere;
    color: var(--ha-card-header-color, var(--primary-text-color));
  }
  .head-meta {
    display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px;
    font-size: 0.875rem; color: var(--secondary-text-color);
  }
  .head-meta .updated { color: var(--primary-text-color); font-weight: min(var(--ha-font-weight-medium, 500), 600); }
  .head-meta ha-icon { --mdc-icon-size: 16px; vertical-align: -3px; }
  .badge {
    padding: 1px 8px; border-radius: 10px; font-size: 0.8125rem;
    border: 1px solid var(--warning-color, var(--divider-color)); color: var(--primary-text-color);
  }
  .badge.error { border-color: var(--error-color, var(--divider-color)); }
  @container (min-width: 560px) {
    .sticky-header { flex-direction: row; align-items: baseline; justify-content: space-between; gap: 16px; }
    .head-meta { justify-content: flex-end; }
  }

  .demo-banner {
    margin: 0 0 8px; padding: 6px 12px; border-radius: 8px;
    border: 1px solid var(--warning-color, var(--divider-color));
    color: var(--primary-text-color);
    font-size: 0.875rem; font-weight: min(var(--ha-font-weight-medium, 500), 600);
    text-align: center; overflow-wrap: anywhere;
  }

  .filters { margin: 0 0 8px; }
  .filter-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 0.875rem; }
  .filter-summary { color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
  button {
    font: inherit; font-size: 0.875rem; cursor: pointer;
    padding: 4px 10px; border-radius: 16px;
    background: transparent; color: var(--primary-text-color);
    border: 1px solid var(--divider-color);
  }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  button[aria-pressed="true"] {
    background: var(--primary-color); color: var(--text-primary-color, var(--card-background-color));
    border-color: var(--primary-color); font-weight: min(var(--ha-font-weight-medium, 500), 600);
  }
  .chip .count { opacity: 0.75; margin-left: 4px; }
  .custom-list { display: flex; flex-direction: column; gap: 4px; margin: 4px 0; }
  .custom-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 0.875rem; }
  .custom-row .name { font-weight: min(var(--ha-font-weight-medium, 500), 600); }
  .custom-row .kw { color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .editor { display: flex; flex-direction: column; gap: 6px; margin: 6px 0; max-width: 480px; }
  .editor label { display: flex; flex-direction: column; gap: 2px; font-size: 0.8125rem; color: var(--secondary-text-color); }
  input {
    font: inherit; font-size: 0.9375rem; padding: 6px 8px; border-radius: 6px; box-sizing: border-box; width: 100%;
    background: var(--card-background-color); color: var(--primary-text-color);
    border: 1px solid var(--divider-color);
  }
  .editor .error { color: var(--error-color, var(--primary-text-color)); font-size: 0.8125rem; }
  .editor .actions { display: flex; gap: 6px; }
  .no-match { padding: 12px 4px; color: var(--secondary-text-color); }
  .grid {
    display: grid; gap: 8px;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, max(220px, (100% - 3 * 8px) / 4)), 1fr));
  }
  .event {
    display: flex; flex-direction: column; min-width: 0; overflow: hidden;
    border-radius: calc(var(--ha-card-border-radius, var(--ha-border-radius-lg, 12px)) * 0.75);
    border: 1px solid var(--ha-card-border-color, var(--divider-color));
    background: var(--secondary-background-color);
    color: var(--primary-text-color);
  }
  @supports (background: color-mix(in srgb, currentColor 5%, transparent)) {
    .event {
      background: color-mix(in srgb, var(--primary-text-color, currentColor) 12%, transparent);
      border-color: transparent;
    }
  }
  .image {
    position: relative; aspect-ratio: 16 / 9; overflow: hidden;
    display: flex; align-items: center; justify-content: center;
    background-color: color-mix(in srgb, var(--primary-text-color, currentColor) 10%, transparent);
    color: var(--secondary-text-color);
  }
  .image img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity 0.25s; }
  .image img.loaded { opacity: 1; }
  .art { position: absolute; inset: 0; --ae-accent: hsl(210 60% 55%); --ae-bg: color-mix(in srgb, var(--ae-accent) 16%, var(--card-background-color, Canvas)); }
  .art svg { width: 100%; height: 100%; display: block; }
  .art .bg, .art .e { fill: var(--ae-bg); }
  .art .a { fill: var(--ae-accent); }
  .art .b { fill: color-mix(in srgb, var(--ae-accent) 38%, transparent); }
  .art .c { fill: color-mix(in srgb, var(--primary-text-color, CanvasText) 62%, transparent); }
  .art .s, .art .sb, .art .sc, .art .se { fill: none; stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; }
  .art .s { stroke: var(--ae-accent); }
  .art .sb { stroke: color-mix(in srgb, var(--ae-accent) 45%, transparent); }
  .art .sc { stroke: color-mix(in srgb, var(--primary-text-color, CanvasText) 55%, transparent); stroke-width: 3; }
  .art .se { stroke: var(--ae-bg); stroke-width: 5; }
  .art .p1 { fill: hsl(8 78% 60%); } .art .p2 { fill: hsl(46 92% 58%); } .art .p3 { fill: hsl(205 75% 56%); } .art .p4 { fill: hsl(140 52% 48%); }
  .art.music { --ae-accent: hsl(285 58% 58%); } .art.theater { --ae-accent: hsl(345 62% 56%); } .art.festival { --ae-accent: hsl(28 86% 54%); }
  .art.food { --ae-accent: hsl(12 74% 56%); } .art.sports { --ae-accent: hsl(150 52% 42%); } .art.arts { --ae-accent: hsl(262 52% 60%); }
  .art.science { --ae-accent: hsl(195 70% 46%); } .art.family { --ae-accent: hsl(42 88% 50%); } .art.community { --ae-accent: hsl(172 52% 42%); }
  .art.outdoors { --ae-accent: hsl(128 46% 42%); } .art.general { --ae-accent: hsl(220 52% 58%); }
  .art.farmers_market { --ae-accent: hsl(100 46% 42%); } .art.carnival { --ae-accent: hsl(340 70% 55%); } .art.car_show { --ae-accent: hsl(358 68% 50%); }
  .art.halloween { --ae-accent: hsl(26 90% 52%); } .art.christmas { --ae-accent: hsl(145 48% 38%); } .art.street_festival { --ae-accent: hsl(318 58% 54%); }
  .pin { position: absolute; left: 50%; top: 50%; width: 30px; height: 40px; transform: translate(-50%, -100%); pointer-events: none; filter: drop-shadow(0 2px 2px rgba(0, 0, 0, 0.45)); }
  .pin svg { width: 100%; height: 100%; display: block; }
  .venue-chip {
    position: absolute; padding: 1px 8px; border-radius: 12px; font-size: 0.6875rem; line-height: 1.5; box-sizing: border-box; max-width: calc(100% - 16px);
    background: rgba(37, 40, 45, 0.86); color: rgb(232 234 238); overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  }
  .credit-button {
    position: absolute; left: 8px; bottom: 8px; width: 22px; height: 22px; padding: 0; margin: 0; border-radius: 50%; box-sizing: border-box; cursor: pointer;
    display: flex; align-items: center; justify-content: center; font: inherit;
    background: rgba(37, 40, 45, 0.72); color: rgb(232 234 238); border: 1px solid rgba(255, 255, 255, 0.35);
  }
  .credit-button::after { content: ""; position: absolute; inset: -11px; }
  .credit-button:hover, .credit-button[aria-expanded="true"] { background: rgba(37, 40, 45, 0.94); }
  .credit-button:focus-visible, .media-credit a:focus-visible { outline: 2px solid rgb(240 138 36); outline-offset: 2px; }
  .credit-button svg { width: 14px; height: 14px; display: block; }
  .media-credit {
    position: absolute; left: 8px; bottom: 38px; max-width: calc(100% - 16px); max-height: calc(100% - 52px); overflow: auto; box-sizing: border-box;
    padding: 6px 10px; border-radius: 8px; font-size: 0.75rem; line-height: 1.45; background: rgba(37, 40, 45, 0.96); color: rgb(232 234 238);
  }
  .media-credit a { color: inherit; text-decoration: underline; }
  .media-credit .note { display: block; opacity: 0.8; }
  .venue-chip { right: 8px; top: 8px; max-width: calc(100% - 90px); font-weight: 600; }
  .art-tag.media-badge { background: rgba(37, 40, 45, 0.88); color: rgb(240 138 36); border-color: rgba(255, 255, 255, 0.22); font-weight: 700; letter-spacing: 0.04em; }
  .art-tag {
    position: absolute; right: 8px; bottom: 8px; padding: 1px 8px; border-radius: 12px; font-size: 0.75rem; box-sizing: border-box;
    background: var(--card-background-color, var(--primary-background-color)); border: 1px solid var(--divider-color); color: var(--secondary-text-color);
  }
  .category {
    position: absolute; top: 8px; left: 8px; max-width: calc(100% - 16px);
    padding: 2px 8px; border-radius: 12px; box-sizing: border-box;
    background: var(--card-background-color, var(--primary-background-color));
    border: 1px solid var(--divider-color);
    color: var(--primary-text-color);
    font-size: 0.8125rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .body { display: flex; flex-direction: column; gap: 4px; padding: 12px; min-width: 0; }
  .title {
    font-size: 1rem; font-weight: min(var(--ha-font-weight-medium, 500), 600);
    color: var(--primary-text-color); overflow-wrap: anywhere;
  }
  .meta {
    display: flex; align-items: flex-start; gap: 6px; min-width: 0; line-height: 1.4;
    font-size: 0.875rem; color: var(--secondary-text-color); overflow-wrap: anywhere;
  }
  /* every icon sits in a box one text line tall, centred on that line: it stays centred on the first line when the text wraps */
  .meta ha-icon, .verify ha-icon { --mdc-icon-size: 16px; flex: none; width: 16px; height: 1.4em; display: inline-flex; align-items: center; justify-content: center; }
  .meta > span, .verify > span { min-width: 0; }
  .verify {
    display: flex; align-items: flex-start; gap: 6px; line-height: 1.4;
    margin-top: 4px; font-size: 0.8125rem;
    font-weight: min(var(--ha-font-weight-medium, 500), 600);
  }
  .verify.verified { color: var(--success-color, var(--primary-text-color)); }
  .verify.corroborated { color: var(--secondary-text-color); }
  .verify.unverified { color: var(--secondary-text-color); }
  .description {
    font-size: 0.875rem; color: var(--secondary-text-color); overflow-wrap: anywhere;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  .source { margin-top: 4px; font-size: 0.8125rem; color: var(--primary-color); overflow-wrap: anywhere; }
  .notice {
    margin: 0 0 8px; padding: 6px 12px; border-radius: 8px; font-size: 0.875rem;
    border: 1px solid var(--divider-color); color: var(--primary-text-color); overflow-wrap: anywhere;
  }
  .notice summary { cursor: pointer; }
  .notice .detail-line { margin-top: 4px; color: var(--secondary-text-color); }
  .empty { padding: 16px 4px; color: var(--secondary-text-color); }
  .progress {
    display: flex; flex-direction: column; gap: 8px; margin: 0 0 8px; padding: 14px 16px; border-radius: 12px; box-sizing: border-box;
    border: 1px solid var(--divider-color); color: var(--primary-text-color);
    background: color-mix(in srgb, var(--primary-text-color, currentColor) 6%, transparent);
  }
  .progress .row { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .progress ha-icon { --mdc-icon-size: 32px; flex: none; color: var(--primary-color); }
  .progress .text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
  .progress .headline { font-weight: min(var(--ha-font-weight-medium, 500), 600); overflow-wrap: anywhere; }
  .progress .detail { font-size: 0.875rem; color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .progress.compact { padding: 6px 12px; gap: 6px; }
  .progress.compact ha-icon { --mdc-icon-size: 20px; }
  .progress.compact .headline { font-size: 0.875rem; }
  .spinner {
    flex: none; width: 20px; height: 20px; box-sizing: border-box; border-radius: 50%;
    border: 3px solid color-mix(in srgb, var(--primary-text-color, currentColor) 15%, transparent); border-top-color: var(--primary-color);
    animation: ae-spin 0.9s linear infinite;
  }
  .progress.compact .spinner { width: 14px; height: 14px; border-width: 2px; }
  .bar { position: relative; height: 4px; border-radius: 2px; overflow: hidden; background: color-mix(in srgb, var(--primary-text-color, currentColor) 12%, transparent); }
  .bar::before {
    content: ""; position: absolute; top: 0; bottom: 0; left: 0; width: 35%; border-radius: 2px; background: var(--primary-color);
    animation: ae-slide 1.4s ease-in-out infinite;
  }
  @keyframes ae-spin { to { transform: rotate(360deg); } }
  @keyframes ae-slide { from { transform: translateX(-100%); } to { transform: translateX(300%); } }
  @media (prefers-reduced-motion: reduce) {
    .image img { transition: none; }
    .spinner { animation: none; }
    .bar::before { animation: none; width: 40%; }
  }
  /* Build tag, bottom-left, below the grid. A diagnostic label, not content. */
  .footer {
    margin-top: 8px; font-size: 0.8125rem; line-height: 1.25; letter-spacing: 0.02em;
    color: var(--secondary-text-color); overflow-wrap: anywhere;
  }
`;

const POLL_READY_MS = 5 * 60 * 1000;
const POLL_RETRY_MS = 30 * 1000;
const FEED_STALE_MS = 12 * 60 * 60 * 1000; // each source is read about hourly; quiet ones less often
const SAVE_DEBOUNCE_MS = 300;
const AGE_TICK_MS = 60 * 1000;
const EDITOR_DEBOUNCE_MS = 700;
// In the dashboard editor every keystroke rebuilds the preview card; wait until the settings stop changing before
// asking for a search, so half-typed areas never become searches of their own.
const PREVIEW_DELAY_MS = 1500;
const WS_SUBSCRIBE = "area_events/subscribe";
// Errors the user must fix; polling faster would not help.
const ACTIVE_PHASES = ["queued", "searching", "processing"];
const PERMANENT_ERRORS = new Set(["invalid_profile", "rejected", "not_configured", "unauthorized", "unknown_area"]);

/*
 * Categories. Defaults are fixed; users add custom ones. An event matches a
 * category when its title, description or source-provided categories contain
 * one of the category's keywords as whole words (a source category equal to the
 * category name also matches). "Other" holds events that match no category.
 */
const OTHER_ID = "other";
const MAX_CUSTOM = 20;
const MAX_KEYWORDS = 40;
const DEFAULT_CATEGORIES = [
  ["music", "Music", "music, concert, band, jazz, blues, symphony, orchestra, choir, singer, songwriter, recital, dj, karaoke, rock, hip hop, folk, opera"],
  ["festivals", "Festivals", "festival, fest, fair, carnival, parade, celebration, fiesta, block party"],
  ["food-drink", "Food & Drink", "food, drink, drinks, beer, wine, brewery, tasting, restaurant, dinner, brunch, cocktail, happy hour, chef, taco, culinary, cafe, coffee, food drinks"],
  ["arts", "Arts", "art, arts, gallery, exhibit, exhibition, painting, sculpture, craft, crafts, photography, pottery, mural, arts crafts"],
  ["sports", "Sports", "sports, race, marathon, 5k, soccer, football, baseball, basketball, hockey, volleyball, tennis, golf, surfing, triathlon, fitness"],
  ["science", "Science", "science, astronomy, planetarium, telescope, laboratory, biology, physics, chemistry, research, aquarium, stem, outer space"],
  ["technology", "Technology", "technology, tech, software, coding, programming, robotics, startup, cyber, hackathon, artificial intelligence"],
  ["family", "Family", "family, kids, children, child, toddler, youth, storytime, story time, all ages, teens"],
  ["community", "Community", "community, neighborhood, volunteer, cleanup, charity, fundraiser, civic, town hall, nonprofit"],
  ["outdoors", "Outdoors", "outdoor, outdoors, hike, hiking, trail, park, beach, garden, nature, camping, kayak, wildlife, birding"],
  ["education", "Education", "education, class, classes, workshop, lecture, seminar, course, lesson, learn, learning, library, training, tutorial"],
  ["craft-fair", "Craft Fair", "craft fair, craft show, craft sale, craft bazaar, craft market, crafts fair, crafts show, handmade market, handmade fair, handmade show, artisan fair, artisan show, artisan craft, artisans fair, makers market, makers fair, maker faire, holiday craft"],
  ["theater", "Theater", "theater, theatre, broadway, drama, ballet, dance, cabaret, shakespeare, performing arts"],
  ["comedy", "Comedy", "comedy, comedian, stand up, standup, improv, open mic, humor"],
  ["markets", "Markets", "market, markets, farmers, bazaar, swap meet, flea, vendor, artisan, artisans, mercato"],
].map(([id, name, words]) => ({ id, name, keywords: splitKeywords(words) }));
DEFAULT_CATEGORIES.push({ id: OTHER_ID, name: "Other", keywords: [] });


function normalizeText(text) {
  return ` ${String(text || "").toLowerCase().replace(/&/g, " and ").replace(/[^a-z0-9]+/g, " ").trim()} `;
}

// Comma separated phrases, lower-cased and de-duplicated (also used for user input).
function splitKeywords(text) {
  return [...new Set(String(text || "").split(",").map((k) => k.trim().toLowerCase()).filter(Boolean))].slice(0, MAX_KEYWORDS);
}

function matchesKeyword(haystack, keyword) {
  const needle = normalizeText(keyword);
  if (needle.trim() === "") return false;
  return haystack.includes(needle) || haystack.includes(needle.trimEnd() + "s ");
}

/** Ids of every category (default and custom) an event belongs to; OTHER_ID if none. */
function categoriesFor(event, customCategories = []) {
  const sourceCategories = Array.isArray(event.categories) ? event.categories : event.category ? [event.category] : [];
  const haystack = normalizeText([event.title, event.description, ...sourceCategories].join(" . "));
  const sourceNames = new Set(sourceCategories.map((c) => normalizeText(c)));
  const matched = new Set();
  for (const category of [...DEFAULT_CATEGORIES, ...customCategories]) {
    if (category.id === OTHER_ID) continue;
    const keywords = category.keywords.concat(category.id.startsWith("c_") ? [category.name] : []);
    if (sourceNames.has(normalizeText(category.name)) || keywords.some((k) => matchesKeyword(haystack, k))) {
      matched.add(category.id);
    }
  }
  if (matched.size === 0) matched.add(OTHER_ID);
  return matched;
}

/** Validate saved preferences; returns a clean object or null if unusable. `extraIds` are the YAML interests. */
function normalizePrefs(raw, extraIds = []) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const custom = [];
  for (const item of Array.isArray(raw.custom) ? raw.custom : []) {
    const name = typeof item?.name === "string" ? item.name.trim().slice(0, 40) : "";
    const id = typeof item?.id === "string" && /^c_[a-z0-9]{3,20}$/.test(item.id) ? item.id : "";
    if (!name || !id || custom.some((c) => c.id === id) || custom.length >= MAX_CUSTOM) continue;
    const keywords = Array.isArray(item.keywords) ? item.keywords.filter((k) => typeof k === "string").map((k) => k.trim().toLowerCase().slice(0, 40)).filter(Boolean).slice(0, MAX_KEYWORDS) : [];
    custom.push({ id, name, keywords });
  }
  const known = new Set([...DEFAULT_CATEGORIES, ...custom].map((c) => c.id).concat(extraIds));
  const selected = (Array.isArray(raw.selected) ? raw.selected : []).filter((id) => typeof id === "string" && known.has(id));
  return { v: 1, selected: [...new Set(selected)], custom };
}

/*
 * Configuration. Everything is optional:
 *
 *   title, id                  header text; `id` keeps saved filters apart for identical cards on one dashboard
 *   area                       the name (or key) of a discovery area configured in the Discovery Service; required
 *   radius_miles, sources      no longer used (areas and their sources belong to the Discovery Service); accepted and ignored
 *   categories                 limit events to these built-in categories (ids or names); omit for all
 *   interests                  extra topics: "Name: keyword, keyword" (or {name, keywords})
 *   exclude                    topics to hide (any event mentioning one is dropped)
 *   days, max_events, sort     only events starting within this many days; at most this many; date | title | verified
 *   columns, show_images, show_filters   layout
 */
const SORTS = ["date", "title", "verified"];
const MAX_INTERESTS = 20;
const MAX_EXCLUDE = 30;
const MAX_SOURCES = 10;

function listOf(value, what) {
  if (value === undefined || value === null || value === "") return [];
  const items = Array.isArray(value) ? value : String(value).split("\n");
  if (!items.every((item) => typeof item === "string")) throw new Error(`'${what}' must be a list of text.`);
  return items.map((item) => item.trim()).filter(Boolean);
}

function numberIn(config, key, low, high, whole) {
  const value = config[key];
  if (value === undefined || value === null || value === "") return undefined;
  const number = Number(value);
  if (!Number.isFinite(number) || number < low || number > high || (whole && !Number.isInteger(number))) {
    throw new Error(`'${key}' must be ${whole ? "a whole number" : "a number"} from ${low} to ${high}.`);
  }
  return number;
}

function flag(config, key, fallback) {
  if (config[key] === undefined) return fallback;
  if (typeof config[key] !== "boolean") throw new Error(`'${key}' must be true or false.`);
  return config[key];
}

function parseInterests(value) {
  const interests = [];
  const items = Array.isArray(value) ? value : listOf(value, "interests");
  for (const item of items) {
    let name;
    let keywords;
    if (typeof item === "string") {
      const [head, ...rest] = item.split(":");
      name = head.trim();
      keywords = splitKeywords(rest.join(":"));
    } else if (item && typeof item === "object" && typeof item.name === "string") {
      name = item.name.trim();
      keywords = Array.isArray(item.keywords) ? item.keywords.filter((k) => typeof k === "string").map((k) => k.trim().toLowerCase()).filter(Boolean).slice(0, MAX_KEYWORDS) : splitKeywords(item.keywords);
    } else {
      throw new Error("Each interest must be text like 'Board games: board game, tabletop' or an object with 'name' and 'keywords'.");
    }
    name = name.slice(0, 40);
    if (!name) throw new Error("An interest needs a name.");
    const taken = [...DEFAULT_CATEGORIES, ...interests].some((c) => normalizeText(c.name) === normalizeText(name));
    if (taken) throw new Error(`The interest '${name}' duplicates another category.`);
    interests.push({ id: `c_y${hashString(name.toLowerCase())}`.slice(0, 20), name, keywords });
  }
  if (interests.length > MAX_INTERESTS) throw new Error(`At most ${MAX_INTERESTS} interests are allowed.`);
  return interests;
}

/** Validate a card configuration and return the settings the card works with; throws a message for the dashboard editor. */
function readSettings(config) {
  if (!config || typeof config !== "object") throw new Error("The card needs a configuration.");
  if (config.feed !== undefined || config.entity !== undefined) {
    throw new Error("'feed' and 'entity' are no longer supported. Remove them and set 'area' (or leave it out to use the Home Assistant home location); events now come from the Area Events integration.");
  }
  if (config.area !== undefined && (typeof config.area !== "string" || !config.area.trim())) {
    throw new Error("'area' must be text such as 'San Diego, CA' (a city, a ZIP code or 'latitude, longitude').");
  }
  const sources = listOf(config.sources, "sources");
  if (sources.length > MAX_SOURCES || !sources.every((url) => /^https?:\/\/[^\s/]+/i.test(url))) {
    throw new Error(`'sources' must be at most ${MAX_SOURCES} website addresses starting with http:// or https://.`);
  }
  const interests = parseInterests(config.interests);
  const wanted = listOf(config.categories, "categories");
  const known = DEFAULT_CATEGORIES.filter((c) => c.id !== OTHER_ID);
  const categories = new Set();
  for (const token of wanted) {
    if (token.toLowerCase() === "history") continue;   // the retired built-in category: dropped, never converted (if it was the only one, all categories show)
    const match = known.find((c) => c.id === token.toLowerCase() || normalizeText(c.name) === normalizeText(token)) ||
      (normalizeText(token) === normalizeText("Other") ? DEFAULT_CATEGORIES.at(-1) : undefined);
    if (!match) throw new Error(`Unknown category '${token}'. Use: ${known.map((c) => c.id).join(", ")}, other.`);
    categories.add(match.id);
  }
  const exclude = listOf(config.exclude, "exclude").map((t) => t.toLowerCase()).slice(0, MAX_EXCLUDE);
  if (config.sort !== undefined && !SORTS.includes(config.sort)) throw new Error(`'sort' must be one of: ${SORTS.join(", ")}.`);
  return {
    area: config.area === undefined ? undefined : config.area.trim(),
    radius: numberIn(config, "radius_miles", 1, 250) ?? 25,
    sources,
    categories: categories.size ? categories : undefined,
    interests,
    exclude,
    days: numberIn(config, "days", 1, 365),
    max: numberIn(config, "max_events", 1, 200, true),
    sort: config.sort || "date",
    columns: numberIn(config, "columns", 1, 4, true),
    showImages: flag(config, "show_images", true),
    showFilters: flag(config, "show_filters", true),
  };
}

function hashString(text) {
  let hash = 5381;
  for (let i = 0; i < text.length; i++) hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  return hash.toString(36);
}

/*
 * Preferences: saved per card instance, following the NVR Card approach. The
 * primary store is Home Assistant's per-user data (frontend/get_user_data and
 * frontend/set_user_data, so choices follow the user across devices) with
 * localStorage as an immediate cache and fallback. The key combines the
 * dashboard path with the card's `id` option, or, when there is none, a hash
 * of its title/area/radius/sources so different cards do not collide. Two cards with
 * identical configuration on one dashboard need distinct `id` values.
 */
function prefsKey(config) {
  const path = (typeof window !== "undefined" && window.location && window.location.pathname) || "/";
  const explicit = typeof config.id === "string" ? config.id.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40) : "";
  const instance = explicit || `h${hashString(JSON.stringify([config.title || "", config.area || "", config.radius_miles || "", config.sources || "", Array.isArray(config.events) ? "inline" : ""]))}`;
  return `area-events-card:prefs:v1:${encodeURIComponent(path)}:${instance}`;
}

function readLocal(key, extraIds) {
  try {
    const text = window.localStorage.getItem(key);
    return text === null ? null : normalizePrefs(JSON.parse(text), extraIds);
  } catch {
    return null;
  }
}

function writeLocal(key, prefs) {
  try {
    window.localStorage.setItem(key, JSON.stringify(prefs));
  } catch {
    // storage unavailable or full: the choice still applies for this session
  }
}

const EMPTY_PREFS = () => ({ v: 1, selected: [], custom: [] });

/*
 * Category artwork. Original flat illustrations drawn for this card (no third-party assets), embedded here so they
 * ship with the card, need no network and follow the Home Assistant theme: every colour is a theme variable or a
 * tint of one. They are shown when an event has no usable photo, and are always labelled as illustrations.
 * Which one an event gets is decided by the engine from what the event is about (discovery/classify.py) and arrives
 * as event.illustration.category; the card only draws it, and events without one get "general".
 */
const SVG_HEAD = '<svg viewBox="0 0 320 180" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false"><rect class="bg" width="320" height="180"/>';
const ART = {
  music: SVG_HEAD +
    '<g class="b"><rect x="28" y="96" width="10" height="40" rx="5"/><rect x="48" y="78" width="10" height="76" rx="5"/><rect x="68" y="62" width="10" height="108" rx="5"/>' +
    '<rect x="242" y="62" width="10" height="108" rx="5"/><rect x="262" y="78" width="10" height="76" rx="5"/><rect x="282" y="96" width="10" height="40" rx="5"/></g>' +
    '<g class="a"><ellipse cx="136" cy="128" rx="20" ry="15" transform="rotate(-20 136 128)"/><ellipse cx="196" cy="116" rx="20" ry="15" transform="rotate(-20 196 116)"/>' +
    '<rect x="150" y="42" width="8" height="82"/><rect x="210" y="30" width="8" height="82"/><path d="M150 42 L218 30 L218 54 L150 66 Z"/></g></svg>',
  theater: SVG_HEAD +
    '<path class="b" d="M0 0 H70 Q60 90 40 180 H0 Z"/><path class="b" d="M320 0 H250 Q260 90 280 180 H320 Z"/>' +
    '<ellipse class="a" cx="130" cy="92" rx="42" ry="52"/><ellipse class="e" cx="116" cy="80" rx="8" ry="6"/><ellipse class="e" cx="146" cy="80" rx="8" ry="6"/>' +
    '<path class="se" d="M108 108 Q130 134 152 108"/>' +
    '<ellipse class="c" cx="200" cy="104" rx="38" ry="48"/><ellipse class="e" cx="187" cy="94" rx="7" ry="5"/><ellipse class="e" cx="213" cy="94" rx="7" ry="5"/>' +
    '<path class="se" d="M184 128 Q200 112 216 128"/></svg>',
  festival: SVG_HEAD +
    '<path class="sc" d="M0 30 Q160 70 320 30"/>' +
    '<path class="a" d="M28 39 L52 39 L40 65 Z"/><path class="c" d="M68 45 L92 45 L80 71 Z"/><path class="a" d="M108 49 L132 49 L120 75 Z"/>' +
    '<path class="c" d="M148 50 L172 50 L160 76 Z"/><path class="a" d="M188 49 L212 49 L200 75 Z"/><path class="c" d="M228 45 L252 45 L240 71 Z"/><path class="a" d="M268 39 L292 39 L280 65 Z"/>' +
    '<g class="s"><path d="M110 92 V108 M110 148 V164 M72 128 H88 M132 128 H148 M83 101 L94 112 M126 144 L137 155 M137 101 L126 112 M94 144 L83 155"/></g><circle class="a" cx="110" cy="128" r="6"/>' +
    '<g class="sb"><path d="M236 104 V116 M236 144 V156 M208 130 H220 M252 130 H264 M216 110 L224 118 M248 142 L256 150 M256 110 L248 118 M224 142 L216 150"/></g><circle class="b" cx="236" cy="130" r="5"/></svg>',
  food: SVG_HEAD +
    '<circle class="a" cx="160" cy="96" r="54"/><circle class="e" cx="160" cy="96" r="40"/><circle class="b" cx="160" cy="96" r="22"/>' +
    '<g class="c"><rect x="76" y="42" width="5" height="34" rx="2.5"/><rect x="86" y="42" width="5" height="34" rx="2.5"/><rect x="96" y="42" width="5" height="34" rx="2.5"/>' +
    '<rect x="76" y="70" width="25" height="10" rx="5"/><rect x="85" y="78" width="7" height="68" rx="3.5"/>' +
    '<path d="M240 42 Q264 58 258 104 H240 Z"/><rect x="240" y="102" width="18" height="44" rx="5"/></g></svg>',
  sports: SVG_HEAD +
    '<path class="b" d="M0 152 H320 V180 H0 Z"/><g class="s"><path d="M52 66 H98 M40 90 H90 M54 114 H100"/></g>' +
    '<circle class="a" cx="170" cy="88" r="48"/><path class="e" d="M170 64 L192 80 L184 106 L156 106 L148 80 Z"/>' +
    '<g class="se"><path d="M170 64 V42 M192 80 L214 72 M184 106 L196 126 M156 106 L144 126 M148 80 L126 72"/></g>' +
    '<ellipse class="c" cx="170" cy="150" rx="40" ry="6"/></svg>',
  arts: SVG_HEAD +
    '<path class="a" d="M160 34 C100 34 60 70 60 108 C60 142 92 152 112 148 C128 145 124 128 140 128 C156 128 160 142 176 144 C236 152 264 120 260 92 C256 56 214 34 160 34 Z"/>' +
    '<circle class="p1" cx="106" cy="92" r="11"/><circle class="p2" cx="144" cy="68" r="11"/><circle class="p3" cx="192" cy="70" r="11"/><circle class="p4" cx="226" cy="98" r="11"/>' +
    '<ellipse class="e" cx="136" cy="116" rx="9" ry="7"/>' +
    '<g transform="rotate(32 270 110)"><rect class="c" x="266" y="40" width="9" height="86" rx="4.5"/><rect class="a" x="263" y="124" width="15" height="22" rx="4"/></g></svg>',
  science: SVG_HEAD +
    '<circle class="b" cx="44" cy="40" r="3"/><circle class="b" cx="284" cy="56" r="4"/><circle class="b" cx="270" cy="150" r="3"/><circle class="b" cx="52" cy="140" r="4"/>' +
    '<g class="s"><ellipse cx="160" cy="90" rx="78" ry="28"/><ellipse cx="160" cy="90" rx="78" ry="28" transform="rotate(60 160 90)"/><ellipse cx="160" cy="90" rx="78" ry="28" transform="rotate(120 160 90)"/></g>' +
    '<circle class="a" cx="160" cy="90" r="13"/><circle class="c" cx="238" cy="90" r="6"/><circle class="c" cx="199" cy="158" r="6"/><circle class="c" cx="121" cy="158" r="6"/></svg>',
  family: SVG_HEAD +
    '<circle class="b" cx="262" cy="46" r="28"/><path class="b" d="M0 150 Q80 112 160 140 T320 132 V180 H0 Z"/>' +
    '<circle class="a" cx="112" cy="70" r="15"/><rect class="a" x="96" y="88" width="32" height="58" rx="15"/>' +
    '<circle class="a" cx="208" cy="70" r="15"/><rect class="a" x="192" y="88" width="32" height="58" rx="15"/>' +
    '<circle class="c" cx="160" cy="100" r="11"/><rect class="c" x="148" y="114" width="24" height="32" rx="11"/>' +
    '<path class="s" d="M128 112 L148 124 M192 112 L172 124"/></svg>',
  community: SVG_HEAD +
    '<path class="b" d="M160 30 C152 14 130 22 138 38 L160 58 L182 38 C190 22 168 14 160 30 Z"/>' +
    '<circle class="a" cx="106" cy="86" r="17"/><path class="a" d="M74 156 Q106 104 138 156 Z"/>' +
    '<circle class="a" cx="214" cy="86" r="17"/><path class="a" d="M182 156 Q214 104 246 156 Z"/>' +
    '<circle class="c" cx="160" cy="76" r="19"/><path class="c" d="M124 160 Q160 98 196 160 Z"/></svg>',
  outdoors: SVG_HEAD +
    '<circle class="b" cx="244" cy="46" r="24"/><path class="b" d="M0 150 L90 58 L150 122 L212 68 L320 150 V180 H0 Z"/>' +
    '<path class="a" d="M0 180 V152 L60 112 L110 152 L172 122 L232 162 L282 132 L320 160 V180 Z"/>' +
    '<path class="c" d="M236 166 L254 120 L272 166 Z"/><path class="c" d="M262 166 L276 130 L290 166 Z"/></svg>',
  farmers_market: SVG_HEAD +
    '<path class="b" d="M0 150 Q80 120 160 140 T320 132 V180 H0 Z"/>' +
    '<path class="a" d="M60 62 H260 L276 100 H44 Z"/><g class="e"><path d="M92 62 L84 100 H116 L120 62 Z"/><path d="M152 62 L152 100 H184 L188 62 Z"/><path d="M220 62 L224 100 H252 L244 62 Z"/></g>' +
    '<path class="c" d="M48 100 H272 V112 H48 Z"/><rect class="c" x="54" y="112" width="7" height="46" rx="3"/><rect class="c" x="259" y="112" width="7" height="46" rx="3"/>' +
    '<rect class="a" x="72" y="126" width="176" height="14" rx="4"/>' +
    '<circle class="p1" cx="96" cy="120" r="9"/><circle class="p2" cx="118" cy="121" r="9"/><circle class="p4" cx="142" cy="120" r="9"/>' +
    '<circle class="p3" cx="168" cy="121" r="8"/><circle class="p1" cx="192" cy="120" r="9"/><circle class="p2" cx="216" cy="121" r="9"/></svg>',
  carnival: SVG_HEAD +
    '<circle class="b" cx="48" cy="40" r="3"/><circle class="b" cx="276" cy="36" r="4"/><circle class="b" cx="30" cy="96" r="3"/>' +
    '<circle class="s" cx="160" cy="92" r="62"/><circle class="a" cx="160" cy="92" r="9"/>' +
    '<g class="sb"><path d="M160 30 V154 M98 92 H222 M116 48 L204 136 M204 48 L116 136"/></g>' +
    '<circle class="p1" cx="160" cy="30" r="8"/><circle class="p2" cx="204" cy="48" r="8"/><circle class="p3" cx="222" cy="92" r="8"/><circle class="p4" cx="204" cy="136" r="8"/>' +
    '<circle class="p1" cx="160" cy="154" r="8"/><circle class="p2" cx="116" cy="136" r="8"/><circle class="p3" cx="98" cy="92" r="8"/><circle class="p4" cx="116" cy="48" r="8"/>' +
    '<path class="c" d="M160 98 L120 170 H134 L160 122 L186 170 H200 Z"/><path class="b" d="M0 168 H320 V180 H0 Z"/></svg>',
  car_show: SVG_HEAD +
    '<path class="b" d="M0 142 H320 V180 H0 Z"/><g class="sb"><path d="M20 160 H70 M120 160 H160 M220 160 H280"/></g>' +
    '<path class="a" d="M44 130 L52 108 Q56 100 66 98 L108 94 L132 70 Q138 64 148 64 H186 Q196 64 202 70 L226 94 L262 100 Q276 102 278 116 V130 Q278 136 272 136 H50 Q44 136 44 130 Z"/>' +
    '<path class="e" d="M122 94 L142 74 H188 L208 94 Z"/><path class="se" d="M165 76 V94"/>' +
    '<circle class="c" cx="96" cy="136" r="19"/><circle class="e" cx="96" cy="136" r="8"/><circle class="c" cx="226" cy="136" r="19"/><circle class="e" cx="226" cy="136" r="8"/>' +
    '<circle class="a" cx="268" cy="114" r="5"/><path class="c" d="M268 40 L274 54 L290 56 L278 66 L282 82 L268 74 L254 82 L258 66 L246 56 L262 54 Z"/></svg>',
  halloween: SVG_HEAD +
    '<circle class="b" cx="250" cy="48" r="26"/><circle class="e" cx="260" cy="42" r="22"/>' +
    '<path class="c" d="M30 64 q8 -10 16 0 q8 -10 16 0 q-4 12 -16 14 q-12 -2 -16 -14 Z"/><path class="c" d="M268 112 q6 -8 12 0 q6 -8 12 0 q-3 9 -12 10 q-9 -1 -12 -10 Z"/>' +
    '<path class="b" d="M0 152 Q80 132 160 148 T320 140 V180 H0 Z"/>' +
    '<ellipse class="a" cx="160" cy="108" rx="62" ry="50"/><path class="b" d="M160 58 Q150 48 160 40 Q170 48 160 58 Z"/><rect class="c" x="154" y="40" width="12" height="16" rx="4"/>' +
    '<path class="e" d="M120 94 L142 94 L131 74 Z"/><path class="e" d="M178 94 L200 94 L189 74 Z"/><path class="e" d="M144 110 L160 122 L176 110 Z"/>' +
    '<path class="e" d="M116 126 L128 134 L140 126 L152 136 L164 126 L176 136 L188 126 L200 134 L204 126 L204 142 H116 Z"/></svg>',
  christmas: SVG_HEAD +
    '<circle class="b" cx="44" cy="38" r="3"/><circle class="b" cx="90" cy="22" r="2.5"/><circle class="b" cx="262" cy="30" r="3"/><circle class="b" cx="292" cy="64" r="2.5"/><circle class="b" cx="60" cy="86" r="2.5"/><circle class="b" cx="250" cy="96" r="3"/>' +
    '<path class="b" d="M0 150 Q80 134 160 148 T320 140 V180 H0 Z"/>' +
    '<path class="a" d="M160 26 L198 78 H178 L212 120 H186 L226 160 H94 L134 120 H108 L142 78 H122 Z"/>' +
    '<rect class="c" x="148" y="160" width="24" height="16" rx="3"/>' +
    '<polygon class="p2" points="160,10 164,21 176,21 166,28 170,40 160,33 150,40 154,28 144,21 156,21"/>' +
    '<circle class="p1" cx="140" cy="106" r="7"/><circle class="p3" cx="176" cy="84" r="7"/><circle class="p2" cx="182" cy="134" r="7"/><circle class="p1" cx="122" cy="146" r="7"/><circle class="p3" cx="158" cy="60" r="6"/></svg>',
  street_festival: SVG_HEAD +
    '<path class="sc" d="M0 26 Q80 62 160 36 T320 30"/>' +
    '<path class="p1" d="M30 44 L52 46 L38 68 Z"/><path class="p2" d="M76 54 L98 54 L88 76 Z"/><path class="p3" d="M122 52 L144 48 L136 72 Z"/><path class="p4" d="M178 46 L200 50 L186 70 Z"/><path class="p1" d="M224 50 L246 54 L232 74 Z"/><path class="p2" d="M270 44 L292 40 L284 64 Z"/>' +
    '<path class="b" d="M0 150 H320 V180 H0 Z"/>' +
    '<path class="a" d="M36 148 V104 Q36 84 56 84 H92 Q112 84 112 104 V148 Z"/><path class="e" d="M48 148 V108 H100 V148 Z"/>' +
    '<path class="c" d="M120 148 V96 Q120 76 140 76 H180 Q200 76 200 96 V148 Z"/><path class="e" d="M132 148 V100 H188 V148 Z"/>' +
    '<path class="a" d="M208 148 V108 Q208 88 228 88 H264 Q284 88 284 108 V148 Z"/><path class="e" d="M220 148 V112 H272 V148 Z"/>' +
    '<circle class="p2" cx="74" cy="124" r="9"/><circle class="p1" cx="160" cy="122" r="10"/><circle class="p4" cx="246" cy="130" r="9"/></svg>',
  general: SVG_HEAD +
    '<circle class="b" cx="52" cy="46" r="3"/><circle class="b" cx="268" cy="44" r="4"/><circle class="b" cx="262" cy="146" r="3"/><circle class="b" cx="60" cy="138" r="4"/>' +
    '<rect class="a" x="96" y="46" width="128" height="104" rx="14"/><rect class="e" x="106" y="72" width="108" height="68" rx="8"/>' +
    '<rect class="c" x="124" y="34" width="10" height="26" rx="5"/><rect class="c" x="186" y="34" width="10" height="26" rx="5"/>' +
    '<polygon class="a" points="160,84 165.3,98.7 180.9,99.2 168.6,108.8 172.9,123.8 160,115 147.1,123.8 151.4,108.8 139.1,99.2 154.7,98.7"/></svg>',
};
const ART_LABELS = {
  music: "Music and concerts", theater: "Theater and performing arts", festival: "Festivals", food: "Food and drink", sports: "Sports",
  arts: "Arts and exhibitions", science: "Science and technology", family: "Family activities", community: "Community events",
  outdoors: "Outdoors and recreation", general: "Events",
  farmers_market: "Farmers markets", carnival: "Carnivals and fairs", car_show: "Car shows", halloween: "Halloween",
  christmas: "Christmas and holiday events", street_festival: "Street festivals",
};
/** The illustration for an event: the category the engine resolved (see discovery/classify.py), else the general one. */
function artFor(event) {
  const resolved = event && event.illustration && event.illustration.category;
  return typeof resolved === "string" && Object.prototype.hasOwnProperty.call(ART, resolved) ? resolved : "general";
}

/*
 * Location imagery from the Area Events app (event.media): a verified photograph of the venue and/or a map. Everything is
 * validated here as data from outside: addresses must be this integration's own /area_events/media/ files, links must be
 * https addresses on the expected sites, and every text is shown as text. The card draws the pin, badges, venue name and
 * credits itself; the map files are vector images in a light and a graphite variant with the same geography.
 */
const MEDIA_FILE = /^\/area_events\/media\/[a-z0-9][a-z0-9-]*(\.light|\.dark)?\.(svg|jpg)$/;
// a photograph the event's source supplied, cached and served by this integration (never a path from anywhere else)
const EVENT_PHOTO_FILE = /^\/area_events\/media\/p[a-f0-9]{24}\.(jpg|png|webp)$/;
/** The credit for an event's own picture, as plain text and a link only to the page it came from (https). */
/** An IANA time zone name this browser knows, else undefined (then the viewer's own zone is used). */
function validZone(name) {
  if (typeof name !== "string" || !name || name.length > 64) return undefined;
  try {
    new Intl.DateTimeFormat("en", { timeZone: name });
    return name;
  } catch {
    return undefined;
  }
}
function safeImageCredit(raw) {
  if (!raw || typeof raw !== "object") return undefined;
  const text = (value, max) => (typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, max) : "");
  let sourceUrl = "";
  try {
    const url = new URL(raw.source_url);
    if (url.protocol === "https:") sourceUrl = url.href;
  } catch { /* no usable link */ }
  const credit = { text: text(raw.text, 80), license: text(raw.license, 60), sourceUrl };
  return credit.text ? credit : undefined;
}
// A badge names a place; it is never a sentence. The service sends a short `label`; for older answers the venue text is used only if it reads like a name.
const SENTENCE_WORDS = /\b(is|are|was|were|will|operates?|located|takes? place|between|along|including|vehicles?|this|there|inside|outside)\b/i;
function shortPlace(value) {
  const first = typeof value === "string" ? value.replace(/\s+/g, " ").trim().split(",")[0].trim() : "";
  return first.length >= 2 && first.length <= 60 && first.split(" ").length <= 8 && !/[.!?]$/.test(first) && !SENTENCE_WORDS.test(first) ? first : "";
}
function safeMedia(raw, event) {
  if (!raw || typeof raw !== "object") return undefined;
  const text = (value, max = 120) => (typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, max) : "");
  const link = (value, host) => {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && (url.hostname === host || url.hostname.endsWith(`.${host}`)) ? url.href : "";
    } catch {
      return "";
    }
  };
  const out = {};
  const venue = shortPlace(event && event.venue);
  const photo = raw.photo;
  if (photo && MEDIA_FILE.test(photo.url) && /\.jpg$/.test(photo.url)) {
    out.photo = {
      url: photo.url, venue: shortPlace(photo.venue) || venue, creator: text(photo.creator), license: text(photo.license, 40),
      licenseUrl: link(photo.license_url, "creativecommons.org"), sourceUrl: link(photo.source_url, "wikimedia.org"),
    };
  }
  const map = raw.map;
  if (map && MEDIA_FILE.test(map.light) && MEDIA_FILE.test(map.dark) && /\.svg$/.test(map.light) && /\.svg$/.test(map.dark)) {
    out.map = { light: map.light, dark: map.dark, approximate: map.approximate === true, credit: text(map.credit, 80) || "OpenFreeMap © OpenMapTiles Data from OpenStreetMap", venue: shortPlace(map.label) || venue };
  }
  return out.photo || out.map ? out : undefined;
}

const PIN_SVG = '<svg viewBox="0 0 30 40" aria-hidden="true" focusable="false"><path d="M15 39 L5 23 A13 13 0 1 1 25 23 Z" fill="#f08a24" stroke="#fff" stroke-width="2.4" stroke-linejoin="round"/>' +
  '<circle cx="15" cy="14" r="5.2" fill="#25282d"/></svg>';
const MEDIA_BADGES = { venue: "VENUE", map: "MAP" };
const IMAGE_RETRIES = 3;
const IMAGE_RETRY_MS = 60000;
const INFO_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/>' +
  '<path d="M12 11v6" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="7.2" r="1.5" fill="currentColor"/></svg>';
// The credit a map must carry (OpenFreeMap's required text, the OpenStreetMap copyright page) lives behind a small info button on the tile.
const MAP_CREDITS = [["OpenFreeMap", "https://openfreemap.org"], [" © OpenMapTiles", "https://openmaptiles.org/"], [" Data from OpenStreetMap", "https://www.openstreetmap.org/copyright"]];

class AreaEventsCard extends HTMLElement {
  setConfig(config) {
    this._stopFeed();
    this._settings = readSettings(config);
    this._config = config;
    this._feed = undefined;
    this._status = undefined;
    this._feedError = undefined;
    this._feedErrorCode = undefined;
    this._previewWaited = false;
    this._filterOpen = false;
    this._editing = undefined;
    this._initPrefs();
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
    this._startFeed();
    this._loadRemotePrefs();
  }

  connectedCallback() {
    if (this._config) {
      this._startFeed();
      this._startAgeTimer();
    }
  }

  disconnectedCallback() {
    this._stopFeed();
    clearInterval(this._ageTimer);
    this._ageTimer = undefined;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._startFeed();
      this._loadRemotePrefs();
    }
    // The effective Home Assistant theme decides the map: it is read from Home Assistant on every update, so a change of theme
    // (or of the automatic dark mode) reaches the maps on screen at once, without a refresh.
    const dark = this._isDark();
    if (dark !== this._dark) {
      this._dark = dark;
      for (const tile of this._tiles || []) if (tile.el && tile.el._aeTheme) tile.el._aeTheme();
    }
  }

  /** Is Home Assistant showing a dark theme right now? (Its own setting; the browser's colour scheme only if Home Assistant gives none.) */
  _isDark() {
    const themes = this._hass && this._hass.themes;
    if (themes && typeof themes.darkMode === "boolean") return themes.darkMode;
    return Boolean(typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  getCardSize() {
    return 4;
  }

  static getConfigForm() {
    return {
      schema: EDITOR_SCHEMA,
      computeLabel: (item) => EDITOR_LABELS[item.name] || item.name,
      computeHelper: (item) => EDITOR_HELP[item.name] || "",
      assertConfig: assertEditable,
    };
  }

  static getStubConfig() {
    return { title: "Area Events" };
  }

  static get _internals() {
    return { categoriesFor, normalizePrefs, prefsKey, DEFAULT_CATEGORIES, readSettings, assertEditable, EDITOR_SCHEMA, ART, ART_LABELS, artFor, safeMedia };
  }

  // --- preferences ------------------------------------------------------------

  _interestIds() {
    return this._settings.interests.map((c) => c.id);
  }

  _initPrefs() {
    this._prefsKey = prefsKey(this._config);
    this._prefsGeneration = (this._prefsGeneration || 0) + 1;
    const local = readLocal(this._prefsKey, this._interestIds());
    this._prefs = local || EMPTY_PREFS();
    this._hadLocalPrefs = Boolean(local);
    this._prefsDirty = false;
    this._remoteState = "pending";
  }

  _loadRemotePrefs() {
    if (this._remoteState !== "pending" || !this._hass || typeof this._hass.callWS !== "function") return;
    this._remoteState = "loading";
    const generation = this._prefsGeneration;
    const key = this._prefsKey;
    Promise.resolve()
      .then(() => this._hass.callWS({ type: "frontend/get_user_data", key }))
      .then((response) => {
        if (generation !== this._prefsGeneration) return;
        const value = response && typeof response === "object" && "value" in response ? response.value : response;
        const remote = value === null || value === undefined ? null : normalizePrefs(value, this._interestIds());
        this._remoteState = "ready";
        if (remote && !this._prefsDirty) {
          this._prefs = remote;
          writeLocal(key, remote);
          this._refreshFilters();
        } else if (this._hadLocalPrefs || this._prefsDirty) {
          this._saveRemote(); // first use on this user: migrate the local choice
        }
      })
      .catch(() => {
        if (generation === this._prefsGeneration) this._remoteState = "failed"; // local storage only
      });
  }

  _saveRemote() {
    if (!this._hass || typeof this._hass.callWS !== "function") return;
    Promise.resolve()
      .then(() => this._hass.callWS({ type: "frontend/set_user_data", key: this._prefsKey, value: this._prefs }))
      .catch(() => {});
  }

  _setPrefs(next) {
    this._prefs = normalizePrefs(next, this._interestIds()) || EMPTY_PREFS();
    this._prefsDirty = true;
    writeLocal(this._prefsKey, this._prefs);
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => {
      if (this._remoteState !== "loading") this._saveRemote();
    }, SAVE_DEBOUNCE_MS);
    this._computeMatches();
    this._refreshFilters();
  }

  // --- events from the integration ----------------------------------------------------

  _usesInline() {
    return Array.isArray(this._config.events);
  }

  _stopFeed() {
    clearTimeout(this._feedTimer);
    this._feedTimer = undefined;
    this._feedRun = (this._feedRun || 0) + 1; // answers still in flight are ignored
    const unsubscribe = this._unsubscribe;
    this._unsubscribe = undefined;
    if (typeof unsubscribe === "function") unsubscribe();
  }

  /** Follow this card's area: Home Assistant sends the current view now and a new one whenever it changes (also after a reconnect). */
  _startFeed() {
    if (!this._config || this._usesInline() || !this._hass || !this.isConnected) return;
    if (this._unsubscribe !== undefined) return; // already following, or about to
    const run = this._feedRun;
    const area = this._settings.area;
    const connection = this._hass.connection;
    if (!area) return this._onFeed(run, undefined, "Set 'area' to the name of a discovery area configured in the Discovery Service.", "invalid_profile");
    if (!connection || typeof connection.subscribeMessage !== "function") {
      return this._onFeed(run, undefined, "Home Assistant's connection is not available.", "unavailable");
    }
    this._unsubscribe = null; // subscribing
    this._feedTimer = setTimeout(() => {
      if (this.preview && !this._previewWaited) {
        this._previewWaited = true;
        this._feedTimer = setTimeout(() => this._subscribeNow(run), PREVIEW_DELAY_MS);
        return;
      }
      this._subscribeNow(run);
    }, 0);
  }

  _subscribeNow(run) {
    if (run !== this._feedRun || !this._hass) return;
    const connection = this._hass.connection;
    connection.subscribeMessage((reply) => this._onFeed(run, reply), { type: WS_SUBSCRIBE, area: this._settings.area }).then(
      (unsubscribe) => {
        if (run !== this._feedRun) unsubscribe();
        else this._unsubscribe = unsubscribe;
      },
      (caught) => {
        if (run !== this._feedRun) return;
        this._unsubscribe = undefined;
        const code = caught && caught.code;
        this._onFeed(run, undefined, (caught && caught.message) || String(caught), code);
        this._feedTimer = setTimeout(() => this._startFeed(), PERMANENT_ERRORS.has(code) ? POLL_READY_MS : POLL_RETRY_MS);
      },
    );
  }

  _startAgeTimer() {
    clearInterval(this._ageTimer);
    this._ageTimer = setInterval(() => this._updateHeader(), AGE_TICK_MS);
  }

  _onFeed(run, reply, error, code) {
    if (run !== this._feedRun) return;
    const before = JSON.stringify([this._status, this._feedError, this._feed && this._feed.key]);
    if (reply) {
      this._status = {
        state: reply.state, message: reply.message, refreshing: Boolean(reply.refreshing), connected: reply.connected !== false,
        report: reply.report || [], initial: reply.initial !== false, outcome: reply.outcome || undefined, progress: reply.progress || undefined,
        notice: reply.notice || undefined, service: reply.service,
      };
      this._feedError = this._feedErrorCode = undefined;
      if (Array.isArray(reply.events)) {
        // `key` changes whenever the area's view does, so an unchanged push changes nothing on screen
        const revision = Number(reply.revision) || 0;
        this._feed = { events: reply.events, generatedAt: reply.generated_at, revision, key: `${reply.generated_at || ""}#${revision}#${reply.events.length}`, sources: reply.sources, location: reply.location, area: reply.area };
      }
    } else {
      this._feedError = error;
      this._feedErrorCode = code;
    }
    const after = JSON.stringify([this._status, this._feedError, this._feed && this._feed.key]);
    // Nothing new changes nothing on screen; new events are merged into the existing tiles in place.
    if (before === after && this._els) {
      this._updateHeader();
      if (reply && (this._tiles || []).some((tile) => this._retryable(tile))) this._applyData();
    } else this._applyData();
  }

  /** The place being searched, for messages. */
  _placeLabel() {
    return this._settings.area || "this card's area";
  }

  _events() {
    const settings = this._settings;
    const source = this._usesInline() ? this._config.events : (this._feed && this._feed.events) || [];
    const now = Date.now();
    let list = source.map((event) => this._normalizeEvent(event));
    if (!this._usesInline()) {
      list = list.filter((event) => {
        const last = new Date(event.end || event.start).getTime();
        return Number.isNaN(last) || last >= now;
      });
    }
    if (settings.days !== undefined) {
      const limit = now + settings.days * 86400000;
      list = list.filter((event) => {
        const start = new Date(event.start).getTime();
        return Number.isNaN(start) || start <= limit;
      });
    }
    if (settings.exclude.length) {
      list = list.filter((event) => {
        const haystack = normalizeText([event.title, event.description, ...event.categories].join(" . "));
        return !settings.exclude.some((topic) => matchesKeyword(haystack, topic));
      });
    }
    if (settings.categories) {
      const extra = [...settings.interests, ...this._prefs.custom];
      const interestIds = new Set(settings.interests.map((c) => c.id));
      list = list.filter((event) => [...categoriesFor(event, extra)].some((id) => settings.categories.has(id) || interestIds.has(id)));
    }
    const time = (event) => {
      const start = new Date(event.start).getTime();
      return Number.isNaN(start) ? Infinity : start;
    };
    if (settings.sort === "title") list.sort((a, b) => String(a.title).localeCompare(String(b.title)) || time(a) - time(b));
    else if (settings.sort === "verified") list.sort((a, b) => Number(b.verified) - Number(a.verified) || time(a) - time(b));
    else list.sort((a, b) => time(a) - time(b));
    return settings.max ? list.slice(0, settings.max) : list;
  }

  // Accepts both the card's own event fields and the discovery feed format.
  _normalizeEvent(event) {
    const verification = event.verification || {};
    const categories = Array.isArray(event.categories) ? event.categories : event.category ? [event.category] : [];
    return {
      id: event.id,
      illustration: event.illustration,
      title: event.title,
      start: event.start,
      end: event.end,
      timezone: validZone(event.timezone),
      location: event.location || [event.venue, event.address].filter(Boolean).join(", "),
      category: event.category || categories[0],
      categories,
      images: [...new Set([event.image, event.image_url, ...(Array.isArray(event.images) ? event.images : [])]
        .filter((url) => typeof url === "string" && (/^https?:\/\//i.test(url.trim()) || EVENT_PHOTO_FILE.test(url.trim()))).map((url) => url.trim()))],
      imageCredit: safeImageCredit(event.image_credit),
      description: event.description,
      venue: event.venue,
      media: safeMedia(event.media, event),
      url: event.url,
      sourceName: event.source && event.source.name,
      locationStatus: event.location_status,
      verified: event.verified === true,
      status: verification.status,
      evidence: Array.isArray(verification.evidence) ? verification.evidence : [],
    };
  }

  /** Authoritative generation time: when the app last discovered events, never the page-load time. */
  _generatedAt() {
    return this._usesInline() ? undefined : this._feed && this._feed.generatedAt;
  }

  _viewerZone() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  }

  /** A time as people read it. With `zone` (an event's own IANA time zone) the time is shown there, not in the viewer's zone, with the zone's short name when it differs. */
  _formatDate(value, zone) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "";
    const locale = this._hass && this._hass.locale && this._hass.locale.language;
    const known = validZone(zone);
    if (!known) return date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
    if (known === this._viewerZone()) return date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short", timeZone: known });
    return date.toLocaleString(locale, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: known, timeZoneName: "short" });
  }

  _ago(ms) {
    const minutes = Math.floor(ms / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    return hours < 48 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
  }

  /** Is a search for this card's profile queued or running (so the progress panel shows)? */
  _discovering() {
    if (this._usesInline() || this._feedError || !this._status) return false;
    const progress = this._status.progress;
    if (progress && ACTIVE_PHASES.includes(progress.phase)) return true;
    return !progress && (this._status.state === "pending" || this._status.state === "discovering");
  }

  /** What the viewer should know about the search, or undefined when there is nothing to say. */
  _noticeParts(count) {
    if (this._usesInline()) return {};
    const updated = this._feed && this._feed.generatedAt ? this._formatDate(this._feed.generatedAt) : undefined;
    const status = this._status || {};
    const notes = [];
    if (this._feedError) {
      notes.push(this._feed ? `Could not refresh events (${this._feedError}). Showing earlier data${updated ? ` from ${updated}` : ""}.` : `Events are not available: ${this._feedError}`);
    } else if (!this._status) {
      notes.push("Loading events...");
    } else if (status.state === "unavailable") {
      notes.push(`The Area Events app is not reachable (${status.message || "no answer"}).${this._feed ? ` Showing earlier data${updated ? ` from ${updated}` : ""}.` : ""}`);
    } else if (this._discovering()) {
      // the progress panel says what is happening; a failed earlier run is still worth a word
      if (status.message && this._feed) notes.push(`The last refresh failed (${status.message}); showing earlier data${updated ? ` from ${updated}` : ""}.`);
    } else if (status.state === "error" && status.outcome === "no_results") {
      notes.push(`${status.message || "No upcoming events were found."} Try a larger radius or another area.`);
    } else if (status.state === "error") {
      notes.push(`Events could not be found for ${this._placeLabel()}: ${status.message || "discovery failed"}`);
    } else if (status.message) {
      notes.push(`Could not refresh events (${status.message}). Showing data${updated ? ` from ${updated}` : ""}.`);
    } else if (status.outcome === "partial") {
      const sources = this._feed && this._feed.sources && typeof this._feed.sources === "object" ? Object.values(this._feed.sources) : [];
      const failing = sources.filter((source) => source && source.ok === false).length;
      notes.push(`${failing || "Some"} of ${sources.length || "the"} sources could not be read; events they provided earlier are kept.`);
    } else if (count === 0 && !status.notice) {
      notes.push("No upcoming events match.");
    }
    if (status.notice && !this._feedError) notes.push(status.notice);
    // Websites added to the card are extras: one short line says how many could not be read; the reasons sit behind a disclosure.
    const problems = [], provided = [];
    let providedEvents = 0;
    for (const item of status.report || []) {
      if (item.status === "unsupported") problems.push(`Source not supported: ${item.url} (${item.reason}).`);
      else if (item.status === "blocked") problems.push(`Source blocked: ${item.url} (${item.reason}).`);
      else if (item.status === "unavailable") problems.push(`Source unavailable: ${item.url} (${item.reason}).`);
      else if (item.status === "no_events") problems.push(`No events found at ${item.url} (${item.reason}).`);
      else if (item.status === "supported" && item.id && (item.adapter === "web" || item.adapter === "localist") && !this._discovering()) {
        const found = this._feed && this._feed.sources && this._feed.sources[item.id];
        if (found && found.ok !== false && found.events > 0) {
          provided.push(`${item.name || item.url}: ${found.events} event${found.events === 1 ? "" : "s"} found.`);
          providedEvents += found.events;
        } else if (found && found.ok === false) {
          problems.push(`${item.name || item.url}: no events found in the search area${found.error ? ` (${found.error})` : ""}.`);
        }
      }
    }
    let summary;
    if (problems.length) summary = `${problems.length} additional website${problems.length === 1 ? "" : "s"} couldn't be read.`;
    else if (provided.length) summary = `Additional websites: ${providedEvents} event${providedEvents === 1 ? "" : "s"} found.`;
    return { text: notes.join(" ") || undefined, summary, lines: [...problems, ...provided] };
  }

  /** The notice as plain text (the summary line included), or undefined when there is nothing to say. */
  _feedNotice(count) {
    const { text, summary } = this._noticeParts(count);
    return [text, summary].filter(Boolean).join(" ") || undefined;
  }

  // --- rendering ----------------------------------------------------------------

  _categories() {
    const wanted = this._settings.categories;
    const builtin = DEFAULT_CATEGORIES.filter((c) => c.id !== OTHER_ID && (!wanted || wanted.has(c.id)));
    const other = !wanted || wanted.has(OTHER_ID) ? [DEFAULT_CATEGORIES.at(-1)] : [];
    return [...builtin, ...this._settings.interests, ...this._prefs.custom, ...other];
  }

  _computeMatches() {
    const extra = [...this._settings.interests, ...this._prefs.custom];
    for (const tile of this._tiles || []) tile.matches = categoriesFor(tile.event, extra);
  }

  /** Builds the card's frame once; events and status are then merged in place by _applyData. */
  _render() {
    if (!this._config || !this.shadowRoot) return;
    const root = this.shadowRoot;
    root.replaceChildren();

    const style = document.createElement("style");
    style.textContent = STYLES + (this._settings.columns ? `\n  .grid { grid-template-columns: repeat(auto-fill, minmax(min(100%, max(160px, (100% - ${(this._settings.columns - 1) * 8}px) / ${this._settings.columns})), 1fr)); }` : "");
    const card = document.createElement("ha-card");
    const wrap = this._el("div", "wrap");

    this._els = {};
    this._tiles = [];
    this._els.header = this._el("div", "sticky-header");
    wrap.append(this._els.header);
    if (this._config.demo) wrap.append(this._el("div", "demo-banner", "DEMONSTRATION DATA - not real events"));

    this._els.notice = this._el("div", "notice");
    wrap.append(this._els.notice);

    this._els.progress = this._renderProgress();
    wrap.append(this._els.progress);

    this._els.filters = this._el("div", "filters");
    wrap.append(this._els.filters);

    this._els.body = this._el("div", "events");
    wrap.append(this._els.body);

    this._els.footer = this._el("div", "footer");
    wrap.append(this._els.footer);
    card.append(wrap);
    root.append(style, card);
    this._applyData();
  }

  /** The progress panel: built once so its animation is never restarted; _updateProgress only changes its text. */
  _renderProgress() {
    const panel = this._el("div", "progress");
    panel.hidden = true;
    panel.setAttribute("role", "status");
    panel.setAttribute("aria-live", "polite");
    const row = this._el("div", "row");
    const text = this._el("div", "text");
    this._els.headline = this._el("div", "headline");
    this._els.detail = this._el("div", "detail");
    text.append(this._els.headline, this._els.detail);
    row.append(this._icon("mdi:calendar-search"), text, this._el("div", "spinner"));
    panel.append(row, this._el("div", "bar"));
    return panel;
  }

  /** Merges the current events and status into the frame: tiles are kept, added or removed individually. */
  _applyData() {
    if (!this._els || !this._els.body) {
      this._render();
      return;
    }
    const events = this._events();
    this._syncTiles(events);
    this._updateNotice(events.length);
    this._updateProgress(events.length);
    const updated = this._generatedAt() ? ` · events updated ${this._formatDate(this._generatedAt())}` : "";
    this._els.footer.textContent = `${AREA_EVENTS_CARD_BUILD}${updated}`;
    this._refreshFilters();
  }

  /** A tile whose images all failed (a busy or restarting app, a dropped connection) may try again on a later poll, a few times and not at once. */
  _retryable(tile) {
    const failed = tile.el && tile.el._aeFailed;
    return Boolean(failed) && (tile.retries || 0) < IMAGE_RETRIES && Date.now() - failed >= IMAGE_RETRY_MS;
  }

  /**
   * Reconciles the tiles with the events: a tile whose event is unchanged keeps its element (so images do not
   * reload or flash), changed events get a new tile, vanished ones are removed, and the order follows the events.
   */
  _syncTiles(events) {
    const extra = [...this._settings.interests, ...this._prefs.custom];
    const previous = new Map((this._tiles || []).map((tile) => [tile.key, tile]));
    const used = new Set();
    const tiles = events.map((event, index) => {
      let key = event.id || `${event.title}|${event.start}`;
      if (used.has(key)) key = `${key}#${index}`;
      used.add(key);
      const signature = JSON.stringify(event);
      const old = previous.get(key);
      const same = old && old.signature === signature;
      if (same && !this._retryable(old)) return old;
      return { event, key, signature, el: this._renderEvent(event), matches: categoriesFor(event, extra), retries: same ? (old.retries || 0) + 1 : 0 };
    });
    this._tiles = tiles;

    const body = this._els.body;
    if (tiles.length === 0) {
      body.replaceChildren();
      this._els.grid = this._els.noMatch = undefined;
      if (this._usesInline()) body.append(this._el("div", "empty", "No events to show."));
      return;
    }
    if (!this._els.grid) {
      this._els.grid = this._el("div", "grid");
      this._els.noMatch = this._el("div", "no-match", "No events match the selected categories.");
      this._els.noMatch.hidden = true;
      body.replaceChildren(this._els.grid, this._els.noMatch);
    }
    const wanted = tiles.map((tile) => tile.el);
    const current = this._els.grid.children;
    if (current.length !== wanted.length || wanted.some((el, i) => current[i] !== el)) this._els.grid.replaceChildren(...wanted);
  }

  /** Shows what discovery is really doing; large while there is nothing else to look at, compact above existing tiles. */
  _updateProgress(count) {
    const panel = this._els && this._els.progress;
    if (!panel) return;
    const active = this._discovering();
    panel.hidden = !active;
    if (!active) return;
    const status = this._status || {};
    const progress = status.progress || {};
    const compact = count > 0;
    panel.className = compact ? "progress compact" : "progress";
    const refreshing = status.initial === false;
    this._els.headline.textContent = compact
      ? (refreshing ? "Refreshing events..." : "Finding more events...")
      : `Discovering events near ${this._placeLabel()}`;
    const parts = [];
    if (progress.phase === "queued" || !progress.phase) parts.push("Waiting to start");
    else if (progress.current) parts.push(`${progress.phase === "processing" ? "Processing" : "Searching"} ${progress.current}`);
    else parts.push("Starting");
    if (progress.total) parts.push(`${Math.min(progress.completed || 0, progress.total)} of ${progress.total} sources done`);
    if (progress.failed) parts.push(`${progress.failed} could not be read`);
    if (progress.events) parts.push(`${progress.events} event${progress.events === 1 ? "" : "s"} so far`);
    this._els.detail.textContent = parts.join(" · ");
  }

  _updateNotice(count) {
    if (!this._els || !this._els.notice) return;
    const { text, summary, lines } = this._noticeParts(count === undefined ? (this._tiles || []).length : count);
    const notice = this._els.notice;
    notice.replaceChildren();
    if (text) notice.append(this._el("div", "", text));
    if (summary) {
      const details = this._el("details", "");
      details.append(this._el("summary", "", summary));
      for (const line of lines) details.append(this._el("div", "detail-line", line));
      notice.append(details);
    }
    notice.hidden = !(text || summary);
  }

  _visibleCount() {
    return this._tiles.filter((t) => !t.el.hidden).length;
  }

  _applyFilter() {
    const selected = new Set(this._prefs.selected);
    for (const tile of this._tiles) {
      tile.el.hidden = selected.size > 0 && ![...tile.matches].some((id) => selected.has(id));
    }
    if (this._els.noMatch) this._els.noMatch.hidden = this._visibleCount() > 0 || this._tiles.length === 0;
  }

  _updateHeader() {
    const header = this._els && this._els.header;
    if (!header) return;
    header.replaceChildren();
    header.append(this._el("div", "head-title", this._config.title || "Area Events"));
    const meta = this._el("div", "head-meta");

    const generated = this._generatedAt();
    const generatedMs = generated ? new Date(generated).getTime() : NaN;
    if (!Number.isNaN(generatedMs)) {
      const age = Math.max(0, Date.now() - generatedMs);
      const updated = this._el("span", "updated");
      updated.append(document.createTextNode(`Updated ${this._formatDate(generated)} (${this._ago(age)})`));
      meta.append(updated);
      if (age > FEED_STALE_MS) meta.append(this._el("span", "badge", "Stale"));
    }
    if (this._feedError || (this._status && this._status.state === "unavailable")) {
      meta.append(this._el("span", "badge error", this._feed ? "Update failed" : "Events unavailable"));
    }
    const place = this._feed && this._feed.location && this._feed.location.display_name;
    if (place) meta.append(this._el("span", "place", this._feed.location.coverage === "sources" ? `${place} · by source` : `${place} · ${this._settings.radius} mi`));

    const total = this._tiles.length;
    const shown = this._visibleCount();
    if (this._usesInline() || this._feed) {
      meta.append(this._el("span", "counts", shown === total ? `${total} event${total === 1 ? "" : "s"}` : `${shown} of ${total} events`));
    }
    const sources = this._feed && this._feed.sources && typeof this._feed.sources === "object" ? Object.values(this._feed.sources) : [];
    if (sources.length > 0) {
      const failing = sources.filter((s) => s && s.ok === false).length;
      meta.append(this._el("span", "sources", failing ? `${sources.length - failing} of ${sources.length} sources ok` : `${sources.length} sources`));
      if (failing) meta.append(this._el("span", "badge", `${failing} source${failing > 1 ? "s" : ""} failing`));
    }
    header.append(meta);
  }

  _refreshFilters() {
    if (!this._els) return;
    if (!this._tiles || this._tiles.length === 0 || !this._settings.showFilters) {
      if (this._els.filters) this._els.filters.replaceChildren();
      this._updateHeader();
      return;
    }
    this._applyFilter();
    this._updateHeader();
    this._renderFilterUI();
  }

  _button(label, onClick, pressed) {
    const button = this._el("button", "", label);
    button.type = "button";
    if (pressed !== undefined) button.setAttribute("aria-pressed", String(pressed));
    button.addEventListener("click", onClick);
    return button;
  }

  _renderFilterUI() {
    const host = this._els.filters;
    host.replaceChildren();
    const selected = new Set(this._prefs.selected);
    const categories = this._categories();
    const counts = new Map(categories.map((c) => [c.id, 0]));
    for (const tile of this._tiles) for (const id of tile.matches) counts.set(id, (counts.get(id) || 0) + 1);

    const bar = this._el("div", "filter-bar");
    const toggle = this._button(`Filters${selected.size ? ` (${selected.size})` : ""}`, () => {
      this._filterOpen = !this._filterOpen;
      this._renderFilterUI();
    });
    toggle.className = "filter-toggle";
    toggle.setAttribute("aria-expanded", String(this._filterOpen));
    const names = categories.filter((c) => selected.has(c.id)).map((c) => c.name);
    const all = this._button("Show all", () => this._setPrefs({ ...this._prefs, selected: [] }), selected.size === 0);
    all.className = "show-all";
    bar.append(all, toggle, this._el("span", "filter-summary", selected.size ? `Showing: ${names.join(", ")}` : "Showing all categories"));
    host.append(bar);
    if (!this._filterOpen) return;

    const chips = this._el("div", "chips");
    const allChip = this._button(`${selected.size ? "" : "✓ "}All`, () => this._setPrefs({ ...this._prefs, selected: [] }), selected.size === 0);
    allChip.className = "chip chip-all";
    chips.append(allChip);
    for (const category of categories) {
      const on = selected.has(category.id);
      const chip = this._button("", () => {
        const next = new Set(this._prefs.selected);
        if (next.has(category.id)) next.delete(category.id);
        else next.add(category.id);
        this._setPrefs({ ...this._prefs, selected: [...next] });
      }, on);
      chip.className = "chip";
      chip.setAttribute("data-category", category.id);
      chip.append(document.createTextNode(`${on ? "✓ " : ""}${category.name}`), this._el("span", "count", String(counts.get(category.id) || 0)));
      chips.append(chip);
    }
    host.append(chips);

    const custom = this._el("div", "custom-list");
    for (const category of this._prefs.custom) {
      const row = this._el("div", "custom-row");
      row.append(this._el("span", "name", category.name), this._el("span", "kw", category.keywords.join(", ") || "(matches its name)"));
      const edit = this._button("Edit", () => this._openEditor(category));
      edit.className = "edit";
      const remove = this._button(this._confirmDelete === category.id ? "Confirm delete" : "Delete", () => {
        if (this._confirmDelete !== category.id) {
          this._confirmDelete = category.id;
          this._renderFilterUI();
          return;
        }
        this._confirmDelete = undefined;
        this._setPrefs({
          ...this._prefs,
          custom: this._prefs.custom.filter((c) => c.id !== category.id),
          selected: this._prefs.selected.filter((id) => id !== category.id),
        });
      });
      remove.className = "delete";
      row.append(edit, remove);
      custom.append(row);
    }
    host.append(custom);

    if (this._editing) {
      host.append(this._renderEditor());
    } else if (this._prefs.custom.length < MAX_CUSTOM) {
      const add = this._button("New category", () => this._openEditor());
      add.className = "new-category";
      host.append(add);
    }
  }

  _openEditor(category) {
    this._confirmDelete = undefined;
    this._editing = { id: category && category.id, name: category ? category.name : "", keywords: category ? category.keywords.join(", ") : "", error: "" };
    this._renderFilterUI();
  }

  _renderEditor() {
    const state = this._editing;
    const editor = this._el("div", "editor");
    const field = (labelText, value, onInput, className) => {
      const label = this._el("label", "", labelText);
      const input = document.createElement("input");
      input.className = className;
      input.type = "text";
      input.value = value;
      input.addEventListener("input", () => onInput(input.value));
      label.append(input);
      return label;
    };
    editor.append(
      field("Name", state.name, (v) => { state.name = v; }, "name-input"),
      field("Keywords (comma separated)", state.keywords, (v) => { state.keywords = v; }, "keywords-input"),
    );
    if (state.error) editor.append(this._el("div", "error", state.error));
    const actions = this._el("div", "actions");
    const save = this._button("Save", () => this._saveEditor());
    save.className = "save";
    const cancel = this._button("Cancel", () => {
      this._editing = undefined;
      this._renderFilterUI();
    });
    cancel.className = "cancel";
    actions.append(save, cancel);
    editor.append(actions);
    return editor;
  }

  _saveEditor() {
    const state = this._editing;
    const name = state.name.trim().slice(0, 40);
    const reserved = this._categories().filter((c) => c.id !== state.id).map((c) => normalizeText(c.name));
    if (!name) state.error = "Enter a name.";
    else if (reserved.includes(normalizeText(name))) state.error = "That name is already used.";
    if (state.error) {
      this._renderFilterUI();
      return;
    }
    const keywords = splitKeywords(state.keywords);
    let custom;
    if (state.id) {
      custom = this._prefs.custom.map((c) => (c.id === state.id ? { ...c, name, keywords } : c));
    } else {
      const id = `c_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
      custom = [...this._prefs.custom, { id, name, keywords }];
    }
    this._editing = undefined;
    this._setPrefs({ ...this._prefs, custom });
  }

  _renderEvent(event) {
    const item = this._el("div", "event");

    const image = this._el("div", "image");
    const artKind = artFor(event);
    // The illustration is always there first, so a tile never shows an empty box, a broken-image icon or a layout shift.
    const art = this._el("div", `art ${artKind}`);
    art.innerHTML = ART[artKind];
    art.setAttribute("role", "img");
    art.setAttribute("aria-label", `Illustration: ${ART_LABELS[artKind]}`);
    const tag = this._el("span", "art-tag", "Illustration");
    image.append(art, tag);
    // Image priority: the event's own photos (the engine put its best first, then verified ones from other matched records), a
    // verified photograph of the venue, a map of the event's location, and finally the category illustration, which is always
    // underneath. An image that fails to load hands over to the next one. Only the event's own photos are ever shown without
    // a label: VENUE and MAP say what the rest are.
    const media = event.media;
    const place = media && media.map && media.map.venue;
    const candidates = [];
    if (this._settings.showImages) {
      for (const url of event.images) candidates.push({ kind: "event", url: () => url, alt: `Image for ${event.title || "this event"}`, credit: event.imageCredit });
      if (media && media.photo) candidates.push({ kind: "venue", url: () => media.photo.url, alt: `Photograph of the venue${media.photo.venue ? `, ${media.photo.venue}` : ""}, not of the event`, photo: media.photo });
      if (media && media.map) {
        candidates.push({ kind: "map", url: () => (this._isDark() ? media.map.dark : media.map.light), map: media.map,
          alt: `Map of the event location${place ? `: ${place}` : ""}${media.map.approximate ? " (approximate position)" : ""}` });
      }
    }
    if (candidates.length) {
      let current = candidates.shift();
      const img = document.createElement("img");
      let requested; // the address last given to the image, and the one that last loaded (img.src itself is the resolved absolute address)
      let loaded;
      const show = (url) => { requested = url; img.src = url; };
      const pin = this._el("span", "pin");
      pin.innerHTML = PIN_SVG;
      pin.hidden = true;
      const credit = this._el("div", "media-credit");
      credit.hidden = true;
      this._creditIds = (this._creditIds || 0) + 1;
      credit.id = `ae-credit-${this._creditIds}`;
      // a small information button; the full credit and its links open from it and close again with it, Escape or a change of image
      const info = this._el("button", "credit-button");
      info.type = "button";
      info.hidden = true;
      info.innerHTML = INFO_SVG;
      info.setAttribute("aria-expanded", "false");
      info.setAttribute("aria-controls", credit.id);
      const collapse = (focus) => {
        credit.hidden = true;
        info.setAttribute("aria-expanded", "false");
        if (focus && info.focus) info.focus();
      };
      info.addEventListener("click", () => {
        credit.hidden = !credit.hidden;
        info.setAttribute("aria-expanded", String(!credit.hidden));
      });
      const escape = (e) => { if (e && e.key === "Escape" && !credit.hidden) collapse(true); };
      info.addEventListener("keydown", escape);
      credit.addEventListener("keydown", escape);
      const chip = this._el("span", "venue-chip");
      chip.hidden = true;
      const fill = () => {
        // what the image that is showing needs around it: a pin and the map credit, or the photograph's credit
        credit.replaceChildren();
        collapse(false);
        info.hidden = current.kind !== "map" && current.kind !== "venue" && !(current.kind === "event" && current.credit);
        info.setAttribute("aria-label", current.kind === "map" ? "Map credits and licence" : "Photo credit and licence");
        info.title = info.getAttribute("aria-label");
        pin.hidden = current.kind !== "map";
        chip.hidden = current.kind !== "map" || !place;
        if (!chip.hidden) chip.textContent = place;   // the position's confidence stays in the data and the credit note; the card says once, at the top, that areas are matched by source
        if (current.kind === "venue") {
          const { creator, license, licenseUrl, sourceUrl } = current.photo;
          credit.append(document.createTextNode(`Photo: ${creator || "Wikimedia Commons"} · `));
          const licence = this._el(licenseUrl ? "a" : "span", "", license || "see file page");
          if (licenseUrl) Object.assign(licence, { href: licenseUrl, target: "_blank", rel: "noopener noreferrer" });
          credit.append(licence);
          if (sourceUrl) {
            const source = this._el("a", "", " · Wikimedia Commons");
            Object.assign(source, { href: sourceUrl, target: "_blank", rel: "noopener noreferrer" });
            credit.append(source);
          }
        } else if (current.kind === "event" && current.credit) {
          credit.append(document.createTextNode(`Photo: ${current.credit.text}`));
          if (current.credit.license) credit.append(this._el("span", "note", current.credit.license));
          if (current.credit.sourceUrl) {
            const source = this._el("a", "", "Event page");
            Object.assign(source, { href: current.credit.sourceUrl, target: "_blank", rel: "noopener noreferrer" });
            credit.append(document.createTextNode(" · "), source);
          }
        } else if (current.kind === "map") {
          credit.append(document.createTextNode("Map: "));
          for (const [label, href] of MAP_CREDITS) {
            const source = this._el("a", "", label);
            Object.assign(source, { href, target: "_blank", rel: "noopener noreferrer" });
            credit.append(source);
          }
          if (current.map.approximate) credit.append(this._el("span", "note", "The pin shows an approximate position."));
        }
      };
      img.alt = current.alt;
      img.loading = "lazy";
      img.decoding = "async";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("load", () => {
        loaded = requested;
        img.className = "loaded";
        art.hidden = true;
        const badge = MEDIA_BADGES[current.kind];
        tag.textContent = badge || "Illustration";
        tag.className = badge ? "art-tag media-badge" : "art-tag";
        tag.hidden = !badge;
        fill();
      });
      img.addEventListener("error", () => {
        // back to the illustration (and its label) while the next image loads, or for good when none is left
        img.className = "";
        art.hidden = false;
        tag.textContent = "Illustration";
        tag.className = "art-tag";
        tag.hidden = false;
        pin.hidden = chip.hidden = info.hidden = true;
        collapse(false);
        if (current.kind === "map" && loaded && requested !== loaded) {
          // the other theme's variant failed after this map had loaded: the map is still good, so keep showing the one that loaded
          // (the next theme change tries again) instead of giving up on the map
          show(loaded);
          return;
        }
        loaded = undefined;
        current = candidates.shift();
        if (current) {
          img.alt = current.alt;
          show(current.url());
        } else {
          img.remove();
          item._aeFailed = Date.now(); // every image failed: later polls may try again (see _retryImages)
        }
      });
      // a theme change swaps the map being shown for its other variant (only the one in use is ever requested)
      item._aeTheme = () => {
        if (current && current.kind === "map" && requested !== current.url()) show(current.url());
      };
      show(current.url());
      image.append(img, pin, chip, info, credit);
    }
    if (event.category) image.append(this._el("span", "category", event.category));

    const body = this._el("div", "body");
    body.append(this._el("div", "title", event.title || "Untitled event"));
    if (event.start) body.append(this._meta("mdi:calendar-clock", this._formatDate(event.start, event.timezone)));
    if (event.location) body.append(this._meta("mdi:map-marker", event.location));
    if (event.locationStatus === "description_only" || event.locationStatus === "unknown") {
      body.append(this._meta("mdi:help-circle-outline", event.location ? "The location is the source's own description and is not verified." : "The source gave no location."));
    }
    if (event.description) body.append(this._el("div", "description", event.description));

    // Only an explicit verified=true is "Verified"; a corroborated status means
    // the source's own signals agree, which is not independent verification.
    let kind = "unverified";
    let label = "Unverified";
    let icon = "mdi:help-circle-outline";
    if (event.verified) [kind, label, icon] = ["verified", "Verified", "mdi:check-decagram"];
    else if (event.status === "corroborated") [kind, label, icon] = ["corroborated", "Source corroborated", "mdi:check-circle-outline"];
    const verify = this._el("div", `verify ${kind}`);
    verify.append(this._icon(icon), this._el("span", "", label));
    if (event.evidence.length) verify.title = event.evidence.join("\n");
    body.append(verify);

    if (/^https?:\/\//i.test(event.url || "")) {
      const link = this._el("a", "source", `Source: ${event.sourceName || "event page"}`);
      link.href = event.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      body.append(link);
    }

    item.append(image, body);
    return item;
  }

  _meta(icon, text) {
    const row = this._el("div", "meta");
    row.append(this._icon(icon), this._el("span", "", text));
    return row;
  }

  _icon(name) {
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", name);
    return icon;
  }

  _el(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
}

/*
 * Visual editor: Home Assistant renders this schema itself (`getConfigForm`), with the same controls and the
 * user's language as for its own cards. List settings are chip pickers that accept free text; an interest with
 * its own keyword list written as an object can only be edited in YAML.
 */
const FREE_LIST = { select: { multiple: true, custom_value: true, options: [] } };
const EDITOR_SCHEMA = [
  { name: "title", selector: { text: {} } },
  {
    type: "grid", name: "", schema: [
      { name: "area", selector: { text: {} } },
    ],
  },
  {
    name: "categories",
    selector: { select: { multiple: true, mode: "dropdown", options: DEFAULT_CATEGORIES.map((c) => ({ value: c.id, label: c.name })) } },
  },
  { name: "interests", selector: FREE_LIST },
  { name: "exclude", selector: FREE_LIST },
  {
    type: "grid", name: "", schema: [
      { name: "days", selector: { number: { min: 1, max: 365, mode: "box", unit_of_measurement: "days" } } },
      { name: "max_events", selector: { number: { min: 1, max: 200, mode: "box" } } },
      { name: "sort", selector: { select: { mode: "dropdown", options: [{ value: "date", label: "Date" }, { value: "title", label: "Title" }, { value: "verified", label: "Verified first" }] } } },
    ],
  },
  {
    type: "expandable", name: "", title: "Layout", schema: [
      { name: "columns", selector: { number: { min: 1, max: 4, mode: "box" } } },
      { name: "show_images", selector: { boolean: {} } },
      { name: "show_filters", selector: { boolean: {} } },
    ],
  },
];
const EDITOR_LABELS = {
  title: "Title", area: "Discovery area (name)",
  categories: "Only these categories", interests: "Interests (Name: keyword, keyword)", exclude: "Excluded topics",
  days: "Only the next days", max_events: "Maximum events", sort: "Sort by", columns: "Columns (at most)", show_images: "Show images", show_filters: "Show filters",
};
const EDITOR_HELP = {
  area: "The name of an area configured in the Discovery Service, for example San Diego. Each card follows one area; the filters below are this card's own.",
  interests: "Extra topics that become their own filters, for example Board games: board game, tabletop",
};

/** Throws (which makes Home Assistant show the YAML editor instead) when the visual editor cannot represent the configuration. */
function assertEditable(config) {
  readSettings(config);
  if (Array.isArray(config.interests) && config.interests.some((item) => typeof item !== "string")) {
    throw new Error("Interests written as objects can only be edited in YAML.");
  }
}

/*
 * Registration. Home Assistant's frontend replaces window.customElements with a scoped-custom-element-registry polyfill
 * while it starts, and the polyfilled registry does not see elements that were defined in the browser's own registry
 * before the swap. The integration's script is imported in parallel with the frontend, so it can run before the swap;
 * a card defined then is invisible to Lovelace ("Custom element doesn't exist" although this script ran). The
 * <home-assistant> element is defined by the frontend after the polyfill is installed, so the card is defined only
 * then, in whichever registry is current at that moment (customElements is read at call time, never kept earlier).
 */
function defineCard() {
  const registry = customElements;
  const registered = registry.get(CARD_TAG);
  if (!registered) {
    registry.define(CARD_TAG, AreaEventsCard);
  } else if (registered !== AreaEventsCard && typeof registered.getConfigForm !== "function") {
    // A tag can only be defined once per page. An older Area Events card (the dashboard resource
    // /local/area-events-card/loader.js of the pre-integration versions) that loaded first has no visual editor and
    // none of the integration support, and this module can not replace it.
    console.error(
      "Area Events: an older Area Events card is already registered, so this card and its visual editor are not in use. " +
      "Remove the dashboard resource /local/area-events-card/loader.js (Settings > Dashboards > Resources); " +
      "the Area Events integration loads the card by itself."
    );
  }
  // What was true at the moment of registration, for diagnosing a failure from the console:
  //   __AREA_EVENTS_CARD_LOAD__.registry === customElements          false: the page's registry was replaced after the card was defined
  //   __AREA_EVENTS_CARD_LOAD__.nativeDefine / registryName          whether define was the browser's own, and the registry's constructor
  //   __AREA_EVENTS_CARD_LOAD__.afterHomeAssistant                   true: defined after <home-assistant> (the intended order)
  const load = globalThis.__AREA_EVENTS_CARD_LOAD__;
  load.defined = registry.get(CARD_TAG) === AreaEventsCard;
  load.definedAt = Date.now();
  load.registry = registry;
  load.cardClass = AreaEventsCard;
  load.nativeDefine = /\[native code\]/.test(Function.prototype.toString.call(registry.define));
  load.registryName = registry.constructor && registry.constructor.name;
}

if (customElements.get("home-assistant")) {
  globalThis.__AREA_EVENTS_CARD_LOAD__.afterHomeAssistant = true;
  defineCard();
} else {
  globalThis.__AREA_EVENTS_CARD_LOAD__.afterHomeAssistant = false;
  customElements.whenDefined("home-assistant").then(() => {
    globalThis.__AREA_EVENTS_CARD_LOAD__.afterHomeAssistant = true;
    defineCard();
  });
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === `custom:${CARD_TAG}`)) {
  window.customCards.push({
    type: `custom:${CARD_TAG}`,
    name: "Area Events Card",
    description: "Local events for your area, found by the Area Events integration.",
    preview: false,
  });
}

console.info(
  `%c AREA-EVENTS-CARD %c ${AREA_EVENTS_CARD_BUILD} `,
  "color:#fff;background:#03a9f4;font-weight:700",
  "color:#03a9f4;background:#fff;font-weight:700"
);
