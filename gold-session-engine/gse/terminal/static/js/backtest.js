// Backtest: configure runs, follow progress, read results against the validation plan.
import {
  api, card, cls, confirmBox, download, field, fmtDateTime, guard, h, kpi, money, mount, moduleTag, num, pct, price,
  rfmt, sideCell, signed, sw, table, toast,
} from "./lib.js";
import { LineChart, css, dayTime } from "./charts.js";

let root, settings, datasets = [], presets = {}, runsTimer, charts = [], selected = null, initialized = false;
const cfg = {
  name: "", dataset: "", start: "", end: "", preset: "doc_defaults", modules: { A: true, B: true, C: true, D: false },
  risk: 0.3, spread: 0.25, commission: 0.07, slippage: 0.05, balance: 100000, split: "2022-01-01",
  iterations: 200, news: true, sweepParam: "", sweepValues: "", variantsJson: "", fill: "bars",
};

const PRESET_UI = {
  doc_defaults: { modules: null, risk: null },
  module_a_reproduction: { modules: { A: true, B: false, C: false, D: false }, risk: 1.0 },
  module_a_alone: { modules: { A: true, B: false, C: false, D: false } },
  module_b_alone: { modules: { A: false, B: true, C: false, D: false } },
  module_c_alone: { modules: { A: false, B: false, C: true, D: false } },
  module_d_alone: { modules: { A: false, B: false, C: false, D: true } },
};

const SWEEP_PARAMS = [
  "module_a.target_r", "module_a.breakeven_r", "module_a.entry_buffer", "module_a.range_min_pct", "module_a.range_max_pct",
  "module_a.stop_cap_pct", "module_a.exit_mode", "module_a.apply_level_rules", "module_b.sweep_min", "module_b.reclaim_bars",
  "module_c.target_r", "module_c.slope_bars", "module_c.touch_tolerance", "module_c.setup_expiry_bars", "module_c.swing_strength",
  "module_d.move_atr_mult", "module_d.target_r", "risk.min_stop_usd", "levels.min_room_r", "levels.target_offset",
];

export async function render(main, { params }) {
  root = h("div", { class: "stack" }, h("div", { class: "empty" }, "Loading…"));
  mount(main, root);
  const [s, ds, pr] = await Promise.all([api("/api/settings"), api("/api/datasets"), api("/api/presets")]);
  settings = s.settings;
  datasets = ds;
  presets = pr;
  if (!cfg.dataset && datasets.length) {
    const real = datasets.find((d) => !d.synthetic) || datasets[0];
    cfg.dataset = real.id;
    cfg.start = real.start;
    cfg.end = real.end;
  }
  if (!initialized) {
    initialized = true;
    cfg.risk = settings.risk.risk_per_trade_pct;
    cfg.spread = settings.costs.spread_usd;
    cfg.commission = settings.costs.commission_per_oz_rt;
    cfg.slippage = settings.costs.slippage_usd;
    cfg.balance = settings.backtest.initial_balance;
    cfg.split = settings.backtest.split_date;
    cfg.iterations = settings.backtest.random_iterations;
    cfg.modules = { A: settings.module_a.enabled, B: settings.module_b.enabled, C: settings.module_c.enabled, D: settings.module_d.enabled };
  }
  draw();
  if (params.get("run")) openRun(params.get("run"));
}

export function destroy() {
  clearInterval(runsTimer);
  runsTimer = null;
  charts.forEach((c) => c.destroy());
  charts = [];
  root = null;
}

function draw() {
  mount(root,
    h("div", { class: "page-head" }, h("h2", {}, "Backtest"), h("span", { class: "sub" }, "The live engine replayed on M1 data, with spread, commission and slippage")),
    h("div", { class: "grid cols-2" }, configCard(), h("div", { class: "card", id: "runs" })),
    h("div", { id: "result", class: "stack" }));
  loadRuns();
  clearInterval(runsTimer);
  runsTimer = setInterval(loadRuns, 2000);
}

