// Terminal shell: routing, live updates over WebSocket, top bar.
import { api, confirmBox, guard, h, mount, price, toast, TZ, fmtTime } from "./lib.js";
import * as dashboard from "./dashboard.js";
import * as strategy from "./strategy.js";
import * as trades from "./trades.js";
import * as backtest from "./backtest.js";
import * as data from "./data.js";
import * as connection from "./connection.js";

const pages = { dashboard, strategy, trades, backtest, data, connection };

export const store = {
  snap: null,
  events: [],
  listeners: new Set(),
  emit() { for (const fn of this.listeners) { try { fn(this.snap); } catch (e) { console.error(e); } } },
};

let current = null;
const main = document.getElementById("main");

function route() {
  const [name, query] = (location.hash.slice(1) || "dashboard").split("?");
  const page = pages[name] ? name : "dashboard";
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === page));
  if (current && current.destroy) current.destroy();
  current = pages[page];
  mount(main);
  main.scrollTop = 0;
  current.render(main, { store, params: new URLSearchParams(query || "") });
}

document.querySelectorAll("#nav button").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.tab; }));
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- top bar
const statusPill = document.getElementById("top-status");
const quoteEl = document.getElementById("top-quote");
const pauseBtn = document.getElementById("btn-pause");

function statusInfo(snap) {
  const r = snap && snap.runner;
  if (!snap || !snap.running || !r) return ["Stopped", "idle"];
  const mode = { live: "LIVE", paper: "Paper", replay: "Replay" }[r.mode] || r.mode;
  if (r.status === "running") {
    const paused = snap.engine && snap.engine.paused;
    return [paused ? `${mode} · paused` : `${mode} · running`, paused ? "warn" : r.mode === "live" ? "bad live" : "ok live"];
  }
  if (r.status === "error") return [`${mode} · error`, "bad"];
  return [`${mode} · ${r.status}`, "warn"];
}

function updateTop(snap) {
  const [label, kind] = statusInfo(snap);
  statusPill.className = `pill ${kind}`;
  statusPill.textContent = label;
  const e = snap && snap.engine;
  if (e && e.bid) {
    mount(quoteEl, h("span", { class: "px" }, price(e.bid)),
      h("span", { class: "sp" }, `ask ${price(e.ask)} · spr ${e.spread !== null ? e.spread.toFixed(2) : "—"}`));
  } else {
    mount(quoteEl, h("span", { class: "px dim" }, "—"), h("span", { class: "sp" }, "no price"));
  }
  pauseBtn.textContent = e && e.paused ? "Resume" : "Pause";
  pauseBtn.disabled = !(snap && snap.running);
  document.getElementById("btn-flatten").disabled = !(snap && snap.running);
}

function tickClocks() {
  const now = Date.now() / 1000;
  const zones = [["Local", TZ.local.zone, ""], ["UTC", "UTC", "opt"], ["LDN", "Europe/London", ""], ["NY", "America/New_York", ""], ["SYD", "Australia/Sydney", "opt"]];
  mount(document.getElementById("clocks"), zones.map(([l, z, c]) => h("span", { class: c }, h("b", {}, l), fmtTime(now, z))));
}
setInterval(tickClocks, 10_000);
tickClocks();

pauseBtn.addEventListener("click", () => {
  const paused = store.snap && store.snap.engine && store.snap.engine.paused;
  guard(() => api("/api/bot/pause", { method: "POST", body: { paused: !paused } }), paused ? "Entries resumed" : "New entries paused");
});
document.getElementById("btn-flatten").addEventListener("click", async () => {
  if (await confirmBox("Flatten everything?", "Close every position the bot opened and cancel its pending orders now. The bot keeps running.", "Flatten", "danger")) {
    guard(() => api("/api/bot/flatten", { method: "POST" }), "Flatten sent");
  }
});
document.getElementById("btn-theme").addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.dataset.theme === "light" ? "dark" : "light";
  root.dataset.theme = next;
  try { localStorage.setItem("gse-theme", next); } catch {}
  route();
});

// ---------------------------------------------------------------- live updates
let ws = null;
let backoff = 1000;
let pollTimer = null;

function onSnapshot(snap) {
  store.snap = snap;
  updateTop(snap);
  store.emit();
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { backoff = 1000; if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.kind === "snapshot") onSnapshot(msg.payload);
    else if (msg.kind === "event") {
      store.events.push(msg.payload);
      if (store.events.length > 300) store.events.shift();
      const ev = msg.payload;
      const replay = store.snap && store.snap.runner && store.snap.runner.mode === "replay";
      const important = ["risk", "error", "reject"].includes(ev.kind) || (!replay && ["fill", "close"].includes(ev.kind));
      if (important) toast(ev.message, ev.kind === "error" || ev.kind === "risk" || ev.kind === "reject" ? "bad" : ev.kind === "close" ? "" : "ok");
    } else if (msg.kind === "status") {
      if (msg.payload.status === "error") toast(`Bot error: ${msg.payload.detail}`, "bad");
    }
  };
  ws.onclose = () => {
    if (!pollTimer) pollTimer = setInterval(() => api("/api/status").then(onSnapshot).catch(() => {}), 2000);
    setTimeout(connectWs, backoff);
    backoff = Math.min(backoff * 2, 15000);
  };
}

api("/api/status").then((s) => {
  onSnapshot(s);
  document.getElementById("nav-foot").textContent = `v${s.version} · not financial advice`;
}).catch((e) => toast(e.message, "bad"));
connectWs();
route();
