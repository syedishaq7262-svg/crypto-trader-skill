---
name: crypto-timemachine
description: >-
  Time Machine for crypto perpetual futures. Replays a plan 10,000 times before
  you risk a dollar: rebuilds the rest of the trade from blocks of the coin's own
  5-minute candles (time of day kept, scaled to today's volatility, past trend
  removed), with fees, slippage and funding taken off every replay. Then re-runs
  the plan's own setup rule on every past day of the last 30 days, and tests it
  against random entries at the same times of day with the same stop and target:
  did the setup beat them, or could luck have done as well? Prints the odds,
  the time to target and stop, the average R after costs, the real record with
  95% ranges, an edge test and one honest verdict. Use when the user says
  "run the time machine", "replay this trade", "what are the odds", "simulate",
  "monte carlo", "backtest this setup", "does this setup work", "is this an
  edge", "is it a coin flip", or after /crypto-exit writes a plan, before
  /crypto-budget.
argument-hint: "[run | chart] [--days 30] [--replays 10000] [--seed 19]"
---

# crypto-timemachine

The Time Machine sits between the plan and the money. crypto-exit has set
the entry, the stop, the target and the time limit. Before anything is sized
or sent, this skill answers three questions, every one after fees, slippage
and funding:

1. **The replays.** If the rest of this trade moves the way this coin's own
   candles have moved, with today's volatility and no trend assumed, how
   often does the target come first, the stop first, or neither?
2. **The history.** The plan's own setup rule, re-run on every past day of
   the pool: what really happened?
3. **The edge test.** Random entries at the same times of day, with the same
   stop distance and target: did the setup beat them, or could luck have
   matched it?

It is a simulation built from the recent past, not a prediction. Most
setups, tested honestly, are close to break-even after costs. Saying so is
the point.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                  # crypto/plan.json -> crypto/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --days 60        # a longer pool and history
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --block 1        # bar by bar, like the stock version
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart                # ASCII fan chart of the last run
```

Flags: `--replays 10000`, `--seed 19`, `--days 30` (3 to 120; Hyperliquid
keeps about 17 days of 5-minute candles), `--block 12` (average block length
in 5-minute bars), `--window 2` (time-of-day match, +/- bars),
`--no-vol-scale`, `--keep-drift`, `--no-baseline`, `--plan FILE`, `--now`,
`--offline`, `--record`, `--json`, `--no-write`, `--stats`.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/plan.json` | the plan from crypto-exit: entry, stop, target, R, time limit, costs, setup and its scan thresholds, `plan_id` |
| exchange data | 5-minute candles for the pool (30 days by default) and the funding actually paid over them |
| `crypto-scanner/scan.py` | the scanner's own setup code, re-run on each past moment for the history |
| `crypto/timemachine.json` | the odds, R percentiles, history trades, edge test, verdict, fan-chart bands and 40 sample paths, tied to the plan by `plan_id` |

## How it works

1. **The pool.** Every 5-minute bar before the entry becomes four ratios to
   the bar before it (open, high, low, close). Each bar is then scaled so its
   own day had today's volatility (the last 24 hours against that bar's
   previous 24 hours, clipped to 0.5x to 2x). This is filtered historical
   simulation: calm-day bars count when today is wild, and the other way.
2. **No borrowed trend.** The average move at each time of day is taken out,
   slot by slot, so replays carry the coin's rhythm (busy US open, quiet
   weekend hours) but no drift. With a month of data the average path of a
   time of day is mostly noise, and every replay starting then would inherit
   it. Without this step a driftless random walk replays wrong by up to 9
   points; with it the engine matches the textbook gambler's-ruin odds (50%,
   33% and 25% for 1R, 2R and 3R targets) to within a point, which the tests
   check. `--keep-drift` keeps the pool's overall trend instead.
3. **One replay.** Start at the entry. Draw a past bar near the same time of
   day and keep walking forward through its real neighbours: blocks of about
   an hour (stationary bootstrap) keep momentum and volatility clusters that
   bar-by-bar sampling loses. Bar by bar it checks the stop first, then the
   target. A bar that touches both counts as the stop. A bar that opens past
   the stop fills at its open, worse than the stop. Funding is charged at
   every payment time crossed, at today's rate. A replay still open at the
   time limit exits at the market.
