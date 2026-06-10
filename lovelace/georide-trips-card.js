/*
 * GeoRide Trips Card — a minimalist, read-only status glance for the bike.
 *
 * Shows: odometer, remaining range, lock + alarm state, online status,
 * external battery, and a strip of whatever maintenance is currently due.
 *
 * Pure vanilla custom element — no build step, no dependencies. Drop this
 * file in /config/www/, register it as a Lovelace "module" resource, then:
 *
 *   type: custom:georide-trips-card
 *   prefix: honda_deauville     # the entity_id slug of your tracker (required)
 *   variant: tiles              # tiles | instrument | compact | spec | hero
 *   name: Honda Deauville       # optional, overrides the card title
 *
 * Companion to the GeoRide Trips integration. Personal fork — no support.
 */

const VERSION = "1.3.0";

// Entity-id suffixes, resolved against the configured `prefix`.
const SUFFIX = {
  odometer: "sensor.{p}_odometer",
  range: "sensor.{p}_remaining_range",
  totalRange: "number.{p}_fuel_total_range", // tank capacity → fuel-bar max
  locked: "binary_sensor.{p}_locked", // device_class lock: off = LOCKED
  online: "binary_sensor.{p}_online",
  theft: "binary_sensor.{p}_theft_alarm",
  fall: "binary_sensor.{p}_fall_detected",
  extBattery: "sensor.{p}_external_battery",
  intBattery: "sensor.{p}_internal_battery",
};

// Maintenance "due" binary sensors — shown only while `on`.
const DUE = [
  "binary_sensor.{p}_refuel_needed",
  "binary_sensor.{p}_chain_due",
  "binary_sensor.{p}_oil_change_due",
  "binary_sensor.{p}_service_due",
];

const UNSET = new Set(["unknown", "unavailable", "none", "", null, undefined]);

class GeoRideTripsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._sig = null;
    // Delegated click handler (survives re-renders): any element carrying a
    // data-entity attribute opens HA's native more-info dialog (with history).
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
  }

  _onClick(e) {
    const el = e.composedPath().find((n) => n && n.getAttribute && n.getAttribute("data-entity"));
    if (!el) return;
    const entityId = el.getAttribute("data-entity");
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  setConfig(config) {
    if (!config || !config.prefix) {
      throw new Error("georide-trips-card: 'prefix' is required (the entity_id slug, e.g. honda_deauville)");
    }
    this._config = config;
    this._sig = null; // force a re-render on next hass
  }

  getCardSize() {
    return this._config && this._config.variant === "compact" ? 1 : 3;
  }

  set hass(hass) {
    this._hass = hass;
    const p = this._config.prefix;
    const id = (tpl) => tpl.replace("{p}", p);
    const st = (eid) => (hass.states[eid] ? hass.states[eid].state : undefined);

    // Only re-render when one of OUR entities changed (hass fires globally).
    const watched = [...Object.values(SUFFIX).map(id), ...DUE.map(id)];
    const sig = (this._config.variant || "instrument") + "|" + watched.map((eid) => st(eid)).join("|");
    if (sig === this._sig) return;
    this._sig = sig;

    const d = this._collect(hass);
    const variant = this._config.variant || "instrument";
    const body = {
      instrument: this._vInstrument,
      compact: this._vCompact,
      spec: this._vSpec,
      hero: this._vHero,
    }[variant]?.call(this, d) ?? this._vTiles(d);

    this.shadowRoot.innerHTML =
      `<style>${this._styles()}</style>` +
      `<ha-card class="${variant === "compact" ? "compact" : ""}">${body}</ha-card>`;
  }

  // ---- Data collection: one normalized object the variants render from ----
  _collect(hass) {
    const p = this._config.prefix;
    const id = (t) => t.replace("{p}", p);
    const S = hass.states;
    const st = (eid) => (S[eid] ? S[eid].state : undefined);
    const at = (eid, a) => (S[eid] && S[eid].attributes ? S[eid].attributes[a] : undefined);
    const unset = (x) => UNSET.has(x);

    const title = (this._config.name || at(id(SUFFIX.odometer), "friendly_name") || "GeoRide")
      .replace(/\s+Odometer$/i, "");

    const onlineRaw = st(id(SUFFIX.online));
    const online = onlineRaw === "on";
    const onlineUnset = unset(onlineRaw);
    const conn = onlineUnset
      ? { icon: "mdi:signal-off", cls: "muted", text: "unknown" }
      : online
        ? { icon: "mdi:signal", cls: "ok", text: "online" }
        : { icon: "mdi:signal-off", cls: "off", text: "offline" };

    const odo = this._fmtKm(st(id(SUFFIX.odometer)));

    const rangeRaw = st(id(SUFFIX.range));
    const range = this._fmtKm(rangeRaw);
    const rangeUnset = unset(rangeRaw) || range === "—";
    const rangeNum = rangeUnset ? null : parseFloat(rangeRaw);
    const totalRaw = st(id(SUFFIX.totalRange));
    const totalNum = unset(totalRaw) ? null : parseFloat(totalRaw);
    let rangePct = null;
    if (rangeNum != null && totalNum && totalNum > 0) {
      rangePct = Math.max(0, Math.min(100, (rangeNum / totalNum) * 100));
    }

    // Lock — device_class lock => off = LOCKED, on = UNLOCKED.
    const lockRaw = st(id(SUFFIX.locked));
    let lock;
    if (unset(lockRaw)) lock = { icon: "mdi:lock-question", text: "Lock ?", cls: "muted", unlocked: false };
    else if (lockRaw === "off") lock = { icon: "mdi:lock", text: "Locked", cls: "ok", unlocked: false };
    else lock = { icon: "mdi:lock-open-variant", text: "Unlocked", cls: "warn", unlocked: true };

    // Battery — external = the bike's 12 V system.
    const battRaw = st(id(SUFFIX.extBattery));
    let batt = { present: false };
    if (!unset(battRaw)) {
      const v = parseFloat(battRaw);
      const parked = lockRaw === "off"; // lock engaged = confirmed stop (engine off)
      const { cls, glyph, charging, source, label } = GeoRideTripsCard.batteryState(v, parked);
      const intV = st(id(SUFFIX.intBattery));
      batt = {
        present: true, v, cls, glyph, charging, source, label, low: cls !== "ok" && !charging,
        text: v.toFixed(1) + " V",
        intTitle: unset(intV) ? "" : `Internal battery: ${parseFloat(intV).toFixed(1)} V`,
      };
    }

    const theft = st(id(SUFFIX.theft)) === "on";
    const fall = st(id(SUFFIX.fall)) === "on";
    const alarms = [];
    if (theft) alarms.push({ icon: "mdi:alarm-light", label: "Theft alarm" });
    if (fall) alarms.push({ icon: "mdi:alert", label: "Fall detected" });

    const due = [];
    for (const t of DUE) {
      const eid = id(t);
      if (st(eid) === "on") due.push({ label: this._dueLabel(at(eid, "friendly_name"), title) });
    }

    let severity = "good";
    if (theft || fall) severity = "alarm";
    else if (lock.unlocked || due.length || batt.low || (!online && !onlineUnset)) severity = "attention";

    return {
      title, online, onlineUnset, conn, odo, range, rangeUnset, rangePct, lock, batt, alarms, due, severity, theft, fall,
      ids: { range: id(SUFFIX.range), odometer: id(SUFFIX.odometer), battery: id(SUFFIX.extBattery) },
    };
  }

  // ---- Shared fragments ----
  _onlineEl(d) {
    return `<div class="online"><ha-icon class="conn ${d.conn.cls}" icon="${d.conn.icon}"></ha-icon>${d.conn.text}</div>`;
  }
  _onlineDot(d) {
    return `<ha-icon class="conn ${d.conn.cls}" icon="${d.conn.icon}" title="${d.conn.text}"></ha-icon>`;
  }
  _battChip(d) {
    if (!d.batt.present) return "";
    return `<div class="chip ${d.batt.cls}" title="${d.batt.intTitle}">` +
      `<ha-icon icon="${d.batt.glyph}"></ha-icon><span>${d.batt.text}</span></div>`;
  }
  _alarmChips(d) {
    return d.alarms.map((a) =>
      `<div class="chip alarm"><ha-icon icon="${a.icon}"></ha-icon><span>${a.label}</span></div>`).join("");
  }
  _dueStrip(d) {
    if (!d.due.length) return "";
    const chips = d.due.map((x) =>
      `<div class="chip warn"><ha-icon icon="mdi:wrench"></ha-icon><span>${x.label}</span></div>`).join("");
    return `<div class="row wrap due">${chips}</div>`;
  }

  // ---- Variant: tiles (default) ----
  _vTiles(d) {
    return `
      <div class="header">
        <div class="title"><ha-icon icon="mdi:motorbike"></ha-icon>${d.title}</div>
        ${this._onlineEl(d)}
      </div>
      <div class="metrics">
        <div class="metric">
          <ha-icon icon="mdi:counter"></ha-icon>
          <div class="value mono">${d.odo}<span class="unit">km</span></div>
          <div class="label">Odometer</div>
        </div>
        <div class="metric">
          <ha-icon icon="mdi:gas-station"></ha-icon>
          <div class="value mono">${d.range}${d.rangeUnset ? "" : '<span class="unit">km</span>'}</div>
          <div class="label">Range left</div>
        </div>
      </div>
      <div class="row wrap">
        <div class="chip ${d.lock.cls}"><ha-icon icon="${d.lock.icon}"></ha-icon><span>${d.lock.text}</span></div>
        ${this._battChip(d)}
        ${this._alarmChips(d)}
      </div>
      ${this._dueStrip(d)}
    `;
  }

  // ---- Variant: instrument cluster (range as a fuel bar) — DEFAULT ----
  // Proportional tabular numerals (tighter than monospace), a fuel-pump icon
  // anchoring the bar, tank % on the label row, a gradient fill, and a small
  // counter icon on the odometer row. (v1's monospace layout is archived in
  // lovelace/versions/georide-trips-card.v1.1.0.js.)
  _vInstrument(d) {
    const pct = d.rangePct;
    const fillCls = d.rangeUnset || pct == null ? "muted" : pct < 10 ? "alarm" : pct < 25 ? "warn" : "ok";
    const width = d.rangeUnset || pct == null ? 0 : pct;
    const pctTag = d.rangeUnset || pct == null ? "" : `<span class="ic2-pct">${Math.round(pct)}%</span>`;
    return `
      <div class="ic-head">
        <div class="title sm"><ha-icon icon="mdi:motorbike"></ha-icon>${d.title}</div>
        <div class="rail">
          <ha-icon class="${d.lock.cls}" icon="${d.lock.icon}" title="${d.lock.text}"></ha-icon>
          ${this._onlineDot(d)}
          ${d.batt.present ? `<ha-icon class="${d.batt.cls} clickable" data-entity="${d.ids.battery}" icon="${d.batt.glyph}" title="${d.batt.label} · ${d.batt.text}${d.batt.intTitle ? " · " + d.batt.intTitle : ""}"></ha-icon>` : ""}
        </div>
      </div>
      <div class="ic-range">
        <div class="ic2-rl"><span class="label">Range left</span>${pctTag}</div>
        <div class="bar-row">
          <ha-icon class="ic2-pump" icon="mdi:gas-station"></ha-icon>
          <div class="bar tall"><div class="bar-fill grad ${fillCls}" style="width:${width}%"></div></div>
          <div class="ic-range-val tnum clickable" data-entity="${d.ids.range}" title="Range — tap for history">${d.range}${d.rangeUnset ? "" : '<span class="unit">km</span>'}</div>
        </div>
      </div>
      <div class="ic-odo">
        <span class="label"><ha-icon class="ic2-odoicon" icon="mdi:counter"></ha-icon>Odometer</span>
        <span class="ic-odo-val tnum clickable" data-entity="${d.ids.odometer}" title="Odometer — tap for history">${d.odo}<span class="unit">km</span></span>
      </div>
      ${d.alarms.length ? `<div class="row wrap" style="margin-top:12px">${this._alarmChips(d)}</div>` : ""}
      ${this._dueStrip(d)}
    `;
  }

  // ---- Variant: compact strip (single line) ----
  _vCompact(d) {
    return `
      <div class="strip">
        <ha-icon class="strip-bike" icon="mdi:motorbike"></ha-icon>
        <span class="strip-name">${d.title}</span>
        <span class="strip-nums mono">${d.odo}<span class="u">km</span> · ${d.range}${d.rangeUnset ? "" : '<span class="u">km</span>'}</span>
        <span class="strip-icons">
          <ha-icon class="${d.lock.cls}" icon="${d.lock.icon}" title="${d.lock.text}"></ha-icon>
          ${this._onlineDot(d)}
          ${d.batt.present ? `<ha-icon class="${d.batt.cls}" icon="${d.batt.glyph}" title="Battery ${d.batt.text}${d.batt.intTitle ? " · " + d.batt.intTitle : ""}"></ha-icon>` : ""}
          ${d.alarms.length ? `<ha-icon class="alarm-ic" icon="mdi:alarm-light" title="${d.alarms.map((a) => a.label).join(", ")}"></ha-icon>` : ""}
          ${d.due.length ? `<ha-icon class="warn" icon="mdi:wrench" title="${d.due.map((x) => x.label).join(", ")}"></ha-icon>` : ""}
        </span>
      </div>
    `;
  }

  // ---- Variant: spec-sheet list ----
  _vSpec(d) {
    const row = (icon, label, valueHtml, cls = "") =>
      `<div class="sp-row ${cls}"><ha-icon icon="${icon}"></ha-icon>` +
      `<span class="sp-label">${label}</span><span class="sp-val mono">${valueHtml}</span></div>`;
    let rows = "";
    rows += row("mdi:counter", "Odometer", `${d.odo} <span class="u">km</span>`);
    rows += row("mdi:gas-station", "Range left", d.rangeUnset ? "—" : `${d.range} <span class="u">km</span>`);
    rows += row(d.lock.icon, "Lock", d.lock.text, d.lock.unlocked ? "warn" : "");
    if (d.batt.present) rows += row(d.batt.glyph, "Battery", d.batt.text, d.batt.low ? "warn" : "");
    rows += row(
      d.alarms.length ? "mdi:alarm-light" : "mdi:shield-check",
      "Alarm",
      d.alarms.length ? d.alarms.map((a) => a.label).join(", ") : "OK",
      d.alarms.length ? "alarm" : ""
    );
    for (const x of d.due) rows += row("mdi:wrench", x.label, "DUE", "warn");
    return `
      <div class="sp-head">
        <span class="title sm"><ha-icon icon="mdi:motorbike"></ha-icon>${d.title}</span>
        ${this._onlineDot(d)}
      </div>
      <div class="sp-rows">${rows}</div>
    `;
  }

  // ---- Variant: status-first hero ----
  _vHero(d) {
    const word = d.severity === "alarm" ? "ALARM" : d.severity === "attention" ? "ATTENTION" : "ALL GOOD";
    const icon = d.severity === "alarm" ? "mdi:alert-octagon" : d.severity === "attention" ? "mdi:alert" : "mdi:check-circle";
    const tok = [];
    if (d.theft) tok.push("Theft alarm");
    if (d.fall) tok.push("Fall detected");
    tok.push(d.lock.text);
    if (d.due.length) tok.push(d.due.length === 1 ? d.due[0].label : `${d.due.length} maintenance due`);
    if (!d.online && !d.onlineUnset) tok.push("Offline");
    if (d.batt.low) tok.push("Battery low");
    const subtitle = tok.slice(0, 3).join(" · ");
    return `
      <div class="banner ${d.severity}">
        <ha-icon class="banner-icon" icon="${icon}"></ha-icon>
        <div class="banner-txt"><div class="banner-word">${word}</div><div class="banner-sub">${subtitle}</div></div>
        ${this._onlineEl(d)}
      </div>
      <div class="metrics tight">
        <div class="metric">
          <div class="value mono">${d.odo}<span class="unit">km</span></div>
          <div class="label">Odometer</div>
        </div>
        <div class="metric">
          <div class="value mono">${d.range}${d.rangeUnset ? "" : '<span class="unit">km</span>'}</div>
          <div class="label">Range left</div>
        </div>
      </div>
      ${this._dueStrip(d)}
    `;
  }

  _styles() {
    return `
      :host { display: block; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
      ha-card { padding: 16px; font-family: var(--paper-font-body1_-_font-family, inherit); }
      ha-card.compact { padding: 10px 14px; }
      .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
      .unit, .u { font-size: .8rem; font-weight: 500; color: var(--secondary-text-color); margin-left: 3px; }
      .title { display: flex; align-items: center; gap: 8px; font-size: 1.05rem; font-weight: 600; color: var(--primary-text-color); }
      .title.sm { font-size: .95rem; }
      .title ha-icon { color: var(--state-icon-color, var(--primary-text-color)); }
      .label { font-size: .72rem; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: .4px; }

      .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
      .online { display: flex; align-items: center; gap: 5px; font-size: .8rem; color: var(--secondary-text-color); }
      .dot { --mdc-icon-size: 12px; }
      .dot.on { color: var(--success-color, #4caf50); }
      .dot.off { color: var(--error-color, #f44336); }
      .conn { --mdc-icon-size: 19px; }
      .conn.ok { color: var(--success-color, #4caf50); }
      .conn.off { color: var(--error-color, #f44336); }
      .conn.muted { color: var(--disabled-text-color, #999); }

      .metrics { display: flex; gap: 12px; margin-bottom: 14px; }
      .metrics.tight { margin: 14px 0 6px; }
      .metric { flex: 1; text-align: center; background: var(--secondary-background-color, rgba(0,0,0,.04)); border-radius: 12px; padding: 12px 8px; }
      .metric ha-icon { color: var(--state-icon-color, var(--primary-text-color)); --mdc-icon-size: 22px; }
      .metric .value { font-size: 1.5rem; font-weight: 700; line-height: 1.2; color: var(--primary-text-color); }

      .row { display: flex; align-items: center; gap: 8px; }
      .row.wrap { flex-wrap: wrap; }
      .row.due { margin-top: 10px; }
      .chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px; font-size: .82rem; font-weight: 500; background: var(--secondary-background-color, rgba(0,0,0,.05)); color: var(--primary-text-color); }
      .chip ha-icon { --mdc-icon-size: 18px; }
      .chip.ok { color: var(--success-color, #4caf50); }
      .chip.warn { color: var(--warning-color, #ff9800); }
      .chip.alarm { color: var(--text-primary-color, #fff); background: var(--error-color, #f44336); }
      .chip.muted { color: var(--disabled-text-color, #999); }

      /* instrument */
      .ic-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
      .ic-head .title { min-width: 0; }
      .rail { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
      .rail ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
      .rail .ok { color: var(--success-color); } .rail .warn { color: var(--warning-color); }
      .rail .alarm { color: var(--error-color); } .rail .muted { color: var(--disabled-text-color); }
      .rail .off { color: var(--error-color); }
      .ic-range { margin-bottom: 14px; }
      .bar-row { display: flex; align-items: center; gap: 14px; margin-top: 8px; }
      .bar { flex: 1; height: 12px; border-radius: 7px; background: var(--secondary-background-color, rgba(0,0,0,.08)); box-shadow: inset 0 0 0 1px var(--divider-color, rgba(0,0,0,.07)); overflow: hidden; }
      .bar-fill { height: 100%; border-radius: 7px; transition: width .45s cubic-bezier(.4,0,.2,1); }
      .bar-fill.ok { background: var(--success-color, #4caf50); }
      .bar-fill.warn { background: var(--warning-color, #ff9800); }
      .bar-fill.alarm { background: var(--error-color, #f44336); }
      .bar-fill.muted { background: transparent; }
      .ic-range-val { font-size: 1.5rem; font-weight: 800; letter-spacing: -.5px; color: var(--primary-text-color); white-space: nowrap; }
      .ic-range-val .unit { font-weight: 600; }
      .ic-odo { display: flex; align-items: baseline; justify-content: space-between; padding-top: 12px; border-top: 1px solid var(--divider-color, rgba(0,0,0,.1)); }
      .ic-odo-val { font-size: 1.2rem; font-weight: 700; letter-spacing: -.3px; color: var(--primary-text-color); }
      /* instrument — refined */
      .tnum { font-family: var(--paper-font-body1_-_font-family, inherit); font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; }
      .ic2-rl { display: flex; align-items: baseline; justify-content: space-between; }
      .ic2-pct { font-size: .8rem; font-weight: 700; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
      .ic2-pump { --mdc-icon-size: 18px; color: var(--secondary-text-color); flex-shrink: 0; }
      .ic2-odoicon { --mdc-icon-size: 16px; color: var(--secondary-text-color); margin-right: 5px; vertical-align: -3px; }
      .bar.tall { height: 14px; border-radius: 8px; }
      .bar.tall .bar-fill { border-radius: 8px; }
      .bar-fill.grad.ok { background: linear-gradient(90deg, var(--success-color, #4caf50), color-mix(in srgb, var(--success-color, #4caf50) 72%, #fff)); }
      .bar-fill.grad.warn { background: linear-gradient(90deg, var(--warning-color, #ff9800), color-mix(in srgb, var(--warning-color, #ff9800) 78%, #fff)); }
      .bar-fill.grad.alarm { background: linear-gradient(90deg, var(--error-color, #f44336), color-mix(in srgb, var(--error-color, #f44336) 80%, #fff)); }
      /* clickable → opens HA more-info (history) */
      .clickable { cursor: pointer; }
      .ic-range-val.clickable:hover, .ic-odo-val.clickable:hover { text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 3px; }
      .rail .clickable:hover { filter: brightness(1.2); }

      /* compact strip */
      .strip { display: flex; align-items: center; gap: 10px; font-size: .9rem; color: var(--primary-text-color); }
      .strip-bike { --mdc-icon-size: 22px; color: var(--state-icon-color, var(--primary-text-color)); }
      .strip-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .strip-nums { margin-left: auto; white-space: nowrap; }
      .strip-icons { display: flex; align-items: center; gap: 8px; }
      .strip-icons ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
      .strip-icons .ok { color: var(--success-color); } .strip-icons .warn { color: var(--warning-color); }
      .strip-icons .alarm, .strip-icons .alarm-ic { color: var(--error-color); } .strip-icons .muted { color: var(--disabled-text-color); }
      .strip-icons .dot { --mdc-icon-size: 12px; }

      /* spec sheet */
      .sp-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
      .sp-row { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--divider-color, rgba(0,0,0,.08)); }
      .sp-row:last-child { border-bottom: none; }
      .sp-row ha-icon { --mdc-icon-size: 20px; color: var(--state-icon-color, var(--secondary-text-color)); }
      .sp-label { flex: 1; color: var(--secondary-text-color); font-size: .9rem; }
      .sp-val { font-size: .95rem; font-weight: 600; color: var(--primary-text-color); text-align: right; }
      .sp-row.warn ha-icon, .sp-row.warn .sp-val { color: var(--warning-color); }
      .sp-row.alarm ha-icon, .sp-row.alarm .sp-val { color: var(--error-color); }

      /* hero */
      .banner { display: flex; align-items: center; gap: 12px; padding: 14px; border-radius: 12px; }
      .banner-icon { --mdc-icon-size: 30px; }
      .banner-txt { flex: 1; }
      .banner-word { font-size: 1.1rem; font-weight: 700; letter-spacing: .5px; color: inherit; }
      .banner-sub { font-size: .8rem; color: var(--primary-text-color); opacity: .8; margin-top: 2px; }
      .banner .online { color: inherit; opacity: .85; }
      .banner.good { background: color-mix(in srgb, var(--success-color, #4caf50) 15%, transparent); color: var(--success-color, #4caf50); }
      .banner.attention { background: color-mix(in srgb, var(--warning-color, #ff9800) 16%, transparent); color: var(--warning-color, #ff9800); }
      .banner.alarm { background: color-mix(in srgb, var(--error-color, #f44336) 18%, transparent); color: var(--error-color, #f44336); }
      @keyframes gr-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .5; } }
      @media (prefers-reduced-motion: no-preference) { .banner.alarm .banner-icon { animation: gr-pulse 1.1s ease-in-out infinite; } }
    `;
  }

  _fmtKm(state) {
    if (UNSET.has(state)) return "—";
    const n = parseFloat(state);
    if (Number.isNaN(n)) return "—";
    return Math.round(n).toLocaleString("fr-FR");
  }

  _dueLabel(friendlyName, deviceName) {
    if (!friendlyName) return "Maintenance due";
    let s = String(friendlyName);
    if (deviceName && s.startsWith(deviceName + " ")) {
      s = s.slice(deviceName.length + 1); // strip the has_entity_name device prefix
    }
    return s
      .replace(/\s*[–-]\s*due$/i, "")
      .replace(/\s+needed$/i, "")
      .replace(/\s+due$/i, "")
      .trim();
  }

  // Voltage → battery glyph + colour class. Single source of truth (used by
  // the card and by the preview's battery-level showcase). A 12 V system has a
  // narrow useful band (~11.8–12.7 V at rest, up to ~14.4 V charging), so the
  // colour stays a 3-bucket signal while the glyph adds finer level resolution.
  // GeoRide exposes no charging flag — only voltage + lock state. We infer:
  // ≥13.4 V means something is charging the 12 V battery. Parked (lock engaged ⇒
  // engine off) ⇒ that can only be an external/WALL charger; riding ⇒ alternator.
  // Charging colour reflects charge-voltage health (overcharge = amber/red), and
  // since elevated voltage hides true SoC, we can't show a level while charging.
  static batteryState(v, parked) {
    if (v >= 13.4) {
      const cls = v >= 15.2 ? "alarm" : v >= 14.8 ? "warn" : "ok";
      const label = v >= 14.8 ? "Overcharging" : parked ? "Charging (wall)" : "Charging (engine)";
      return { cls, glyph: "mdi:battery-charging", charging: true, source: parked ? "wall" : "alternator", label };
    }
    // Resting state-of-charge (engine off, no external charge). Colour bands
    // (motorcycle): red <12.0 V, amber 12.0–12.4 V, green ≥12.4 V (≈75% SoC =
    // genuinely able to crank). "Full" at ≥12.8 V (AGM rests ~12.8 V). Sources:
    // Battery University BU-903 + motorcycle battery references.
    const cls = v < 12.0 ? "alarm" : v < 12.4 ? "warn" : "ok";
    let glyph;
    if (v < 11.8) glyph = "mdi:battery-alert";
    else if (v < 12.0) glyph = "mdi:battery-10";
    else if (v < 12.2) glyph = "mdi:battery-30";
    else if (v < 12.4) glyph = "mdi:battery-50";
    else if (v < 12.6) glyph = "mdi:battery-70";
    else if (v < 12.8) glyph = "mdi:battery-90";
    else glyph = "mdi:battery";
    return { cls, glyph, charging: false, source: null, label: "Battery" };
  }

  static getStubConfig() {
    return { prefix: "honda_deauville", variant: "instrument" };
  }
}

customElements.define("georide-trips-card", GeoRideTripsCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "georide-trips-card",
  name: "GeoRide Trips Card",
  description: "Minimalist read-only status glance for a GeoRide-tracked bike.",
  preview: false,
});

console.info(`%c GEORIDE-TRIPS-CARD %c v${VERSION} `,
  "color:#fff;background:#3949ab;font-weight:700",
  "color:#3949ab;background:#fff");
