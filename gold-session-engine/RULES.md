# Frozen rules

Validation plan step 1 is "freeze the rules". This file states exactly how the code reads each rule in the strategy document, especially where the document leaves room for interpretation. The setting that controls each rule is shown as `section.name` (Strategy tab → parameters, or `config/settings.yaml`).

Change a rule only by changing a setting and recording the variant *before* testing it (at most ~24 variants per module).

## Data and time

- The engine runs on closed **M1 bid bars**. Each bar carries its average spread; ask = bid + spread. M5 and M15 bars are built from M1 bars aligned to UTC.
- **Bar fills (fast mode):** inside a bar, price is assumed to go open → low → high → close for an up bar (close ≥ open) and open → high → low → close for a down bar. Entries, stops, targets, break-even moves and partials fire in the order price reaches them on that path. A bar that opens beyond a level fills at the open (gaps never fill at the better level).
- **Tick replay (`fill_model = ticks`):** raw bid/ask ticks are streamed into the simulator, so every order fires at the quote that reaches it. The engine still decides on the same closed M1 bars (built from the bid ticks) as in live trading. Use this for validation gate 2 and for Modules B and C.
- A **trading day** is the London calendar date. Every window is converted from London or New York local time with the IANA timezone database, so the DST weeks (London and New York changing clocks on different dates) are handled. Covered by `tests/test_sessions.py`.
- Indicators: ATR(14) with Wilder smoothing; EMA(20) seeded with a simple average; VWAP of typical price weighted by tick volume, anchored at 07:00 London and reset daily.

## Shared rules (they override module rules)

| Rule | How it is applied |
| --- | --- |
| Risk per trade | `risk.risk_per_trade_pct` of the current balance (`risk.risk_basis` = `balance`) or of the starting balance. |
| Position sizing | units = risk money ÷ [(stop distance + `risk.sizing_slippage_usd` + round-turn commission per oz) × USD→account rate], rounded **down** to the broker's volume step. Skipped if below the broker minimum. The journal compares the first full stop-out's real loss with the plan (Trade log → Position sizing check). |
| Order of the stop/target rules | 1. widen the stop to the minimum distance; 2. push the stop away from a $20 level; 3. measure R from the final stop and set the target; 4. pull the target in front of the next $20 level; 5. skip if room < 1.2R; 6. size. |
| Minimum stop | max(`risk.min_stop_usd`, `risk.min_stop_atr_mult` × M5 ATR(14)) from the planned entry. |
| Targets near $20 levels | Long: if the first $20 level **strictly above the entry** is at or before the target, target = level − `levels.target_offset`. Short mirrored. |
| Minimum room | Skip if (target − entry)/R < `levels.min_room_r`. For Module B (two targets) the **final** target is used. |
| Stops near $20 levels | Only a level between the stop and the entry counts. Long: a stop in (L − 2.5, L + 2] is moved to L − 2.5; short mirrored. Pushing only ever widens the stop; size shrinks so risk stays 0.3%. |
| Max trades per day | Counted at **fill**. When the 8th trade fills, all pending entries are cancelled. |
| Daily loss stop | Equity (balance + open P&L, checked every M1 close) ≤ −`risk.daily_loss_stop_pct` from the day's starting balance, or `risk.max_consecutive_losses` losses in a row. The % stop cancels pending orders and, with `risk.flatten_on_daily_stop`, closes open trades; the losing-streak stop only blocks new entries. |
| Win / loss / scratch | A trade within ±`risk.scratch_r` R (e.g. stopped at break-even, minus commission) is a scratch: it neither extends nor resets a losing streak. |
| Daily profit stop | Same as the loss stop at +`risk.daily_profit_stop_pct`. |
| News filter | No new entries from `news.before_minutes` before to `news.after_minutes` after high-impact events in `news.currencies`; `news.major_after_minutes` after events whose title matches `news.major_keywords` (NFP, CPI, FOMC, Fed rate). Pending entry orders are cancelled when a blackout starts; modules re-place them afterwards if still valid. |
| Session close | Pending entries cancelled at 11:00 New York; positions closed at the first M1 close at or after 11:30 New York (in live mode also by the wall clock). |
| Weekend | Covered by the daily 11:30 flat. Saturday/Sunday dates never trade, and Monday entries are blocked before 07:00 London. |
| One direction | A market signal against an open position closes that position first. A stop entry that fills against an open position closes the older position at once. Same-direction positions may coexist. |

## Module A: London range breakout

