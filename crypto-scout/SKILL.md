---
name: crypto-scout
description: >-
  Scout for crypto perpetual futures: today's live setups that pass the cost
  rule, each with its record. Scans every liquid perpetual, reads the market
  regime, plans the live setups the way the Exit Planner would, leaves out
  stops too tight for the costs, and orders the rest by their record in the
  pack's walk-forward backtests, the least bad first. Shows how each rule did
  on its own coin's last 30 days against random entries, as context; flags
  trades that already ran, crowded sides, costly funding and a regime against
  the side; and says plainly that no setup has shown an edge after costs.
  `pick N` hands one to /crypto-budget. Use when the user says "what should
  I trade today", "what is worth trading", "find me a trade", "best setups
  right now", "anything good today", "what has an edge", "where should I put
  money today", or wants the scanner's hits ranked.
argument-hint: "[--top 10] [--symbols BTC,ETH] [--side long|short] [--session ny] | pick <N>"
---

# crypto-scout

The scanner finds charts that match a rule. The Scout asks what each of
today's live setups has done before. It plans each one the way the Exit
Planner would (the setup's own stop, a 2R target, a 24-hour time limit),
leaves out any whose stop is too tight for the costs, and orders the rest by
their record in the pack's walk-forward backtests: two 90-day runs, 37,401
trades, after fees, slippage and funding. Then it shows how each rule did on
its own coin's last 30 days, against random entries at the same hours.

No setup in those backtests made money after costs clearly enough to tell
from luck, and a coin's recent record with a setup did not predict its next
trade. The board says so every time: it is a list of what the rules found,
not a list of trades to take. Day trading loses most people money.

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
Exit code 0 when the board lists at least one setup within the cost rule, 1
when it lists none, 1 with one plain line on a problem.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/watchlist.json` | the scanner's hits; reused when under 15 minutes old on the same venue, otherwise a new scan writes it |
| `crypto/regime.json` | the market regime; reused for 2 hours |
| exchange data | 30 days of 5-minute bars per tested coin (17 on Hyperliquid), funding history, open interest, long/short positioning, the order book (slippage) |
| `crypto/config.json` | the Exit Planner's defaults, fees, `slippage_bps`, `min_reward_risk`, `min_quote_volume_usd`, `allow_short` |
| `crypto/scout.json` | the board: every tested setup with its plan, its Time Machine result, its flags, its group and its backtest record |
| `crypto/plan.json`, `crypto/timemachine.json` | written by `pick N` |

## How the board is built

1. **Planned like a real trade.** Entry at the trigger if it fired in the
   last 10 minutes, otherwise at the market; the setup's own stop; 2R (or
   `min_reward_risk`); out within 24 hours. A plan whose stop-out would cost
   more than `max_cost_r` (0.2R) in fees and slippage is left out and named,
   and the next setup is tested in its place: crypto-risk would block it
   anyway.
2. **The backtest record leads.** The setup and side's record in the pack's
   walk-forward backtests: its average a trade after costs over the trades
   within the cost rule (the ones the pack lets you take), then over all of
   them. "Can't be told from zero" means the range around the average,
   widened for testing 14 setup-and-side groups at once, spans zero. The
   board is ordered by the first figure, the least bad first: across the two
   90-day runs, that ordering of setups held up better than anything else
   tested. Setups of the same kind go cheapest stop-out first.
3. **This coin, last 30 days.** The scanner's exact rule re-run on the
   coin's own last 30 days, one trade at a time, against 100 random entries
   per trade at the same time of day with the same stop distance (in
   volatility terms) and the same target. The luck figure is how often
   random entries did as well; "still clear after testing N setups at once"
   means it survived a Benjamini-Hochberg correction at a 10% false-discovery
   rate. This is context, not a forecast: in the backtests a coin's recent
   record with a setup did not predict its next trade.
4. **Replayed.** Thousands of block-bootstrapped days from the coin's own
   recent 5-minute bars, scaled to today's volatility, with funding charged
   at each payment time and fees and slippage taken from every exit: the
   "replays" word on the plan line.
5. **Flags.** Hard: the price has already run more than half the risk past
   the trigger, or is past the stop. Soft: a long in a Risk-off, Fearful or
   Overheated market (a short in a Risk-on one), joining crowded longs or
   shorts, funding of 30% a year or more against the side, costs over 0.2R
   at a stop-out (only when the cost rule is off), thin 24-hour volume,
   reward:risk under the minimum.

| group | what it means |
| --- | --- |
| Within the cost rule, best long-run record first | can be taken as planned: a stop-out costs at most `max_cost_r`, and nothing hard against it |
| Gone since it fired | the price has run more than half the risk past the trigger, or is past the stop: the plan no longer holds |
| No fixed entry rule to test | relative strength: there is no entry rule to re-run |

A squeeze that is coiled but has not broken
has no side yet: the board lists it, and `/crypto-entry COIN --setup squeeze`
waits for the break. Opening-range breakouts whose entry window (4 hours
after the open) has closed are left out and named: their record is of
entries at the trigger, and a late entry is a different trade. The board
names the most recent session whose range is fresh.

## Steps

1. Run `scout.py` (add `--session ny` for the New York opening range, or the
   user's own session). It takes a minute or two on live data: say so.
2. Read back the regime line, then what the board's last line says: nothing
   on it has shown an edge, and not trading is a fine answer. The first entry
   has the least bad record, not a good one; never present it as a pick.
3. For each setup worth mentioning: coin, side, setup, the backtest line
   (within the cost rule, then all of them), the coin's last 30 days (average
   R, how many trades, the luck figure), the plan (entry, stop, target, time
   limit, what a stop-out costs), and every flag. Say which setups were left
   out for costs.
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

Within the cost rule, best long-run record first (10)
 1. SHRTX long, squeeze break (broke up 15:00 UTC)
     backtest: squeeze break longs within the cost rule +0.06R a trade after costs over 713 trades (can't be told from zero); all of them 0.00R over 993 (can't be told from zero)
     this coin, last 30 days: +0.93R a trade over 3 trades, target first 2 of 3; random entries +0.43R; luck matches it 30% of the time
     plan: entry 0.11885, stop 0.11728, target 0.12199 (2.0R), out within 24h; a stop-out costs 0.16R; replays: close to break-even
 2. VOLX long, volume spike (spike 15:15 UTC)
     backtest: volume spike longs within the cost rule +0.03R a trade after costs over 3,215 trades (can't be told from zero); all of them -0.05R over 4,494 (can't be told from zero)
     this coin, last 30 days: +1.96R a trade over 3 trades, target first 3 of 3; random entries +0.00R; luck matches it 0.2% of the time (still clear after testing 9 setups at once)
     plan: entry 0.68892, stop 0.66998, target 0.72680 (2.0R), out within 24h; a stop-out costs 0.04R; replays: leans toward the stop
 6. TRDX long, trend pullback (reclaimed 15:15 UTC)
     backtest: trend pullback longs within the cost rule -0.01R a trade after costs over 3,248 trades (can't be told from zero); all of them -0.12R over 4,650
     this coin, last 30 days: +0.63R a trade over 45 trades, target first 26 of 41; random entries +0.53R; luck matches it 33% of the time
     plan: entry 76.274, stop 75.223, target 78.376 (2.0R), out within 24h; a stop-out costs 0.08R; replays: mostly out of time

Nothing on this board has shown an edge: in the pack's backtests (37,401 trades, Binance, 30 Apr to 27 Oct 2026) no setup made money after costs clearly enough to tell from luck, and a setup's last 30 days on a coin did not predict its next trade. Not trading is a position too.
A list of what the rules found, not advice. To take one further: scout.py pick N, then /crypto-budget.
```

SHRTX's squeeze break comes first only because squeeze-break longs have the
least bad record within the cost rule: +0.06R a trade, which can't be told
from zero. VOLX won all three of its recent trades on VOLX, which random
entries matched 0.2% of the time, and TRDX made +0.63R a trade over 45
trades while random long entries on TRDX made +0.53R. In the backtests a
coin's recent record with a setup did not predict its next trade, so neither
is a reason to trade. BONK's breakout short is left out: its stop is so
close that fees and slippage would take half the risk at a stop-out.

## Rules

- The backtest record orders the board, never the size of a move, a story
  about a coin, or one coin's recent run.
- Report the luck figure and the trade count with every recent record. A big
  average over three trades is three trades.
- Never present a setup as a trade to take, and never re-run the Scout until
  something shows up.
- Past trades on 30 days of one coin are a small sample, and in the backtests
  they did not predict the next trade. Say so when the user leans on them.

## What it never does

It never places an order, never sizes a position, never loosens a rule to
get a setup onto the board, and never calls a result a forecast.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
