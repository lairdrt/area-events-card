import { element, PRESENTATION_CONTRACT } from "./contract.js";

/** Safe generic renderer used for unknown event types and renderer failures. */
export const genericRenderer = {
  id: "generic",
  contract: PRESENTATION_CONTRACT,
  eventTypes: ["*"],
  styles: ".generic-event { min-height: 100%; }",
  render(event, context) {
    const tile = element("article", "event generic-event");
    const body = element("div", "body");
    body.append(element("div", "title", event?.title || "Untitled event"));
    if (event?.start) body.append(context.meta("mdi:calendar-clock", context.formatWhen ? context.formatWhen(event.start, event.end, event.timezone) : context.formatDate(event.start, event.timezone)));
    if (event?.location) body.append(context.meta("mdi:map-marker", event.location));
    if (event?.description) body.append(element("div", "description", event.description));
    tile.append(body);
    return tile;
  },
  cleanup() {},
};
