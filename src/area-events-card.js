/**
 * Area Events Card - Home Assistant custom Lovelace card.
 *
 * Reads events from `config.events` or from the `events` attribute of
 * `config.entity`. Contains no sample data; see examples/demo-card.yaml.
 */

const AREA_EVENTS_CARD_BUILD = "__AREA_EVENTS_CARD_BUILD__";
const CARD_TAG = "area-events-card";

const STYLES = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; padding: 16px 16px 8px;
    font-size: var(--ha-card-header-font-size, 24px);
    color: var(--ha-card-header-color, var(--primary-text-color));
  }
  .demo-banner {
    margin: 0 16px 8px; padding: 6px 12px; border-radius: 8px;
    background: var(--warning-color, #ffa600); color: var(--text-primary-color, #fff);
    font-size: 12px; font-weight: 500; text-align: center;
  }
  .grid {
    display: grid; gap: 12px; padding: 8px 16px 16px;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }
  .event {
    display: flex; flex-direction: column; overflow: hidden;
    border-radius: var(--ha-card-border-radius, 12px);
    border: 1px solid var(--divider-color);
    background: var(--card-background-color);
  }
  .image {
    position: relative; aspect-ratio: 16 / 9;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, var(--primary-color), var(--accent-color, var(--secondary-text-color)));
    color: var(--text-primary-color, #fff);
    background-size: cover; background-position: center;
  }
  .image ha-icon { --mdc-icon-size: 48px; opacity: 0.85; }
  .category {
    position: absolute; top: 8px; left: 8px; padding: 2px 8px; border-radius: 12px;
    background: rgba(0, 0, 0, 0.55); color: #fff; font-size: 12px;
  }
  .body { display: flex; flex-direction: column; gap: 4px; padding: 12px; }
  .title { font-size: 16px; font-weight: 500; color: var(--primary-text-color); }
  .meta {
    display: flex; align-items: center; gap: 6px;
    font-size: 13px; color: var(--secondary-text-color);
  }
  .meta ha-icon { --mdc-icon-size: 16px; flex: none; }
  .verify { margin-top: 4px; font-size: 12px; font-weight: 500; }
  .verify.verified { color: var(--success-color, #43a047); }
  .verify.unverified { color: var(--secondary-text-color); }
  .verify ha-icon { --mdc-icon-size: 16px; vertical-align: -3px; }
  .empty { padding: 16px; color: var(--secondary-text-color); }
  .footer {
    padding: 0 16px 12px; text-align: left;
    font-size: 11px; color: var(--secondary-text-color);
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