function configCard() {
  if (!datasets.length) {
    return card("New backtest", h("div", { class: "stack" },
      h("p", { class: "muted" }, "No market data yet. Download XAU/USD history from your cTrader account or Dukascopy, import a CSV, or generate synthetic data to try the software."),
      h("a", { class: "btn primary", href: "#data" }, "Get market data")));
  }
  const ds = datasets.find((d) => d.id === cfg.dataset);
  const num_ = (key, step = "any") => h("input", { type: "number", step, value: cfg[key], onchange: (e) => { cfg[key] = Number(e.target.value); } });
  const presetSel = h("select", { onchange: (e) => {
    cfg.preset = e.target.value;
    const ui = PRESET_UI[cfg.preset] || {};
    cfg.modules = ui.modules ? { ...ui.modules } : { A: settings.module_a.enabled, B: settings.module_b.enabled, C: settings.module_c.enabled, D: settings.module_d.enabled };
    cfg.risk = ui.risk ?? settings.risk.risk_per_trade_pct;
    draw();
  } }, Object.entries(presets).map(([k, l]) => h("option", { value: k, selected: k === cfg.preset }, l)));
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h3", {}, "New backtest"), h("div", { class: "actions" }, h("span", { class: "dim small" }, "uses your saved strategy settings"))),
    h("div", { class: "stack", style: { gap: "12px" } },
      h("div", { class: "form-grid" },
        field("Dataset", h("select", { onchange: (e) => { cfg.dataset = e.target.value; const d = datasets.find((x) => x.id === cfg.dataset); cfg.start = d.start; cfg.end = d.end; draw(); } },
          datasets.map((d) => h("option", { value: d.id, selected: d.id === cfg.dataset }, `${d.id}${d.synthetic ? " (synthetic)" : ""}`)))),
        field("From", h("input", { type: "date", value: cfg.start, min: ds?.start, max: ds?.end, onchange: (e) => { cfg.start = e.target.value; } })),
        field("To", h("input", { type: "date", value: cfg.end, min: ds?.start, max: ds?.end, onchange: (e) => { cfg.end = e.target.value; } })),
        field("Preset", presetSel),
        field("Fills", h("select", { onchange: (e) => { cfg.fill = e.target.value; } },
          h("option", { value: "bars", selected: cfg.fill === "bars" }, "M1 bars (fast)"),
          h("option", { value: "ticks", selected: cfg.fill === "ticks", disabled: !(ds && ds.tick_days) }, `Tick replay${ds && ds.tick_days ? ` (${ds.tick_days} days)` : " (no ticks stored)"}`)),
          "Tick replay fills orders exactly as quotes arrive; needs a dataset downloaded as cTrader ticks")),
      ds ? h("div", { class: "small dim" }, `${ds.source} · ${ds.start} → ${ds.end} · ${num(ds.bars, 0)} M1 bars · real spread on ${num(ds.spread_coverage * 100, 0)}% of bars${ds.tick_days ? ` · raw ticks for ${ds.tick_days} days` : ""}`) : null,
      ds && ds.synthetic ? h("div", { class: "banner warn" }, h("b", {}, "Synthetic"), "Random data: good for checking the software, meaningless for judging the strategy.") : null,
      h("div", { class: "row", style: { gap: "16px" } }, ["A", "B", "C", "D"].map((m) => h("label", { class: "inline" },
        sw(cfg.modules[m], (on) => { cfg.modules[m] = on; }), h("span", { class: `tag ${m}` }, m), m === "D" ? h("span", { class: "dim small" }, "exp.") : null))),
      h("div", { class: "form-grid" },
        field("Risk per trade %", num_("risk")), field("Starting balance", num_("balance", "1000")),
        field("Spread $ (if data has none)", num_("spread")), field("Commission $/oz round turn", num_("commission")),
        field("Slippage $", num_("slippage")), field("Second half starts", h("input", { type: "date", value: cfg.split, onchange: (e) => { cfg.split = e.target.value; } })),
        field("Random-entry iterations", num_("iterations", "1")),
        field("Run name", h("input", { type: "text", value: cfg.name, placeholder: "optional", onchange: (e) => { cfg.name = e.target.value; } }))),
      h("label", { class: "inline" }, sw(cfg.news, (on) => { cfg.news = on; }), "Apply the news filter using the stored calendar"),
      h("details", { class: "rule" }, h("summary", {}, h("span", { class: "small" }, "Variant sweep (max 24, freeze the list first)")),
        h("div", { class: "body stack", style: { gap: "8px" } },
          h("div", { class: "form-grid" },
            field("Parameter", h("select", { onchange: (e) => { cfg.sweepParam = e.target.value; } }, h("option", { value: "" }, "—"), SWEEP_PARAMS.map((p) => h("option", { value: p, selected: p === cfg.sweepParam }, p)))),
            field("Values (comma separated)", h("input", { type: "text", value: cfg.sweepValues, placeholder: "1.5, 2, 2.5", onchange: (e) => { cfg.sweepValues = e.target.value; } }))),
          h("div", { class: "small dim" }, "Or paste variants as JSON: [{\"name\": \"tight\", \"overrides\": {\"module_c.target_r\": 1.5}}, …]"),
          h("textarea", { placeholder: "[]", onchange: (e) => { cfg.variantsJson = e.target.value; } }, cfg.variantsJson))),
      h("div", { class: "row" }, h("button", { class: "btn primary", onclick: run }, "Run backtest"),
        h("span", { class: "small dim" }, "Runs in the background; you can keep using the terminal."))));
}

