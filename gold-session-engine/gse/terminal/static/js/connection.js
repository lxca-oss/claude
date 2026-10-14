// cTrader: Open API app credentials, OAuth, account choice and starting/stopping the bot.
import { api, card, confirmBox, field, guard, h, modal, money, mount, num, sw, table, toast, todayIso } from "./lib.js";

let root, unsub, accounts = null, chosen = null, settings = null;

export async function render(main, { store, params }) {
  root = h("div", { class: "stack" }, h("div", { class: "empty" }, "Loading…"));
  mount(main, root);
  if (params.get("oauth") === "ok") toast("cTrader connected", "ok");
  if (params.get("oauth_error")) toast(`cTrader authorisation failed: ${params.get("oauth_error")}`, "bad");
  await draw(store);
  const upd = () => drawRunner(store.snap);
  store.listeners.add(upd);
  unsub = () => store.listeners.delete(upd);
}

export function destroy() { if (unsub) unsub(); root = null; }

async function draw(store) {
  const [cred, s, ds] = await Promise.all([api("/api/ctrader/credentials"), api("/api/settings"), api("/api/datasets")]);
  settings = s.settings;
  chosen = chosen ?? settings.ctrader.account_id;
  if (!root) return;
  const expDays = cred.token_expires_at ? (cred.token_expires_at - Date.now() / 1000) / 86400 : null;
  mount(root,
    h("div", { class: "page-head" }, h("h2", {}, "cTrader connection"), h("span", { class: "sub" }, "Open API: Protobuf over SSL to demo.ctraderapi.com / live.ctraderapi.com")),
    cred.mock ? h("div", { class: "banner info" }, h("b", {}, "Mock server"), "GSE_CTRADER_HOST is set: the bot talks to a local test server, not cTrader.") : null,
    h("div", { id: "runner" }),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Set up")),
        h("div", { class: "steps" },
          credentialsStep(cred),
          authStep(cred, expDays),
          h("div", { class: `step ${chosen ? "done" : ""}` }, h("div", { style: { fontWeight: 600 } }, "Choose the account"),
            h("p", { class: "small muted" }, "Pick the FTMO (or other cTrader) account the bot should trade. Demo and live accounts behind your cTrader ID are listed."),
            h("button", { class: "btn", onclick: loadAccounts }, accounts ? "Reload accounts" : "Load accounts")),
        )),
      h("div", { class: "stack" },
        h("div", { class: "card", id: "accounts" }, accountsView()),
        startCard(ds))),
    vpsCard(),
  );
  drawRunner(store.snap);
}

function credentialsStep(cred) {
  const st = { client_id: cred.client_id, client_secret: "", redirect_uri: cred.redirect_uri };
  return h("div", { class: `step ${cred.client_id && cred.has_secret ? "done" : ""}` },
    h("div", { style: { fontWeight: 600 } }, "Register an Open API application"),
    h("p", { class: "small muted" }, "At ", h("a", { href: "https://openapi.ctrader.com/apps", target: "_blank", rel: "noopener" }, "openapi.ctrader.com/apps"),
      " create an app (approval can take a little while), add the redirect URI below to it, then paste its client ID and secret here. They are stored only on this machine, in data/secrets.json."),
    h("div", { class: "form-grid" },
      field("Client ID", h("input", { type: "text", value: st.client_id, onchange: (e) => { st.client_id = e.target.value; } })),
      field("Client secret", h("input", { type: "password", placeholder: cred.has_secret ? "•••••• saved" : "", onchange: (e) => { st.client_secret = e.target.value; } })),
      field("Redirect URI", h("input", { type: "text", value: st.redirect_uri, onchange: (e) => { st.redirect_uri = e.target.value; } }))),
    h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "btn", onclick: async () => {
      const r = await guard(() => api("/api/ctrader/credentials", { method: "POST", body: st }), "Saved");
      if (r) draw({ snap: null, listeners: new Set() });
    } }, "Save")));
}

