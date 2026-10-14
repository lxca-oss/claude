// Data & news: market datasets for backtests and the high-impact news calendar.
import {
  api, ago, card, confirmBox, download, field, fmtDateTime, guard, h, mount, num, sw, table, toast, todayIso, zone,
} from "./lib.js";

let root, timer;

export function render(main) {
  root = h("div", { class: "stack" });
  mount(main, root);
  draw();
  timer = setInterval(refreshDownloads, 1500);
}

export function destroy() { clearInterval(timer); root = null; }

async function draw() {
  const [datasets, news] = await Promise.all([api("/api/datasets"), api("/api/news?hours=336&past_hours=48")]);
  if (!root) return;
  mount(root,
    h("div", { class: "page-head" }, h("h2", {}, "Data & news"), h("span", { class: "sub" }, "M1 history for backtests and the high-impact news calendar")),
    card("Datasets", table([
      { label: "Dataset", render: (d) => h("div", {}, h("div", { style: { fontWeight: 600 } }, d.id), h("div", { class: "dim small" }, d.notes || "")) },
      { label: "Source", render: (d) => d.synthetic ? h("span", { class: "pill warn plain" }, "synthetic") : d.source },
      { label: "From", key: "start" }, { label: "To", key: "end" },
      { label: "M1 bars", num: true, render: (d) => num(d.bars, 0) },
      { label: "Real spread", num: true, render: (d) => `${num(d.spread_coverage * 100, 0)}%` },
      { label: "Raw ticks", num: true, render: (d) => d.tick_days ? `${d.tick_days} days` : "—" },
      { label: "Updated", key: "updated" },
      { label: "", render: (d) => h("div", { class: "row" },
        h("a", { class: "btn sm", href: "#backtest" }, "Backtest"),
        h("button", { class: "btn sm ghost", onclick: async () => {
          if (await confirmBox(`Delete ${d.id}?`, "The stored bars are removed from disk.", "Delete", "danger")) {
            await guard(() => api(`/api/datasets/${d.id}`, { method: "DELETE" }), "Deleted");
            draw();
          }
        } }, "✕")) },
    ], datasets, { empty: "No datasets yet: download or import one below" })),
    h("div", { id: "downloads" }),
    h("div", { class: "grid cols-2" }, ctraderCard(), dukascopyCard()),
    h("div", { class: "grid cols-2" }, importCard(), syntheticCard()),
    newsCard(news),
  );
  refreshDownloads();
}

async function refreshDownloads() {
  const el = root && root.querySelector("#downloads");
  if (!el) return;
  let rows = [];
  try { rows = await api("/api/downloads"); } catch { return; }
  if (!rows.length) { mount(el); return; }
  const wasRunning = el.dataset.running === "1";
  const running = rows.some((r) => r.state === "running");
  el.dataset.running = running ? "1" : "0";
  mount(el, card("Downloads", h("div", { class: "stack", style: { gap: "10px" } }, rows.slice().reverse().map((r) => h("div", {},
    h("div", { class: "row", style: { justifyContent: "space-between" } }, h("span", { style: { fontWeight: 600 } }, `${r.kind} · ${r.params.start} → ${r.params.end}`),
      h("span", { class: `pill ${r.state === "done" ? "ok" : r.state === "error" ? "bad" : "info"}` }, r.state)),
    h("div", { class: "progress", style: { margin: "6px 0 3px" } }, h("i", { style: { width: `${(r.progress || 0) * 100}%` } })),
    h("div", { class: "dim small" }, r.message))))));
  if (wasRunning && !running) draw();
}

function dateRange(state) {
  return [
    field("From", h("input", { type: "date", value: state.start, onchange: (e) => { state.start = e.target.value; } })),
    field("To", h("input", { type: "date", value: state.end, onchange: (e) => { state.end = e.target.value; } })),
  ];
}

