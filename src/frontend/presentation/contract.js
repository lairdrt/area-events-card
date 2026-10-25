/** Versioned input and renderer contract shared by presentation modules. */
export const PRESENTATION_CONTRACT = "area-events.presentation.v1";

export function isRenderer(value) {
  return Boolean(value && typeof value === "object" && typeof value.id === "string" && value.id.trim()
    && value.contract === PRESENTATION_CONTRACT && Array.isArray(value.eventTypes)
    && typeof value.render === "function" && typeof value.styles === "string");
}

export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