function authStep(cred, expDays) {
  const tok = { access_token: "", refresh_token: "" };
  return h("div", { class: `step ${cred.has_token ? "done" : ""}` },
    h("div", { style: { fontWeight: 600 } }, "Authorise the bot"),
    h("p", { class: "small muted" }, "Log in with your cTrader ID and tick the accounts the bot may trade. The access token lasts about 30 days and is refreshed automatically."),
    cred.has_token ? h("div", { class: "row small", style: { marginBottom: "8px" } },
      h("span", { class: `pill ${expDays !== null && expDays < 3 ? "warn" : "ok"}` }, "token stored"),
      expDays !== null ? h("span", { class: "dim" }, `expires in ${num(expDays, 1)} days`) : null,
      cred.has_refresh_token ? h("button", { class: "btn sm ghost", onclick: () => guard(() => api("/api/ctrader/refresh-token", { method: "POST" }), "Token refreshed") }, "Refresh now") : null) : null,
    h("div", { class: "row" },
      h("button", { class: "btn primary", disabled: !(cred.client_id && cred.has_secret), onclick: async () => {
        const r = await guard(() => api("/api/ctrader/oauth-url"));
        if (r) location.href = r.url;
      } }, "Connect with cTrader ID")),
    h("details", { class: "rule", style: { marginTop: "10px" } }, h("summary", {}, h("span", { class: "small" }, "Or paste a token (e.g. from the app's Playground, handy on a VPS)")),
      h("div", { class: "body stack", style: { gap: "8px" } },
        field("Access token", h("input", { type: "password", onchange: (e) => { tok.access_token = e.target.value; } })),
        field("Refresh token (optional)", h("input", { type: "password", onchange: (e) => { tok.refresh_token = e.target.value; } })),
        h("div", {}, h("button", { class: "btn sm", onclick: async () => {
          const r = await guard(() => api("/api/ctrader/credentials", { method: "POST", body: tok }), "Token saved");
          if (r) draw({ snap: null, listeners: new Set() });
        } }, "Save token")))));
}

async function loadAccounts() {
  const el = root.querySelector("#accounts");
  mount(el, h("div", { class: "empty" }, "Connecting to cTrader…"));
  const r = await guard(() => api("/api/ctrader/accounts"));
  accounts = r ? r.accounts : [];
  mount(el, accountsView());
}

function accountsView() {
  if (accounts === null) {
    return [h("div", { class: "card-head" }, h("h3", {}, "Accounts")), h("p", { class: "muted small" }, chosen ? `Saved account: ${chosen}. Load the list to check it or pick another.` : "Load your accounts after authorising.")];
  }
  return [h("div", { class: "card-head" }, h("h3", {}, "Accounts")),
    table([
      { label: "", render: (a) => h("input", { type: "radio", name: "acc", checked: a.account_id === chosen, onchange: () => { chosen = a.account_id; } }) },
      { label: "Login", render: (a) => h("div", {}, h("div", { style: { fontWeight: 600 } }, a.login ?? a.account_id), h("div", { class: "dim small" }, `ctid ${a.account_id}`)) },
      { label: "Type", render: (a) => h("span", { class: `pill ${a.is_live ? "bad" : "info"}` }, a.is_live ? "live" : "demo") },
      { label: "Broker", render: (a) => a.broker || "—" },
      { label: "Balance", num: true, render: (a) => a.balance !== undefined ? `${money(a.balance)} ${a.currency || ""}` : "—" },
      { label: "Leverage", num: true, render: (a) => a.leverage ? `1:${num(a.leverage, 0)}` : "—" },
      { label: "", render: (a) => a.error ? h("span", { class: "down small" }, a.error) : "" },
    ], accounts, { empty: "No accounts for this token. Did you tick the accounts when authorising?" })];
}