function buildVariants() {
  if (cfg.variantsJson.trim()) {
    const v = JSON.parse(cfg.variantsJson);
    if (!Array.isArray(v)) throw new Error("variants JSON must be a list");
    return v;
  }
  if (cfg.sweepParam && cfg.sweepValues.trim()) {
    return cfg.sweepValues.split(",").map((x) => x.trim()).filter(Boolean).map((raw) => {
      let v = raw;
      if (raw === "true" || raw === "false") v = raw === "true";
      else if (!Number.isNaN(Number(raw))) v = Number(raw);
      return { name: `${cfg.sweepParam} = ${raw}`, overrides: { [cfg.sweepParam]: v } };
    });
  }
  return [];
}

async function run() {
  let variants;
  try { variants = buildVariants(); } catch (e) { toast(`Variants: ${e.message}`, "bad"); return; }
  const overrides = {
    "module_a.enabled": cfg.modules.A, "module_b.enabled": cfg.modules.B, "module_c.enabled": cfg.modules.C, "module_d.enabled": cfg.modules.D,
    "risk.risk_per_trade_pct": cfg.risk, "costs.spread_usd": cfg.spread, "costs.commission_per_oz_rt": cfg.commission,
    "costs.slippage_usd": cfg.slippage, "backtest.initial_balance": cfg.balance, "backtest.split_date": cfg.split,
  };
  const body = { dataset: cfg.dataset, start: cfg.start, end: cfg.end, preset: cfg.preset, overrides, name: cfg.name || presets[cfg.preset],
    random_iterations: cfg.iterations, use_news: cfg.news, variants, fill_model: cfg.fill };
  const r = await guard(() => api("/api/backtests", { method: "POST", body }));
  if (r) { toast("Backtest started", "ok"); selected = r.id; loadRuns(); showProgress(r.id); }
}

