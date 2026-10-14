// Dashboard: the live view of the running bot.
import {
  api, ago, card, cls, confirmBox, fmtDateTime, fmtTime, guard, h, kpi, money, mount, moduleTag, num, pct,
  price, rfmt, sideCell, signed, sw, table, zone, seg, getTz, setTz, TZ,
} from "./lib.js";
import { PriceChart } from "./charts.js";

let root, chart, chartTimer, unsub, lastBarTime = 0, eventsEl, eventsSeen = new Set(), storeRef;

export function render(main, { store }) {
  storeRef = store;
  root = h("div", { class: "stack" });
  mount(main, root);
  const update = (snap) => draw(snap, store);
  store.listeners.add(update);
  unsub = () => store.listeners.delete(update);
  draw(store.snap, store);
}

export function destroy() {
  if (unsub) unsub();
  if (chart) chart.destroy();
  chart = null;
  clearInterval(chartTimer);
  chartTimer = null;
  eventsEl = null;
  eventsSeen = new Set();
  lastBarTime = 0;
}

function draw(snap, store) {
  if (!root) return;
  const e = snap && snap.engine;
  if (!snap || !snap.running || !e) {
    if (chart) { chart.destroy(); chart = null; clearInterval(chartTimer); chartTimer = null; }
    drawStopped(snap);
    return;
  }
  if (!root.querySelector("#dash-live")) buildLive(snap);
  fill(snap, store);
}

function drawStopped(snap) {
  const r = snap && snap.runner;
  mount(root,
    h("div", { class: "page-head" }, h("h2", {}, "Dashboard"), h("span", { class: "sub" }, "The bot is not running")),
    r && r.status === "error" ? h("div", { class: "banner bad" }, h("b", {}, "Last run stopped with an error:"), r.detail) : null,
    h("div", { class: "grid cols-2" },
      card("Start trading", h("div", { class: "stack" },
        h("p", { class: "muted" }, "Connect your cTrader account (FTMO or any cTrader broker) through the Open API, choose an account, then start in Paper mode (live prices, simulated fills) or Live mode (real orders)."),
        h("div", { class: "row" }, h("a", { class: "btn primary", href: "#connection" }, "Open cTrader connection")))),
      card("Watch it work first", h("div", { class: "stack" },
        h("p", { class: "muted" }, "No account yet? Replay stored market data through the exact same engine at high speed and watch the dashboard, chart and trade log fill in."),
        h("div", { class: "row" }, h("a", { class: "btn", href: "#connection" }, "Start a replay"), h("a", { class: "btn ghost", href: "#data" }, "Get market data"))))),
    card("Today's sessions", scheduleTable()),
  );
}

function scheduleTable() {
  const el = h("div", {}, h("div", { class: "empty" }, "Loading…"));
  api("/api/schedule").then((s) => {
    mount(el, h("p", { class: "muted small" }, `${s.weekday} ${s.day}: ${s.note}`),
      table([
        { label: "Window", key: "window" }, { label: "Defined in", key: "defined_in" },
        { label: "UTC", key: "utc", num: true }, { label: "London", key: "london", num: true },
        { label: "New York", key: "new_york", num: true }, { label: "Sydney", key: "sydney", num: true },
      ], s.rows));
  }).catch(() => {});
  return el;
}

// ---------------------------------------------------------------- live layout
function buildLive(snap) {
  const chartEl = h("div", { class: "chart", id: "price-chart" });
  eventsEl = h("div", { class: "events", id: "events" });
  mount(root,
    h("div", { id: "dash-live", class: "stack" },
      h("div", { id: "banners", class: "stack" }),
      h("div", { class: "grid cols-6", id: "kpis" }),
      h("div", { class: "dash-main" },
        h("div", { class: "stack" },
          h("div", { class: "card" },
            h("div", { class: "card-head" }, h("h3", {}, "XAU/USD · M5"),
              h("div", { class: "legend" },
                h("span", {}, h("i", { style: { background: "var(--gold)" } }), "EMA 20"),
                h("span", {}, h("i", { style: { background: "var(--info)" } }), "VWAP (07:00 London)"),
                h("span", {}, h("i", { style: { background: "var(--text-2)" } }), "Asian range"),
                h("span", {}, h("i", { style: { background: "var(--line-2)" } }), "$20 levels")),
              h("div", { class: "actions", id: "tz-seg" })),
            chartEl,
            h("div", { id: "timeline" })),
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Open positions"), h("div", { class: "actions", id: "pos-actions" })), h("div", { id: "positions" })),
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Pending orders")), h("div", { id: "orders" })),
        ),
        h("div", { class: "stack" },
          h("div", { class: "card", id: "risk" }),
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Modules")), h("div", { class: "stack", id: "modules", style: { gap: "8px" } })),
          h("div", { class: "card", id: "market" }),
          h("div", { class: "card", id: "news" }),
        ),
      ),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Event log"), h("div", { class: "actions" }, h("a", { href: "#trades", class: "small" }, "Full trade log →"))), eventsEl),
    ));
  chart = new PriceChart(chartEl);
  refreshChart();
  chartTimer = setInterval(refreshChart, 5000);
  mount(root.querySelector("#tz-seg"), seg(Object.entries(TZ).map(([k, v]) => [k, v.label]), getTz(), (k) => {
    setTz(k);
    if (chart) chart.destroy();
    chart = null;
    clearInterval(chartTimer);
    eventsSeen = new Set();
    mount(root);
    draw(storeRef.snap, storeRef);
  }));
  loadEvents();
}