function startCard(datasets) {
  const replay = { dataset: (datasets.find((d) => !d.synthetic) || datasets[0] || {}).id, start: "", end: "", speed: 600 };
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h3", {}, "Run the bot")),
    h("div", { class: "stack", style: { gap: "12px" } },
      h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => start("paper") }, "Start paper trading"),
        h("button", { class: "btn danger", onclick: () => start("live") }, "Start live trading"),
        h("button", { class: "btn ghost", onclick: () => guard(() => api("/api/bot/stop", { method: "POST" }), "Stopped") }, "Stop")),
      h("p", { class: "small muted" }, "Paper: real cTrader prices, simulated fills, nothing sent to the account (forward testing). Live: real orders on the chosen account. Live also needs mode = live in Strategy → All parameters."),
      h("label", { class: "inline" }, sw(settings.autostart, async (on) => {
        settings.autostart = on;
        await guard(() => api("/api/settings", { method: "PUT", body: settings }), on ? "Autostart on" : "Autostart off");
      }), "Autostart with the terminal (reconnects after VPS reboots)"),
      h("details", { class: "rule" }, h("summary", {}, h("span", { class: "small" }, "Replay stored data instead (no account needed)")),
        h("div", { class: "body stack", style: { gap: "8px" } },
          datasets.length ? h("div", { class: "form-grid" },
            field("Dataset", h("select", { onchange: (e) => { replay.dataset = e.target.value; } }, datasets.map((d) => h("option", { value: d.id, selected: d.id === replay.dataset }, d.id)))),
            field("From", h("input", { type: "date", onchange: (e) => { replay.start = e.target.value; } })),
            field("To", h("input", { type: "date", onchange: (e) => { replay.end = e.target.value; } })),
            field("Speed (× real time)", h("input", { type: "number", value: replay.speed, onchange: (e) => { replay.speed = Number(e.target.value); } }))) : h("p", { class: "small muted" }, "No datasets yet: get some in Data & news."),
          h("div", {}, h("button", { class: "btn", disabled: !datasets.length, onclick: async () => {
            const r = await guard(() => api("/api/bot/start", { method: "POST", body: { kind: "replay", ...replay, start: replay.start || undefined, end: replay.end || undefined } }), "Replay started");
            if (r) location.hash = "dashboard";
          } }, "Start replay"))))));
}

async function start(mode) {
  if (!chosen) { toast("Choose an account first (load accounts)", "bad"); return; }
  const acc = (accounts || []).find((a) => a.account_id === chosen);
  const isLive = acc ? !!acc.is_live : false;
  if (mode === "live") {
    const input = h("input", { type: "text", placeholder: "type LIVE" });
    const ok = await modal("Start live trading?", h("div", { class: "stack", style: { gap: "8px" } },
      h("p", { class: "muted" }, `The bot will send real orders to account ${acc ? acc.login : chosen}${isLive ? " (a LIVE money account)" : ""}, risking ${settings.risk.risk_per_trade_pct}% per trade.`),
      h("p", { class: "small dim" }, "This is research software, not financial advice. Forward-test on demo first, as the validation plan says."),
      input), [{ label: "Start live", kind: "danger" }]);
    if (!ok || input.value.trim().toUpperCase() !== "LIVE") { if (ok) toast("Type LIVE to confirm", "bad"); return; }
  }
  const r = await guard(() => api("/api/bot/start", { method: "POST", body: { kind: mode, account_id: chosen, is_live: isLive } }), `Starting ${mode} trading…`);
  if (r) location.hash = "dashboard";
}

function drawRunner(snap) {
  const el = root && root.querySelector("#runner");
  if (!el) return;
  const r = snap && snap.runner;
  if (!snap || !snap.running || !r) { mount(el); return; }
  const a = snap.account || {};
  mount(el, h("div", { class: `banner ${r.status === "running" ? (r.mode === "live" ? "bad" : "ok") : r.status === "error" ? "bad" : "warn"}` },
    h("b", {}, `${r.mode.toUpperCase()} · ${r.status}`),
    h("span", {}, [r.detail, a.broker ? `${a.broker} ${a.login || ""} (${a.currency || ""})` : "", a.symbol ? `${a.symbol.name}: ${a.symbol.contract_size} oz/lot, min ${a.symbol.min_units} oz, commission ≈ $${num(a.symbol.commission_per_oz_rt, 3)}/oz round turn` : ""].filter(Boolean).join(" · ")),
    h("a", { href: "#dashboard", style: { marginLeft: "auto" } }, "Dashboard →")));
}

function vpsCard() {
  return card("Running on a VPS", h("div", { class: "small muted stack", style: { gap: "6px" } },
    h("p", {}, "The New York window is around midnight Sydney time, so the bot should live on an always-on VPS (Windows or Linux)."),
    h("p", {}, "The terminal only listens on 127.0.0.1. From your own computer open it through an SSH tunnel: ",
      h("code", {}, "ssh -L 8050:127.0.0.1:8050 you@your-vps"), " then browse to http://127.0.0.1:8050. To expose it directly, set GSE_TERMINAL_PASSWORD first."),
    h("p", {}, "For OAuth on a VPS either tunnel as above (the redirect URI stays http://localhost:8050/oauth/callback) or paste a token from the app's Playground.")));
}