async function loadRuns() {
  const el = root && root.querySelector("#runs");
  if (!el) return;
  let runs = [];
  try { runs = await api("/api/backtests"); } catch { return; }
  mount(el, h("div", { class: "card-head" }, h("h3", {}, "Runs"), h("div", { class: "actions" }, h("span", { class: "dim small" }, `${runs.length} saved`))),
    runs.length ? h("div", { class: "table-wrap scroll-y", style: { maxHeight: "520px" } }, h("table", {},
      h("thead", {}, h("tr", {}, ["Run", "Trades", "Mean R", "PF", "Return", "Max DD", ""].map((c, i) => h("th", { class: i >= 1 && i <= 5 ? "num" : "" }, c)))),
      h("tbody", {}, runs.map((r) => {
        const s = r.summary || {};
        const running = r.state === "running" || r.state === "queued";
        return h("tr", { style: { cursor: "pointer", background: r.id === selected ? "var(--gold-dim)" : "" }, onclick: () => openRun(r.id, r.state) },
          h("td", { style: { whiteSpace: "normal", minWidth: "180px" } }, h("div", { style: { fontWeight: 600 } }, r.name || r.id),
            h("div", { class: "dim small" }, [r.dataset, r.start ? `${r.start} → ${r.end}` : "", r.created].filter(Boolean).join(" · ")),
            running ? h("div", { class: "progress", style: { marginTop: "4px", width: "160px" } }, h("i", { style: { width: `${(r.progress || 0) * 100}%` } })) : null,
            r.state === "error" ? h("div", { class: "down small" }, r.message) : null,
            running ? h("div", { class: "dim small" }, r.message) : null),
          r.kind === "variants" ? h("td", { colspan: 5, class: "dim small" }, `${s.variants} variants`) : [
            h("td", { class: "num" }, s.trades ?? "—"),
            h("td", { class: `num ${cls(s.mean_r)}` }, s.mean_r !== undefined && s.mean_r !== null ? rfmt(s.mean_r) : "—"),
            h("td", { class: "num" }, s.profit_factor !== undefined ? num(s.profit_factor, 2) : "—"),
            h("td", { class: `num ${cls(s.return_pct)}` }, s.return_pct !== undefined ? signed(s.return_pct, 1, "%") : "—"),
            h("td", { class: "num" }, s.max_drawdown_pct !== undefined ? pct(s.max_drawdown_pct, 1) : "—")],
          h("td", {}, h("button", { class: "btn sm ghost", title: running ? "Cancel" : "Delete", onclick: async (e) => {
            e.stopPropagation();
            if (running) { await api(`/api/backtests/${r.id}/cancel`, { method: "POST" }); }
            else if (await confirmBox("Delete this run?", r.name || r.id, "Delete", "danger")) { await api(`/api/backtests/${r.id}`, { method: "DELETE" }); if (selected === r.id) mount(root.querySelector("#result")); }
            loadRuns();
          } }, running ? "Cancel" : "✕")));
      })))) : h("div", { class: "empty" }, "No runs yet"));
}

async function openRun(id, state) {
  selected = id;
  loadRuns();
  if (state === "running" || state === "queued") return showProgress(id);
  const el = root.querySelector("#result");
  mount(el, h("div", { class: "card empty" }, "Loading result…"));
  try {
    const res = await api(`/api/backtests/${id}`);
    showResult(res);
  } catch (e) {
    const st = await api(`/api/backtests/${id}/status`);
    if (st.state === "running" || st.state === "queued") showProgress(id);
    else mount(el, h("div", { class: "banner bad" }, h("b", {}, "No result"), st.message || e.message));
  }
}