function ctraderCard() {
  const st = { start: todayIso(-365), end: todayIso(-1), kind: "ctrader-trendbars", dataset: "" };
  return card("Download from cTrader", h("div", { class: "stack", style: { gap: "10px" } },
    h("p", { class: "small muted" }, "Uses your connected account (cTrader tab). Trendbars are fast but bid-only (fixed spread assumed). Ticks give the real spread and are kept for tick-replay backtests, which validation gate 2 asks for, but are slow to download (5 requests/second limit). FTMO's server may not have history back to 2018: check the range you get."),
    h("div", { class: "form-grid" },
      field("Type", h("select", { onchange: (e) => { st.kind = e.target.value; } },
        h("option", { value: "ctrader-trendbars" }, "M1 trendbars (fast)"), h("option", { value: "ctrader-ticks" }, "Ticks → M1 with real spread (slow)"))),
      ...dateRange(st),
      field("Dataset name", h("input", { type: "text", placeholder: "auto", onchange: (e) => { st.dataset = e.target.value; } }))),
    h("div", { class: "row" }, h("button", { class: "btn primary", onclick: async () => {
      const s = await api("/api/settings");
      const acc = s.settings.ctrader.account_id;
      if (!acc) { toast("Choose an account in the cTrader tab first", "bad"); return; }
      let isLive = false;
      try { const a = await api("/api/ctrader/accounts"); isLive = !!(a.accounts.find((x) => x.account_id === acc) || {}).is_live; } catch {}
      const r = await guard(() => api("/api/datasets/download", { method: "POST", body: { source: st.kind, start: st.start, end: st.end, dataset: st.dataset || undefined, account_id: acc, is_live: isLive } }));
      if (r) { toast("Download started", "ok"); refreshDownloads(); }
    } }, "Download"))));
}

function dukascopyCard() {
  const st = { start: "2018-01-01", end: todayIso(-1), dataset: "dukascopy-XAUUSD" };
  return card("Download from Dukascopy", h("div", { class: "stack", style: { gap: "10px" } },
    h("p", { class: "small muted" }, "Free public M1 bid and ask history back to 2003, so the two-halves test (2018–2021 vs 2022–2026) can run even if your broker's history is short. It is a different broker's feed: confirm results on cTrader data where they overlap."),
    h("div", { class: "form-grid" }, ...dateRange(st),
      field("Dataset name", h("input", { type: "text", value: st.dataset, onchange: (e) => { st.dataset = e.target.value; } }))),
    h("div", { class: "row" }, h("button", { class: "btn primary", onclick: async () => {
      const r = await guard(() => api("/api/datasets/download", { method: "POST", body: { source: "dukascopy", ...st } }));
      if (r) { toast("Download started (about 8 years takes a while)", "ok"); refreshDownloads(); }
    } }, "Download"))));
}

function importCard() {
  const st = { dataset: "csv-XAUUSD", tz: 0, file: null };
  const status = h("div", { class: "small dim" });
  return card("Import a CSV", h("div", { class: "stack", style: { gap: "10px" } },
    h("p", { class: "small muted" }, "Any M1 OHLC file: cTrader/MT4/MT5 exports, Dukascopy CSV and similar. Needs time (or date + time), open, high, low, close; optional volume, spread or ask_close. Bid prices expected."),
    h("div", { class: "form-grid" },
      field("File", h("input", { type: "file", accept: ".csv,.txt", onchange: (e) => { st.file = e.target.files[0]; } })),
      field("Dataset name", h("input", { type: "text", value: st.dataset, onchange: (e) => { st.dataset = e.target.value; } })),
      field("File timezone (UTC offset, h)", h("input", { type: "number", step: "0.5", value: st.tz, onchange: (e) => { st.tz = Number(e.target.value); } }), "Many MT servers use UTC+2/+3; times are converted to UTC")),
    h("div", { class: "row" }, h("button", { class: "btn primary", onclick: async () => {
      if (!st.file) { toast("Choose a file", "bad"); return; }
      status.textContent = `Uploading ${num(st.file.size / 1e6, 1)} MB…`;
      const r = await guard(() => api(`/api/datasets/import?dataset=${encodeURIComponent(st.dataset)}&tz_offset=${st.tz}`, { method: "POST", body: st.file, contentType: "text/csv" }));
      status.textContent = r ? `Imported ${num(r.bars, 0)} bars (${r.start} → ${r.end})` : "";
      if (r) draw();
    } }, "Import"), status)));
}

function syntheticCard() {
  const st = { start: "2018-01-01", end: todayIso(-1), seed: 7, dataset: "synthetic-XAUUSD" };
  return card("Synthetic data (software testing only)", h("div", { class: "stack", style: { gap: "10px" } },
    h("p", { class: "small muted" }, "A random walk with gold-like session volatility. Use it to see the terminal and backtester working before you have real data. It has no edge to find, so every module should land on its random baseline."),
    h("div", { class: "form-grid" }, ...dateRange(st),
      field("Seed", h("input", { type: "number", value: st.seed, onchange: (e) => { st.seed = Number(e.target.value); } }))),
    h("div", { class: "row" }, h("button", { class: "btn", onclick: async () => {
      const r = await guard(() => api("/api/datasets/synthetic", { method: "POST", body: st }), "Synthetic dataset created");
      if (r) draw();
    } }, "Generate"))));
}

