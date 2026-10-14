// Strategy: rules with live parameter values, the settings editor and the session schedule.
import { api, card, field, guard, h, mount, num, sw, table, toast, todayIso } from "./lib.js";

let root, draft, schema;

export async function render(main) {
  root = h("div", { class: "stack" }, h("div", { class: "empty" }, "Loading…"));
  mount(main, root);
  const r = await api("/api/settings");
  draft = r.settings;
  schema = r.schema;
  draw();
}

export function destroy() { root = null; }

const MODULES = {
  A: {
    key: "module_a", title: "London range breakout", window: "07:00–10:00 London", trades: "0–1",
    evidence: "Strongest published evidence (PF 1.38 over 715 trades, 2023–2026). Reproduce exactly before changing anything.",
    rules: (c) => [
      "Asian range = high and low of 00:00–07:00 London.",
      `Skip the day if the range is under ${c.range_min_pct}% or over ${c.range_max_pct}% of price.`,
      `At 07:00 London place a buy stop $${c.entry_buffer} above the high and a sell stop $${c.entry_buffer} below the low (OCO). Unfilled orders are cancelled at ${c.order_expiry === "window_end" ? "10:00 London" : "11:00 New York"}.`,
      `Stop: opposite side of the range, capped at ${c.stop_cap_pct}% of price.`,
      `Target ${c.target_r}R; stop to break-even at ${c.breakeven_r || "—"}R.`,
      `At most ${c.max_trades} Module A trade per day. Exit: ${c.exit_mode === "session_close" ? "flat by 11:30 New York (shared rule)" : "21:00 server time (source's exit)"}.`,
    ],
  },
  B: {
    key: "module_b", title: "Asian sweep and reclaim", window: "07:00–10:00 London", trades: "0–2",
    evidence: "Hypothesis: trades the failure of Module A's breakout. Must beat random entries first.",
    rules: (c) => [
      `Sweep: price trades at least $${c.sweep_min} beyond the Asian high or low.`,
      `Reclaim: an M5 candle closes back inside the range within ${c.reclaim_bars} bars of the sweep.`,
      "Entry at market on the reclaim candle's close, back into the range.",
      `Stop beyond the sweep wick plus max($${c.stop_buffer_min}, ${c.stop_buffer_atr} × M5 ATR(14)).`,
      `${Math.round(c.partial_fraction * 100)}% off at the range midpoint, the rest $${c.final_target_offset} short of the opposite edge; stop to break-even after the first target.`,
      "An opposite Module A position is closed first. One trade per side (high sweep, low sweep) per day.",
    ],
  },
  C: {
    key: "module_c", title: "NY continuation pullback", window: "08:20–11:00 New York", trades: "0–3",
    evidence: "Main source of trade count: pullbacks in London's direction during US data, COMEX open and the PM fix.",
    rules: (c) => [
      `Bias long when price is above the VWAP anchored at 07:00 London AND the M15 ${c.ema_period} EMA slopes up (over ${c.slope_bars} M15 bars); short when both point down; no trade when they disagree.`,
      `Setup: an M5 pullback touching the M5 ${c.ema_period} EMA or the anchored VWAP (within $${c.touch_tolerance}); armed for ${c.setup_expiry_bars} M5 bars.`,
      `Trigger: an M1 break of the pullback's last swing (fractal, ${c.swing_strength} bars each side) + $${c.entry_buffer}.`,
      `Stop $${c.stop_buffer} beyond the pullback extreme, respecting the minimum stop distance.`,
      `Target ${c.target_r}R, adjusted by the $20-level rule; skipped with less than the minimum room.`,
      `At most ${c.max_trades} trades; stops for the day after ${c.max_losses} losses.`,
    ],
  },
  D: {
    key: "module_d", title: "PM-fix reversion (experimental)", window: "09:45–10:30 New York", trades: "0–2",
    evidence: "Stays off until it passes the full validation plan alone. The $20-level effect (~$0.33) is about the size of the spread.",
    rules: (c) => [
      `Price has moved at least ${c.move_atr_mult} × M15 ATR(14) since 08:30 New York and is within $${c.level_proximity} of a $20 level.`,
      "Trigger: an M5 candle closes back away from the level (a rejection close).",
      "Entry on that candle's close, against the prior move.",
      `Stop: $${c.stop_beyond_level} beyond the level or $${c.extreme_buffer} beyond the move's extreme, whichever is further.`,
      `Target: ${c.target_r}R or the anchored VWAP, whichever is nearer.`,
      `At most ${c.max_trades} trades, one per $20 level.`,
    ],
  },
};

