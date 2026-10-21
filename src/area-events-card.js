/**
 * Area Events Card - Home Assistant custom Lovelace card.
 *
 * Shows the events the Area Events integration discovers for this card's `area` and `radius_miles` (or, without
 * an area, for the Home Assistant home location). The card talks only to Home Assistant, over the signed-in
 * user's own WebSocket session (`area_events/events`); it holds no secret and contacts no other server.
 * `events` (an inline list) shows fixed events instead, for demonstrations; see examples/demo-card.yaml.
 * Everything about what is shown (categories, interests, exclusions, date range, sorting, layout) is YAML or the
 * visual editor; the filter chips are the viewer's own choices, saved per card instance (see "Preferences").
 */

const AREA_EVENTS_CARD_BUILD = "__AREA_EVENTS_CARD_BUILD__";
const CARD_TAG = "area-events-card";

/*
 * Theme: colors come only from Home Assistant theme variables, so the card
 * follows light/dark theme changes through CSS inheritance (no JS needed).
 * Tiles are a tint of the primary text color over the card background, which
 * stays visible even in themes (e.g. Graphite E-ink Dark) where card, page
 * and secondary backgrounds are identical. Verification, category and filter
 * state are always conveyed by icon or text, never by color alone.
 *
 * Layout: auto-fit columns, 220px minimum tile width, capped at four, so the
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
    grid-template-columns: repeat(auto-fit, minmax(min(100%, max(220px, (100% - 3 * 8px) / 4)), 1fr));
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
  .image img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .image ha-icon { --mdc-icon-size: 48px; }
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
    display: flex; align-items: flex-start; gap: 6px; min-width: 0;
    font-size: 0.875rem; color: var(--secondary-text-color); overflow-wrap: anywhere;
  }
  .meta ha-icon { --mdc-icon-size: 16px; flex: none; }
  .verify {
    margin-top: 4px; font-size: 0.8125rem;
    font-weight: min(var(--ha-font-weight-medium, 500), 600);
  }
  .verify.verified { color: var(--success-color, var(--primary-text-color)); }
  .verify.corroborated { color: var(--secondary-text-color); }
  .verify.unverified { color: var(--secondary-text-color); }
  .verify ha-icon { --mdc-icon-size: 16px; vertical-align: -3px; }
  .description {
    font-size: 0.875rem; color: var(--secondary-text-color); overflow-wrap: anywhere;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  .source { margin-top: 4px; font-size: 0.8125rem; color: var(--primary-color); overflow-wrap: anywhere; }
  .notice {
    margin: 0 0 8px; padding: 6px 12px; border-radius: 8px; font-size: 0.875rem;
    border: 1px solid var(--divider-color); color: var(--primary-text-color); overflow-wrap: anywhere;
  }
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
const POLL_BUSY_MS = 5 * 1000;
const POLL_RETRY_MS = 30 * 1000;
const FEED_STALE_MS = 3 * 60 * 60 * 1000; // the app refreshes hourly by default
const SAVE_DEBOUNCE_MS = 300;
const AGE_TICK_MS = 60 * 1000;
const EDITOR_DEBOUNCE_MS = 700;
// In the dashboard editor every keystroke rebuilds the preview card; wait until the settings stop changing before
// asking for a search, so half-typed areas never become searches of their own.
const PREVIEW_DELAY_MS = 1500;
const WS_EVENTS = "area_events/events";
// Errors the user must fix; polling faster would not help.
const ACTIVE_PHASES = ["queued", "searching", "processing"];
const PERMANENT_ERRORS = new Set(["invalid_profile", "rejected", "not_configured", "unauthorized"]);

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
  ["history", "History", "history, historic, historical, heritage, vintage, walking tour, archaeology"],
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
 *   area, radius_miles         the place (a city, a ZIP code or "latitude, longitude") and the search radius;
 *                              without `area` the Home Assistant home location is used
 *   sources                    website addresses to read events from; only some websites have an adapter and the
 *                              card reports the ones that do not
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

class AreaEventsCard extends HTMLElement {
  setConfig(config) {
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
    return { title: "Area Events", radius_miles: 25 };
  }

  static get _internals() {
    return { categoriesFor, normalizePrefs, prefsKey, DEFAULT_CATEGORIES, readSettings, assertEditable, EDITOR_SCHEMA };
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

  _profileRequest() {
    const { area, radius, sources } = this._settings;
    return { radius_miles: radius, ...(area ? { area } : {}), ...(sources.length ? { sources } : {}) };
  }

  _stopFeed() {
    clearTimeout(this._feedTimer);
    this._feedTimer = undefined;
    this._feedRun = (this._feedRun || 0) + 1; // answers still in flight are ignored
  }

  _startFeed() {
    if (!this._config || this._usesInline() || !this._hass || !this.isConnected) return;
    this._stopFeed();
    const run = this._feedRun;
    this._feedTimer = setTimeout(() => {
      if (this.preview && !this._previewWaited) {
        this._previewWaited = true;
        this._feedTimer = setTimeout(() => this._loadFeed(run), PREVIEW_DELAY_MS);
        return;
      }
      this._loadFeed(run);
    }, 0);
  }

  _startAgeTimer() {
    clearInterval(this._ageTimer);
    this._ageTimer = setInterval(() => this._updateHeader(), AGE_TICK_MS);
  }

  async _loadFeed(run) {
    let reply;
    let error;
    let code;
    try {
      const since = this._feed && this._feed.generatedAt;
      reply = await this._hass.callWS({ type: WS_EVENTS, ...this._profileRequest(), ...(since ? { since } : {}) });
    } catch (caught) {
      error = (caught && caught.message) || String(caught);
      code = caught && caught.code;
    }
    if (run !== this._feedRun) return;

    const before = JSON.stringify([this._status, this._feedError, this._feed && this._feed.generatedAt]);
    if (reply) {
      const events = Array.isArray(reply.events) ? reply.events : reply.unchanged && this._feed ? this._feed.events : undefined;
      this._status = {
        state: reply.state, message: reply.message, refreshing: Boolean(reply.refreshing), connected: reply.connected !== false,
        report: reply.report || [], initial: reply.initial !== false, outcome: reply.outcome || undefined, progress: reply.progress || undefined,
      };
      this._feedError = this._feedErrorCode = undefined;
      if (Array.isArray(events)) this._feed = { events, generatedAt: reply.generated_at, sources: reply.sources, location: reply.location };
    } else {
      this._feedError = error;
      this._feedErrorCode = code;
    }
    const after = JSON.stringify([this._status, this._feedError, this._feed && this._feed.generatedAt]);
    // Nothing new changes nothing on screen; new events are merged into the existing tiles in place.
    if (before === after && this._els) this._updateHeader();
    else this._applyData();

    const state = reply && reply.state;
    const busy = reply && (state === "pending" || state === "discovering" || state === "unavailable" || reply.refreshing || this._discovering());
    const delay = reply ? (busy ? POLL_BUSY_MS : POLL_READY_MS) : PERMANENT_ERRORS.has(code) ? POLL_READY_MS : POLL_RETRY_MS;
    this._feedTimer = setTimeout(() => this._loadFeed(run), delay);
  }

  /** The place being searched, for messages. */
  _placeLabel() {
    return this._settings.area || "your Home Assistant home location";
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
      title: event.title,
      start: event.start,
      end: event.end,
      location: event.location || [event.venue, event.address].filter(Boolean).join(", "),
      category: event.category || categories[0],
      categories,
      image: event.image || event.image_url,
      description: event.description,
      url: event.url,
      sourceName: event.source && event.source.name,
      verified: event.verified === true,
      status: verification.status,
      evidence: Array.isArray(verification.evidence) ? verification.evidence : [],
    };
  }

  /** Authoritative generation time: when the app last discovered events, never the page-load time. */
  _generatedAt() {
    return this._usesInline() ? undefined : this._feed && this._feed.generatedAt;
  }

  _formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "";
    const locale = this._hass && this._hass.locale && this._hass.locale.language;
    return date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
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
  _feedNotice(count) {
    if (this._usesInline()) return undefined;
    const updated = this._feed && this._feed.generatedAt ? this._formatDate(this._feed.generatedAt) : undefined;
    const status = this._status || {};
    const notes = [];
    if (this._feedErrorCode === "capacity") {
      // the app is busy with other areas: not an empty feed and not a bad configuration; the card keeps trying
      notes.push(`Too many different areas are being searched right now; this card will try again shortly.${this._feed ? ` Showing earlier data${updated ? ` from ${updated}` : ""}.` : ""}`);
    } else if (this._feedError) {
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
    } else if (count === 0) {
      notes.push("No upcoming events match.");
    }
    for (const item of status.report || []) {
      if (item.status === "unsupported") notes.push(`Source not supported: ${item.url} (${item.reason}).`);
    }
    return notes.join(" ") || undefined;
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
    style.textContent = STYLES + (this._settings.columns ? `\n  .grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, max(160px, (100% - ${(this._settings.columns - 1) * 8}px) / ${this._settings.columns})), 1fr)); }` : "");
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
      if (old && old.signature === signature) return old;
      return { event, key, signature, el: this._renderEvent(event), matches: categoriesFor(event, extra) };
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
    const text = this._feedNotice(count === undefined ? (this._tiles || []).length : count) || "";
    this._els.notice.textContent = text;
    this._els.notice.hidden = !text;
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
    if (place) meta.append(this._el("span", "place", `${place} · ${this._settings.radius} mi`));

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
    const placeholder = () => image.prepend(this._icon("mdi:calendar-star"));
    if (event.image && this._settings.showImages) {
      const img = document.createElement("img");
      img.src = event.image;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("error", () => {
        img.remove();
        placeholder();
      });
      image.append(img);
    } else {
      placeholder();
    }
    if (event.category) image.append(this._el("span", "category", event.category));

    const body = this._el("div", "body");
    body.append(this._el("div", "title", event.title || "Untitled event"));
    if (event.start) body.append(this._meta("mdi:calendar-clock", this._formatDate(event.start)));
    if (event.location) body.append(this._meta("mdi:map-marker", event.location));
    if (event.description) body.append(this._el("div", "description", event.description));

    // Only an explicit verified=true is "Verified"; a corroborated status means
    // the source's own signals agree, which is not independent verification.
    let kind = "unverified";
    let label = "Unverified";
    let icon = "mdi:help-circle-outline";
    if (event.verified) [kind, label, icon] = ["verified", "Verified", "mdi:check-decagram"];
    else if (event.status === "corroborated") [kind, label, icon] = ["corroborated", "Source corroborated", "mdi:check-circle-outline"];
    const verify = this._el("div", `verify ${kind}`);
    verify.append(this._icon(icon), document.createTextNode(` ${label}`));
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
      { name: "radius_miles", selector: { number: { min: 1, max: 250, mode: "box", unit_of_measurement: "mi" } } },
    ],
  },
  { name: "sources", selector: FREE_LIST },
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
  title: "Title", area: "Area (city, ZIP code or latitude, longitude)", radius_miles: "Search radius", sources: "Source websites",
  categories: "Only these categories", interests: "Interests (Name: keyword, keyword)", exclude: "Excluded topics",
  days: "Only the next days", max_events: "Maximum events", sort: "Sort by", columns: "Columns (at most)", show_images: "Show images", show_filters: "Show filters",
};
const EDITOR_HELP = {
  area: "Leave empty to use the Home Assistant home location.",
  sources: "Type an address and press Enter. Only websites with an adapter work; the card says which ones do not.",
  interests: "Extra topics that become their own filters, for example Board games: board game, tabletop",
};

/** Throws (which makes Home Assistant show the YAML editor instead) when the visual editor cannot represent the configuration. */
function assertEditable(config) {
  readSettings(config);
  if (Array.isArray(config.interests) && config.interests.some((item) => typeof item !== "string")) {
    throw new Error("Interests written as objects can only be edited in YAML.");
  }
}

const registered = customElements.get(CARD_TAG);
if (!registered) {
  customElements.define(CARD_TAG, AreaEventsCard);
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
