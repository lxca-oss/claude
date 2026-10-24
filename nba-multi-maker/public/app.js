import { extractLegs, buildMultis, TIERS } from '/lib/multi.js';

const TZ = 'Australia/Sydney';
const state = { games: [], selected: null, multis: null, index: {}, stake: 10, meta: {} };

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c != null));
  return node;
};
const money = (n) => n.toLocaleString('en-AU', { style: 'currency', currency: 'AUD' });
const pct = (p) => `${(p * 100).toFixed(p < 0.01 ? 2 : 1)}%`;
const oneIn = (p) => `1 in ${Math.round(1 / p).toLocaleString('en-AU')}`;

async function api(path, refresh) {
  const res = await fetch(path + (refresh ? '?refresh=1' : ''));
  const body = await res.json();
  state.meta = body;
  renderStatus();
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}

function renderStatus() {
  const { demo, quota, bookmaker } = state.meta;
  $('#demo-badge').hidden = !demo;
  if (bookmaker) $('#bookmaker').textContent = bookmaker === 'sportsbet' ? 'Sportsbet' : bookmaker;
  const q = $('#quota');
  if (quota?.remaining != null) {
    q.hidden = false;
    q.textContent = `${quota.remaining} API credits left`;
  }
}

function dayLabel(date) {
  return date.toLocaleDateString('en-AU', { timeZone: TZ, weekday: 'long', day: 'numeric', month: 'short' });
}
function timeLabel(date) {
  return date.toLocaleTimeString('en-AU', { timeZone: TZ, hour: 'numeric', minute: '2-digit' });
}

function renderGames() {
  const list = $('#games-list');
  list.replaceChildren();
  if (!state.games.length) {
    list.append(el('p', { className: 'muted', textContent: 'No upcoming NBA games found.' }));
    return;
  }
  let lastDay = '';
  for (const g of state.games) {
    const start = new Date(g.commence_time);
    const day = dayLabel(start);
    if (day !== lastDay) {
      list.append(el('div', { className: 'day', textContent: day }));
      lastDay = day;
    }
    const btn = el('button', { className: 'game' + (state.selected?.id === g.id ? ' active' : '') },
      el('div', { className: 'teams' }, g.away_team, el('span', { className: 'at', textContent: ' @ ' }), g.home_team),
      el('div', { className: 'time', textContent: `${timeLabel(start)} (Sydney time)` }),
    );
    btn.addEventListener('click', () => selectGame(g));
    list.append(btn);
  }
}

async function loadGames(refresh = false) {
  try {
    const { games } = await api('/api/games', refresh);
    state.games = games;
    renderGames();
  } catch (err) {
    $('#games-list').replaceChildren(el('p', { className: 'notice error', textContent: err.message }));
  }
}

async function selectGame(game, refresh = false) {
  state.selected = game;
  renderGames();
  const detail = $('#detail');
  detail.replaceChildren(el('p', { className: 'muted', textContent: `Loading odds for ${game.away_team} @ ${game.home_team}…` }));
  try {
    const { event } = await api(`/api/games/${encodeURIComponent(game.id)}`, refresh);
    if (state.selected?.id !== game.id) return;
    const legs = extractLegs(event, state.meta.bookmaker);
    state.event = event;
    state.legs = legs;
    state.multis = buildMultis(legs);
    state.index = { low: 0, medium: 0, high: 0 };
    renderDetail();
  } catch (err) {
    detail.replaceChildren(el('p', { className: 'notice error', textContent: err.message }));
  }
}

function renderDetail() {
  const { event, legs } = state;
  const start = new Date(event.commence_time);
  const book = event.bookmakers?.[0];
  const stakeInput = el('input', { type: 'number', min: '1', step: '1', value: state.stake });
  stakeInput.addEventListener('input', () => {
    state.stake = Math.max(0, Number(stakeInput.value) || 0);
    renderCards();
  });

  const notices = [];
  if (!legs.length) {
    notices.push(el('div', { className: 'notice', textContent: `${state.meta.bookmaker} hasn't posted odds for this game yet. Check back closer to tip-off.` }));
  } else if (!legs.some((l) => l.market.startsWith('player_'))) {
    notices.push(el('div', { className: 'notice', textContent: 'No player props are available for this game yet (they usually appear on game day), so these multis use game lines only.' }));
  }
  if (event.warning) notices.push(el('div', { className: 'notice', textContent: event.warning }));

  $('#detail').replaceChildren(
    el('div', { className: 'game-head' },
      el('div', {},
        el('h2', { textContent: `${event.away_team} @ ${event.home_team}` }),
        el('p', { className: 'muted', textContent: `${dayLabel(start)}, ${timeLabel(start)} · ${legs.length} markets from ${book?.title ?? 'bookmaker'}${book?.last_update ? ` · odds updated ${timeLabel(new Date(book.last_update))}` : ''}` }),
      ),
      el('label', { className: 'stake' }, 'Stake $', stakeInput),
    ),
    ...notices,
    el('div', { className: 'cards', id: 'cards' }),
    learnPanel(),
  );
  renderCards();
}

