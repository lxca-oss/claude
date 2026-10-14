// Small helpers shared by every page: DOM building, API calls, formatting, toasts.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function mount(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

export async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    if (typeof opts.body === "string" || opts.body instanceof Blob || opts.body instanceof ArrayBuffer) {
      init.body = opts.body;
      init.headers["Content-Type"] = opts.contentType || "text/plain";
    } else {
      init.body = JSON.stringify(opts.body);
      init.headers["Content-Type"] = "application/json";
    }
  }
  const r = await fetch(path, init);
  if (r.status === 401) { location.href = "/login"; throw new Error("login required"); }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    const msg = (data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

// ---------------------------------------------------------------- formatting
const nf = (d) => new Intl.NumberFormat(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const _nf = {};
export function num(x, d = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  if (!Number.isFinite(x)) return x > 0 ? "∞" : "-∞";
  return (_nf[d] ||= nf(d)).format(x);
}
export function money(x, d = 2) { return x === null || x === undefined ? "—" : num(x, d); }
export function signed(x, d = 2, suffix = "") {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return (x > 0 ? "+" : "") + num(x, d) + suffix;
}
export function pct(x, d = 2) { return x === null || x === undefined ? "—" : num(x, d) + "%"; }
export function cls(x) { return x > 0 ? "up" : x < 0 ? "down" : "muted"; }
export function price(x) { return x === null || x === undefined ? "—" : num(x, 2); }
export function rfmt(x) { return signed(x, 2, "R"); }

// ---------------------------------------------------------------- time
export const TZ = {
  local: { label: "Local", zone: Intl.DateTimeFormat().resolvedOptions().timeZone },
  utc: { label: "UTC", zone: "UTC" },
  london: { label: "London", zone: "Europe/London" },
  ny: { label: "New York", zone: "America/New_York" },
  sydney: { label: "Sydney", zone: "Australia/Sydney" },
};
let displayTz = "local";
try { displayTz = localStorage.getItem("gse-tz") || "local"; } catch {}
export function getTz() { return displayTz; }
export function setTz(k) { displayTz = k; try { localStorage.setItem("gse-tz", k); } catch {} }
export function zone() { return TZ[displayTz]?.zone || "UTC"; }

const _dtf = {};
function dtf(opts, z) {
  const key = z + JSON.stringify(opts);
  return (_dtf[key] ||= new Intl.DateTimeFormat("en-GB", { ...opts, timeZone: z }));
}
export function fmtTime(ts, z = zone()) {
  if (!ts) return "—";
  return dtf({ hour: "2-digit", minute: "2-digit" }, z).format(new Date(ts * 1000));
}
export function fmtDateTime(ts, z = zone()) {
  if (!ts) return "—";
  return dtf({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }, z).format(new Date(ts * 1000));
}
export function fmtDate(ts, z = zone()) {
  if (!ts) return "—";
  return dtf({ day: "2-digit", month: "short", year: "numeric" }, z).format(new Date(ts * 1000));
}
export function tzOffsetSeconds(ts, z = zone()) {
  // offset of zone z from UTC at instant ts (seconds), used to shift chart axes
  const d = new Date(ts * 1000);
  const parts = dtf({ year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }, z)
    .formatToParts(d).reduce((a, p) => (a[p.type] = p.value, a), {});
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour % 24, +parts.minute, +parts.second);
  return Math.round((asUtc - d.getTime()) / 1000);
}
export function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
export function todayIso(offsetDays = 0) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------- UI bits
export function toast(msg, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, msg);
  const box = document.getElementById("toasts");
  box.append(el);
  while (box.childElementCount > 3) box.firstChild.remove();
  setTimeout(() => el.remove(), kind === "bad" ? 9000 : 4500);
}

export async function guard(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg, "ok");
    return r;
  } catch (e) {
    toast(e.message || String(e), "bad");
    return undefined;
  }
}

export function modal(title, body, actions = []) {
  return new Promise((resolve) => {
    const close = (v) => { back.remove(); resolve(v); };
    const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(null); } },
      h("div", { class: "modal" }, h("h3", {}, title), body,
        h("div", { class: "row", style: { marginTop: "14px", justifyContent: "flex-end" } },
          h("button", { class: "btn ghost", onclick: () => close(null) }, "Cancel"),
          ...actions.map((a) => h("button", { class: `btn ${a.kind || "primary"}`, onclick: () => close(a.value ?? true) }, a.label)))));
    document.body.append(back);
  });
}

export async function confirmBox(title, text, label = "Confirm", kind = "primary") {
  return modal(title, h("p", { class: "muted" }, text), [{ label, kind }]);
}

export function card(title, body, actions) {
  return h("div", { class: "card" },
    (title || actions) && h("div", { class: "card-head" }, title && h("h3", {}, title), actions && h("div", { class: "actions" }, actions)),
    body);
}

export function kpi(label, value, foot, valueClass = "") {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label),
    h("div", { class: `value ${valueClass}` }, value), foot && h("div", { class: "foot" }, foot));
}

export function table(cols, rows, { empty = "Nothing yet", maxHeight } = {}) {
  if (!rows || !rows.length) return h("div", { class: "table-wrap" }, h("div", { class: "empty" }, empty));
  const head = h("tr", {}, cols.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)));
  const body = rows.map((r) => h("tr", {}, cols.map((c) => {
    const v = c.render ? c.render(r) : r[c.key];
    return h("td", { class: [c.num ? "num" : "", c.wrap ? "wrap" : "", c.cls ? c.cls(r) : ""].join(" ") }, v instanceof Node ? v : (v ?? "—"));
  })));
  const wrap = h("div", { class: "table-wrap" + (maxHeight ? " scroll-y" : ""), style: maxHeight ? { maxHeight } : {} },
    h("table", {}, h("thead", {}, head), h("tbody", {}, body)));
  return wrap;
}

export function sideCell(side) {
  const buy = side > 0 || side === "BUY";
  return h("span", { class: buy ? "side-buy" : "side-sell" }, buy ? "BUY" : "SELL");
}
export function moduleTag(m) { return h("span", { class: `tag ${m}` }, m); }

export function sw(checked, onchange, title) {
  const input = h("input", { type: "checkbox", onchange: (e) => onchange(e.target.checked) });
  input.checked = !!checked;
  return h("label", { class: "switch", title }, input, h("i"));
}

export function seg(options, value, onchange) {
  const wrap = h("div", { class: "seg" });
  for (const [k, label] of options) {
    wrap.append(h("button", { class: k === value ? "on" : "", onclick: () => { onchange(k); } }, label));
  }
  return wrap;
}

export function field(label, input, hint) {
  return h("label", { class: "field", title: hint || "" }, h("span", {}, label, hint ? h("span", { class: "dim" }, "ⓘ") : null), input);
}

export function download(url) {
  const a = h("a", { href: url, download: "" });
  document.body.append(a);
  a.click();
  a.remove();
}
