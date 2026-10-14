// Trade log: every live, paper and replay trade from the journal, with stats and export.
import {
  api, card, cls, download, field, fmtDateTime, h, kpi, money, mount, moduleTag, num, pct, price, rfmt, sideCell,
  signed, table,
} from "./lib.js";
import { LineChart, css } from "./charts.js";

let root, chart;
const filters = { mode: "", account: "", module: "", status: "", start: "", end: "" };

export async function render(main) {
  root = h("div", { class: "stack" });
  mount(main, root);
  let accounts = [];
  try { accounts = await api("/api/journal/accounts"); } catch {}
  const sel = (key, options) => h("select", { onchange: (e) => { filters[key] = e.target.value; load(); } },
    options.map(([v, l]) => h("option", { value: v, selected: filters[key] === v }, l)));
  const date = (key) => h("input", { type: "date", value: filters[key], onchange: (e) => { filters[key] = e.target.value; load(); } });
  mount(root,
    h("div", { class: "page-head" }, h("h2", {}, "Trade log"), h("span", { class: "sub" }, "Every trade with its module tag, R result and timestamps"),
      h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => download(`/api/trades.csv?${qs()}`) }, "Export CSV"))),
    h("div", { class: "card tight" }, h("div", { class: "form-grid" },
      field("Mode", sel("mode", [["", "All"], ["live", "Live"], ["paper", "Paper"], ["replay", "Replay"]])),
      field("Account", sel("account", [["", "All"], ...accounts.map((a) => [a.account, `${a.account} (${a.mode})`])])),
      field("Module", sel("module", [["", "All"], ["A", "A"], ["B", "B"], ["C", "C"], ["D", "D"]])),
      field("Status", sel("status", [["", "All"], ["open", "Open"], ["closed", "Closed"]])),
      field("From", date("start")), field("To", date("end")))),
    h("div", { id: "t-body", class: "stack" }, h("div", { class: "empty" }, "Loading…")));
  load();
}

export function destroy() { if (chart) chart.destroy(); chart = null; root = null; }

function qs() {
  return new URLSearchParams(Object.entries(filters).filter(([, v]) => v)).toString();
}

