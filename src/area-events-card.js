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
    background-size: cover; background-position: center;
    color: var(--secondary-text-color);
  }
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
  .verify.unverified { color: var(--secondary-text-color); }
  .verify ha-icon { --mdc-icon-size: 16px; vertical-align: -3px; }
  .empty { padding: 16px 4px; color: var(--secondary-text-color); }
  /* Build tag, bottom-left, below the grid. A diagnostic label, not content. */
  .footer {
    margin-top: 8px; font-size: 0.8125rem; line-height: 1.25; letter-spacing: 0.02em;
    color: var(--secondary-text-color); overflow-wrap: anywhere;
  }
`;

class AreaEventsCard extends HTMLElement {
  setConfig(config) {
    if (!config || (!config.entity && !Array.isArray(config.events))) {
      throw new Error("Define either 'entity' or 'events'.");
    }
    this._config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return { title: "Area Events", events: [] };
  }

  _events() {
    const { entity, events } = this._config;
    if (Array.isArray(events)) return events;
    const state = this._hass && this._hass.states[entity];
    return state && Array.isArray(state.attributes.events) ? state.attributes.events : [];
  }

  _formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "";
    const locale = this._hass && this._hass.locale && this._hass.locale.language;
    return date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
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

    if (events.length === 0) {
      card.append(this._el("div", "empty", "No events to show."));
    } else {
      const grid = this._el("div", "grid");
      events.forEach((event) => grid.append(this._renderEvent(event)));
      card.append(grid);
    }
    card.append(this._el("div", "footer", AREA_EVENTS_CARD_BUILD));
    root.append(style, card);
  }

  _renderEvent(event) {
    const item = this._el("div", "event");

    const image = this._el("div", "image");
    if (event.image) image.style.backgroundImage = `url("${String(event.image).replace(/"/g, "%22")}")`;
    else image.append(this._icon("mdi:calendar-star"));
    if (event.category) image.append(this._el("span", "category", event.category));

    const body = this._el("div", "body");
    body.append(this._el("div", "title", event.title || "Untitled event"));
    if (event.start) body.append(this._meta("mdi:calendar-clock", this._formatDate(event.start)));
    if (event.location) body.append(this._meta("mdi:map-marker", event.location));

    const verified = Boolean(event.verified);
    const verify = this._el("div", `verify ${verified ? "verified" : "unverified"}`);
    verify.append(this._icon(verified ? "mdi:check-decagram" : "mdi:help-circle-outline"));
    verify.append(document.createTextNode(verified ? " Verified" : " Unverified"));
    body.append(verify);

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
  description: "Verified local events for your area.",
});

console.info(
  `%c AREA-EVENTS-CARD %c ${AREA_EVENTS_CARD_BUILD} `,
  "color:#fff;background:#03a9f4;font-weight:700",
  "color:#03a9f4;background:#fff;font-weight:700"
);