function refreshChart() {
  api("/api/chart?limit=400").then((d) => chart && chart.update(d)).catch(() => {});
}

async function loadEvents() {
  try {
    const rows = await api("/api/events?limit=120");
    for (const ev of rows.reverse()) addEvent(ev);
  } catch {}
}

function addEvent(ev) {
  if (!eventsEl) return;
  const key = `${ev.ts}|${ev.kind}|${ev.message}`;
  if (eventsSeen.has(key)) return;
  eventsSeen.add(key);
  eventsEl.prepend(h("div", { class: "ev" }, h("span", { class: "dim" }, fmtDateTime(ev.ts).replace(",", "")),
    h("span", { class: `k k-${ev.kind}` }, ev.kind), h("span", {}, ev.message)));
  while (eventsEl.childElementCount > 250) eventsEl.lastChild.remove();
}

function fill(snap, store) {
  const e = snap.engine;
  const d = e.daily || {};
  const startBal = d.start_balance || e.balance;
  const dayPnl = e.equity - startBal;

  // banners
  const banners = [];
  if (snap.runner.mode === "live") banners.push(h("div", { class: "banner bad" }, h("b", {}, "LIVE"), "Real orders are being sent to your cTrader account."));
  if (snap.runner.status !== "running") banners.push(h("div", { class: "banner warn" }, h("b", {}, snap.runner.status), snap.runner.detail || ""));
  if (e.paused) banners.push(h("div", { class: "banner warn" }, h("b", {}, "Paused"), "No new entries. Open trades keep their server-side stops."));
  if (d.stopped) banners.push(h("div", { class: "banner bad" }, h("b", {}, "Stopped for today"), d.stopped));
  if (e.news && e.news.blackout) banners.push(h("div", { class: "banner warn" }, h("b", {}, "News blackout"), `${e.news.blackout.title} at ${fmtTime(e.news.blackout.ts)}: no new entries`));
  for (const err of (snap.errors || []).slice(-2)) banners.push(h("div", { class: "banner bad" }, h("b", {}, "Broker"), `${err.message} (${ago(err.ts)})`));
  if (snap.runner.mode === "replay") banners.push(h("div", { class: "banner info" }, h("b", {}, "Replay"), `${snap.runner.detail} · ${num((snap.runner.progress || 0) * 100, 1)}% · ${e.now_london || ""} London`));
  mount(root.querySelector("#banners"), banners);

  // KPIs
  const acct = snap.account || {};
  mount(root.querySelector("#kpis"),
    kpi("Balance", money(e.balance), `${acct.currency || ""} ${acct.broker ? "· " + acct.broker : ""}`),
    kpi("Equity", money(e.equity), `open P&L ${signed(e.equity - e.balance)}`, cls(e.equity - e.balance)),
    kpi("Today", signed(e.daily_pnl_pct, 2, "%"), `${signed(dayPnl)} since ${fmtTime(e.windows?.[0]?.start_ts, "Europe/London")} London`, cls(dayPnl)),
    kpi("Trades today", `${d.trades ?? 0} / ${e.limits.max_trades}`, `${d.wins ?? 0} won · ${d.losses ?? 0} lost`),
    kpi("Loss streak", `${d.consecutive_losses ?? 0} / ${e.limits.max_consecutive_losses}`, "stops the day at the limit", (d.consecutive_losses || 0) >= 2 ? "warn-text" : ""),
    kpi("Spread", e.spread !== null ? num(e.spread, 2) : "—", `ATR M5 ${num(e.indicators.atr_m5, 2)}`),
  );

  // timeline
  mount(root.querySelector("#timeline"), timeline(e));

  // positions
  const pos = e.positions || [];
  mount(root.querySelector("#positions"), table([
    { label: "Module", render: (p) => moduleTag(p.module) },
    { label: "Side", render: (p) => sideCell(p.side) },
    { label: "Size (oz)", num: true, render: (p) => num(p.units, 0) },
    { label: "Entry", num: true, render: (p) => price(p.entry) },
    { label: "Stop", num: true, render: (p) => h("span", {}, price(p.sl), p.be_done ? h("span", { class: "pill plain gold", style: { marginLeft: "6px" } }, "BE") : null) },
    { label: "Target", num: true, render: (p) => price(p.tp) },
    { label: "Now", num: true, render: (p) => h("span", { class: cls(p.r_now) }, rfmt(p.r_now)) },
    { label: "P&L", num: true, render: (p) => { const v = (p.mark - p.entry) * p.side * p.units + (p.realized_pnl || 0); return h("span", { class: cls(v) }, signed(v)); } },
    { label: "Opened", render: (p) => fmtTime(p.open_ts) },
    { label: "", render: (p) => h("button", { class: "btn sm danger", onclick: async () => {
      if (await confirmBox(`Close ${p.ref}?`, `Close this ${p.module} position at market now.`, "Close", "danger")) guard(() => api(`/api/bot/close/${encodeURIComponent(p.ref)}`, { method: "POST" }), "Close sent");
    } }, "Close") },
  ], pos, { empty: "No open positions" }));

  mount(root.querySelector("#orders"), table([
    { label: "Module", render: (o) => moduleTag(o.module) },
    { label: "Type", render: (o) => h("span", {}, sideCell(o.side), " stop") },
    { label: "Trigger", num: true, render: (o) => price(o.entry) },
    { label: "Stop", num: true, render: (o) => price(o.sl) },
    { label: "Target", num: true, render: (o) => price(o.tp) },
    { label: "Size (oz)", num: true, render: (o) => num(o.units, 0) },
    { label: "Risk", num: true, render: (o) => money(o.risk_money) },
    { label: "Expires", render: (o) => o.expiry_ts ? fmtTime(o.expiry_ts) : "—" },
  ], e.orders || [], { empty: "No pending orders" }));

  // risk card
  const lossStop = e.limits.daily_loss_stop_pct, profitStop = e.limits.daily_profit_stop_pct;
  const p = e.daily_pnl_pct || 0;
  const span = lossStop + profitStop;
  const zero = (lossStop / span) * 100;
  const pos01 = Math.max(0, Math.min(100, ((p + lossStop) / span) * 100));
  mount(root.querySelector("#risk"),
    h("div", { class: "card-head" }, h("h3", {}, "Daily risk"), h("span", { class: `pill ${d.stopped ? "bad" : "ok"}` }, d.stopped ? "stopped" : "trading allowed")),
    h("div", { class: "row small muted", style: { justifyContent: "space-between" } }, h("span", {}, `−${lossStop}% stop`), h("span", { class: `num ${cls(p)}` }, signed(p, 2, "%")), h("span", {}, `+${profitStop}% stop`)),
    h("div", { class: "meter split" }, h("i", { class: "center", style: { left: `${zero}%` } }),
      h("i", { style: { left: `${Math.min(zero, pos01)}%`, width: `${Math.abs(pos01 - zero)}%`, background: p >= 0 ? "var(--up)" : "var(--down)" } })),
    h("div", { class: "grid cols-2", style: { marginTop: "12px", gap: "8px" } },
      stat("FTMO daily limit used", pct(Math.max(0, -p) / 5 * 100, 0), "of the 5% daily loss"),
      stat("Realized today", signed(d.realized_pnl || 0), "closed trades"),
      stat("Risk per trade", `${num(e.limits.risk_per_trade_pct, 2)}%`, "of balance"),
      stat("Min stop now", `$${num(e.indicators.min_stop, 2)}`, "max($3, ATR M5)")));

  // modules
  mount(root.querySelector("#modules"), (e.modules || []).map((m) => h("div", { class: `module ${m.enabled ? "" : "off"}` },
    h("div", { class: "head" }, moduleTag(m.name), h("span", { class: "title" }, m.title),
      h("span", { style: { marginLeft: "auto" } }, sw(m.enabled, (on) => toggleModule(m.name, on), "Switch this module on/off (applies live)"))),
    h("div", { class: "status" }, m.enabled ? m.status : "switched off"),
    h("div", { class: "meta" }, h("span", {}, m.window), h("span", {}, `${m.trades} trade${m.trades === 1 ? "" : "s"}`), m.losses ? h("span", { class: "down" }, `${m.losses} loss${m.losses === 1 ? "" : "es"}`) : null))));

  // market card
  const a = e.asian;
  const ind = e.indicators;
  const slope = ind.ema_m15_slope;
  const bias = ind.vwap && e.bid ? (e.bid > ind.vwap && slope > 0 ? "long" : e.bid < ind.vwap && slope < 0 ? "short" : "none") : "—";
  mount(root.querySelector("#market"),
    h("div", { class: "card-head" }, h("h3", {}, "Market structure")),
    h("div", { class: "grid cols-2", style: { gap: "8px" } },
      stat("Asian high", a ? price(a.high) : "—", a ? `range ${num(a.width, 2)} (${num(a.width_pct, 2)}%)` : "building"),
      stat("Asian low", a ? price(a.low) : "—", a ? `${num(a.coverage * 100, 0)}% data` : ""),
      stat("VWAP", price(ind.vwap), "anchored 07:00 London"),
      stat("Module C bias", bias, `M15 EMA slope ${signed(slope, 2)}`),
      stat("$20 above", price(e.levels.above), e.bid ? `${num(e.levels.above - e.bid, 2)} away` : ""),
      stat("$20 below", price(e.levels.below), e.bid ? `${num(e.bid - e.levels.below, 2)} away` : "")));

  // news
  const nx = e.news && e.news.next;
  mount(root.querySelector("#news"),
    h("div", { class: "card-head" }, h("h3", {}, "News filter"), h("div", { class: "actions" }, h("a", { href: "#data", class: "small" }, "Calendar →"))),
    e.news && e.news.blackout ? h("div", { class: "banner warn" }, h("b", {}, "Now"), e.news.blackout.title) : null,
    nx ? h("div", {}, h("div", { class: "muted small" }, "Next high-impact USD event"), h("div", { style: { fontWeight: 600 } }, nx.title),
      h("div", { class: "small dim" }, `${fmtDateTime(nx.ts)} (${zone()})`)) : h("div", { class: "muted small" }, "No upcoming events loaded. The calendar refreshes hourly while the bot runs."));

  // events from websocket
  for (const ev of store.events) addEvent(ev);
  if (e.now && e.now !== lastBarTime) { lastBarTime = e.now; refreshChart(); }
}

