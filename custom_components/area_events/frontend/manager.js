/* Area Events Management Console v1: native HA session, stable control contracts, no secrets in browser. */
const WS = "area_events/";
const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[c]);
const asInt = (value, fallback) => value === "" || value == null ? fallback : Number(value);
const REASON_LABELS = { duplicate: "Duplicate event", not_relevant: "Not relevant to my area", incorrect: "Incorrect or misleading information",
  expired_or_canceled: "Expired or canceled event", advertisement: "Advertisement / not a real event", other: "Other / don't want to see it" };
const CSS = [
  ":host{display:block;color:var(--primary-text-color);background:var(--primary-background-color);min-height:100%;font-family:Roboto,Arial,sans-serif;--ae-line:var(--divider-color,#d4d7dd);--ae-surface:var(--card-background-color,#fff);--ae-muted:var(--secondary-text-color,#657083);--ae-accent:var(--primary-color,#187bcb)}",
  "*{box-sizing:border-box}button,input,select{font:inherit}button{cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}",
  ".shell{max-width:1450px;margin:0 auto;padding:24px 28px 60px}.top{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:23px}",
  "h1{font-size:27px;font-weight:650;letter-spacing:-.5px;margin:0}h2{font-size:17px;font-weight:650;margin:0 0 5px}h3{font-size:15px;margin:0}",
  ".sub,.muted{color:var(--ae-muted)}.sub{font-size:13px;margin-top:5px}.eyebrow{text-transform:uppercase;letter-spacing:.7px;font-size:11px;font-weight:650;color:var(--ae-muted)}",
  ".pill{border:1px solid var(--ae-line);border-radius:30px;padding:6px 10px;font-size:12px;display:inline-flex;align-items:center;gap:6px}.dot{width:8px;height:8px;border-radius:50%;background:#999}.good .dot{background:#35a86c}.warn .dot{background:#d89631}",
  ".toolbar,.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.button{border:1px solid var(--ae-line);background:var(--ae-surface);color:var(--primary-text-color);border-radius:9px;padding:9px 13px;font-size:13px;font-weight:550}",
  ".button.primary{border-color:var(--ae-accent);background:var(--ae-accent);color:white}.button.danger{color:var(--error-color,#d14f4f)}.button:hover:not(:disabled){filter:brightness(.94)}",
  ".stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:22px}.stat,.pane{border:1px solid var(--ae-line);background:var(--ae-surface);border-radius:13px}",
  ".stat{padding:16px 18px}.value{font-size:26px;font-weight:650;margin-top:5px}.layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:16px}",
  ".nav{display:flex;flex-direction:column;gap:4px;align-self:start}.nav button{text-align:left;border:0;border-radius:9px;background:transparent;padding:12px 13px;color:var(--primary-text-color);font-weight:550}",
  ".nav button.active{background:color-mix(in srgb,var(--ae-accent) 12%,transparent);color:var(--ae-accent)}.pane{padding:20px;margin-bottom:15px}.pane-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:15px}",
  ".split{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(280px,1fr);gap:14px}.list{border:1px solid var(--ae-line);border-radius:9px;overflow:hidden}.source-row{display:grid;grid-template-columns:minmax(0,1fr) 95px 70px;align-items:center;gap:10px;padding:13px;border:0;border-bottom:1px solid var(--ae-line);background:transparent;color:inherit;width:100%;text-align:left}",
  ".source-row:last-child{border:0}.source-row.selected{background:color-mix(in srgb,var(--ae-accent) 9%,transparent)}.source-row:hover{background:color-mix(in srgb,var(--ae-accent) 7%,transparent)}",
  ".source-name{font-size:13px;font-weight:650;display:block}.small{font-size:11px;color:var(--ae-muted);margin-top:4px}.state{font-size:12px;font-weight:600}.state.warn{color:#c48e3a}.state.good{color:#36a46b}",
  ".detail{background:var(--secondary-background-color,rgba(120,130,140,.06));border:1px solid var(--ae-line);border-radius:10px;padding:16px;min-width:0}",
  "label{display:block;margin-top:13px;font-size:12px;font-weight:600}input,select{display:block;width:100%;margin-top:5px;border:1px solid var(--ae-line);border-radius:8px;padding:9px 10px;background:var(--ae-surface);color:var(--primary-text-color);min-width:0}",
  "input[type=checkbox]{width:auto;display:inline-block;margin-right:8px}fieldset{border:0;padding:0;margin:10px 0}.inline{display:flex;align-items:center;gap:9px}.inline input{width:auto;margin:0}",
  "textarea{display:block;width:100%;min-height:120px;resize:vertical;margin-top:5px;border:1px solid var(--ae-line);border-radius:8px;padding:9px 10px;background:var(--ae-surface);color:var(--primary-text-color);font:inherit;font-size:13px}",
  ".detail .actions{margin-top:18px}.message{padding:12px 14px;margin:0 0 16px;border:1px solid var(--ae-line);border-radius:8px;background:var(--ae-surface);font-size:13px}.message.error{border-color:var(--error-color,#bf4646)}",
  ".empty{color:var(--ae-muted);font-size:13px;padding:25px;text-align:center}.area-row{padding:13px 0;border-top:1px solid var(--ae-line)}.section-note{font-size:12px;color:var(--ae-muted);line-height:1.55}",
  "table.trash{width:100%;border-collapse:collapse;font-size:13px}table.trash th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--ae-muted);padding:10px 12px;border-bottom:1px solid var(--ae-line)}",
  "table.trash td{padding:11px 12px;border-bottom:1px solid var(--ae-line);vertical-align:top;overflow-wrap:anywhere}table.trash tr:last-child td{border-bottom:0}table.trash .num{text-align:right}",
  "@media(max-width:540px){table.trash th:nth-child(3),table.trash td:nth-child(3),table.trash th:nth-child(5),table.trash td:nth-child(5){display:none}}",
  "@media(max-width:900px){.layout{grid-template-columns:1fr}.nav{flex-direction:row;overflow-x:auto}.split{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}",
  "@media(max-width:540px){.shell{padding:14px 12px 45px}.top{align-items:flex-start}.stats{gap:7px}.stat{padding:12px}.value{font-size:22px}.pane{padding:13px}.source-row{grid-template-columns:minmax(0,1fr) 72px 53px;padding:11px 7px;gap:4px}}"
].join("\n");
class AreaEventsManager extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "overview";
    this._selectedSource = null;
    this._selectedArea = null;
    this._creating = false;
    this._creatingArea = false;
    this._loading = false;
    this._saving = false;
    this._snapshot = null;
    this._message = "";
    this._error = "";
  }
  set hass(hass) {
    this._hass = hass;
    if (this.isConnected && !this._loaded) {
      this._loaded = true;
      this.refresh();
    }
  }
  set narrow(value) { this._narrow = value; }
  set panel(value) { this._panel = value; }
  connectedCallback() {
    if (!this._listenersAttached) {
      this._listenersAttached = true;
      this.shadowRoot.addEventListener("click", (ev) => this.handleClick(ev));
      this.shadowRoot.addEventListener("submit", (ev) => this.handleSubmit(ev));
      this.shadowRoot.addEventListener("change", (ev) => this.handleChange(ev));
    }
    this.render();
    if (this._hass && !this._loaded) {
      this._loaded = true;
      this.refresh();
    }
  }
  async call(type, params = {}) {
    if (!this._hass || !this._hass.callWS) throw new Error("Home Assistant is not connected.");
    return this._hass.callWS({ type: WS + type, ...params });
  }
  async refresh() {
    if (this._loading || this._saving) return;
    this._loading = true;
    this._error = "";
    this.render();
    try {
      const snapshot = await this.call("management");
      if (snapshot.schema !== "area_events.management.v1") throw new Error("Unsupported management contract.");
      this._snapshot = snapshot;
      const sources = snapshot.sources || [];
      if (!sources.some((item) => item.id === this._selectedSource)) this._selectedSource = sources[0]?.id || null;
      if (!snapshot.areas?.some((item) => item.area_key === this._selectedArea)) this._selectedArea = snapshot.areas?.[0]?.area_key || null;
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._loading = false;
      this.render();
    }
  }
  async mutate(type, params) {
    if (this._saving) return;
    this._saving = true;
    this._error = "";
    this._message = "";
    try {
      await this.call(type, params);
      this._saving = false;
      this._creating = false;
      this._creatingArea = false;
      this._message = "Changes saved on Cubi.";
      await this.refresh();
    } catch (err) {
      this._saving = false;
      this._error = err.message || String(err);
      this.render();
    }
  }
  handleClick(ev) {
    const button = ev.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.action;
    if (action === "menu") { this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })); return; }
    if (action === "refresh") return void this.refresh();
    if (action === "tab") { this._tab = button.dataset.value; this._creating = false; this._creatingArea = false; return void this.render(); }
    if (action === "source") { this._selectedSource = button.dataset.value; this._creating = false; return void this.render(); }
    if (action === "area") { this._selectedArea = button.dataset.value; this._creatingArea = false; return void this.render(); }
    if (action === "create-area") { this._creatingArea = true; return void this.render(); }
    if (action === "cancel-area") { this._creatingArea = false; return void this.render(); }
    if (action === "create") { this._creating = true; return void this.render(); }
    if (action === "cancel") { this._creating = false; return void this.render(); }
    if (action === "toggle") {
      const source = (this._snapshot?.sources || []).find((item) => item.id === this._selectedSource);
      if (!source) return;
      const enabled = !source.enabled;
      if (!window.confirm((enabled ? "Enable " : "Disable ") + source.name + "?")) return;
      return void this.mutate("source_command", { action: "toggle", source_id: source.id, expected_revision: source.revision, enabled });
    }
  }
  handleChange(ev) {
    if (ev.target.name === "producer_type" && this._creating) {
      const preserved = this.shadowRoot.querySelector("#source-form");
      const name = preserved?.elements.namedItem("name")?.value || "";
      const id = preserved?.elements.namedItem("source_id")?.value || "";
      const interval = preserved?.elements.namedItem("interval_minutes")?.value || "60";
      this._selectedType = ev.target.value;
      this.render();
      const form = this.shadowRoot.querySelector("#source-form");
      if (form) { form.elements.namedItem("name").value = name; form.elements.namedItem("source_id").value = id; form.elements.namedItem("interval_minutes").value = interval; }
    }
  }
  handleSubmit(ev) {
    const form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    ev.preventDefault();
    if (form.id === "source-form") return void this.saveSource(form);
    if (form.id === "area-form") return void this.saveArea(form);
  }
  sourceTypes() { return this._snapshot?.producer_types?.types || []; }
  fieldsFor(type) { return this.sourceTypes().find((item) => item.id === type)?.fields || []; }
  fieldMarkup(fields, values = {}) {
    return fields.map((field) => {
      const value = values[field.name] ?? field.default ?? "";
      if (field.kind === "url_list") {
        const list = Array.isArray(value) ? value.join("\n") : String(value);
        return "<label>" + escapeHtml(field.label || field.name) +
          "<textarea name='opt:" + escapeHtml(field.name) + "' rows='5' " +
          (field.required ? "required " : "") +
          (field.placeholder ? "placeholder='" + escapeHtml(field.placeholder) + "' " : "") +
          ">" + escapeHtml(list) + "</textarea></label>";
      }
      const kind = field.kind === "integer" ? "number" : field.kind === "url" ? "url" : "text";
      return "<label>" + escapeHtml(field.label || field.name) +
        "<input name='opt:" + escapeHtml(field.name) + "' type='" + kind + "' value='" + escapeHtml(value) + "'" +
        (field.min != null ? " min='" + Number(field.min) + "'" : "") +
        (field.max != null ? " max='" + Number(field.max) + "'" : "") +
        (field.required ? " required" : "") +
        (field.placeholder ? " placeholder='" + escapeHtml(field.placeholder) + "'" : "") + "></label>";
    }).join("");
  }
  optionsFrom(form, type) {
    const options = {};
    for (const field of this.fieldsFor(type)) {
      const element = form.elements.namedItem("opt:" + field.name);
      if (!element || element.value.trim() === "") continue;
      const value = field.kind === "integer" ? Number(element.value) :
        field.kind === "url_list" ? element.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean) : element.value.trim();
      if (field.kind === "integer" && !Number.isInteger(value)) throw new Error("Invalid integer in " + field.label);
      options[field.name] = value;
    }
    return options;
  }
  saveSource(form) {
    const data = new FormData(form);
    const id = String(data.get("source_id") || "");
    const name = String(data.get("name") || "").trim();
    const interval = asInt(data.get("interval_minutes"), 60);
    const type = String(data.get("producer_type") || "");
    if (!name || !Number.isInteger(interval) || interval < 1) { this._error = "Enter a name and a positive interval."; return void this.render(); }
    try {
      const options = this.optionsFrom(form, type);
      if (this._creating) {
        return void this.mutate("source_command", { action: "create", source_id: id, producer_type: type, name, enabled: false,
          interval_minutes: interval, options });
      }
      const source = (this._snapshot.sources || []).find((item) => item.id === id);
      if (!source) throw new Error("Select a source.");
      return void this.mutate("source_command", { action: "update", source_id: id, expected_revision: source.revision,
        name, interval_minutes: interval, options });
    } catch (error) {
      this._error = error.message;
      this.render();
    }
  }
  saveArea(form) {
    const area = (this._snapshot?.areas || []).find((item) => item.area_key === this._selectedArea);
    if (!this._creatingArea && !area) return;
    const data = new FormData(form);
    const sources = (this._snapshot.sources || []).filter((item) => data.has("source:" + item.id)).map((item) => item.id);
    const name = String(data.get("name") || "").trim();
    const latitude = Number(data.get("latitude"));
    const longitude = Number(data.get("longitude"));
    const radius = Number(data.get("radius"));
    const unit = String(data.get("unit") || "mi");
    const enabled = data.has("enabled");
    if (!name || !Number.isFinite(latitude) || !Number.isFinite(longitude) ||
        latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180 ||
        !Number.isFinite(radius) || radius <= 0 || radius > 400) {
      this._error = "Enter a name and a valid center latitude, longitude, and radius.";
      return void this.render();
    }
    const fields = { name, latitude, longitude, radius, unit, enabled, sources };
    if (this._creatingArea) return void this.mutate("area_create", fields);
    return void this.mutate("area_update", { area_key: area.area_key, expected_revision: area.revision, ...fields });
  }
  renderHeader() {
    const s = this._snapshot;
    const isReady = !!s;
    const status = s?.status || {};
    const readyJobs = status.jobs?.ready ?? "—";
    const count = s?.sources?.length ?? "—";
    const areas = s?.areas?.length ?? "—";
    return [
      "<div class='top'><div><h1>Area Events</h1><div class='sub'>Discovery management · Home Assistant</div></div>",
      "<div class='toolbar'><span class='pill " + (isReady ? "good" : "warn") + "'><span class='dot'></span>" +
      (isReady ? "Cubi connected" : "Not connected") + "</span>",
      "<button class='button' data-action='refresh' " + (this._loading ? "disabled" : "") + ">Refresh</button></div></div>",
      "<div class='stats'>",
      this.stat("Producers", count, "Configured instances"),
      this.stat("Discovery areas", areas, "Assigned collections"),
      this.stat("Queued jobs", readyJobs, "Ready for discovery"),
      this.stat("Control", s?.can_control ? "Ready" : "Read-only", "Administrator access"),
      "</div>"
    ].join("");
  }
  stat(label, value, subtitle) {
    return "<div class='stat'><div class='eyebrow'>" + escapeHtml(label) + "</div><div class='value'>" +
      escapeHtml(value) + "</div><div class='small'>" + escapeHtml(subtitle) + "</div></div>";
  }
  renderSources() {
    const sources = this._snapshot?.sources || [];
    const statuses = this._snapshot?.status?.sources || [];
    const states = new Map(statuses.map((item) => [item.source_key, item]));
    const list = sources.map((source) => {
      const state = states.get(source.id);
      const installed = this.sourceTypes().some((item) => item.id === source.type);
      const status = !installed ? (source.enabled ? "Enabled · Retired adapter" : "Disabled · Retired adapter")
        : !source.enabled ? "Disabled" : state?.eligible ? "Ready" : "Idle";
      return "<button class='source-row " + (source.id === this._selectedSource && !this._creating ? "selected" : "") +
        "' data-action='source' data-value='" + escapeHtml(source.id) + "'><span><span class='source-name'>" +
        escapeHtml(source.name) + "</span><span class='small'>" + escapeHtml(source.type) + "</span></span>" +
        "<span class='state " + (status === "Ready" ? "good" : !installed ? "warn" : "") + "'>" +
        escapeHtml(status) + "</span><span class='small'>" + escapeHtml(source.interval_minutes) + " min</span></button>";
    }).join("");
    const source = sources.find((item) => item.id === this._selectedSource);
    return "<div class='pane'><div class='pane-head'><div><h2>Producers</h2><div class='sub'>Add and manage independently configured event sources.</div></div>" +
      "<button class='button primary' data-action='create' " + (!this._snapshot?.can_control ? "disabled" : "") + ">+ Add producer</button></div>" +
      "<div class='split'><div class='list'>" + (list || "<div class='empty'>No producers yet. Add one to get started.</div>") +
      "</div><div class='detail'>" + (this._creating ? this.sourceForm(null) : source ? this.sourceForm(source) :
      "<div class='empty'>Select a producer to view its settings.</div>") + "</div></div></div>";
  }
  sourceForm(source) {
    const creating = !source;
    const type = creating ? (this._selectedType || this.sourceTypes()[0]?.id || "") : source.type;
    const descriptor = this.sourceTypes().find((item) => item.id === type);
    const retired = !!source && !descriptor;
    const canControl = !!this._snapshot?.can_control;
    const header = "<div class='pane-head'><h3>" + escapeHtml(creating ? "New producer" : source.name) + "</h3>" +
      (creating ? "<button class='button' data-action='cancel'>Cancel</button>" : "") + "</div>";
    if (retired) return header + "<p class='section-note'>This source uses an adapter that is no longer installed. " +
      "It cannot be edited or enabled. You can disable it safely without deleting events or history.</p>" +
      "<div class='actions'><button class='button danger' data-action='toggle' " +
      (!canControl || !source.enabled ? "disabled" : "") + ">Disable source</button></div>";
    const typeOptions = this.sourceTypes().map((item) => "<option value='" + escapeHtml(item.id) + "' " +
      (item.id === type ? "selected" : "") + ">" + escapeHtml(item.name) + "</option>").join("");
    const editor = "<form id='source-form'>" +
      (!creating ? "<input type='hidden' name='source_id' value='" + escapeHtml(source.id) + "'>" : "") +
      (creating ? "<label>Producer type<select name='producer_type' required>" + typeOptions + "</select></label>" :
        "<div class='section-note'>Producer type: " + escapeHtml(descriptor?.name || type) + "</div>" +
        "<input type='hidden' name='producer_type' value='" + escapeHtml(type) + "'>") +
      (creating ? "<label>Source ID<input name='source_id' type='text' value='' pattern='[a-z][a-z0-9_-]*' maxlength='80' required placeholder='my_event_calendar'></label>" : "") +
      "<label>Display name<input name='name' required maxlength='80' value='" + escapeHtml(source?.name || "") + "'></label>" +
      "<label>Interval (minutes)<input name='interval_minutes' type='number' required min='1' max='525600' value='" +
      escapeHtml(source?.interval_minutes || 60) + "'></label>" +
      "<div class='eyebrow' style='margin-top:19px'>Producer configuration</div>" +
      this.fieldMarkup(descriptor?.fields || [], source?.options || {}) +
      "<div class='actions'><button class='button primary' type='submit' " +
      (!canControl || this._saving ? "disabled" : "") + ">" + (creating ? "Create disabled producer" : "Save changes") + "</button>" +
      (!creating ? "<button class='button " + (source.enabled ? "danger" : "") + "' type='button' data-action='toggle' " +
        (!canControl || this._saving ? "disabled" : "") + ">" + (source.enabled ? "Disable" : "Enable") + "</button>" : "") +
      "</div></form>";
    return header + editor;
  }
  renderAreas() {
    const areas = this._snapshot?.areas || [];
    const area = this._creatingArea ? null : areas.find((item) => item.area_key === this._selectedArea) || areas[0];
    const list = areas.map((item) => "<button class='source-row " + (item.area_key === area?.area_key ? "selected" : "") +
      "' data-action='area' data-value='" + escapeHtml(item.area_key) + "'><span><span class='source-name'>" +
      escapeHtml(item.name) + "</span><span class='small'>" + escapeHtml(item.area_key) + "</span></span>" +
      "<span class='state'>" + (item.enabled ? "Enabled" : "Disabled") + "</span></button>").join("");
    const checked = new Set(area?.sources || []);
    const checkboxes = (this._snapshot?.sources || []).map((source) => "<label class='inline'><input type='checkbox' name='source:" +
      escapeHtml(source.id) + "' " + (checked.has(source.id) ? "checked" : "") + ">" +
      escapeHtml(source.name) + "</label>").join("");
    const creating = this._creatingArea;
    const areaForm = "<div class='pane-head'><h3>" + (creating ? "New discovery area" : escapeHtml(area?.name || "")) + "</h3>" +
      (creating ? "<button class='button' data-action='cancel-area'>Cancel</button>" : "") + "</div>" +
      "<form id='area-form'>" +
      "<label>Area name<input name='name' value='" + escapeHtml(area?.name || "") + "' maxlength='80' required></label>" +
      "<label>Center latitude<input name='latitude' type='number' step='any' min='-90' max='90' required value='" +
      escapeHtml(area?.latitude ?? "") + "'></label>" +
      "<label>Center longitude<input name='longitude' type='number' step='any' min='-180' max='180' required value='" +
      escapeHtml(area?.longitude ?? "") + "'></label>" +
      "<label>Radius<input name='radius' type='number' step='any' min='0.1' max='400' required value='" +
      escapeHtml(area?.radius ?? 20) + "'></label>" +
      "<label>Units<select name='unit'><option value='mi' " + (!area || area.unit === "mi" ? "selected" : "") +
      ">Miles</option><option value='km' " + (area?.unit === "km" ? "selected" : "") + ">Kilometers</option></select></label>" +
      "<label class='inline'><input type='checkbox' name='enabled' " + (area?.enabled !== false ? "checked" : "") + ">Area enabled</label>" +
      "<div class='eyebrow' style='margin-top:19px'>Assigned producers</div><fieldset>" + checkboxes + "</fieldset>" +
      "<div class='actions'><button class='button primary' type='submit' " +
      (!this._snapshot?.can_control || this._saving ? "disabled" : "") + ">" +
      (creating ? "Create area" : "Save area") + "</button></div></form>";
    return "<div class='pane'><div class='pane-head'><div><h2>Discovery areas</h2>" +
      "<div class='sub'>Manage area boundaries and assign producer sources.</div></div>" +
      "<button class='button primary' data-action='create-area' " +
      (!this._snapshot?.can_control ? "disabled" : "") + ">+ Add area</button></div>" +
      "<div class='split'><div class='list'>" + (list || "<div class='empty'>No areas configured yet.</div>") + "</div>" +
      "<div class='detail'>" + (creating || area ? areaForm : "<div class='empty'>Select or create an area.</div>") +
      "</div></div></div>";
  }
  renderTrashcan() {
    const trash = this._snapshot?.trashcan;
    if (!trash || trash.schema !== "discovery.trashcan.v1") {
      return "<div class='pane'><h2>Trashcan</h2><div class='empty'>This Discovery Service does not report a trashcan.</div></div>";
    }
    const date = (epoch) => epoch ? new Date(epoch * 1000).toLocaleString() : "—";
    const reasons = Object.entries(trash.by_reason || {}).map(([key, count]) => "<span class='pill'>" + escapeHtml(REASON_LABELS[key] || key) +
      ": <strong>" + escapeHtml(count) + "</strong></span>").join(" ");
    const rows = (trash.events || []).map((item) => "<tr><td><span class='source-name'>" + escapeHtml(item.title) + "</span><span class='small'>" +
      escapeHtml(item.event_key) + "</span></td><td>" + escapeHtml(date(item.starts_at)) + "</td><td>" +
      escapeHtml((item.sources || []).map((source) => source.name).join(", ")) + "</td><td>" + escapeHtml(item.reason_label || item.reason) + "</td><td>" +
      escapeHtml(date(item.deleted_at)) + "</td><td class='num'>" + escapeHtml(item.suppressed_count) + "</td></tr>").join("");
    return "<div class='pane'><div class='pane-head'><div><h2>Trashcan</h2><div class='sub'>Events an administrator deleted from a card. " +
      "Rediscovery cannot bring them back.</div></div></div>" +
      "<div class='stats'>" + this.stat("Deleted events", trash.total, "In the trashcan") +
      this.stat("Rediscoveries suppressed", trash.suppressed, "Found again, not stored") + "</div>" +
      "<div class='toolbar' style='margin-bottom:14px'>" + reasons + "</div>" +
      (rows ? "<div class='list'><table class='trash'><thead><tr><th>Event</th><th>Date</th><th>Source</th><th>Reason</th><th>Deleted</th>" +
        "<th class='num'>Suppressed</th></tr></thead><tbody>" + rows + "</tbody></table></div>" : "<div class='empty'>The trashcan is empty.</div>") + "</div>";
  }
  renderOverview() {
    const s = this._snapshot || {};
    const status = s.status || {};
    // The card publishes the build it actually loaded; the placeholder of an unbuilt card is not a build.
    const loaded = globalThis.__AREA_EVENTS_CARD_LOAD__?.build;
    const cardBuild = typeof loaded === "string" && loaded && !loaded.includes("__AREA_EVENTS_CARD_BUILD__") ? loaded : "Unavailable";
    return "<div class='pane'><h2>Service overview</h2><p class='section-note'>This panel reads the authoritative Cubi service " +
      "through Home Assistant. Producer and area configuration is not duplicated in HA.</p>" +
      "<p>Discovery: <strong>" + escapeHtml(status.runtime?.state || "Unknown") + "</strong></p>" +
      "<p>Data schema: <strong>" + escapeHtml(status.schema || "—") + "</strong></p>" +
      "<p>Control access: <strong>" + (s.can_control ? "Available" : "Read-only") + "</strong></p>" +
      "<p>Card build: <strong class='build-card'>" + escapeHtml(cardBuild) + "</strong></p>" +
      "<p>Backend build: <strong class='build-backend'>" + escapeHtml(status.build || "Unavailable") + "</strong></p></div>";
  }
  render() {
    const tabs = [["overview","Overview"],["sources","Sources"],["areas","Areas"],["trashcan","Trashcan"]];
    const nav = tabs.map(([key,name]) => "<button data-action='tab' data-value='" + key + "' class='" +
      (this._tab === key ? "active" : "") + "'>" + name + "</button>").join("");
    const body = this._tab === "areas" ? this.renderAreas() : this._tab === "overview" ? this.renderOverview() :
      this._tab === "trashcan" ? this.renderTrashcan() : this.renderSources();
    this.shadowRoot.innerHTML = "<style>" + CSS + "</style><div class='shell'>" + this.renderHeader() +
      (this._error ? "<div class='message error' role='alert'>" + escapeHtml(this._error) + "</div>" : "") +
      (this._message ? "<div class='message' role='status'>" + escapeHtml(this._message) + "</div>" : "") +
      "<div class='layout'><nav class='nav' aria-label='Area Events sections'>" +
      "<button data-action='menu' class='button'>☰ HA Menu</button>" + nav + "</nav><main>" +
      (this._loading && !this._snapshot ? "<div class='pane'>Loading Area Events…</div>" : body) +
      "</main></div></div>";
  }
}
customElements.define("area-events-manager", AreaEventsManager);
