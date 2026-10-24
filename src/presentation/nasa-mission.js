import { element, PRESENTATION_CONTRACT } from "./contract.js";

/** Synthetic-only renderer proof. Its detail fields are optional typed event data. */
export const nasaMissionRenderer = {
  id: "nasa-mission",
  contract: PRESENTATION_CONTRACT,
  eventTypes: ["nasa.mission"],
  styles: `
    .mission-event { min-height: 100%; }
    .mission-kicker { color: var(--primary-color); font-size: 0.75rem; font-weight: 700; letter-spacing: 0.08em; }
    .mission-detail { font-size: 0.875rem; color: var(--secondary-text-color); overflow-wrap: anywhere; }
    .mission-label { color: var(--primary-text-color); font-weight: min(var(--ha-font-weight-medium, 500), 600); }
  `,
  render(event) {
    const details = event?.details && typeof event.details === "object" ? event.details : {};
    const tile = element("article", "event mission-event");
    const body = element("div", "body");
    body.append(element("div", "mission-kicker", "MISSION"));
    body.append(element("div", "title", details.name || event?.title || "Untitled mission"));
    for (const [label, value] of [
      ["Launch window", details.launch_window],
      ["Launch date", details.launch_date],
      ["Vehicle", details.launch_vehicle],
      ["Status", details.status],
      ["Contractor / operator", details.operator],
    ]) {
      if (typeof value === "string" && value.trim()) {
        const row = element("div", "mission-detail");
        row.append(element("span", "mission-label", `${label}: `), element("span", "", value));
        body.append(row);
      }
    }
    tile.append(body);
    return tile;
  },
  cleanup() {},
};