async function load() {
  const body = root && root.querySelector("#t-body");
  if (!body) return;
  const [data, sizing] = await Promise.all([api(`/api/trades?${qs()}`), api(`/api/sizing-check${filters.mode ? "?mode=" + filters.mode : ""}`)]);
  const st = data.stats.all || {};
  const closed = data.trades.filter((t) => t.status === "closed").slice().reverse();
  const chartEl = h("div", { class: "chart small" });
  if (chart) chart.destroy();
  chart = null;
  const moduleRows = Object.entries(data.stats).filter(([k]) => k !== "all").map(([k, v]) => ({ module: k, ...v }));
  mount(body,
    h("div", { class: "grid cols-6" },
      kpi("Closed trades", num(st.trades || 0, 0)),
      kpi("Win rate", st.trades ? pct((st.win_rate || 0) * 100, 1) : "—"),
      kpi("Mean R", st.trades ? rfmt(st.mean_r) : "—", st.trades ? `t = ${num(st.t_stat, 2)}` : "", cls(st.mean_r)),
      kpi("Profit factor", st.trades ? num(st.profit_factor, 2) : "—"),
      kpi("Total R", st.trades ? rfmt(st.total_r) : "—", "", cls(st.total_r)),
      kpi("Net P&L", st.trades ? signed(st.net_pnl) : "—", st.trades ? `commission ${money(st.commission)}` : "", cls(st.net_pnl))),
    h("div", { class: "grid cols-2" },
      card("Cumulative R", closed.length ? chartEl : h("div", { class: "empty" }, "No closed trades yet")),
      h("div", { class: "stack" },
        card("By module", table([
          { label: "Module", render: (r) => moduleTag(r.module) },
          { label: "Trades", num: true, render: (r) => num(r.trades, 0) },
          { label: "Win %", num: true, render: (r) => pct(r.win_rate * 100, 1) },
          { label: "Mean R", num: true, render: (r) => h("span", { class: cls(r.mean_r) }, rfmt(r.mean_r)) },
          { label: "PF", num: true, render: (r) => num(r.profit_factor, 2) },
          { label: "t", num: true, render: (r) => num(r.t_stat, 2) },
          { label: "Net", num: true, render: (r) => h("span", { class: cls(r.net_pnl) }, signed(r.net_pnl)) },
        ], moduleRows, { empty: "No closed trades" })),
        card("Position sizing check", sizingCard(sizing)))),
    card(`Trades (${data.trades.length})`, table([
      { label: "Opened", render: (t) => fmtDateTime(t.open_ts) },
      { label: "Mode", render: (t) => h("span", { class: "dim" }, t.mode) },
      { label: "Module", render: (t) => moduleTag(t.module) },
      { label: "Side", render: (t) => sideCell(t.side) },
      { label: "Size", num: true, render: (t) => num(t.units, 0) },
      { label: "Entry", num: true, render: (t) => price(t.entry) },
      { label: "Stop", num: true, render: (t) => price(t.sl_initial) },
      { label: "Target", num: true, render: (t) => price(t.tp) },
      { label: "Exit", num: true, render: (t) => t.status === "open" ? h("span", { class: "pill info" }, "open") : price(t.exit) },
      { label: "Reason", render: (t) => t.exit_reason || "" },
      { label: "R", num: true, render: (t) => t.r === null ? "—" : h("span", { class: cls(t.r) }, rfmt(t.r)) },
      { label: "P&L", num: true, render: (t) => t.pnl === null ? "—" : h("span", { class: cls(t.pnl) }, signed(t.pnl)) },
      { label: "Risk", num: true, render: (t) => money(t.risk_money) },
      { label: "Closed", render: (t) => fmtDateTime(t.close_ts) },
      { label: "Notes", wrap: true, render: (t) => h("span", { class: "dim small" }, [t.tag, ...(t.notes || [])].filter(Boolean).join(" · ")) },
    ], data.trades, { empty: "No trades match these filters", maxHeight: "560px" })),
  );
  if (closed.length) {
    chart = new LineChart(chartEl);
    let cum = 0;
    const seen = new Set();
    const pts = closed.map((t) => { cum += t.r; let time = t.close_ts; while (seen.has(time)) time += 1; seen.add(time); return { time, value: +cum.toFixed(3) }; });
    chart.set([{ type: "area", color: css("--gold"), fill: "rgba(217,180,90,.18)", data: pts }]);
  }
}

function sizingCard(s) {
  if (!s || !s.ref) {
    return h("p", { class: "small muted" }, "The strategy asks to check the first trade that hits its initial stop: the money actually lost should equal the planned 0.3% risk. It appears here once one exists.");
  }
  const ratio = s.ratio || 0;
  const ok = ratio > 0.85 && ratio < 1.2;
  return h("div", { class: "stack", style: { gap: "6px" } },
    h("div", { class: "row" }, h("span", { class: `pill ${ok ? "ok" : "warn"}` }, ok ? "sizing looks right" : "check sizing"), h("span", { class: "dim small" }, `${s.module} trade ${s.ref} on ${s.day}`)),
    h("div", { class: "grid cols-3", style: { gap: "8px" } },
      h("div", {}, h("div", { class: "dim small" }, "Planned risk"), h("div", { class: "num" }, money(s.planned_risk))),
      h("div", {}, h("div", { class: "dim small" }, "Actual loss"), h("div", { class: "num" }, money(s.actual_loss))),
      h("div", {}, h("div", { class: "dim small" }, "Ratio"), h("div", { class: "num" }, num(ratio, 2) + "×"))),
    ok ? null : h("p", { class: "small warn-text" }, "Actual loss differs from plan by more than ~15%: check the commission value (Connection tab) and the symbol's contract size."));
}
