/**
 * Area Events Card - Home Assistant custom Lovelace card.
 *
 * Reads events from `config.feed` (a JSON URL), `config.events` or from the
 * `events` attribute of `config.entity`. Contains no sample data; see
 * examples/demo-card.yaml. Category filters are applied in the browser and
 * saved per card instance (see "Preferences" below).
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
  .area { display: flex; flex-direction: column; gap: 4px; margin: 8px 0; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--divider-color); font-size: 0.875rem; }
  .area-line { color: var(--primary-text-color); overflow-wrap: anywhere; }
  .area-detail, .area-note { color: var(--secondary-text-color); font-size: 0.8125rem; overflow-wrap: anywhere; }
  .area-message { color: var(--primary-text-color); font-weight: min(var(--ha-font-weight-medium, 500), 600); overflow-wrap: anywhere; }
  .area-name { color: var(--primary-text-color); }
  .area-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .hint-command { font-family: var(--code-font-family, monospace); font-size: 0.8125rem; color: var(--secondary-text-color); overflow-wrap: anywhere; user-select: all; }

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
  /* Build tag, bottom-left, below the grid. A diagnostic label, not content. */
  .footer {
    margin-top: 8px; font-size: 0.8125rem; line-height: 1.25; letter-spacing: 0.02em;
    color: var(--secondary-text-color); overflow-wrap: anywhere;
  }
