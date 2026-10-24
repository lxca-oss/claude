# 🏀 NBA Multi Maker

An educational app for learning probability and how betting markets work. It pulls
**Sportsbet** odds for upcoming NBA games from [The Odds API](https://the-odds-api.com)
and builds three same-game multis for any game you click:

| Tier | Target odds |
|------|-------------|
| 🟢 Low | around 10x (8x–14x) |
| 🟡 Medium | 20x–40x |
| 🔴 High | 40x–70x |

Each multi shows the payout, the bookie's implied chance, the estimated *true* chance
(with the bookie's margin removed), and the expected result per bet. It does not place bets.

## Run it

You need [Node.js](https://nodejs.org) 18 or newer. There's nothing to `npm install`.

```bash
cd nba-multi-maker
cp .env.example .env      # Windows: copy .env.example .env
# open .env and paste your key after ODDS_API_KEY=
npm start
```

Then open **http://localhost:3000**.

There's no API key yet? Run `npm run demo` to try it with made-up games and players.

## API credits

The free Odds API plan gives you 500 credits a month.

- **Game list** uses the `/events` endpoint, which costs no credits.
- **Clicking a game** costs about 1 credit per market in `EVENT_MARKETS`. That's about 10 with the defaults.
- Odds are cached for `CACHE_MINUTES` (default 10), even across restarts, so clicking the same game again is free.
- **↻ Refresh** skips the cache, so it costs credits.

The credits you have left show in the top-right corner. To make credits last, trim
`EVENT_MARKETS` in `.env`, for example to `h2h,spreads,totals,player_points`.

Player props usually only show up on game day. Before then, the multis use game lines only.

## How it picks the legs

1. Every Sportsbet price becomes a possible leg. Its implied chance is `1 ÷ odds`.
2. The two sides of each market (Over/Under, home/away) are scaled so they add to 100%. That removes the bookie's margin and gives an estimated true chance.
3. A beam search tries thousands of combinations for each tier. It never pairs legs from the same market (e.g. two winning margins or two totals), and it allows at most 2 legs per player.
4. Combos in the tier's odds range are ranked by expected value, meaning the smallest built-in bookie edge. You get up to 5 different options per tier ("Show another").

**Same-game multi caveat:** legs from one game are correlated. Sportsbet prices its
SGMs with this in mind, so the real price in the Sportsbet app will usually be a bit lower
than the odds multiplied together here.

## Files

```
server.js        local server + Odds API proxy (keeps your key private, caches responses)
lib/multi.js     the multi-building maths (shared by browser and tests)
lib/demo.js      sample data for demo mode
public/          the web interface
test/            run with `npm test`
```

---
Gambling is 18+. Free, confidential support: Gambling Help 1800 858 858 · gamblinghelponline.org.au