function stat(label, value, foot) {
  return h("div", {}, h("div", { class: "dim small" }, label), h("div", { class: "num", style: { fontWeight: 600 } }, value), foot ? h("div", { class: "dim small" }, foot) : null);
}

function timeline(e) {
  const rows = e.windows || [];
  if (!rows.length) return h("div");
  const start = rows[0].start_ts, end = rows[rows.length - 1].end_ts + 1800;
  const x = (ts) => Math.max(0, Math.min(100, ((ts - start) / (end - start)) * 100));
  const colors = ["var(--line-2)", "var(--gold-dim)", "var(--up-dim)", "rgba(190,120,240,.18)", "var(--down-dim)"];
  const labels = ["Asian range", "London A+B", "NY C", "D", "flat"];
  const segs = rows.map((r, i) => h("div", { class: "seg-w", style: { left: `${x(r.start_ts)}%`, width: `${x(r.end_ts) - x(r.start_ts)}%`, background: colors[i], color: "var(--text-2)", top: i === 3 ? "11px" : "0", bottom: i === 3 ? "0" : i === 2 ? "11px" : "0" } }, labels[i]));
  const ticks = [];
  for (let t = Math.ceil(start / 3600) * 3600; t <= end; t += 7200) ticks.push(h("span", { style: { left: `${x(t)}%` } }, fmtTime(t)));
  const now = e.now;
  return h("div", { class: "timeline" }, h("div", { class: "ticks" }, ticks), h("div", { class: "track" }, segs),
    now >= start && now <= end ? h("div", { class: "now", style: { left: `${x(now)}%` }, title: "now" }) : null);
}

async function toggleModule(name, on) {
  const cur = await api("/api/settings");
  const s = cur.settings;
  s[`module_${name.toLowerCase()}`].enabled = on;
  const r = await guard(() => api("/api/settings", { method: "PUT", body: s }));
  if (r && r.notes && r.notes.length) r.notes.forEach((n) => console.info(n));
}