function draw() {
  if (!root) return;
  const s = draft;
  const warnings = [];
  if (s.module_a.enabled && s.module_a.apply_level_rules) {
    warnings.push(h("div", { class: "banner warn" }, h("b", {}, "Rule conflict"),
      h("span", {}, "With $20 spacing, any target more than $20 from entry always has a $20 level in front of it, so the shared “$1 short of the next level” rule cuts Module A's 2.5R target on most days, and many trades then fail the 1.2R room check. The shared rules override modules (as the document says), so this is on. For validation gate 2 (reproduce Module A) run the “Module A reproduction” backtest preset, which switches the level rules off for A.")));
  }
  if (s.mode === "live") warnings.push(h("div", { class: "banner bad" }, h("b", {}, "Live trading enabled"), "The bot may send real orders when started in live mode."));

  mount(root,
    h("div", { class: "page-head" },
      h("h2", {}, "Strategy"),
      h("span", { class: "sub" }, `XAU/USD only · ${s.risk.risk_per_trade_pct}% risk per trade · max ${s.risk.max_trades_per_day} trades/day · expect 2–6 on a typical day`),
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: async () => { const r = await api("/api/settings"); draft = r.settings; draw(); toast("Changes discarded"); } }, "Discard changes"),
        h("button", { class: "btn primary", onclick: save }, "Save settings"))),
    warnings,
    h("div", { class: "grid cols-2" }, Object.entries(MODULES).map(([name, m]) => moduleCard(name, m))),
    card("Shared rules (override any module rule they conflict with)", sharedRules()),
    h("div", { class: "grid cols-2" },
      card("Session schedule", schedule()),
      card("Validation plan", validation())),
    card("All parameters", allParams(), h("span", { class: "dim small" }, "Module on/off switches apply immediately while the bot runs; everything else applies the next time it starts.")),
  );
}

function moduleCard(name, m) {
  const c = draft[m.key];
  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("span", { class: `tag ${name}` }, name), h("h3", {}, m.title),
      h("div", { class: "actions" }, h("span", { class: `pill ${c.enabled ? "ok" : "idle"}` }, c.enabled ? "on" : "off"),
        sw(c.enabled, (on) => { draft[m.key].enabled = on; draw(); }))),
    h("div", { class: "row small dim", style: { marginBottom: "8px" } }, h("span", {}, m.window), h("span", {}, `· ${m.trades} trades/day`)),
    h("p", { class: "small muted" }, m.evidence),
    h("ol", { class: "rules small" }, m.rules(c).map((r) => h("li", {}, r))),
    h("details", { class: "rule", style: { marginTop: "10px" } }, h("summary", {}, h("span", { class: "small" }, "Parameters")),
      h("div", { class: "body" }, form(m.key, defName(m.key)))));
}

