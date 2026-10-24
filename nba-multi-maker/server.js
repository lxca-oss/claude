// Tiny zero-dependency server: serves the web app and proxies The Odds API so the
// API key stays on your machine. Run with `npm start` (or `npm run demo`).

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { demoGames, demoEvent } from './lib/demo.js';

const ROOT = path.dirname(fileURLToPath(import.meta.url));

function loadEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
}
loadEnv(path.join(ROOT, '.env'));

const API = 'https://api.the-odds-api.com/v4';
const SPORT = 'basketball_nba';
const KEY = process.env.ODDS_API_KEY ?? '';
const DEMO = process.env.DEMO === '1' || !KEY;
const BOOKMAKER = process.env.BOOKMAKER || 'sportsbet';
const BASIC_MARKETS = 'h2h,spreads,totals';
const EVENT_MARKETS = process.env.EVENT_MARKETS ||
  'h2h,spreads,totals,alternate_spreads,alternate_totals,player_points,player_rebounds,player_assists,player_threes,player_points_rebounds_assists';
const CACHE_MS = Number(process.env.CACHE_MINUTES || 10) * 60_000;
const PORT = Number(process.env.PORT || 3000);
const CACHE_DIR = path.join(ROOT, '.cache');

let quota = { remaining: null, used: null };

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// Cache responses on disk so restarting the server doesn't spend credits again.
function cachePath(name) {
  return path.join(CACHE_DIR, `${name.replace(/[^a-z0-9_-]/gi, '_')}.json`);
}
function readCache(name, force) {
  if (force) return null;
  try {
    const entry = JSON.parse(fs.readFileSync(cachePath(name), 'utf8'));
    return Date.now() - entry.fetchedAt < CACHE_MS ? entry : null;
  } catch {
    return null;
  }
}
function writeCache(name, data) {
  const entry = { fetchedAt: Date.now(), data };
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  fs.writeFileSync(cachePath(name), JSON.stringify(entry));
  return entry;
}

async function oddsApi(pathname, params = {}) {
  const url = new URL(API + pathname);
  url.search = new URLSearchParams({ apiKey: KEY, ...params });
  const res = await fetch(url);
  const remaining = res.headers.get('x-requests-remaining');
  if (remaining != null) quota = { remaining: Number(remaining), used: Number(res.headers.get('x-requests-used')) };
  if (!res.ok) {
    let message = `The Odds API returned ${res.status}`;
    try {
      message = (await res.json()).message || message;
    } catch {}
    if (res.status === 401) message = `API key rejected: ${message}. Check ODDS_API_KEY in .env.`;
    if (res.status === 429) message = `Out of API credits or rate limited: ${message}`;
    throw new ApiError(res.status, message);
  }
  return res.json();
}

async function getGames(force) {
  if (DEMO) return { fetchedAt: Date.now(), data: demoGames() };
  const cached = readCache('games', force);
  if (cached) return cached;
  // The events list is free on The Odds API (doesn't use credits).
  const events = await oddsApi(`/sports/${SPORT}/events`, { dateFormat: 'iso' });
  return writeCache('games', events);
}

async function getEvent(id, force) {
  if (DEMO) {
    const game = demoGames().find((g) => g.id === id);
    if (!game) throw new ApiError(404, 'Game not found');
    return { fetchedAt: Date.now(), data: demoEvent(game) };
  }
  const cached = readCache(`event-${id}`, force);
  if (cached) return cached;
  const params = { bookmakers: BOOKMAKER, oddsFormat: 'decimal', dateFormat: 'iso' };
  let event;
  try {
    event = await oddsApi(`/sports/${SPORT}/events/${encodeURIComponent(id)}/odds`, { ...params, markets: EVENT_MARKETS });
  } catch (err) {
    // 422 = a market key isn't supported; fall back to the basic game markets.
    if (err.status !== 422) throw err;
    event = await oddsApi(`/sports/${SPORT}/events/${encodeURIComponent(id)}/odds`, { ...params, markets: BASIC_MARKETS });
    event.warning = `Some markets weren't accepted (${err.message}). Showing game lines only.`;
  }
  return writeCache(`event-${id}`, event);
}

function sendJson(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };

function serveStatic(res, pathname) {
  const [dir, rel] = pathname.startsWith('/lib/') ? ['lib', pathname.slice(5)] : ['public', pathname.slice(1) || 'index.html'];
  const base = path.join(ROOT, dir);
  const file = path.normalize(path.join(base, rel));
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end('Not found');
    return;
  }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] ?? 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const force = url.searchParams.get('refresh') === '1';
  const meta = () => ({ demo: DEMO, bookmaker: BOOKMAKER, quota });
  try {
    if (url.pathname === '/api/games') {
      const { data, fetchedAt } = await getGames(force);
      const now = Date.now();
      const games = data
        .filter((g) => new Date(g.commence_time).getTime() > now)
        .sort((a, b) => a.commence_time.localeCompare(b.commence_time));
      return sendJson(res, 200, { ...meta(), fetchedAt, games });
    }
    const match = url.pathname.match(/^\/api\/games\/([^/]+)$/);
    if (match) {
      const { data, fetchedAt } = await getEvent(decodeURIComponent(match[1]), force);
      return sendJson(res, 200, { ...meta(), fetchedAt, event: data });
    }
    if (url.pathname.startsWith('/api/')) return sendJson(res, 404, { error: 'Unknown endpoint' });
    serveStatic(res, url.pathname);
  } catch (err) {
    console.error(err.message);
    sendJson(res, err.status && err.status < 600 ? err.status : 500, { ...meta(), error: err.message });
  }
});

server.listen(PORT, () => {
  console.log(`NBA Multi Maker running at http://localhost:${PORT}`);
  console.log(DEMO
    ? 'DEMO MODE: using made-up games. Add ODDS_API_KEY to .env for real Sportsbet odds.'
    : `Live mode: ${BOOKMAKER} odds via The Odds API (cached ${CACHE_MS / 60000} min).`);
});
