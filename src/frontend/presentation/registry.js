import { isRenderer } from "./contract.js";

/** A small registry independent of event discovery and the Home Assistant host. */
export class PresentationRegistry {
  constructor(renderers) {
    this._renderers = new Map();
    for (const renderer of renderers) this.register(renderer);
  }

  register(renderer) {
    if (!isRenderer(renderer)) throw new TypeError("Renderer must implement area-events.presentation.v1");
    if (this._renderers.has(renderer.id)) throw new Error(`Renderer already registered: ${renderer.id}`);
    this._renderers.set(renderer.id, renderer);
    return this;
  }

  validate(config = {}) {
    const normalized = config === undefined ? {} : config;
    if (!normalized || typeof normalized !== "object" || Array.isArray(normalized)) {
      throw new TypeError("presentation must be an object");
    }
    const unknown = Object.keys(normalized).filter((key) => !["default_renderer", "event_types"].includes(key));
    if (unknown.length) throw new TypeError(`Unknown presentation option: ${unknown[0]}`);
    const fallback = this._renderers.get("generic");
    const defaultId = normalized.default_renderer === undefined ? "community" : normalized.default_renderer;
    if (typeof defaultId !== "string" || !this._renderers.has(defaultId)) {
      throw new TypeError(`Unknown presentation.default_renderer: ${String(defaultId)}`);
    }
    const mappings = normalized.event_types === undefined ? {} : normalized.event_types;
    if (!mappings || typeof mappings !== "object" || Array.isArray(mappings)) {
      throw new TypeError("presentation.event_types must be an object mapping event types to renderer ids");
    }
    const mappingPrototype = Object.getPrototypeOf(mappings);
    if (mappingPrototype !== Object.prototype && mappingPrototype !== null) {
      throw new TypeError("presentation.event_types must be a plain object");
    }
    const eventTypes = Object.create(null);
    for (const [type, id] of Object.entries(mappings)) {
      if (!type.trim()) throw new TypeError("presentation.event_types cannot contain an empty event type");
      const renderer = this._renderers.get(id);
      if (!renderer) throw new TypeError(`Unknown renderer for event type ${type}: ${String(id)}`);
      if (!renderer.eventTypes.includes("*") && !renderer.eventTypes.includes(type)) {
        throw new TypeError(`Renderer ${id} does not support event type ${type}`);
      }
      eventTypes[type] = id;
    }
    if (!fallback || !fallback.eventTypes.includes("*")) throw new Error("A generic wildcard fallback renderer is required");
    return { default_renderer: defaultId, event_types: eventTypes };
  }

  select(event, config) {
    const settings = this.validate(config);
    const type = event && event.type;
    if (typeof type === "string" && type) {
      const mapped = Object.hasOwn(settings.event_types, type) ? settings.event_types[type] : undefined;
      if (mapped) return this._renderers.get(mapped);
      const preferred = this._renderers.get(settings.default_renderer);
      return preferred.eventTypes.includes("*") || preferred.eventTypes.includes(type)
        ? preferred : this._renderers.get("generic");
    }
    return this._renderers.get(settings.default_renderer);
  }

  render(event, config, context) {
    const renderer = this.select(event, config);
    try {
      const output = renderer.render(event, context);
      if (output && output.nodeType === 1) return { element: output, renderer };
      context.onRendererError?.(renderer.id, new TypeError("Renderer output must be one DOM element"));
    } catch (error) {
      context.onRendererError?.(renderer.id, error);
    }
    const fallback = this._renderers.get("generic");
    const output = fallback.render(event, context);
    if (!output || output.nodeType !== 1) throw new TypeError("Generic renderer returned invalid output");
    return { element: output, renderer: fallback };
  }

  get ids() {
    return [...this._renderers.keys()];
  }

  get styles() {
    return [...this._renderers.values()].map((renderer) => renderer.styles || "").filter(Boolean).join("\n");
  }
}