function sharedRules() {
  const r = draft.risk, l = draft.levels, n = draft.news, se = draft.sessions;
  return table([{ label: "Rule", key: "rule" }, { label: "Setting", key: "setting", wrap: true }], [
    { rule: "Risk per trade", setting: `${r.risk_per_trade_pct}% of ${r.risk_basis === "balance" ? "current balance" : "starting balance"}` },
    { rule: "Position sizing", setting: `From money lost at the stop: units × (stop distance + $${r.sizing_slippage_usd} slippage + round-turn commission), in account currency` },
    { rule: "Max trades per day", setting: `${r.max_trades_per_day} (a ceiling, not a quota)` },
    { rule: "Daily loss stop", setting: `−${r.daily_loss_stop_pct}% equity or ${r.max_consecutive_losses} consecutive losses, whichever first${r.flatten_on_daily_stop ? " (flattens on the % stop)" : ""}` },
    { rule: "Daily profit stop", setting: `+${r.daily_profit_stop_pct}% (protects the 30% Best Day Rule)` },
    { rule: "Minimum stop distance", setting: `max($${r.min_stop_usd}, ${r.min_stop_atr_mult} × M5 ATR(14))` },
    { rule: "Targets near $20 levels", setting: `If the next $${l.step} level comes before the target, target $${l.target_offset} short of it` },
    { rule: "Minimum room to target", setting: `Skip if less than ${l.min_room_r}R after the $20-level rule` },
    { rule: "Stops near $20 levels", setting: `Never within $${l.stop_near_zone} on the near side; pushed to $${l.stop_beyond} beyond` },
    { rule: "News filter", setting: n.enabled ? `No entries ${n.before_minutes} min before to ${n.after_minutes} min after high-impact ${n.currencies.join("/")} news; ${n.major_after_minutes} min after NFP, CPI, FOMC` : "OFF" },
    { rule: "Session close", setting: `No new trades after ${se.no_new_trades_ny} New York; flat by ${se.flat_ny} New York` },
    { rule: "Weekend", setting: "Flat before Friday close (daily flat covers it); no Monday trading before the London open" },
    { rule: "One direction at a time", setting: "A new signal against an open position closes that position first; never both sides" },
  ]);
}

function schedule() {
  const el = h("div", { class: "stack" });
  const dateInput = h("input", { type: "date", value: todayIso(), onchange: () => load() });
  const out = h("div");
  el.append(h("div", { class: "row" }, field("Date", dateInput)), out);
  async function load() {
    const s = await api(`/api/schedule?day=${dateInput.value}`);
    mount(out,
      h("p", { class: "small muted" }, `${s.weekday}: ${s.note}`),
      table([
        { label: "Window", key: "window" },
        { label: "UTC", key: "utc", num: true }, { label: "London", key: "london", num: true },
        { label: "New York", key: "new_york", num: true }, { label: "Sydney", key: "sydney", num: true },
      ], s.rows),
      s.dst_changes.length ? h("div", { class: "small", style: { marginTop: "10px" } },
        h("div", { class: "dim" }, "Clock changes in the next 90 days"),
        h("ul", { style: { margin: "4px 0 0", paddingLeft: "18px" } }, s.dst_changes.map((c) => h("li", { class: "muted" }, `${c.date}: ${c.zone} moves to UTC${c.new_offset_hours >= 0 ? "+" : ""}${c.new_offset_hours}`)))) : null,
      h("p", { class: "small dim", style: { marginTop: "8px" } }, "The New York window falls around midnight Sydney time, so run the bot on a VPS."));
  }
  load();
  return el;
}

function validation() {
  const gates = [
    ["Freeze the rules", "Write each module's rules and up to ~24 variants before running anything."],
    ["Reproduce Module A", "cTrader tick data, real spread and commission: PF ≈ 1.3+ or debug the build."],
    ["Beat random entries", "Each module alone vs random entries with the same windows, stops, targets and exits."],
    ["Two halves", "Profitable in both 2018–2021 and 2022–2026."],
    ["Significance", "t ≥ 3 on mean R per trade."],
    ["Combine", "All passing modules together: drawdown well inside FTMO limits at 0.3% risk."],
    ["Forward test", "1–2 months demo, then an FTMO free trial."],
  ];
  return h("div", { class: "steps" }, gates.map(([t, d]) => h("div", { class: "step" }, h("div", { style: { fontWeight: 600 } }, t), h("div", { class: "small muted" }, d))),
    h("div", { class: "row" }, h("a", { class: "btn", href: "#backtest" }, "Run the backtests →")));
}

// ---------------------------------------------------------------- generic settings forms from the JSON schema
function defName(section) {
  const prop = schema.properties[section];
  const ref = prop.$ref || (prop.allOf && prop.allOf[0].$ref);
  return ref ? ref.split("/").pop() : null;
}

