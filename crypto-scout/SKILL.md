---
name: crypto-scout
description: >-
  Scout for crypto perpetual futures: what is worth a look today, ranked by
  evidence. Scans every liquid perpetual, reads the market regime, plans the
  strongest live setups, then tests each on its own coin's last 30 days with
  the Time Machine: replays, the same rule's past trades, and an edge test
  against random entries at the same hours, all after fees, slippage and
  funding. Corrects for testing many setups at once (Benjamini-Hochberg),
  leaves out stops too tight for the costs, shows each setup's backtest
  record, flags trades that already ran, crowded sides, costly funding and a
  regime against the side, and sorts them into has the evidence, worth
  watching, no edge, and cannot be tested. Often nothing qualifies today.
  `pick N` hands one to /crypto-budget. Use when the user says "what should
  I trade today", "what is worth trading", "find me a trade", "best setups
  right now", "anything good today", "what has an edge", "where should I put
  money today", or wants the scanner's hits ranked.
argument-hint: "[--top 10] [--symbols BTC,ETH] [--side long|short] [--session ny] | pick <N>"
---

# crypto-scout

The scanner finds charts that match a rule. The Scout asks the next
question: has that rule, on that coin, made money lately, after costs, more
often than luck would? It plans each live setup the way the Exit Planner
would (the setup's own stop, a 2R target, a 24-hour time limit), replays it
on the coin's own last 30 days, re-runs the same rule on those days to see
how its past trades went, and compares them with random entries at the same
hours of the day. Then it corrects for having tested many setups at once,
because one in ten will look good by chance when you test ten.

Most days, nothing clears that bar. The board says so plainly: not trading
is a position too. When something does clear it, that is evidence from a
small sample, not a promise. Day trading loses most people money.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scout.py"                        # scan, plan and test the 10 strongest setups
python3 "${CLAUDE_SKILL_DIR}/scout.py" --top 5 --session ny   # fewer, faster; the New York opening range
python3 "${CLAUDE_SKILL_DIR}/scout.py" --symbols BTC,ETH,SOL  # only these coins
python3 "${CLAUDE_SKILL_DIR}/scout.py" --side long            # longs only
python3 "${CLAUDE_SKILL_DIR}/scout.py" pick 1                 # board entry 1 becomes the plan, with its test
```

Flags: `--top N` (1 to 40, default 10), `--symbols`, `--setups orb,breakout,...`,
`--side long|short|both`, `--session utc|tokyo|london|ny`, `--rescan` (ignore
a fresh watchlist), `--replays N` (default 2,000 per setup), `--seed`,
`--venue`, `--now`, `--offline`, `--record`, `--json`, `--no-write`, `--stats`.
Exit code 0 when at least one setup has the evidence, 1 when none does, 1
with one plain line on a problem.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/watchlist.json` | the scanner's hits; reused when under 15 minutes old on the same venue, otherwise a new scan writes it |
| `crypto/regime.json` | the market regime; reused for 2 hours |
| exchange data | 30 days of 5-minute bars per tested coin (17 on Hyperliquid), funding history, open interest, long/short positioning, the order book (slippage) |
| `crypto/config.json` | the Exit Planner's defaults, fees, `slippage_bps`, `min_reward_risk`, `min_quote_volume_usd`, `allow_short` |
| `crypto/scout.json` | the board: every tested setup with its plan, its Time Machine result, its flags and its tier |
| `crypto/plan.json`, `crypto/timemachine.json` | written by `pick N` |

## How a setup earns its place

1. **Planned like a real trade.** Entry at the trigger if it fired in the
   last 10 minutes, otherwise at the market; the setup's own stop; 2R (or
   `min_reward_risk`); out within 24 hours. A plan whose stop-out would cost
   more than `max_cost_r` (0.2R) in fees and slippage is left out and named,
   and the next setup is tested in its place: crypto-risk would block it
   anyway.
2. **Replayed.** Thousands of block-bootstrapped days from the coin's own
   recent 5-minute bars, scaled to today's volatility, with funding charged
   at each payment time and fees and slippage taken from every exit.
3. **Its own history.** The scanner's exact rule re-run on the same 30 days,
   one trade at a time, each played out bar by bar with the same stop and
   target rules.
4. **The edge test.** 100 random entries per past trade at the same time of
   day, with the same stop distance (in volatility terms) and the same
   target. The luck figure is how often random entries did as well as the
   rule.
5. **Tested together.** The luck figures of every tested setup go through a
   Benjamini-Hochberg correction at a 10% false-discovery rate. Only the ones
   that survive count as evidence.
6. **Flags.** Hard: the price has already run more than half the risk past
   the trigger, or is past the stop. Soft: a long in a Risk-off, Fearful or
   Overheated market (a short in a Risk-on one), joining crowded longs or
   shorts, funding of 30% a year or more against the side, costs over 0.2R
   at a stop-out (only when the cost rule is off), thin 24-hour volume,
   reward:risk under the minimum.
7. **The backtest line.** The setup and side's record in the pack's
   walk-forward backtest across 40 coins: its average a trade after costs,
   and its average over the trades that passed both checks (the cost rule,
   and a positive 30-day record on that coin). "Can't be told from zero"
   means the range around the average, widened for testing 14
   setup-and-side groups at once, spans zero. It is context: the board still
   ranks by the setup's own coin.

