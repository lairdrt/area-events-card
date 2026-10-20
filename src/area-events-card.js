/**
 * Area Events Card - Home Assistant custom Lovelace card.
 *
 * Reads events from `config.events` or from the `events` attribute of
 * `config.entity`. Contains no sample data; see examples/demo-card.yaml.
 */

const AREA_EVENTS_CARD_BUILD = "__AREA_EVENTS_CARD_BUILD__";
const CARD_TAG = "area-events-card";

/*
 * Theme: colors come only from Home Assistant theme variables, so the card
 * follows light/dark theme changes through CSS inheritance (no JS needed).
 * Tiles are a tint of the primary text color over the card background, which
 * stays visible even in themes (e.g. Graphite E-ink Dark) where card, page
 * and secondary backgrounds are identical. Verification and category are
 * always conveyed by icon and text, never by color alone.
 *
 * Layout: auto-fit columns, 220px minimum tile width, capped at four, so the
 * column count follows the card width (not the viewport). No breakpoints and
 * no font scaling.
 */
const STYLES = `
  :host { display: block; }
  ha-card { height: 100%; box-sizing: border-box; padding: 12px 12px 8px; overflow: hidden; }
  .header {
    padding: 4px 4px 8px; overflow-wrap: anywhere;
    font-size: var(--ha-card-header-font-size, 24px);
    color: var(--ha-card-header-color, var(--primary-text-color));
  }
  .demo-banner {
    margin: 0 0 8px; padding: 6px 12px; border-radius: 8px;
    border: 1px solid var(--warning-color, var(--divider-color));
    color: var(--primary-text-color);
    font-size: 0.875rem; font-weight: min(var(--ha-font-weight-medium, 500), 600);
    text-align: center; overflow-wrap: anywhere;
  }
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
const FEED_STALE_MS = 48 * 60 * 60 * 1000;

class AreaEventsCard extends HTMLElement {
  setConfig(config) {
    if (!config || (!config.entity && !config.feed && !Array.isArray(config.events))) {
      throw new Error("Define 'feed', 'entity' or 'events'.");
    }
    this._config = config;
    this._feed = undefined;
    this._feedError = undefined;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
    if (this.isConnected) this._startFeed();
  }

  connectedCallback() {
    if (this._config) this._startFeed();
  }

  disconnectedCallback() {
    clearInterval(this._feedTimer);
    this._feedTimer = undefined;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    // Only entity mode depends on hass state; static data renders once so
    // images are not reloaded on every state change. Themes need no re-render.
    if (first || (this._config && this._config.entity && !this._usesFeedOrInline())) this._render();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return { title: "Area Events", events: [] };
  }

  _usesFeedOrInline() {
    return Array.isArray(this._config.events) || Boolean(this._config.feed);
  }

  _startFeed() {
    if (!this._config.feed || Array.isArray(this._config.events)) return;
    clearInterval(this._feedTimer);
    this._loadFeed();
    this._feedTimer = setInterval(() => this._loadFeed(), FEED_REFRESH_MS);
  }

  async _loadFeed() {
    const config = this._config;
    try {
      // A unique query plus no-store keeps the browser from serving a stale feed.
      const separator = config.feed.includes("?") ? "&" : "?";
      const response = await fetch(`${config.feed}${separator}ts=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const events = Array.isArray(data) ? data : data && data.events;
      if (!Array.isArray(events)) throw new Error("feed has no 'events' list");
      if (config !== this._config) return;
      this._feed = { events, generatedAt: Array.isArray(data) ? undefined : data.generated_at };
      this._feedError = undefined;
    } catch (error) {
      if (config !== this._config) return;
      this._feedError = error.message || String(error);
    }
    this._render();
  }

  _events() {
    const { entity, events, feed } = this._config;
    if (Array.isArray(events)) return events.map((event) => this._normalizeEvent(event));
    if (feed) {
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
    return {
      title: event.title,
      start: event.start,
      end: event.end,
      location: event.location || [event.venue, event.address].filter(Boolean).join(", "),
      category: event.category || (Array.isArray(event.categories) ? event.categories[0] : undefined),
      image: event.image || event.image_url,
      description: event.description,
      url: event.url,
      sourceName: event.source && event.source.name,
      verified: event.verified === true,
      status: verification.status,
      evidence: Array.isArray(verification.evidence) ? verification.evidence : [],
    };
  }

  _formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "";
    const locale = this._hass && this._hass.locale && this._hass.locale.language;
    return date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
  }

  _feedNotice(count) {
    if (!this._config.feed) return undefined;
    const updated = this._feed && this._feed.generatedAt ? this._formatDate(this._feed.generatedAt) : undefined;
    if (this._feedError) {
      return this._feed
        ? `Could not refresh events (${this._feedError}). Showing stale data${updated ? ` from ${updated}` : ""}.`
        : `Could not load events feed: ${this._feedError}`;
    }
    if (!this._feed) return "Loading events...";
    const generated = this._feed.generatedAt ? new Date(this._feed.generatedAt).getTime() : NaN;
    if (!Number.isNaN(generated) && Date.now() - generated > FEED_STALE_MS) {
      return `Events data is stale (last updated ${updated}).`;
    }
    return count === 0 ? "No upcoming events in the feed." : undefined;
  }

  _render() {
    if (!this._config || !this.shadowRoot) return;
    const events = this._events();
    const root = this.shadowRoot;
    root.replaceChildren();

    const style = document.createElement("style");
    style.textContent = STYLES;
    const card = document.createElement("ha-card");
    if (this._config.title) card.append(this._el("div", "header", this._config.title));
    if (this._config.demo) card.append(this._el("div", "demo-banner", "DEMONSTRATION DATA - not real events"));

    const notice = this._feedNotice(events.length);
    if (notice) card.append(this._el("div", "notice", notice));
    if (events.length > 0) {
      const grid = this._el("div", "grid");
      events.forEach((event) => grid.append(this._renderEvent(event)));
      card.append(grid);
    } else if (!this._config.feed) {
      card.append(this._el("div", "empty", "No events to show."));
    }

    const updated = this._feed && this._feed.generatedAt ? ` · feed updated ${this._formatDate(this._feed.generatedAt)}` : "";
    card.append(this._el("div", "footer", `${AREA_EVENTS_CARD_BUILD}${updated}`));
    root.append(style, card);
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