function humanize(k) {
  return k.replace(/_/g, " ").replace(/\b(pct)\b/g, "%").replace(/\b(usd)\b/g, "$").replace(/\b(rt)\b/g, "round-turn")
    .replace(/\b(ny)\b/g, "NY").replace(/\b(atr)\b/g, "ATR").replace(/\b(r)\b/g, "R").replace(/^./, (c) => c.toUpperCase());
}

function form(section, def) {
  const props = schema.$defs[def].properties;
  const values = draft[section];
  const grid = h("div", { class: "form-grid" });
  for (const [key, p] of Object.entries(props)) {
    if (section.startsWith("module_") && key === "enabled") continue;
    grid.append(input(section, key, p, values[key]));
  }
  return grid;
}

function input(section, key, p, value) {
  const set = (v) => { draft[section][key] = v; };
  const types = p.anyOf ? p.anyOf.map((x) => x.type) : [p.type];
  const nullable = types.includes("null");
  const enumVals = p.enum || (p.anyOf && p.anyOf.find((x) => x.enum)?.enum);
  let el;
  if (types.includes("boolean")) {
    el = h("div", { style: { paddingTop: "4px" } }, sw(value, (on) => set(on)));
  } else if (enumVals) {
    el = h("select", { onchange: (e) => set(e.target.value) }, enumVals.map((v) => h("option", { value: v, selected: v === value }, v)));
  } else if (types.includes("number") || types.includes("integer")) {
    el = h("input", { type: "number", step: types.includes("integer") ? "1" : "any", value: value ?? "", placeholder: nullable ? "auto" : "",
      onchange: (e) => set(e.target.value === "" ? (nullable ? null : 0) : Number(e.target.value)) });
  } else if (types.includes("array")) {
    el = h("input", { type: "text", value: (value || []).join(", "), onchange: (e) => set(e.target.value.split(",").map((x) => x.trim()).filter(Boolean)) });
  } else {
    el = h("input", { type: "text", value: value ?? "", onchange: (e) => set(e.target.value) });
  }
  return h("label", { class: "field" }, h("span", {}, humanize(key)), el, p.description ? h("span", { class: "dim", style: { fontSize: "11px" } }, p.description) : null);
}

function allParams() {
  const sections = [
    ["risk", "Risk and daily limits"], ["levels", "$20-level rules"], ["news", "News filter"], ["sessions", "Session windows"],
    ["costs", "Simulator costs (backtest, paper, replay)"], ["backtest", "Backtest and validation"], ["ctrader", "cTrader"],
  ];
  const wrap = h("div", { class: "stack", style: { gap: "8px" } });
  wrap.append(h("details", { class: "rule" }, h("summary", {}, "Bot mode"), h("div", { class: "body" },
    h("div", { class: "form-grid" },
      h("label", { class: "field" }, h("span", {}, "Mode"),
        h("select", { onchange: (e) => { draft.mode = e.target.value; draw(); } },
          h("option", { value: "paper", selected: draft.mode === "paper" }, "paper (no real orders)"),
          h("option", { value: "live", selected: draft.mode === "live" }, "live (real orders allowed)")),
        h("span", { class: "dim", style: { fontSize: "11px" } }, "Live trading must be enabled here before the bot can start in live mode.")),
      h("label", { class: "field" }, h("span", {}, "Autostart"), h("div", { style: { paddingTop: "4px" } }, sw(draft.autostart, (on) => { draft.autostart = on; })),
        h("span", { class: "dim", style: { fontSize: "11px" } }, "Reconnect to the saved account in the saved mode when the terminal starts (VPS reboots)."))))));
  for (const [key, title] of sections) {
    wrap.append(h("details", { class: "rule" }, h("summary", {}, title), h("div", { class: "body" }, form(key, defName(key)))));
  }
  return wrap;
}

async function save() {
  const r = await guard(() => api("/api/settings", { method: "PUT", body: draft }));
  if (!r) return;
  draft = r.settings;
  toast("Settings saved", "ok");
  for (const n of r.notes || []) toast(n);
  draw();
}