function renderCards() {
  const wrap = $('#cards');
  if (!wrap) return;
  wrap.replaceChildren(...TIERS.map(tierCard));
}

function tierCard(tier) {
  const node = $('#tier-card').content.firstElementChild.cloneNode(true);
  node.classList.add(tier.id);
  node.querySelector('.tier-name').textContent = tier.label;
  node.querySelector('.tier-range').textContent = tier.blurb;

  const options = state.multis?.[tier.id] ?? [];
  if (!options.length) {
    node.querySelector('.odds').textContent = '—';
    node.querySelector('.legs').replaceWith(el('p', { className: 'none', textContent: 'Not enough markets to build a multi in this range yet.' }));
    node.querySelector('.stats').remove();
    node.querySelector('.actions').remove();
    return node;
  }

  const i = state.index[tier.id] % options.length;
  const m = options[i];
  node.querySelector('.odds').replaceChildren(m.odds.toFixed(2), el('small', { textContent: 'x' }));
  node.querySelector('.legs').replaceChildren(...m.legs.map((l) =>
    el('li', {},
      el('span', {}, l.label, el('span', { className: 'chance', textContent: `≈ ${pct(l.fair)} chance${l.estimated ? ' (est.)' : ''}` })),
      el('span', { className: 'price', textContent: l.odds.toFixed(2) }),
    )));

  const stake = state.stake;
  const evDollars = m.ev * stake;
  node.querySelector('.stake-echo').textContent = stake;
  node.querySelector('.payout').textContent = money(stake * m.odds);
  node.querySelector('.implied').replaceChildren(pct(m.impliedProb), el('small', { textContent: oneIn(m.impliedProb) }));
  node.querySelector('.fair').replaceChildren(pct(m.fairProb), el('small', { textContent: oneIn(m.fairProb) }));
  const ev = node.querySelector('.ev');
  ev.replaceChildren(`${evDollars < 0 ? '−' : '+'}${money(Math.abs(evDollars))}`, el('small', { textContent: `${(m.ev * 100).toFixed(1)}% of stake` }));
  ev.classList.toggle('negative', evDollars < 0);

  const another = node.querySelector('.another');
  another.textContent = options.length > 1 ? `Show another (${i + 1}/${options.length})` : 'Only option';
  another.disabled = options.length < 2;
  another.addEventListener('click', () => {
    state.index[tier.id] = (i + 1) % options.length;
    renderCards();
  });

  const copy = node.querySelector('.copy');
  copy.addEventListener('click', async () => {
    const text = [
      `${state.event.away_team} @ ${state.event.home_team} — ${tier.label} multi @ ${m.odds.toFixed(2)}`,
      ...m.legs.map((l) => `• ${l.label} (${l.odds.toFixed(2)})`),
    ].join('\n');
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = 'Copied ✓';
    } catch {
      copy.textContent = 'Copy failed';
    }
    setTimeout(() => (copy.textContent = 'Copy legs'), 1500);
  });
  return node;
}

function learnPanel() {
  const html = `
    <summary>📘 What do these numbers mean?</summary>
    <p><strong>Odds → probability.</strong> Decimal odds of <code>2.50</code> mean the bookie thinks the chance is
    <code>1 ÷ 2.50 = 40%</code>. That's the <em>implied probability</em>.</p>
    <p><strong>The margin (overround).</strong> Add up the implied chances of both sides of a market and you get
    more than 100% (e.g. <code>1.91</code> / <code>1.91</code> → 52.4% + 52.4% = 104.8%). The extra 4.8% is how the
    bookie gets paid. Scaling the two sides back to 100% gives the <em>estimated true chance</em> shown on each leg.</p>
    <p><strong>Multis multiply.</strong> The multi's odds are every leg's odds multiplied together. Its chance of
    winning is every leg's true chance multiplied together. Because the margin is multiplied in too, the more legs you
    add, the bigger the bookie's edge gets. That's why the High multi usually has the worst expected result.</p>
    <p><strong>Expected result.</strong> <code>(true chance × payout) − stake</code>. It's what one bet would average out
    to over thousands of repeats. A negative number means you'd lose that much per bet on average.</p>
    <p><strong>Same-game catch.</strong> Legs from the same game are linked (if a team wins big, its star probably scored
    a lot). Sportsbet adjusts its same-game multi price for this, so the price in the app is an estimate and the real
    one in the Sportsbet app will usually be a bit lower.</p>
    <p><strong>How the app picks legs.</strong> For each payout range it searches thousands of combinations. It
    never pairs two legs from the same market (like two different winning margins), and it ranks the options by
    the smallest built-in bookie edge.</p>`;
  const d = el('details', { className: 'learn' });
  d.innerHTML = html;
  return d;
}

$('#refresh').addEventListener('click', () => {
  loadGames(true);
  if (state.selected) selectGame(state.selected, true);
});

loadGames();