async function showProgress(id) {
  const el = root && root.querySelector("#result");
  if (!el) return;
  const bar = h("i", { style: { width: "0%" } });
  const msg = h("div", { class: "dim small" }, "starting…");
  mount(el, card("Running…", h("div", { class: "stack", style: { gap: "8px" } }, h("div", { class: "progress" }, bar), msg)));
  while (root && selected === id) {
    const st = await api(`/api/backtests/${id}/status`);
    bar.style.width = `${(st.progress || 0) * 100}%`;
    msg.textContent = `${num((st.progress || 0) * 100, 0)}% · ${st.message || ""}`;
    if (st.state === "done") { openRun(id, "done"); return; }
    if (st.state === "error" || st.state === "cancelled") {
      mount(el, h("div", { class: "banner bad" }, h("b", {}, st.state), h("span", {}, st.message || ""), st.traceback ? h("pre", { class: "small", style: { whiteSpace: "pre-wrap" } }, st.traceback) : null));
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ---------------------------------------------------------------- result rendering
function showResult(res) {
  charts.forEach((c) => c.destroy());
  charts = [];
  const el = root.querySelector("#result");
  if (res.kind === "variants") return mount(el, variantsView(res));
  const s = res.summary || {};
  const eqEl = h("div", { class: "chart small" });
  mount(el,
    h("div", { class: "page-head" },
      h("h2", {}, res.name || "Backtest result"),
      h("span", { class: "sub" }, `${res.dataset?.id} · ${res.start} → ${res.end} · ${res.fill_model === "ticks" ? `${num(res.bars, 0)} ticks (tick replay)` : `${num(res.bars, 0)} M1 bars`} · ${num(res.elapsed_s, 1)}s`),
      h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => download(`/api/backtests/${res.id}/trades.csv`) }, "Trades CSV"))),
    (res.warnings || []).map((w) => h("div", { class: "banner warn" }, h("b", {}, "Note"), w)),
    h("div", { class: "grid cols-6" },
      kpi("Trades", num(s.trades || 0, 0), `${num(res.trades_per_day?.average, 2)}/day · max ${res.trades_per_day?.max ?? "—"}`),
      kpi("Win rate", s.trades ? pct(s.win_rate * 100, 1) : "—", s.breakeven_win_rate ? `breakeven ${pct(s.breakeven_win_rate * 100, 1)}` : ""),
      kpi("Mean R", s.trades ? rfmt(s.mean_r) : "—", s.trades ? `t = ${num(s.t_stat, 2)}` : "", cls(s.mean_r)),
      kpi("Profit factor", s.trades ? num(s.profit_factor, 2) : "—", s.trades ? `avg win ${rfmt(s.avg_win_r)} / loss ${rfmt(s.avg_loss_r)}` : ""),
      kpi("Return", signed(s.return_pct, 2, "%"), `final ${money(s.final_balance)}`, cls(s.return_pct)),
      kpi("Max drawdown", pct(s.max_drawdown_pct, 2), `worst day ${pct(s.worst_day_pct, 2)}`, (s.max_drawdown_pct || 0) > 5 ? "down" : "")),
    card("Validation gates", gatesTable(res.gates || [])),
    h("div", { class: "grid cols-2" },
      card("Equity (daily close and intraday low)", eqEl),
      card("Random-entry baseline", baselineView(res.baseline || {}, res.modules || {}))),
    card("By module", moduleTable(res.modules || {})),
    h("div", { class: "grid cols-2" },
      card(`Two halves (split ${res.settings?.backtest?.split_date})`, halvesTable(res.halves || {})),
      card("FTMO checks", ftmoView(s, res.settings))),
    card("Monthly returns", heatmap(res.monthly || [])),
    h("div", { class: "grid cols-2" },
      card("By year", table([
        { label: "Year", key: "year" }, { label: "Trades", num: true, render: (r) => r.trades },
        { label: "Win %", num: true, render: (r) => pct(r.win_rate * 100, 1) },
        { label: "Mean R", num: true, render: (r) => h("span", { class: cls(r.mean_r) }, rfmt(r.mean_r)) },
        { label: "PF", num: true, render: (r) => num(r.profit_factor, 2) },
        { label: "Net", num: true, render: (r) => h("span", { class: cls(r.net_pnl) }, signed(r.net_pnl, 0)) },
      ], res.yearly || [])),
      card("Why signals were skipped", skipsTable(res.skips || {}))),
    card(`Trades (${(res.trades || []).length}${(res.trades || []).length > 500 ? ", showing the last 500" : ""})`, tradesTable((res.trades || []).slice(-500).reverse())),
  );
  if ((res.equity || []).length) {
    const c = new LineChart(eqEl);
    c.set([
      { type: "area", color: css("--gold"), fill: "rgba(217,180,90,.15)", data: res.equity.map((d) => ({ time: dayTime(d.day), value: d.balance })) },
      { color: css("--down"), width: 1, lastValue: false, data: res.equity.map((d) => ({ time: dayTime(d.day), value: d.min_equity })) },
    ]);
    charts.push(c);
  }
}