function newsCard(news) {
  const add = { datetime_utc: "", title: "", impact: "High", currency: "USD" };
  let csvFile = null;
  return card("High-impact news calendar", h("div", { class: "stack", style: { gap: "12px" } },
    h("div", { class: "row" },
      h("span", { class: "pill info plain" }, `${num(news.total, 0)} events stored`),
      news.first ? h("span", { class: "small dim" }, `${news.first.time_utc} → ${news.last.time_utc} UTC`) : null,
      h("span", { class: "small dim" }, `last refresh ${ago(news.last_refresh)}`),
      news.last_error ? h("span", { class: "small down" }, news.last_error) : null,
      news.blackout_now ? h("span", { class: "pill warn" }, `blackout: ${news.blackout_now.title}`) : null,
      h("div", { class: "row", style: { marginLeft: "auto" } },
        h("button", { class: "btn sm", onclick: async () => { const r = await guard(() => api("/api/news/refresh", { method: "POST" })); if (r) { toast(r.error ? `Refresh failed: ${r.error}` : `${r.added} new events`, r.error ? "bad" : "ok"); draw(); } } }, "Refresh from Forex Factory"),
        h("button", { class: "btn sm ghost", onclick: () => download("/api/news.csv") }, "Export CSV"))),
    h("p", { class: "small muted" }, "Live: the Forex Factory weekly feed is fetched hourly and stored, so history builds up while the bot runs. Backtests need past events: import a CSV with columns datetime_utc, currency, impact, title (for example exported from an economic calendar). Without it the backtest's news filter is inactive and the report says so."),
    h("div", { class: "grid cols-2" },
      h("div", { class: "stack", style: { gap: "8px" } }, h("h4", {}, "Import history"),
        h("div", { class: "row" }, h("input", { type: "file", accept: ".csv", onchange: (e) => { csvFile = e.target.files[0]; } }),
          h("button", { class: "btn sm", onclick: async () => {
            if (!csvFile) { toast("Choose a CSV", "bad"); return; }
            const r = await guard(() => api("/api/news/import", { method: "POST", body: csvFile, contentType: "text/csv" }));
            if (r) { toast(`${r.added} events added`, "ok"); draw(); }
          } }, "Import"))),
      h("div", { class: "stack", style: { gap: "8px" } }, h("h4", {}, "Add an event manually"),
        h("div", { class: "row" },
          h("input", { type: "datetime-local", onchange: (e) => { add.datetime_utc = e.target.value.replace("T", " "); }, title: "UTC" }),
          h("input", { type: "text", placeholder: "Title, e.g. CPI m/m", onchange: (e) => { add.title = e.target.value; } }),
          h("button", { class: "btn sm", onclick: async () => {
            const r = await guard(() => api("/api/news/event", { method: "POST", body: add }), "Event added");
            if (r) draw();
          } }, "Add")),
        h("span", { class: "dim small" }, "Time in UTC. NFP, CPI and FOMC titles get the 15-minute after-window."))),
    table([
      { label: `Time (${zone()})`, render: (e) => fmtDateTime(e.ts) },
      { label: "Ccy", key: "currency" },
      { label: "Impact", render: (e) => h("span", { class: `pill plain ${e.impact === "High" ? "bad" : e.impact === "Medium" ? "warn" : "idle"}` }, e.impact) },
      { label: "Event", render: (e) => h("span", {}, e.title, e.major ? h("span", { class: "pill plain gold", style: { marginLeft: "6px" } }, "+15 min") : null) },
      { label: "Blocks entries", render: (e) => e.relevant ? h("span", { class: "gate-pass" }, "yes") : h("span", { class: "dim" }, "no") },
      { label: "Source", render: (e) => h("span", { class: "dim" }, e.source) },
      { label: "", render: (e) => h("button", { class: "btn sm ghost", onclick: async () => {
        await guard(() => api(`/api/news/event?ts=${e.ts}&title=${encodeURIComponent(e.title)}`, { method: "DELETE" }));
        draw();
      } }, "✕") },
    ], news.events, { empty: "No events in the next two weeks", maxHeight: "420px" }),
  ));
}