`;

const FEED_REFRESH_MS = 30 * 60 * 1000;
const FEED_STALE_MS = 3 * 60 * 60 * 1000; // the backend refreshes hourly
const SAVE_DEBOUNCE_MS = 300;
const AGE_TICK_MS = 60 * 1000;

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

/** Validate saved preferences; returns a clean object or null if unusable. */
function normalizePrefs(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const custom = [];
  for (const item of Array.isArray(raw.custom) ? raw.custom : []) {
    const name = typeof item?.name === "string" ? item.name.trim().slice(0, 40) : "";
    const id = typeof item?.id === "string" && /^c_[a-z0-9]{3,20}$/.test(item.id) ? item.id : "";
    if (!name || !id || custom.some((c) => c.id === id) || custom.length >= MAX_CUSTOM) continue;
    const keywords = Array.isArray(item.keywords) ? item.keywords.filter((k) => typeof k === "string").map((k) => k.trim().toLowerCase().slice(0, 40)).filter(Boolean).slice(0, MAX_KEYWORDS) : [];
    custom.push({ id, name, keywords });
  }
  const known = new Set([...DEFAULT_CATEGORIES, ...custom].map((c) => c.id));
  const selected = (Array.isArray(raw.selected) ? raw.selected : []).filter((id) => typeof id === "string" && known.has(id));
  const prefs = { v: 1, selected: [...new Set(selected)], custom };
  const area = normalizeArea(raw.area);
  if (area && area.kind !== "configured") prefs.area = area;
  const serviceArea = normalizeServiceArea(raw.service_area);
  if (serviceArea) prefs.service_area = serviceArea;
  return prefs;
}

/*
 * Area (where the user is interested in events). Resolution order, highest first:
 *   1. an area the user chose in the card (saved per card instance): "Home Assistant home" or "my location"
 *   2. an `area` set in the card configuration
 *   3. the Home Assistant home location (hass.config), read live and never hardcoded or persisted
 *   4. unavailable: the card says so and offers the choices above
 * The browser location is requested ONLY from a click on "Use my location", never on load. An area chosen
 * here is for this card; it does not change what the discovery service searches (see the area panel text).
 */
const TIMEZONE_NAME = /^(UTC|[A-Za-z]+(\/[A-Za-z0-9_+-]+)+)$/;
const GEO_TIMEOUT_MS = 10000;
const GEO_MAX_AGE_MS = 10 * 60 * 1000;

function validCoordinates(latitude, longitude) {
  return Number.isFinite(latitude) && Number.isFinite(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180 &&
    !(latitude === 0 && longitude === 0);
}

/** Home Assistant's configured home location, or undefined when it is missing or invalid. */
function haHomeArea(hass) {
  const config = hass && hass.config;
  if (!config || typeof config.latitude !== "number" || typeof config.longitude !== "number") return undefined;
  if (!validCoordinates(config.latitude, config.longitude)) return undefined;
  return {
    source: "home_assistant", latitude: config.latitude, longitude: config.longitude,
    name: (typeof config.location_name === "string" && config.location_name.trim()) || "Home Assistant home",
    timezone: typeof config.time_zone === "string" && TIMEZONE_NAME.test(config.time_zone) ? config.time_zone : undefined,
  };
}

/** Validate a saved or configured area; returns a clean object or undefined. */
function normalizeArea(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  if (raw.kind === "ha_home") return { kind: "ha_home" };
  const latitude = Number(raw.latitude);
  const longitude = Number(raw.longitude);
  if (raw.latitude === null || raw.longitude === null || raw.latitude === "" || raw.longitude === "" || !validCoordinates(latitude, longitude)) return undefined;
  const area = { kind: raw.kind === "browser" ? "browser" : "configured", latitude, longitude };
  if (typeof raw.name === "string" && raw.name.trim()) area.name = raw.name.trim().slice(0, 60);
  if (typeof raw.timezone === "string" && TIMEZONE_NAME.test(raw.timezone)) area.timezone = raw.timezone;
  if (Number.isFinite(raw.radius_miles) && raw.radius_miles > 0 && raw.radius_miles <= 250) area.radius_miles = raw.radius_miles;
  if (Number.isFinite(raw.accuracy_m) && raw.accuracy_m >= 0) area.accuracy_m = raw.accuracy_m;
  if (typeof raw.saved_at === "string") area.saved_at = raw.saved_at.slice(0, 40);
  return area;
}

/** Decide which area applies. `saved` and `configured` come from normalizeArea. */
function resolveArea({ saved, configured, hass }) {
  const home = haHomeArea(hass);
  if (saved && saved.kind === "ha_home") {
    if (home) return { source: "selected_home_assistant", area: home };
  } else if (saved) {
    return { source: "selected_browser", area: { source: "browser", name: saved.name || "Browser location", ...saved } };
  }
  if (configured) return { source: "configured", area: { source: "configured", name: configured.name || "Configured area", ...configured } };
  if (home) return { source: "home_assistant", area: home };
  return { source: "unavailable", reason: saved && saved.kind === "ha_home" ? "The Home Assistant home location is not set." : "No location is available." };
}

function milesBetween(a, b) {
  const rad = (d) => (d * Math.PI) / 180;
  const h = Math.sin(rad(b.latitude - a.latitude) / 2) ** 2 +
    Math.cos(rad(a.latitude)) * Math.cos(rad(b.latitude)) * Math.sin(rad(b.longitude - a.longitude) / 2) ** 2;
  return 2 * 3958.8 * Math.asin(Math.sqrt(h));
}

function geolocationProblem(error) {
  switch (error && error.code) {
    case 1: return "Location permission was denied. Allow it for this site in the browser, or use the Home Assistant home location. (Browsers only offer location on https pages or localhost.)";
    case 2: return "The browser could not determine a position.";
    case 3: return "Finding your location took too long.";
    default: return "Your location could not be read.";
  }
}

function backendHintCommand(area) {
  const source = area.source === "browser" ? "browser" : "home_assistant";
  const accuracy = source === "browser" && Number.isFinite(area.accuracy_m) ? ` --accuracy-m ${Math.round(area.accuracy_m)}` : "";
  const zone = area.timezone ? ` --timezone ${area.timezone}` : "";
  return `python -m discovery.hint --source ${source} --latitude ${area.latitude.toFixed(5)} --longitude ${area.longitude.toFixed(5)}${zone}${accuracy}`;
}

/*
 * Service mode (`source: service`): events come from the discovery server through Home Assistant, one area per
 * card. The card never talks to the server and holds no credential: it runs Home Assistant scripts
 * (script.area_events_list / _ensure / _get / _feed / _refresh) with the signed-in user's own session, and those
 * scripts call the server's authenticated API (see homeassistant/area_events.example.yaml). The chosen area is
 * saved per card with the other preferences.
 */
const SERVICE_AREA_ID = /^(default|a_[0-9a-f]{12})$/;
const SERVICE_POLL_MS = 15000;
const SERVICE_REFRESH_MS = 5 * 60 * 1000;
const SERVICE_MAX_WAIT_MS = 20 * 60 * 1000;
const SPEC_KEYS = ["query", "zip", "country", "region", "timezone", "latitude", "longitude", "radius_miles"];

function roundCoordinate(value) {
  return Math.round(value * 100) / 100; // about 1 km: all the precision an event search needs, and less to disclose
}

/** City, ZIP or "latitude, longitude" typed by the user -> a server request (or an error message). */
function parsePlace(text, country, radius) {
  const value = String(text || "").trim();
  if (!value) return { error: "Enter a city, a ZIP code, or latitude and longitude." };
  const pair = value.match(/^(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)$/);
  if (pair) {
    const latitude = Number(pair[1]);
    const longitude = Number(pair[2]);
    if (!validCoordinates(latitude, longitude)) return { error: "Those coordinates are not valid." };
    return { spec: { latitude, longitude, radius_miles: radius } };
  }
  if (/\d/.test(value) && !value.includes(",") && /^[A-Za-z0-9][A-Za-z0-9 -]{2,10}$/.test(value)) {
    if (!country) return { error: "A ZIP code needs a country (add country: US to the card or type a city)." };
    return { spec: { zip: value, country, radius_miles: radius } };
  }
  return { spec: { query: value.slice(0, 100), ...(country ? { country } : {}), radius_miles: radius } };
}

/** The saved service area of a card: {id, name, spec} with a server-valid id. */
function normalizeServiceArea(raw) {
  if (!raw || typeof raw !== "object" || typeof raw.id !== "string" || !SERVICE_AREA_ID.test(raw.id)) return undefined;
  const area = { id: raw.id, name: typeof raw.name === "string" && raw.name.trim() ? raw.name.trim().slice(0, 60) : "Area" };
  if (raw.spec && typeof raw.spec === "object" && !Array.isArray(raw.spec)) {
    const spec = {};
    for (const key of SPEC_KEYS) {
      const value = raw.spec[key];
      if ((typeof value === "string" && value.length <= 100) || (typeof value === "number" && Number.isFinite(value))) spec[key] = value;
    }
    area.spec = spec;
  }
  return area;
}

/** What a script call returned: {status, content}. Accepts the response directly or wrapped by a service/script key. */
function extractServiceResponse(result) {
  let response = result && typeof result === "object" && "response" in result ? result.response : result;
  const looksRight = (value) => value && typeof value === "object" && typeof value.status === "number";
  if (!looksRight(response) && response && typeof response === "object") {
    const nested = Object.values(response).find(looksRight);
    if (nested) response = nested;
  }
  if (!looksRight(response)) throw new Error("The Home Assistant script returned no response.");
  let content = response.content;
  if (typeof content === "string") {
    try {
      content = JSON.parse(content);
    } catch {
      // keep the text
    }
  }
  return { status: response.status, content };
}

function apiMessage(content, status) {
  return (content && content.error && content.error.message) || (typeof content === "string" && content.slice(0, 200)) || `The server answered ${status}.`;
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
 * of its title/feed/entity so different cards do not collide. Two cards with
 * identical configuration on one dashboard need distinct `id` values.
 */
function prefsKey(config) {
  const path = (typeof window !== "undefined" && window.location && window.location.pathname) || "/";
  const explicit = typeof config.id === "string" ? config.id.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40) : "";
  const instance = explicit || `h${hashString(JSON.stringify([config.title || "", config.feed || "", config.entity || "", Array.isArray(config.events) ? "inline" : ""]))}`;
  return `area-events-card:prefs:v1:${encodeURIComponent(path)}:${instance}`;
}

function readLocal(key) {
  try {
    const text = window.localStorage.getItem(key);
    return text === null ? null : normalizePrefs(JSON.parse(text));
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
    if (!config || (!config.entity && !config.feed && !Array.isArray(config.events) && config.source !== "service")) {
      throw new Error("Define 'feed', 'entity', 'events' or 'source: service'.");
    }
    if (config.script_prefix !== undefined && !/^[a-z][a-z0-9_]{0,40}$/.test(String(config.script_prefix))) {
      throw new Error("'script_prefix' must be lowercase letters, digits and underscores.");
    }
    if (config.area !== undefined && !normalizeArea({ kind: "configured", ...config.area })) {
      throw new Error("'area' needs valid latitude and longitude (and optionally name, radius_miles, timezone).");
    }
    this._config = config;
    this._feed = undefined;
    this._feedError = undefined;
    this._serviceRun = (this._serviceRun || 0) + 1;
    clearTimeout(this._serviceTimer);
    this._service = { phase: "idle" };
    this._serviceStarted = false;
    this._filterOpen = false;
    this._editing = undefined;
    this._initPrefs();
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
    if (this.isConnected) this._startFeed();
    this._loadRemotePrefs();
    this._serviceStart();
  }

  connectedCallback() {
    if (this._config) {
      this._startFeed();
      this._startAgeTimer();
    }
  }

  disconnectedCallback() {
    clearInterval(this._feedTimer);
    clearInterval(this._ageTimer);
    clearTimeout(this._serviceTimer);
    this._feedTimer = this._ageTimer = this._serviceTimer = undefined;
    this._serviceRun = (this._serviceRun || 0) + 1; // answers still in flight are ignored
    this._serviceStarted = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    // Only entity mode depends on hass state; static data renders once so
    // images are not reloaded on every state change. Themes need no re-render.
    if (first || (this._config && this._config.entity && !this._usesFeedOrInline())) this._render();
    this._loadRemotePrefs();
    this._serviceStart();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return { title: "Area Events", events: [] };
  }

  static get _internals() {
    return { categoriesFor, normalizePrefs, prefsKey, DEFAULT_CATEGORIES, haHomeArea, normalizeArea, resolveArea, backendHintCommand, milesBetween,
      parsePlace, extractServiceResponse, normalizeServiceArea, roundCoordinate };
  }

  // --- preferences ------------------------------------------------------------

  _initPrefs() {
    this._prefsKey = prefsKey(this._config);
    this._prefsGeneration = (this._prefsGeneration || 0) + 1;
    const local = readLocal(this._prefsKey);
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
        setTimeout(() => this._serviceStart(), 0);
        const value = response && typeof response === "object" && "value" in response ? response.value : response;
        const remote = value === null || value === undefined ? null : normalizePrefs(value);
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
        setTimeout(() => this._serviceStart(), 0);
      });
  }

  _saveRemote() {
    if (!this._hass || typeof this._hass.callWS !== "function") return;
    Promise.resolve()
      .then(() => this._hass.callWS({ type: "frontend/set_user_data", key: this._prefsKey, value: this._prefs }))
      .catch(() => {});
  }

  _setPrefs(next) {
    this._prefs = normalizePrefs(next) || EMPTY_PREFS();
    this._prefsDirty = true;
    writeLocal(this._prefsKey, this._prefs);
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => {
      if (this._remoteState !== "loading") this._saveRemote();
    }, SAVE_DEBOUNCE_MS);
    this._computeMatches();
    this._refreshFilters();
  }

  // --- feed ---------------------------------------------------------------------

  _usesFeedOrInline() {
    return Array.isArray(this._config.events) || Boolean(this._config.feed) || this._isService();
  }

  _startFeed() {
    if (this._isService()) {
      this._serviceStart();
      return;
    }
    if (!this._config.feed || Array.isArray(this._config.events)) return;
    clearInterval(this._feedTimer);
    this._loadFeed();
    this._feedTimer = setInterval(() => this._loadFeed(), FEED_REFRESH_MS);
  }

  // --- service mode ----------------------------------------------------------------

  _isService() {
    return Boolean(this._config) && this._config.source === "service" && !Array.isArray(this._config.events);
  }

  _setService(patch, feedChanged = false) {
    this._service = { ...this._service, ...patch };
    if (feedChanged) {
      this._render();
      return;
    }
    this._updateNotice();
    this._updateHeader();
    if (this._tiles && this._tiles.length === 0) this._renderFilterUI();
  }

  async _serviceCall(name, data = {}) {
    if (!this._hass || typeof this._hass.callWS !== "function") throw new Error("Home Assistant is not connected.");
    const script = `${this._config.script_prefix || "area_events"}_${name}`;
    let result;
    try {
      result = await this._hass.callWS({ type: "call_service", domain: "script", service: script, service_data: data, return_response: true });
    } catch (error) {
      throw new Error(`Home Assistant could not run script.${script} (${(error && error.message) || error}).`);
    }
    return extractServiceResponse(result);
  }

  async _serviceApi(name, data) {
    const { status, content } = await this._serviceCall(name, data);
    if (status >= 200 && status < 300) return { status, content };
    const error = new Error(apiMessage(content, status));
    error.status = status;
    throw error;
  }

  _defaultCountry() {
    const country = this._config.country || (this._hass && this._hass.config && this._hass.config.country);
    return typeof country === "string" && /^[A-Za-z]{2}$/.test(country) ? country.toUpperCase() : undefined;
  }

  _defaultRadius() {
    const radius = Number(this._config.radius_miles);
    return Number.isFinite(radius) && radius >= 1 && radius <= 250 ? radius : 25;
  }

  /** Where a card with no saved choice starts: its configured area, else the Home Assistant home location. */
  _serviceFallback() {
    const configured = this._config.area ? normalizeArea({ kind: "configured", ...this._config.area }) : undefined;
    if (configured) {
      const { latitude, longitude, timezone } = configured;
      return { name: configured.name || "Configured area", spec: { latitude, longitude, radius_miles: configured.radius_miles || this._defaultRadius(), ...(timezone ? { timezone } : {}) } };
    }
    const home = haHomeArea(this._hass);
    if (home) {
      return { name: home.name, spec: { latitude: roundCoordinate(home.latitude), longitude: roundCoordinate(home.longitude), radius_miles: this._defaultRadius(), ...(home.timezone ? { timezone: home.timezone } : {}) } };
    }
    return undefined;
  }

  _serviceStart() {
    if (!this._isService() || !this._hass || this._serviceStarted) return;
    if (typeof this._hass.callWS !== "function") {
      this._setService({ phase: "error", message: "Home Assistant is not connected." });
      return;
    }
    if (this._remoteState === "pending" || this._remoteState === "loading") return; // wait for the saved choice first
    this._serviceStarted = true;
    const run = ++this._serviceRun;
    if (this._prefs.service_area) {
      this._setService({ phase: "loading", message: "Loading events..." });
      this._serviceLoad(run);
      return;
    }
    const fallback = this._serviceFallback();
    if (fallback) {
      this._serviceSelect(fallback.spec, fallback.name);
      return;
    }
    this._filterOpen = true;
    this._setService({ phase: "nolocation", message: "No location is available. Open Filters > Area to choose a city, ZIP code or coordinates." });
    this._renderFilterUI();
  }

  /** Ask the server for an area (creating it if needed), remember it for this card, then load its events. */
  async _serviceSelect(spec, label) {
    clearTimeout(this._serviceTimer);
    const run = ++this._serviceRun;
    this._serviceRecreated = false;
    this._setService({ phase: "ensuring", message: "Asking the server to prepare this area...", formError: undefined });
    try {
      const { content } = await this._serviceApi("ensure", { spec: label ? { ...spec, name: label } : spec });
      if (run !== this._serviceRun) return;
      const choice = normalizeServiceArea({ id: content && content.id, name: (content && content.names && content.names[0]) || label, spec });
      if (!choice) throw new Error("The server returned an unusable area.");
      this._feed = undefined;
      this._setPrefs({ ...this._prefs, service_area: choice });
      this._setService({ phase: "loading", message: "Loading events...", areaStatus: content.status }, true);
      await this._serviceLoad(run);
    } catch (error) {
      if (run !== this._serviceRun) return;
      this._feedError = error.message;
      this._setService({ phase: "error", message: error.message, formError: error.message }, true);
    }
  }

  async _serviceLoad(run) {
    const area = this._prefs.service_area;
    if (!area || run !== this._serviceRun) return;
    clearTimeout(this._serviceTimer);
    const again = (ms) => {
      this._serviceTimer = setTimeout(() => this._serviceLoad(run), ms);
    };
    try {
      const { status, content } = await this._serviceCall("feed", { area_id: area.id });
      if (run !== this._serviceRun) return;
      if (status === 200 && content && Array.isArray(content.events)) {
        await this._serviceGotFeed(content, area, run);
        again(SERVICE_REFRESH_MS);
        return;
      }
      if (status === 404 && area.spec && !this._serviceRecreated) {
        this._serviceRecreated = true; // the server forgot this area (expired or reset): rebuild it from the saved choice
        await this._serviceSelect(area.spec, area.name);
        return;
      }
      if (status === 503) {
        await this._serviceWait(area, run, again);
        return;
      }
      throw new Error(apiMessage(content, status));
    } catch (error) {
      if (run !== this._serviceRun) return;
      this._feedError = error.message;
      this._setService({ phase: "error", message: error.message }, true);
      again(SERVICE_REFRESH_MS);
    }
  }

  async _serviceWait(area, run, again) {
    this._serviceWaitStarted = this._serviceWaitStarted || Date.now();
    let status;
    try {
      status = (await this._serviceApi("get", { area_id: area.id })).content.status;
    } catch {
      status = undefined;
    }
    if (run !== this._serviceRun) return;
    if (status && status.state === "failed") {
      this._feedError = status.last_error || "Discovery failed for this area.";
      this._setService({ phase: "error", message: this._feedError, areaStatus: status }, true);
      again(SERVICE_REFRESH_MS);
      return;
    }
    if (Date.now() - this._serviceWaitStarted > SERVICE_MAX_WAIT_MS) {
      this._feedError = "Discovery is taking much longer than expected.";
      this._setService({ phase: "error", message: this._feedError, areaStatus: status }, true);
      again(SERVICE_REFRESH_MS);
      return;
    }
    const running = status && status.state === "running";
    this._setService({ phase: "waiting", message: running ? "Discovering events for this area..." : "Waiting for the server to start discovery for this area...", areaStatus: status });
    again(SERVICE_POLL_MS);
  }

  async _serviceGotFeed(content, area, run) {
    this._serviceWaitStarted = undefined;
    let areaStatus;
    try {
      areaStatus = (await this._serviceApi("get", { area_id: area.id })).content.status;
    } catch {
      areaStatus = undefined; // the feed is what matters; status is a refinement
    }
    if (run !== this._serviceRun) return;
    const next = { events: content.events, generatedAt: content.generated_at, sources: content.sources, location: content.location };
    const unchanged = this._feed && !this._feedError && this._feed.generatedAt === next.generatedAt && this._feed.events.length === next.events.length;
    this._feed = next;
    this._feedError = undefined;
    this._setService({ phase: "ready", message: undefined, areaStatus }, !unchanged);
  }

  async _serviceLoadAreas() {
    try {
      const { content } = await this._serviceApi("list", {});
      this._setService({ areas: Array.isArray(content && content.areas) ? content.areas : [] });
    } catch (error) {
      this._setService({ areasError: error.message });
    }
    this._renderFilterUI();
  }

  async _serviceRefreshNow() {
    const area = this._prefs.service_area;
    if (!area) return;
    try {
      await this._serviceApi("refresh", { area_id: area.id });
      this._geoMessage = "Refresh requested. New results appear when discovery finishes.";
      this._feedError = undefined;
      this._serviceWaitStarted = Date.now();
      this._setService({ phase: "waiting", message: "Waiting for the server to refresh this area..." });
      clearTimeout(this._serviceTimer);
      this._serviceTimer = setTimeout(() => this._serviceLoad(this._serviceRun), SERVICE_POLL_MS);
    } catch (error) {
      this._geoMessage = error.status === 429 ? "This area was refreshed a moment ago; try again later." : error.message;
    }
    this._renderFilterUI();
  }

  _serviceChoose(entry) {
    const location = entry.location;
    if (entry.id === "default") {
      const run = ++this._serviceRun;
      this._feed = undefined;
      this._setPrefs({ ...this._prefs, service_area: { id: "default", name: (entry.names && entry.names[0]) || "Default area" } });
      this._setService({ phase: "loading", message: "Loading events...", areaStatus: entry.status }, true);
      this._serviceLoad(run);
      return;
    }
    const spec = { latitude: location.latitude, longitude: location.longitude, radius_miles: location.radius_miles };
    this._serviceSelect(spec, (entry.names && entry.names[0]) || location.display_name);
  }

  _serviceSearch(form) {
    const parsed = parsePlace(form.place, this._defaultCountry(), Number(form.radius) || this._defaultRadius());
    if (parsed.error) {
      this._setService({ formError: parsed.error });
      this._renderFilterUI();
      return;
    }
    this._serviceSelect(parsed.spec, form.name && form.name.trim() ? form.name.trim() : undefined);
  }

  _serviceHome() {
    const home = haHomeArea(this._hass);
    if (!home) return;
    this._serviceSelect({ latitude: roundCoordinate(home.latitude), longitude: roundCoordinate(home.longitude), radius_miles: this._defaultRadius(),
      ...(home.timezone ? { timezone: home.timezone } : {}) }, home.name);
  }

  /** Only ever called from a click, like the legacy panel: the permission prompt must follow a user action. */
  _serviceMine() {
    const geolocation = globalThis.navigator && globalThis.navigator.geolocation;
    if (!geolocation || typeof geolocation.getCurrentPosition !== "function") {
      this._geoMessage = "This browser does not support location.";
      this._renderFilterUI();
      return;
    }
    const request = (this._geoRequest = (this._geoRequest || 0) + 1);
    this._geoMessage = "Waiting for the browser's location permission...";
    this._renderFilterUI();
    const finish = (message) => {
      if (request !== this._geoRequest) return;
      this._geoMessage = message;
      this._renderFilterUI();
    };
    try {
      geolocation.getCurrentPosition(
        (position) => {
          const coords = position && position.coords;
          if (!coords || !validCoordinates(coords.latitude, coords.longitude)) return finish("The browser returned an unusable position.");
          if (request !== this._geoRequest) return;
          this._geoMessage = undefined;
          let timezone;
          try {
            timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
          } catch {
            timezone = undefined;
          }
          this._serviceSelect({ latitude: roundCoordinate(coords.latitude), longitude: roundCoordinate(coords.longitude), radius_miles: this._defaultRadius(),
            ...(timezone && TIMEZONE_NAME.test(timezone) ? { timezone } : {}) }, "My location");
        },
        (error) => finish(geolocationProblem(error)),
        { enableHighAccuracy: false, timeout: GEO_TIMEOUT_MS, maximumAge: GEO_MAX_AGE_MS },
      );
    } catch {
      finish("Your location could not be read.");
    }
  }

  _renderServiceAreaPanel() {
    const panel = this._el("div", "area service-area");
    const line = (text, className = "area-line") => panel.append(this._el("div", className, text));
    const saved = this._prefs.service_area;
    const status = this._service.areaStatus;
    const radius = saved && saved.spec && saved.spec.radius_miles;

    line(saved ? `Area: ${saved.name}${radius ? ` - ${Math.round(radius)} mi radius` : ""}` : "No area chosen yet.");
    const discovered = this._feed && this._feed.location;
    if (saved && discovered && discovered.display_name) {
      line(`The server discovered these events for: ${discovered.display_name}, ${Math.round(discovered.radius_miles || 0)} mi`, "area-detail");
    }
    if (status) {
      const when = status.last_success_at ? ` - last updated ${this._formatDate(status.last_success_at)}` : "";
      line(`Discovery: ${status.state}${when}${status.last_error ? ` - ${status.last_error}` : ""}`, status.state === "failed" ? "area-message" : "area-detail");
    }
    if (this._service.message && ["waiting", "ensuring", "loading"].includes(this._service.phase)) line(this._service.message, "area-detail");

    const areas = (this._service.areas || []).filter((entry) => !saved || entry.id !== saved.id);
    if (areas.length) {
      line("Saved areas:", "area-note");
      const row = this._el("div", "area-actions");
      for (const entry of areas) {
        const button = this._button((entry.names && entry.names[0]) || entry.id, () => this._serviceChoose(entry));
        button.className = "saved-area";
        row.append(button);
      }
      panel.append(row);
    } else if (this._service.areasError) {
      line(`Saved areas could not be loaded: ${this._service.areasError}`, "area-note");
    }

    const buttons = this._el("div", "area-actions");
    if (haHomeArea(this._hass)) {
      const home = this._button("Use Home Assistant home", () => this._serviceHome());
      home.className = "use-home";
      buttons.append(home);
    }
    const mine = this._button("Use my location", () => this._serviceMine());
    mine.className = "use-mine";
    buttons.append(mine);
    if (saved) {
      const refresh = this._button("Refresh now", () => this._serviceRefreshNow());
      refresh.className = "refresh-area";
      buttons.append(refresh);
    }
    panel.append(buttons);

    const form = { place: this._form ? this._form.place : "", radius: this._form ? this._form.radius : String(this._defaultRadius()), name: this._form ? this._form.name : "" };
    this._form = form;
    const field = (label, key, className) => {
      const wrapper = this._el("label", "", label);
      const input = document.createElement("input");
      input.className = className;
      input.type = "text";
      input.value = form[key];
      input.addEventListener("input", () => { form[key] = input.value; });
      wrapper.append(input);
      return wrapper;
    };
    const editor = this._el("div", "editor");
    editor.append(field("City, ZIP code or latitude, longitude", "place", "place-input"), field("Radius (miles)", "radius", "radius-input"), field("Name (optional)", "name", "area-name-input"));
    if (this._service.formError) editor.append(this._el("div", "error", this._service.formError));
    const search = this._button("Search this area", () => this._serviceSearch(form));
    search.className = "search-area";
    editor.append(search);
    panel.append(editor);
    if (this._geoMessage) line(this._geoMessage, "area-message");
    line("Each card keeps its own area. Choosing one asks the server to discover events there; results appear once it has run.", "area-note");
    return panel;
  }

  _startAgeTimer() {
    clearInterval(this._ageTimer);
    this._ageTimer = setInterval(() => this._updateHeader(), AGE_TICK_MS);
  }

  async _loadFeed() {
    const config = this._config;
    let next;
    let error;
    try {
      // A unique query plus no-store keeps the browser from serving a stale feed.
      const separator = config.feed.includes("?") ? "&" : "?";
      const response = await fetch(`${config.feed}${separator}ts=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const events = Array.isArray(data) ? data : data && data.events;
      if (!Array.isArray(events)) throw new Error("feed has no 'events' list");
      next = {
        events,
        generatedAt: Array.isArray(data) ? undefined : data.generated_at,
        sources: Array.isArray(data) ? undefined : data.sources,
        location: Array.isArray(data) ? undefined : data.location,
      };
    } catch (caught) {
      error = caught.message || String(caught);
    }
    if (config !== this._config) return;
    const unchanged = next && this._feed && !this._feedError && this._feed.generatedAt === next.generatedAt &&
      this._feed.events.length === next.events.length;
    if (next) this._feed = next;
    const previousError = this._feedError;
    this._feedError = error;
    // An identical feed leaves the tiles alone, so images are not reloaded.
    if (unchanged && previousError === error) {
      this._updateHeader();
      return;
    }
    this._render();
  }

  _events() {
    const { entity, events, feed } = this._config;
    if (Array.isArray(events)) return events.map((event) => this._normalizeEvent(event));
    if (feed || this._isService()) {
      const now = Date.now();
      return ((this._feed && this._feed.events) || [])
        .map((event) => this._normalizeEvent(event))
        .filter((event) => {
          const last = new Date(event.end || event.start).getTime();
          return Number.isNaN(last) || last >= now;
        });
    }
    const state = this._hass && this._hass.states[entity];
    const list = state && Array.isArray(state.attributes.events) ? state.attributes.events : [];
    return list.map((event) => this._normalizeEvent(event));
  }

  // Accepts both the card's own event fields and the discovery feed format.
  _normalizeEvent(event) {
    const verification = event.verification || {};
    const categories = Array.isArray(event.categories) ? event.categories : event.category ? [event.category] : [];
    return {
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

  /** Authoritative generation time: the feed's `generated_at` (or entity attribute), never the page-load time. */
  _generatedAt() {
    if (this._config.feed || this._isService()) return this._feed && this._feed.generatedAt;
    if (this._config.entity && !Array.isArray(this._config.events)) {
      const state = this._hass && this._hass.states[this._config.entity];
      return state && state.attributes.generated_at;
    }
    return undefined;
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

  _feedNotice(count) {
    if (this._isService()) {
      const state = this._service || {};
      const updated = this._feed && this._feed.generatedAt ? this._formatDate(this._feed.generatedAt) : undefined;
      if (this._feedError) {
        return this._feed
          ? `Could not refresh events (${this._feedError}). Showing stale data${updated ? ` from ${updated}` : ""}.`
          : `Could not load events: ${this._feedError}`;
      }
      if (state.phase === "nolocation") return state.message;
      if (!this._feed && ["idle", "ensuring", "loading", "waiting"].includes(state.phase)) return state.message || "Loading events...";
      if (state.areaStatus && state.areaStatus.state === "failed") {
        return `The last discovery for this area failed${state.areaStatus.last_error ? ` (${state.areaStatus.last_error})` : ""}. Showing the last successful results.`;
      }
      return count === 0 && this._feed ? "No upcoming events in this area." : undefined;
    }
    if (!this._config.feed) return undefined;
    const updated = this._feed && this._feed.generatedAt ? this._formatDate(this._feed.generatedAt) : undefined;
    if (this._feedError) {
      return this._feed
        ? `Could not refresh events (${this._feedError}). Showing stale data${updated ? ` from ${updated}` : ""}.`
        : `Could not load events feed: ${this._feedError}`;
    }
    if (!this._feed) return "Loading events...";
    return count === 0 ? "No upcoming events in the feed." : undefined;
  }

  // --- rendering ----------------------------------------------------------------

  _categories() {
    return [...DEFAULT_CATEGORIES.filter((c) => c.id !== OTHER_ID), ...this._prefs.custom, DEFAULT_CATEGORIES.at(-1)];
  }

  _computeMatches() {
    for (const tile of this._tiles || []) tile.matches = categoriesFor(tile.event, this._prefs.custom);
  }

  _render() {
    if (!this._config || !this.shadowRoot) return;
    const events = this._events();
    const root = this.shadowRoot;
    root.replaceChildren();

    const style = document.createElement("style");
    style.textContent = STYLES;
    const card = document.createElement("ha-card");
    const wrap = this._el("div", "wrap");

    this._els = {};
    this._els.header = this._el("div", "sticky-header");
    wrap.append(this._els.header);
    if (this._config.demo) wrap.append(this._el("div", "demo-banner", "DEMONSTRATION DATA - not real events"));

    this._els.notice = this._el("div", "notice");
    wrap.append(this._els.notice);
    this._updateNotice(events.length);

    this._els.filters = this._el("div", "filters");
    wrap.append(this._els.filters);

    this._tiles = [];
    if (events.length > 0) {
      const grid = this._el("div", "grid");
      for (const event of events) {
        const tile = { event, el: this._renderEvent(event), matches: categoriesFor(event, this._prefs.custom) };
        this._tiles.push(tile);
        grid.append(tile.el);
      }
      wrap.append(grid);
      this._els.noMatch = this._el("div", "no-match", "No events match the selected categories.");
      this._els.noMatch.hidden = true;
      wrap.append(this._els.noMatch);
    } else if (!this._config.feed && !this._isService()) {
      wrap.append(this._el("div", "empty", "No events to show."));
    }

    const updated = this._generatedAt() ? ` · feed updated ${this._formatDate(this._generatedAt())}` : "";
    wrap.append(this._el("div", "footer", `${AREA_EVENTS_CARD_BUILD}${updated}`));
    card.append(wrap);
    root.append(style, card);
    this._refreshFilters();
  }

  _updateNotice(count) {
    if (!this._els || !this._els.notice) return;
    const text = this._feedNotice(count === undefined ? (this._tiles || []).length : count);
    this._els.notice.textContent = text || "";
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
    if (this._feedError) meta.append(this._el("span", "badge error", this._feed ? "Update failed" : "Feed unavailable"));
    if (this._isService()) {
      const state = this._service || {};
      if (["ensuring", "loading", "waiting"].includes(state.phase)) meta.append(this._el("span", "badge", this._feed ? "Updating" : "Discovering"));
      if (state.areaStatus && state.areaStatus.state === "failed" && !this._feedError) meta.append(this._el("span", "badge error", "Discovery failed"));
      const saved = this._prefs.service_area;
      if (saved) meta.append(this._el("span", "area-name", saved.name));
    }

    const total = this._tiles.length;
    const shown = this._visibleCount();
    if ((this._config.feed || this._isService()) ? this._feed : true) {
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
    if (!this._tiles || this._tiles.length === 0) {
      if (this._els.filters) this._els.filters.replaceChildren();
      this._updateHeader();
      if (this._isService()) this._renderFilterUI(); // the area controls matter most when there are no events yet
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

  // --- area ----------------------------------------------------------------------

  _area() {
    const configured = this._config.area ? normalizeArea({ kind: "configured", ...this._config.area }) : undefined;
    return resolveArea({ saved: this._prefs.area, configured, hass: this._hass });
  }

  _chooseHomeAssistant() {
    this._geoRequest = (this._geoRequest || 0) + 1; // a browser answer still pending no longer applies
    this._geoMessage = undefined;
    this._setPrefs({ ...this._prefs, area: { kind: "ha_home" } });
  }

  _clearArea() {
    this._geoRequest = (this._geoRequest || 0) + 1;
    this._geoMessage = undefined;
    const { area, ...rest } = this._prefs;
    this._setPrefs(rest);
  }

  /** Only ever called from a click: the browser's permission prompt must follow an explicit user action. */
  _useMyLocation() {
    const geolocation = globalThis.navigator && globalThis.navigator.geolocation;
    if (!geolocation || typeof geolocation.getCurrentPosition !== "function") {
      this._geoMessage = "This browser does not support location.";
      this._renderFilterUI();
      return;
    }
    const request = (this._geoRequest = (this._geoRequest || 0) + 1);
    this._geoMessage = "Waiting for the browser's location permission...";
    this._renderFilterUI();
    const finish = (message) => {
      if (request !== this._geoRequest) return;
      this._geoMessage = message;
      this._renderFilterUI();
    };
    try {
      geolocation.getCurrentPosition(
        (position) => {
          const coords = position && position.coords;
          if (!coords || !validCoordinates(coords.latitude, coords.longitude)) return finish("The browser returned an unusable position.");
          let timezone;
          try {
            timezone = Intl.DateTimeFormat().resolvedOptions().timeZone; // this browser's own timezone setting
          } catch {
            timezone = undefined;
          }
          if (request !== this._geoRequest) return;
          this._geoMessage = undefined;
          this._setPrefs({
            ...this._prefs,
            area: { kind: "browser", latitude: coords.latitude, longitude: coords.longitude, accuracy_m: coords.accuracy, timezone, saved_at: new Date().toISOString() },
          });
        },
        (error) => finish(geolocationProblem(error)),
        { enableHighAccuracy: false, timeout: GEO_TIMEOUT_MS, maximumAge: GEO_MAX_AGE_MS },
      );
    } catch {
      finish("Your location could not be read.");
    }
  }

  _renderAreaPanel() {
    if (this._isService()) return this._renderServiceAreaPanel();
    const panel = this._el("div", "area");
    const resolved = this._area();
    const labels = {
      selected_home_assistant: "Home Assistant home location (selected)", selected_browser: "your browser location (saved for this card)",
      configured: "set in the card configuration", home_assistant: "Home Assistant home location",
    };
    const line = (text, className = "area-line") => panel.append(this._el("div", className, text));

    if (resolved.area) {
      const area = resolved.area;
      line(`Area: ${area.name} - ${labels[resolved.source]}`);
      line(`${area.latitude.toFixed(4)}, ${area.longitude.toFixed(4)}${area.timezone ? ` - ${area.timezone}` : ""}${Number.isFinite(area.accuracy_m) ? ` - accurate to about ${Math.round(area.accuracy_m)} m` : ""}`, "area-detail");
    } else {
      line(`${resolved.reason} Use the Home Assistant home location, or choose "Use my location" (the browser will ask permission).`);
    }

    const discovered = this._feed && this._feed.location;
    if (discovered && validCoordinates(Number(discovered.latitude), Number(discovered.longitude))) {
      line(`Events were discovered for: ${discovered.display_name || "an unnamed area"}, ${Math.round(discovered.radius_miles || 0)} mi radius`, "area-line");
      if (resolved.area) {
        const miles = milesBetween(resolved.area, { latitude: Number(discovered.latitude), longitude: Number(discovered.longitude) });
        line(miles > (discovered.radius_miles || 0)
          ? `Your area is ${Math.round(miles)} mi from the discovery area, so these events may not be near you.`
          : "Your area is inside the discovery area.", "area-line");
      }
    } else if (this._config.feed) {
      line("This feed does not say which area its events were discovered for.");
    }
    line("Choosing an area here only changes this card. The discovery service searches the area in its own configuration (or a location hint recorded on the server); nothing here is sent to it.", "area-note");

    const buttons = this._el("div", "area-actions");
    const home = haHomeArea(this._hass);
    if (home) {
      const useHome = this._button("Use Home Assistant home", () => this._chooseHomeAssistant(), Boolean(this._prefs.area && this._prefs.area.kind === "ha_home"));
      useHome.className = "use-home";
      buttons.append(useHome);
    }
    const mine = this._button("Use my location", () => this._useMyLocation());
    mine.className = "use-mine";
    buttons.append(mine);
    if (this._prefs.area) {
      const clear = this._button("Clear selection", () => this._clearArea());
      clear.className = "clear-area";
      buttons.append(clear);
    }
    panel.append(buttons);
    if (this._geoMessage) line(this._geoMessage, "area-message");
    if (resolved.area && resolved.source !== "unavailable") {
      line("To make the discovery service use this area, run on the server:", "area-note");
      line(backendHintCommand(resolved.area), "hint-command");
    }
    return panel;
  }

  _renderFilterUI() {
    const host = this._els.filters;
    if (!host) return;
    host.replaceChildren();
    const selected = new Set(this._prefs.selected);
    const categories = this._categories();
    const counts = new Map(categories.map((c) => [c.id, 0]));
    for (const tile of this._tiles) for (const id of tile.matches) counts.set(id, (counts.get(id) || 0) + 1);

    const bar = this._el("div", "filter-bar");
    const toggle = this._button(`Filters${selected.size ? ` (${selected.size})` : ""}`, () => {
      this._filterOpen = !this._filterOpen;
      this._renderFilterUI();
      if (this._filterOpen && this._isService()) this._serviceLoadAreas(); // saved areas are listed only when asked for
    });
    toggle.className = "filter-toggle";
    toggle.setAttribute("aria-expanded", String(this._filterOpen));
    const names = categories.filter((c) => selected.has(c.id)).map((c) => c.name);
    const all = this._button("Show all", () => this._setPrefs({ ...this._prefs, selected: [] }), selected.size === 0);
    all.className = "show-all";
    bar.append(all, toggle, this._el("span", "filter-summary", selected.size ? `Showing: ${names.join(", ")}` : "Showing all categories"));
    host.append(bar);
    if (!this._filterOpen) return;
    host.append(this._renderAreaPanel());
    if (this._tiles.length === 0) return;

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
    if (event.image) {
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

if (!customElements.get(CARD_TAG)) {
  customElements.define(CARD_TAG, AreaEventsCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: `custom:${CARD_TAG}`,
  name: "Area Events Card",
  description: "Local events for your area.",
});

console.info(
  `%c AREA-EVENTS-CARD %c ${AREA_EVENTS_CARD_BUILD} `,
  "color:#fff;background:#03a9f4;font-weight:700",
  "color:#03a9f4;background:#fff;font-weight:700"
);