function gatesTable(gates) {
  const fmtVal = (v) => {
    if (v === null || v === undefined) return "—";
    if (Array.isArray(v)) return v.map((x) => (x === null ? "—" : rfmt(x))).join(" / ");
    if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${k.replace(/_/g, " ")} ${num(x, 2)}`).join(", ");
    return num(v, 2);
  };
  return table([
    { label: "Gate", key: "gate" },
    { label: "Module", render: (g) => g.module === "all" ? "all" : moduleTag(g.module) },
    { label: "Result", render: (g) => h("span", { class: g.pass === true ? "gate-pass" : g.pass === false ? "gate-fail" : "gate-na" }, g.pass === true ? "PASS" : g.pass === false ? "FAIL" : "manual") },
    { label: "Value", num: true, render: (g) => fmtVal(g.value) },
    { label: "Needs", key: "threshold" },
    { label: "Note", wrap: true, render: (g) => h("span", { class: "dim small" }, g.note || "") },
  ], gates);
}

function moduleTable(mods) {
  const rows = Object.entries(mods).map(([m, s]) => ({ m, ...s }));
  return table([
    { label: "Module", render: (r) => moduleTag(r.m) },
    { label: "Trades", num: true, render: (r) => r.trades },
    { label: "Win %", num: true, render: (r) => pct(r.win_rate * 100, 1) },
    { label: "Mean R", num: true, render: (r) => h("span", { class: cls(r.mean_r) }, rfmt(r.mean_r)) },
    { label: "t", num: true, render: (r) => num(r.t_stat, 2) },
    { label: "PF", num: true, render: (r) => num(r.profit_factor, 2) },
    { label: "Total R", num: true, render: (r) => rfmt(r.total_r) },
    { label: "Avg win", num: true, render: (r) => rfmt(r.avg_win_r) },
    { label: "Avg loss", num: true, render: (r) => rfmt(r.avg_loss_r) },
    { label: "Max DD (R)", num: true, render: (r) => num(r.max_dd_r, 1) },
    { label: "Worst streak", num: true, render: (r) => r.max_losing_streak },
    { label: "Avg hold", num: true, render: (r) => `${num(r.avg_minutes, 0)}m` },
    { label: "Net", num: true, render: (r) => h("span", { class: cls(r.net_pnl) }, signed(r.net_pnl, 0)) },
  ], rows, { empty: "No trades" });
}

function halvesTable(hv) {
  const mods = new Set([...Object.keys(hv.first || {}), ...Object.keys(hv.second || {})]);
  const rows = [...mods].sort((a, b) => (a === "all" ? -1 : b === "all" ? 1 : a.localeCompare(b))).map((m) => ({ m, a: hv.first?.[m] || {}, b: hv.second?.[m] || {} }));
  const cell = (s) => s.trades ? h("span", { class: cls(s.mean_r) }, `${rfmt(s.mean_r)} (${s.trades})`) : h("span", { class: "dim" }, "no trades");
  return table([
    { label: "Module", render: (r) => r.m === "all" ? "all" : moduleTag(r.m) },
    { label: "First half: mean R (n)", num: true, render: (r) => cell(r.a) },
    { label: "Second half: mean R (n)", num: true, render: (r) => cell(r.b) },
    { label: "Both positive", render: (r) => (r.a.trades && r.b.trades) ? h("span", { class: r.a.mean_r > 0 && r.b.mean_r > 0 ? "gate-pass" : "gate-fail" }, r.a.mean_r > 0 && r.b.mean_r > 0 ? "yes" : "no") : "—" },
  ], rows, { empty: "No trades" });
}

function baselineView(bl, mods) {
  const entries = Object.entries(bl);
  if (!entries.length) return h("p", { class: "muted small" }, "Not run (0 iterations).");
  return h("div", { class: "stack", style: { gap: "14px" } }, entries.map(([m, b]) => {
    if (!b.iterations) return h("div", { class: "small dim" }, moduleTag(m), ` ${b.note || "not enough trades"}`);
    const counts = b.histogram.counts, edges = b.histogram.edges;
    const maxC = Math.max(...counts, 1);
    const lo = edges[0], hi = edges[edges.length - 1];
    const markerX = Math.max(0, Math.min(100, ((b.real_mean - lo) / (hi - lo || 1)) * 100));
    return h("div", {},
      h("div", { class: "row", style: { justifyContent: "space-between" } },
        h("span", {}, moduleTag(m), " ", h("span", { class: "small" }, `real ${rfmt(b.real_mean)} vs random ${rfmt(b.random_mean)} ± ${num(b.random_sd, 3)}`)),
        h("span", { class: `pill ${b.percentile >= 95 ? "ok" : "bad"}` }, `${num(b.percentile, 0)}th pct · p=${num(b.p_value, 3)}`)),
      h("div", { class: "hist", style: { marginTop: "6px" }, title: `${b.iterations} random runs of ${b.twins} trades` },
        counts.map((c) => h("i", { style: { height: `${(c / maxC) * 100}%` } })),
        h("div", { class: "marker", style: { left: `${markerX}%` }, title: "this module" })),
      h("div", { class: "row small dim", style: { justifyContent: "space-between" } }, h("span", {}, rfmt(lo)), h("span", {}, "random mean R distribution · gold line = module"), h("span", {}, rfmt(hi))));
  }));
}

function ftmoView(s, st) {
  const b = st?.backtest || {};
  const rows = [
    ["Worst day (equity vs day start)", pct(s.worst_day_pct, 2), `limit −${b.ftmo_daily_loss_pct}%`, !s.ftmo_daily_breached],
    ["Max loss from starting balance", pct(s.max_loss_from_initial_pct, 2), `limit ${b.ftmo_max_loss_pct}%`, !s.ftmo_max_breached],
    ["Max drawdown from peak", pct(s.max_drawdown_pct, 2), "", null],
    ["Best day share of total profit", s.best_day_share !== null && s.best_day_share !== undefined ? pct(s.best_day_share * 100, 1) : "—", `≤ ${num((b.best_day_max_share || 0.3) * 100, 0)}%`, s.best_day_share === null || s.best_day_share === undefined ? null : s.best_day_share <= (b.best_day_max_share || 0.3)],
    ["Trading days", s.trading_days ?? "—", "", null],
  ];
  return table([{ label: "Check", render: (r) => r[0] }, { label: "Value", num: true, render: (r) => r[1] }, { label: "Limit", render: (r) => h("span", { class: "dim" }, r[2]) },
    { label: "", render: (r) => r[3] === null ? "" : h("span", { class: r[3] ? "gate-pass" : "gate-fail" }, r[3] ? "OK" : "BREACH") }], rows);
}

function heatmap(monthly) {
  if (!monthly.length) return h("div", { class: "empty" }, "No data");
  const byYear = {};
  for (const m of monthly) {
    const [y, mo] = m.month.split("-");
    (byYear[y] ||= {})[+mo] = m.return_pct;
  }
  const max = Math.max(1, ...monthly.map((m) => Math.abs(m.return_pct)));
  const color = (v) => v === undefined ? "transparent" : v >= 0 ? `rgba(47,191,143,${0.12 + 0.6 * Math.min(1, v / max)})` : `rgba(239,93,107,${0.12 + 0.6 * Math.min(1, -v / max)})`;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const cells = [h("div", { class: "h" }, ""), ...months.map((m) => h("div", { class: "h" }, m)), h("div", { class: "h" }, "Year")];
  for (const [y, ms] of Object.entries(byYear).sort()) {
    cells.push(h("div", { class: "h" }, y));
    let tot = 1;
    for (let i = 1; i <= 12; i++) {
      const v = ms[i];
      if (v !== undefined) tot *= 1 + v / 100;
      cells.push(h("div", { style: { background: color(v) }, title: v !== undefined ? `${y}-${i}: ${num(v, 2)}%` : "" }, v !== undefined ? num(v, 1) : ""));
    }
    const yr = (tot - 1) * 100;
    cells.push(h("div", { style: { fontWeight: 650 }, class: cls(yr) }, num(yr, 1)));
  }
  return h("div", { class: "heat" }, cells);
}

function skipsTable(skips) {
  const rows = Object.entries(skips).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ m: k.split(":")[0], reason: k.split(": ").slice(1).join(": "), n: v }));
  return table([{ label: "Module", render: (r) => moduleTag(r.m) }, { label: "Reason", key: "reason", wrap: true }, { label: "Days", num: true, key: "n" }], rows, { empty: "Nothing skipped" });
}

function tradesTable(trades) {
  return table([
    { label: "Opened (UTC)", render: (t) => fmtDateTime(t.open_ts, "UTC") },
    { label: "Module", render: (t) => moduleTag(t.module) },
    { label: "Side", render: (t) => sideCell(t.side) },
    { label: "Size", num: true, render: (t) => num(t.units, 0) },
    { label: "Entry", num: true, render: (t) => price(t.entry) },
    { label: "Stop", num: true, render: (t) => price(t.sl_initial) },
    { label: "Target", num: true, render: (t) => price(t.tp) },
    { label: "Exit", num: true, render: (t) => price(t.exit) },
    { label: "Reason", render: (t) => t.exit_reason },
    { label: "R", num: true, render: (t) => h("span", { class: cls(t.r) }, rfmt(t.r)) },
    { label: "MFE", num: true, render: (t) => rfmt(t.mfe_r) },
    { label: "P&L", num: true, render: (t) => h("span", { class: cls(t.pnl) }, signed(t.pnl)) },
    { label: "Notes", wrap: true, render: (t) => h("span", { class: "dim small" }, [t.tag, ...(t.notes || [])].filter(Boolean).join(" · ")) },
  ], trades, { maxHeight: "560px" });
}

function variantsView(res) {
  const rows = res.variants || [];
  return h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("h2", {}, res.name || "Variant sweep"), h("span", { class: "sub" }, `${res.dataset?.id} · ${res.start} → ${res.end}`)),
    (res.warnings || []).map((w) => h("div", { class: "banner warn" }, h("b", {}, "Note"), w)),
    card("Variants", table([
      { label: "Variant", render: (r) => h("div", {}, h("div", { style: { fontWeight: 600 } }, r.name), h("div", { class: "dim small mono" }, JSON.stringify(r.overrides))) },
      { label: "Trades", num: true, render: (r) => r.summary.trades },
      { label: "Mean R", num: true, render: (r) => h("span", { class: cls(r.summary.mean_r) }, rfmt(r.summary.mean_r)) },
      { label: "t", num: true, render: (r) => num(r.summary.t_stat, 2) },
      { label: "PF", num: true, render: (r) => num(r.summary.profit_factor, 2) },
      { label: "Return", num: true, render: (r) => signed(r.summary.return_pct, 1, "%") },
      { label: "Max DD", num: true, render: (r) => pct(r.summary.max_drawdown_pct, 1) },
      { label: "Halves", num: true, render: (r) => `${rfmt(r.halves.first.all.mean_r)} / ${rfmt(r.halves.second.all.mean_r)}` },
      { label: "Gates passed", num: true, render: (r) => { const g = r.gates.filter((x) => x.pass !== null); return `${g.filter((x) => x.pass).length}/${g.length}`; } },
    ], rows)));
}