- Range: high and low of M1 bid bars with open time 00:00–06:59 London. The day is skipped if under `sessions.min_asian_coverage` of those minutes have data.
- Width filter: (high − low) ÷ midpoint, against `module_a.range_min_pct` / `range_max_pct`.
- At the first M1 close at 07:00 London: buy stop at high + `module_a.entry_buffer`, sell stop at low − buffer, both OCO. If price is already through a side at 07:00, that side is entered at market. If the bot only starts later and price has already broken out, the day is skipped rather than chasing.
- Stop: the opposite range edge, capped at `module_a.stop_cap_pct` % of the entry price, then the shared stop rules.
- Target `module_a.target_r` R, break-even (stop to entry) once price reaches `module_a.breakeven_r` R.
- Unfilled orders are cancelled at 10:00 London (`module_a.order_expiry = window_end`) or 11:00 New York.
- If a news blackout pulls the orders, they are placed again after it, provided price is still inside the range.
- Exit: `module_a.exit_mode` = `session_close` (11:30 New York, the shared rule) or `server_2100` (21:00 on a New-York-close broker server = 14:00 New York, `sessions.server_close_ny`). This is the document's open question: test both.
- **Known conflict:** with $20 spacing, a 2.5R target more than $20 away always has a $20 level in front of it. The shared target rule then cuts it, and many days fail the 1.2R room check. The shared rules win by default (`module_a.apply_level_rules = true`). The backtest preset **"Module A reproduction"** turns the level rules off for A, uses 1% risk and disables the daily stops, to reproduce the published test (validation gate 2).

## Module B: Asian sweep and reclaim

- Evaluated on M5 closes from 07:05 to 10:00 London.
- Sweep: an M5 bar trades at least `module_b.sweep_min` beyond the Asian high (or low). The sweep bar is bar 0.
- Reclaim: an M5 close back strictly inside the range on bar 0 … `module_b.reclaim_bars`. If that does not happen, the side resets only after a later close inside the range, and a fresh sweep is needed.
- Entry: market at the reclaim close (sell after a high sweep, buy after a low sweep).
- Stop: sweep extreme (highest high / lowest low since the sweep) ± max(`stop_buffer_min`, `stop_buffer_atr` × M5 ATR).
- Targets: `partial_fraction` closed at the range midpoint; the rest at the opposite edge ∓ `final_target_offset`. The stop moves to break-even when the first target is hit. The partial is dropped if the position is too small to split or the midpoint is not between entry and final target.
- At most `max_per_side` filled trades per side per day. An opposite Module A trade is closed by the one-direction rule.

## Module C: NY continuation pullback

- Evaluated from 08:20 to 11:00 New York.
- Bias (on each M5 close): long if close > anchored VWAP **and** M15 EMA(20) now > EMA `module_c.slope_bars` M15 bars ago; short if both point down; otherwise none.
- Setup: an M5 bar whose low (long) / high (short) comes within `touch_tolerance` of the M5 EMA(20) or the VWAP. It stays armed `setup_expiry_bars` M5 bars after the last touch, and is dropped if the bias changes.
- Pullback extreme: the lowest low (long) / highest high (short) since the setup was armed.
- Trigger: the most recent confirmed M1 fractal (`swing_strength` bars each side) since 15 minutes before the setup bar. If none exists yet, the high/low of the last `swing_fallback_bars` M1 bars is used. A buy stop sits at swing + `entry_buffer` and moves with each newer swing. If an M1 bar closes through the swing before an order could be placed, entry is at market on that close. If price is already past the swing from an earlier bar, the module waits for a fresh swing.
- Stop: pullback extreme ∓ `stop_buffer`, then the minimum-distance and $20 rules. Target `target_r` R (document: 1.5–2R).
- At most `max_trades` per day. After `max_losses` losses the module stops for the day, and any working order is cancelled immediately.

## Module D: PM-fix reversion (off by default)

- Move anchor: open of the first M1 bar at 08:30 New York. Evaluated on M5 closes from 09:45 to 10:30 New York.
- Condition: |M5 close − anchor| ≥ `move_atr_mult` × M15 ATR(14), and the bar's extreme in the move direction is within `level_proximity` of the nearest $20 level.
- Rejection: after an up move, a bearish M5 candle closing below the level (mirrored after a down move). Entry is at market on that close, against the move.
- Stop: the further of level ± `stop_beyond_level` and move extreme ± `extreme_buffer`.
- Target: the nearer of `target_r` R and the anchored VWAP (a VWAP on the wrong side is ignored), then the shared rules.
- One trade per $20 level, at most `max_trades` per day.

## Validation tooling

- **Random baseline:** for every real trade of a module, a twin trade is simulated with a random entry bar inside the same window on the same day, a coin-flip direction, and the same stop distance, target distance, break-even/partial plan, costs and session exit. This is repeated `backtest.random_iterations` times. Pass = the module's mean R is above the 95th percentile of the random means.
- **Two halves:** trades are split by entry date at `backtest.split_date`. Pass = mean R > 0 in each half.
- **Significance:** t = mean R ÷ (sd ÷ √n). Pass = t ≥ `backtest.target_t_stat`.
- **Combined drawdown:** pass = worst day (lowest equity vs that day's starting balance) ≤ half of `backtest.ftmo_daily_loss_pct`, and the largest loss from the starting balance ≤ half of `backtest.ftmo_max_loss_pct`.
- **Best Day Rule:** the best day's profit ÷ total profit ≤ `backtest.best_day_max_share`.
