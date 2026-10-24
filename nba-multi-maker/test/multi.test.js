import test from 'node:test';
import assert from 'node:assert/strict';
import { extractLegs, buildMultis, TIERS } from '../lib/multi.js';
import { demoGames, demoEvent } from '../lib/demo.js';

const games = demoGames(new Date('2026-10-24T00:00:00Z'));

test('margin is removed so both sides of a market sum to 100%', () => {
  const legs = extractLegs(demoEvent(games[0]));
  const h2h = legs.filter((l) => l.market === 'h2h');
  assert.equal(h2h.length, 2);
  assert.ok(Math.abs(h2h[0].fair + h2h[1].fair - 1) < 1e-9);
  assert.ok(h2h[0].implied + h2h[1].implied > 1, 'bookie margin should exist');
});

for (const game of games) {
  test(`every tier has valid multis for ${game.home_team} v ${game.away_team}`, () => {
    const multis = buildMultis(extractLegs(demoEvent(game)));
    for (const tier of TIERS) {
      const options = multis[tier.id];
      assert.ok(options.length > 0, `${tier.id} has options`);
      for (const m of options) {
        assert.ok(m.odds >= tier.min && m.odds <= tier.max, `${tier.id} odds ${m.odds}`);
        assert.ok(m.legs.length >= tier.minLegs);
        const product = m.legs.reduce((p, l) => p * l.odds, 1);
        assert.ok(Math.abs(product - m.odds) < 1e-9);
        assert.ok(m.fairProb < m.impliedProb * 1.2);
      }
    }
  });
}

test('no multi combines conflicting legs', () => {
  for (const game of games) {
    const legs = extractLegs(demoEvent(game));
    const groupOf = new Map(legs.map((l) => [l.id, l.group]));
    const multis = buildMultis(legs);
    for (const m of Object.values(multis).flat()) {
      const groups = m.legs.map((l) => groupOf.get(l.id));
      assert.equal(new Set(groups).size, groups.length, `duplicate group in ${m.legs.map((l) => l.label)}`);
    }
  }
});

test('game-lines-only events still produce multis', () => {
  const legs = extractLegs(demoEvent(games[1], { full: false }));
  const multis = buildMultis(legs);
  assert.ok(Array.isArray(multis.low));
});

test('handles an event with no bookmakers', () => {
  const legs = extractLegs({ ...games[0], bookmakers: [] });
  assert.deepEqual(legs, []);
  const multis = buildMultis(legs);
  for (const t of TIERS) assert.deepEqual(multis[t.id], []);
});