4. **R after costs.** +2 at a 2R target less the entry and maker exit fees,
   -1 at the stop less the entry and stop-out costs, otherwise the distance
   moved over the risk less costs. Funding paid or received is included.
5. **The history.** The scanner's own code runs on each past moment of the
   pool: every day's session for an opening range, every 15-minute bar for a
   breakout, trend or volume spike, every hour for a reversal or a squeeze
   break. The plan's
   stop rule (in volatility terms when it was a one-off price) and target R
   are applied, one trade at a time, on the real bars, with the real funding.
   Relative-strength plans and your own levels have no fixed entry rule to
   re-run; a plan made while a squeeze is still coiled is told that the rule
   only trades the break.
6. **The edge test.** For each historical trade, 100 random entries at the
   same time of day (+/- 10 minutes) with the same stop distance in 5-minute
   ATRs and the same target R. Then 2,000 pseudo-histories, each built from
   one random trade per real one: the share whose average R matches or beats
   the setup's is the chance luck alone did as well.
7. **The verdict**, in this order:
   - no setup, or none that can be re-run: the replays speak alone;
   - at least 5 decided past trades and even the top of the 95% range for
     the target-first rate is under the break-even share: *no edge so far*;
   - under 10 past trades: *too few to judge the setup itself*;
   - the setup lost money after costs: *no edge*;
   - it made money and luck matches it 10% of the time or less: *beat random
     entries; a hint, not proof*;
   - it made money but luck matches it more often: *no clear edge*.

   The replays get their own line: *close to break-even*, *leans toward* or
   *strongly toward* the target or the stop, measured against the
   cost-adjusted break-even share (under 5 points, 5 to 15, 15 or more), or
   *mostly out of time* when over half the replays hit neither side.

**The seed.** Same plan, same data, same seed (19 by default): the same
numbers on any machine. Say the seed with every result.

## Steps

1. Make sure `crypto/plan.json` is the plan the user means. If not, run
   `/crypto-exit` first.
2. Run `timemachine.py run`. It takes one to three seconds.
3. Tell the user, in this order: the four replay odds with the median times,
   the average R after costs and its 90% range, the replay line exactly as
   printed, the history lines, the edge test lines, then the verdict line
   exactly as printed. Say the seed.
4. If the verdict is no edge or close to break-even, say so plainly. Do not
   dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 30 days and is not a
   prediction: news, listings and liquidations can do things those days
   never did.
6. Offer `chart` for the fan chart. If the user still wants the trade, hand
   off to `/crypto-budget` to size it.

## Example

From the pack's offline test market (synthetic prices, not real data): the
ORBX opening-range plan from crypto-exit.

```
10,000 replays of the next 24h in blocks of about 1h00m, seed 19:
  target first   31.6%   median 6h30m in
  stop first     68.0%   median 2h50m in
  still open      0.4%   out at the time limit, price median 17.731 (5% to 95%: 17.476 to 17.931)
  green          31.8%   ended above zero after costs
  Average R after costs -0.17 (median -1.13; 90% of replays between -1.15 and +1.91).
  Costs: 0.06R in; out 0.01R at the target, 0.06R at a stop; funding averaged -0.02R.
Close to break-even: to break even after costs the target must come first in 36.6% of the replays that hit a side; it did in 31.7%.

History: the same opening-range (New York open) rule on the pool fired 8 times; 8 became trades, one at a time.
  0 target, 7 stop, 1 out at the time limit. Average R after costs -1.11 (90% range -1.22 to -0.92).
  Target first in 0 of 7 decided: 0% (95% range 0% to 35%); break-even needs 36.6%.
Edge test: 800 random entries at the same times of day, same stop distance and target, averaged -0.52R.
  The setup did worse than random entries: edge -0.58R a trade (90% range -0.70 to -0.40).
Verdict: no edge so far. The target came first in 0 of 7 decided past trades; even the top of the 95% range (35%) is under the 36.6% break-even.
```

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The replay line and the verdict line are printed by the tool. Repeat them,
  do not rewrite them.
- A small sample is a small sample. Never call a setup proven.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or time limit is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
never uses a candle from after the entry, and never calls a result a sure
thing.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
