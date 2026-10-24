// Same-game multi builder. Runs in both the browser and Node (no dependencies).
//
// The idea: every price a bookie offers implies a probability (1 / odds), but the
// implied probabilities of both sides of a market add up to more than 100% - that
// extra is the bookie's margin. Removing it gives a "fair" probability. A multi's
// payout is the product of its legs' odds, and (if legs were independent) its
// chance of winning is the product of their fair probabilities.

export const TIERS = [
  { id: 'low', label: 'Low', blurb: 'around 10x', min: 8, max: 14, minLegs: 2, maxLegs: 5 },
  { id: 'medium', label: 'Medium', blurb: '20x to 40x', min: 20, max: 40, minLegs: 3, maxLegs: 6 },
  { id: 'high', label: 'High', blurb: '40x to 70x', min: 40, max: 70, minLegs: 3, maxLegs: 8 },
];

const LEG_MIN_ODDS = 1.2;
const LEG_MAX_ODDS = 5;
const MAX_CANDIDATES = 70;
const MAX_LEGS_PER_PLAYER = 2;
const DEFAULT_OVERROUND = 1.06;

const STAT_NAMES = {
  player_points: 'points',
  player_rebounds: 'rebounds',
  player_assists: 'assists',
  player_threes: 'threes made',
  player_blocks: 'blocks',
  player_steals: 'steals',
  player_turnovers: 'turnovers',
  player_points_rebounds_assists: 'pts + reb + ast',
  player_points_rebounds: 'pts + reb',
  player_points_assists: 'pts + ast',
  player_rebounds_assists: 'reb + ast',
  player_double_double: 'double-double',
  player_triple_double: 'triple-double',
  player_first_basket: 'first basket',
};

const baseMarket = (key) => key.replace(/_alternate$/, '').replace(/^alternate_/, '');
const isPlayerMarket = (key) => key.startsWith('player_');
const signed = (n) => (n > 0 ? `+${n}` : `${n}`);

function legLabel(market, o, event) {
  const base = baseMarket(market);
  if (base === 'h2h') return `${o.name} to win`;
  if (base === 'spreads') return `${o.name} ${signed(o.point)} line`;
  if (base === 'totals') return `${o.name} ${o.point} total match points`;
  if (base === 'team_totals') return `${o.description} ${o.name} ${o.point} team points`;
  if (isPlayerMarket(base)) {
    const stat = STAT_NAMES[base] ?? base.replace(/^player_/, '').replace(/_/g, ' ');
    if (o.name === 'Over' || o.name === 'Under') return `${o.description} ${o.name} ${o.point} ${stat}`;
    if (o.name === 'Yes') return `${o.description} to record a ${stat}`;
    if (o.name === 'No') return `${o.description} NOT to record a ${stat}`;
    return `${o.description} ${o.name} ${stat}`;
  }
  return `${o.name}${o.point != null ? ` ${o.point}` : ''} (${market})`;
}

// Key that both sides of the same two-way market share, used to remove the margin.
function pairKey(market, o, event) {
  const base = baseMarket(market);
  if (base === 'h2h') return market;
  if (base === 'spreads') return `${market}|${o.name === event.home_team ? o.point : -o.point}`;
  if (base === 'totals') return `${market}|${o.point}`;
  return `${market}|${o.description ?? ''}|${o.point ?? ''}`;
}

// Legs in the same group can't be combined (e.g. two different winning margins).
function conflictGroup(market, o) {
  const base = baseMarket(market);
  if (base === 'h2h' || base === 'spreads') return 'result';
  if (base === 'totals') return 'total';
  if (base === 'team_totals') return `tt|${o.description}`;
  return `${base}|${o.description ?? ''}`;
}

export function pickBookmaker(event, bookmakerKey) {
  const books = event.bookmakers ?? [];
  return books.find((b) => b.key === bookmakerKey) ?? books[0] ?? null;
}

export function extractLegs(event, bookmakerKey = 'sportsbet') {
  const book = pickBookmaker(event, bookmakerKey);
  if (!book) return [];

  const legs = [];
  for (const m of book.markets ?? []) {
    for (const o of m.outcomes ?? []) {
      if (!(o.price > 1)) continue;
      legs.push({
        id: `${m.key}|${o.name}|${o.description ?? ''}|${o.point ?? ''}`,
        market: m.key,
        label: legLabel(m.key, o, event),
        odds: o.price,
        implied: 1 / o.price,
        player: isPlayerMarket(m.key) ? o.description : null,
        group: conflictGroup(m.key, o),
        pair: pairKey(m.key, o, event),
      });
    }
  }

  // Remove the margin from each two-way market, then use the average margin
  // for one-sided markets (like milestones) where we can't see the other side.
  const pairs = new Map();
  for (const leg of legs) {
    if (!pairs.has(leg.pair)) pairs.set(leg.pair, []);
    pairs.get(leg.pair).push(leg);
  }
  const overrounds = [];
  for (const group of pairs.values()) {
    if (group.length < 2) continue;
    const total = group.reduce((s, l) => s + l.implied, 0);
    overrounds.push(total);
    for (const l of group) {
      l.fair = l.implied / total;
      l.margin = total - 1;
    }
  }
  const avgOverround = overrounds.length
    ? overrounds.reduce((a, b) => a + b, 0) / overrounds.length
    : DEFAULT_OVERROUND;
  for (const l of legs) {
    if (l.fair == null) {
      l.fair = Math.min(0.99, l.implied / avgOverround);
      l.margin = avgOverround - 1;
      l.estimated = true;
    }
    l.ev = l.fair * l.odds; // 1.00 would be a perfectly fair price
  }
  return legs;
}