| tier | what it means |
| --- | --- |
| Has the evidence here | 10 or more past trades, made money after costs, beat random entries clearly enough to survive the correction, and nothing against it |
| Worth watching | made money in its past trades, but too few of them, not clearly better than random entries, or something against it today |
| No edge on this coin lately | lost money after costs over its past trades, or even the best case of its win rate is under break-even |
| Cannot be tested | no fixed entry rule to re-run (relative strength) |

Within a tier, setups rank by their edge over random entries, shrunk toward
zero when there are few trades. A squeeze that is coiled but has not broken
has no side yet: the board lists it, and `/crypto-entry COIN --setup squeeze`
waits for the break. Opening-range breakouts whose entry window (4 hours
after the open) has closed are left out and named: their record is of
entries at the trigger, and a late entry is a different trade. The board
names the most recent session whose range is fresh.

## Steps

1. Run `scout.py` (add `--session ny` for the New York opening range, or the
   user's own session). It takes a minute or two on live data: say so.
2. Read back the regime line, then the top tier. If "Has the evidence here"
   is empty, say exactly that: nothing has the evidence today, and not
   trading is a fine answer. Do not dress up a "worth watching" setup as a
   recommendation.
3. For each setup worth mentioning: coin, side, setup, the history line
   (average R after costs, how many trades, the luck figure), the backtest
   line, the plan (entry, stop, target, time limit), and every flag. Say
   which setups were left out for costs.
4. If the user wants to take one further: `scout.py pick N`, then
   `/crypto-budget`, `/crypto-liquidation`, `/crypto-risk`, and only then
   `/crypto-broker`. The board goes stale in 30 minutes; `pick` refuses an
   older one.
5. For a full picture of one coin first, `/crypto-coin COIN`.

## Example

From the pack's offline test market (synthetic prices, not real data),
shortened:

```
Scout | Binance USD-M futures | 2026-10-21 15:15 UTC
Regime: Risk-on. BTC trends up and most coins follow. Long breakouts have the wind behind them.
41 setups live in the scan; 10 tested on each coin's own last 30 days, after fees, slippage and funding. 3 coiled squeezes with no side yet (FUNDX, BRKX, BRKD): /crypto-entry COIN --setup squeeze waits for the break.
Left out, already failed or run: WIF short orb, SOL long orb, XRP long orb, APT long orb, ADA short orb, INJ long orb, WIF long trend, BRKX long trend.
Left out, opening-range breakouts past their 4h00m entry window (LINK long, BTC short, BONK short, DOGE short, SUI short, OP short and 6 more): a late entry is a different trade. For a fresh range: scout.py --session ny (the New York open, 1h45m ago).
Left out, stops too tight for the costs (fees and slippage over 0.2R at a stop-out, which crypto-risk blocks): BONK short range breakout 0.50R.

Worth watching (positive, not proven) (5)
 1. VOLX long, volume spike (spike 15:15 UTC)
     history +1.96R a trade over 3 trades, target first 3 of 3; random entries +0.00R; luck matches it 0.2% of the time (still clear after testing 10 setups at once)
     backtest: volume spike longs -0.02R a trade after costs over 3,226 trades (can't be told from zero); with both checks +0.08R over 840 (can't be told from zero)
     plan: entry 0.68892, stop 0.66998, target 0.72680 (2.0R), out within 24h; replays: leans toward the stop
 4. TRDX long, trend pullback (reclaimed 15:15 UTC)
     history +0.63R a trade over 45 trades, target first 26 of 41; random entries +0.53R; luck matches it 33% of the time
     backtest: trend pullback longs -0.10R a trade after costs over 3,568 trades; with both checks -0.03R over 872 (can't be told from zero)
     plan: entry 76.274, stop 75.223, target 78.376 (2.0R), out within 24h; replays: mostly out of time

No edge on this coin lately (5)
 7. ETH short, squeeze break (broke down 15:00 UTC)
     history -0.07R a trade over 3 trades, target first 1 of 3; random entries -0.30R; luck matches it 42% of the time
     backtest: squeeze break shorts -0.17R a trade after costs over 668 trades; with both checks +0.33R over 95 (can't be told from zero)
     plan: entry 3,435.04, stop 3,476.50, target 3,352.12 (2.0R), out within 24h; replays: leans toward the stop
     note: a short in a Risk-on market

Nothing has the evidence today: no setup beat random entries on its own coin's last 30 days clearly enough to survive testing them all at once, with nothing against it. Not trading is a position too.
Backtest lines: the walk-forward backtest on Binance, 40 coins, 29 Jul to 27 Oct 2026. Both checks: a stop-out costs at most 0.2R, and the setup made money on that coin over its last 30 days.
Evidence, not a promise or advice. To take one further: scout.py pick N, then /crypto-budget.
```

VOLX's three past trades all won, and random entries rarely do that, but
three trades is not a record. TRDX made +0.63R a trade over 45 trades, yet
random long entries on TRDX made +0.53R: the setup added little to what
simply being long earned. Both are worth watching, and neither is evidence.
The backtest lines say the same from further away: across 40 coins and 90
days, volume-spike longs came out about even and trend-pullback longs lost
0.10R a trade. BONK's breakout short is left out: its stop is so close that
fees and slippage would take half the risk at a stop-out.

## Rules

- Evidence ranks the board, never the size of a move or a story about a coin.
- Report the luck figure and the trade count with every result. A big
  average over three trades is three trades.
- Never promote a setup from "worth watching" to a recommendation, and never
  re-run the Scout until something shows up.
- Past trades on 30 days of one coin are a small sample. Say so when it matters.

## What it never does

It never places an order, never sizes a position, never loosens a rule to
get a setup onto the board, and never calls a result a forecast.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
