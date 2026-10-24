// Made-up sample games shaped exactly like The Odds API's responses, so the app
// can be explored without an API key. Player names are fictional.

const TEAMS = [
  'Atlanta Hawks', 'Boston Celtics', 'Brooklyn Nets', 'Charlotte Hornets', 'Chicago Bulls',
  'Cleveland Cavaliers', 'Dallas Mavericks', 'Denver Nuggets', 'Detroit Pistons',
  'Golden State Warriors', 'Houston Rockets', 'Indiana Pacers', 'Los Angeles Clippers',
  'Los Angeles Lakers', 'Memphis Grizzlies', 'Miami Heat', 'Milwaukee Bucks',
  'Minnesota Timberwolves', 'New Orleans Pelicans', 'New York Knicks', 'Oklahoma City Thunder',
  'Orlando Magic', 'Philadelphia 76ers', 'Phoenix Suns', 'Portland Trail Blazers',
  'Sacramento Kings', 'San Antonio Spurs', 'Toronto Raptors', 'Utah Jazz', 'Washington Wizards',
];
const FIRST = ['Jalen', 'Marcus', 'Tyrese', 'Darius', 'Cam', 'Isaiah', 'Malik', 'Andre', 'Devin', 'Jordan', 'Keon', 'Trey', 'Miles', 'Evan', 'Zion', 'Caleb'];
const LAST = ['Carter', 'Brooks', 'Hayes', 'Mitchell', 'Reed', 'Coleman', 'Ward', 'Bennett', 'Foster', 'Price', 'Sanders', 'Hughes', 'Perry', 'Long', 'Fisher', 'Bryant'];

// Season averages for a typical starting five: [points, rebounds, assists, threes]
const ROLES = [[27, 6, 7, 3], [21, 5, 4, 2.5], [16, 9, 2, 1], [12, 4, 3, 1.5], [10, 8, 2, 0.5]];

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function phi(x) {
  // Standard normal CDF (Abramowitz & Stegun approximation)
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.3989423 * Math.exp((-x * x) / 2);
  const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))));
  return x > 0 ? 1 - p : p;
}

// Bookie price for a true probability p, with their margin baked in.
const price = (p, margin = 1.055) => Math.max(1.01, Math.round((100 / (Math.min(0.97, Math.max(0.03, p)) * margin))) / 100);
const half = (x) => Math.floor(x) + 0.5;

function dayKey(now) {
  return Number(now.toISOString().slice(0, 10).replace(/-/g, ''));
}

export function demoGames(now = new Date()) {
  const r = rng(dayKey(now));
  const pool = [...TEAMS].sort(() => r() - 0.5);
  const games = [];
  for (let i = 0; i < 8; i++) {
    const start = new Date(now);
    start.setUTCDate(start.getUTCDate() + 1 + Math.floor(i / 3));
    start.setUTCHours(23 + (i % 3), 0, 0, 0);
    games.push({
      id: `demo-${dayKey(now)}-${i}`,
      sport_key: 'basketball_nba',
      commence_time: start.toISOString(),
      home_team: pool[i * 2],
      away_team: pool[i * 2 + 1],
    });
  }
  return games;
}

function roster(team, r) {
  return ROLES.map(([pts, reb, ast, threes]) => {
    const f = 0.85 + r() * 0.3;
    return {
      name: `${FIRST[Math.floor(r() * FIRST.length)]} ${LAST[Math.floor(r() * LAST.length)]}`,
      team,
      pts: pts * f, reb: reb * f, ast: ast * f, threes: threes * f,
    };
  });
}

function overUnder(desc, line, mean, sd) {
  const pOver = 1 - phi((line - mean) / sd);
  return [
    { name: 'Over', description: desc, point: line, price: price(pOver) },
    { name: 'Under', description: desc, point: line, price: price(1 - pOver) },
  ];
}

export function demoEvent(game, { full = true } = {}) {
  const r = rng(Number(game.id.replace(/\D/g, '')) || 1);
  const { home_team: home, away_team: away } = game;

  const mu = (r() - 0.5) * 16 + 2.5; // expected home winning margin
  const sd = 12;
  const total = 220 + r() * 16;
  const tsd = 18;
  const pHome = phi(mu / sd);
  const line = -half(mu - 0.5); // home spread line
  const spread = (L) => {
    const p = phi((mu + L) / sd);
    return [
      { name: home, point: L, price: price(p) },
      { name: away, point: -L, price: price(1 - p) },
    ];
  };
  const totals = (t) => {
    const p = 1 - phi((t - total) / tsd);
    return [
      { name: 'Over', point: t, price: price(p) },
      { name: 'Under', point: t, price: price(1 - p) },
    ];
  };

  const markets = [
    { key: 'h2h', outcomes: [{ name: home, price: price(pHome, 1.045) }, { name: away, price: price(1 - pHome, 1.045) }] },
    { key: 'spreads', outcomes: spread(line) },
    { key: 'totals', outcomes: totals(half(total)) },
  ];

  if (full) {
    markets.push({ key: 'alternate_spreads', outcomes: [-12, -8, -4, 4, 8, 12].flatMap((d) => spread(line + d)) });
    markets.push({ key: 'alternate_totals', outcomes: [-20, -12, -6, 6, 12, 20].flatMap((d) => totals(half(total) + d)) });

    const players = [...roster(home, r), ...roster(away, r)];
    const stat = (key, field, sdScale) => ({
      key,
      outcomes: players.flatMap((p) => {
        const mean = p[field];
        return overUnder(p.name, Math.max(0.5, half(mean - 0.5)), mean, Math.max(0.9, Math.sqrt(mean) * sdScale));
      }),
    });
    markets.push(stat('player_points', 'pts', 1.35));
    markets.push(stat('player_rebounds', 'reb', 1.1));
    markets.push(stat('player_assists', 'ast', 1.1));
    markets.push(stat('player_threes', 'threes', 0.95));
    markets.push({
      key: 'player_points_rebounds_assists',
      outcomes: players.flatMap((p) => {
        const mean = p.pts + p.reb + p.ast;
        return overUnder(p.name, half(mean - 0.5), mean, Math.sqrt(mean) * 1.4);
      }),
    });
  }

  const now = new Date().toISOString();
  return {
    ...game,
    bookmakers: [{
      key: 'sportsbet',
      title: 'Sportsbet (demo prices)',
      last_update: now,
      markets: markets.map((m) => ({ ...m, last_update: now })),
    }],
  };
}