function candidates(legs) {
  return legs
    .filter((l) => l.odds >= LEG_MIN_ODDS && l.odds <= LEG_MAX_ODDS)
    .sort((a, b) => b.ev - a.ev)
    .slice(0, MAX_CANDIDATES);
}

function conflicts(combo, leg) {
  if (combo.groups.has(leg.group)) return true;
  if (leg.player && (combo.players.get(leg.player) ?? 0) >= MAX_LEGS_PER_PLAYER) return true;
  return false;
}

function extend(combo, leg, index) {
  const players = new Map(combo.players);
  if (leg.player) players.set(leg.player, (players.get(leg.player) ?? 0) + 1);
  return {
    legs: [...combo.legs, leg],
    lastIndex: index,
    odds: combo.odds * leg.odds,
    fairProb: combo.fairProb * leg.fair,
    logEV: combo.logEV + Math.log(leg.ev),
    groups: new Set(combo.groups).add(leg.group),
    players,
  };
}

// Beam search: grow combos one leg at a time, keeping the best partial combos in
// each odds "bucket" so we reach every payout range rather than only cheap ones.
function search(legs, tier, { beamPerBucket = 40 } = {}) {
  const found = new Map();
  let beam = [{ legs: [], lastIndex: -1, odds: 1, fairProb: 1, logEV: 0, groups: new Set(), players: new Map() }];

  for (let depth = 1; depth <= tier.maxLegs && beam.length; depth++) {
    const buckets = new Map();
    for (const combo of beam) {
      for (let i = combo.lastIndex + 1; i < legs.length; i++) {
        const leg = legs[i];
        if (combo.odds * leg.odds > tier.max) continue;
        if (conflicts(combo, leg)) continue;
        const next = extend(combo, leg, i);
        if (depth >= tier.minLegs && next.odds >= tier.min) {
          found.set(next.legs.map((l) => l.id).join('#'), next);
        }
        const b = Math.floor(Math.log(next.odds) / 0.25);
        if (!buckets.has(b)) buckets.set(b, []);
        buckets.get(b).push(next);
      }
    }
    beam = [];
    for (const list of buckets.values()) {
      list.sort((a, b) => b.logEV - a.logEV);
      beam.push(...list.slice(0, beamPerBucket));
    }
  }
  return [...found.values()];
}

function overlap(a, b) {
  const ids = new Set(a.legs.map((l) => l.id));
  const shared = b.legs.filter((l) => ids.has(l.id)).length;
  return shared / Math.min(a.legs.length, b.legs.length);
}

function summarise(combo, tier) {
  const fairProb = combo.fairProb;
  return {
    tier: tier.id,
    legs: combo.legs.map(({ id, label, odds, fair, implied, market, estimated }) => ({
      id, label, odds, fair, implied, market, estimated,
    })),
    odds: combo.odds,
    impliedProb: 1 / combo.odds,
    fairProb,
    // Expected return per $1 staked, if legs were independent. Below 0 = you lose on average.
    ev: fairProb * combo.odds - 1,
  };
}

export function buildMultis(legs, { options = 5 } = {}) {
  const pool = candidates(legs);
  const result = {};
  const leads = []; // first option shown for each earlier tier
  for (const tier of TIERS) {
    let combos = search(pool, tier);
    if (!combos.length) {
      // Thin market (e.g. no player props yet): relax the limits and try again.
      combos = search(legs.filter((l) => l.odds > 1.05), { ...tier, maxLegs: tier.maxLegs + 2 });
    }
    combos.sort((a, b) => b.logEV - a.logEV);
    // Lead with a combo that looks different from the other tiers' leads, if one is close in value.
    const fresh = combos.slice(0, 40).find((c) => leads.every((p) => overlap(p, c) < 0.5));
    if (fresh) combos = [fresh, ...combos.filter((c) => c !== fresh)];
    if (combos.length) leads.push(combos[0]);
    const picked = [];
    for (const c of combos) {
      if (picked.every((p) => overlap(p, c) < 0.6)) picked.push(c);
      if (picked.length >= options) break;
    }
    result[tier.id] = picked.map((c) => summarise(c, tier));
  }
  return result;
}
