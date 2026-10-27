---
name: crypto-exit
description: >-
  Exit Planner for crypto perpetual futures. Sets the stop and the target
  BEFORE the entry and writes crypto/plan.json: entry (the scanner's trigger,
  the market, or a price), stop (the setup's own invalidation level, an ATR
  multiple, a percent, the nearest 1-hour swing, or a price), target (an R
  multiple, the next swing level, an ATR multiple, or a price), a time limit,
  and what fees, slippage (measured from the live order book) and funding do
  to the trade in R. Shows the win rate the target needs to break even after
  costs, and warns about stops inside the noise, targets past resistance or
  costly funding. Long and short. Use when the user says "plan a trade",
  "where should my stop go", "set a stop and target", "plan the exit",
  "how much would I risk", "is 2R realistic", "plan a long on X", "plan a
  short on X", or after /crypto-scanner or /crypto-coin, before
  /crypto-timemachine.
argument-hint: "plan <COIN> [--side long|short] [--stop ...] [--target ...] [--hold 24h] | show"
---

# crypto-exit

The Exit Planner decides how a trade ends before it starts. The stop is
where the setup has failed. The target is where you take what you came for.
The time limit is when you get out either way: crypto never closes, so a
trade without one can drift for days. All three are set BEFORE the entry,
written down and handed on, so nobody decides anything in the middle of a
fast move. A stop is a plan, not a guarantee: a fast market or a thin book
can fill past it. Day trading loses most people money, and a neat plan does
not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan ORBX                       # the scanner's hit: setup stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan SOL --side long --stop atr:1.5 --target 2R
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan BTC --side short --entry 67250 --stop 67900 --target level --hold 8h
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan ETH --setup trend          # pick one setup when a coin has several hits
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                            # the current crypto/plan.json
```

Flags: `--side long|short` (taken from the hit when there is one),
`--setup NAME`, `--manual` (ignore the watchlist), `--entry auto|trigger|market|PRICE`,
`--stop`, `--target`, `--hold 8h|90m|2d`, `--venue`, `--now`, `--offline`,
`--record`, `--json`, `--no-write`. Exit code 0 when the plan was written,
1 when it was refused (one plain line says why).

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/entry.json` | a trigger from `/crypto-entry`, used first while it is under an hour old |
| `crypto/watchlist.json` | the scanner's hits: setup, side, trigger, the setup's stop reference, the scan's thresholds |
| exchange data | 5-minute and 1-hour candles (ATR, swings), the ticker (price, funding), the order book (slippage) |
| `crypto/config.json` | `min_reward_risk` (default target), `max_hold_hours` (24), fees and `slippage_bps` (null = measured) |
| `crypto/plan.json` | everything the next skills need, with a `plan_id` that ties the Time Machine run and the order to this exact plan |

## Entries, stops and targets

| entry | price |
| --- | --- |
| `auto` (default) | the trigger if it fired in the last 10 minutes, otherwise the price now |
| `trigger` | the trigger price, even if older (it warns when the price has moved) |
| `market` | the price now |
| a price | a resting order at that price, which may never fill |

| stop | where it goes |
| --- | --- |
| `setup` (default with a hit) | the setup's invalidation: the opening-range midpoint (orb), one 15-minute ATR back inside the broken level (breakout), the pullback's extreme (trend), the 6-hour extreme (reversal), the spike bar's far end (volume), the middle band (squeeze break) |
| `range` | the far side of the opening range (wider) |
| `atr:K` | K x the 14-bar ATR of 15-minute candles (default `atr:1.5` without a hit) |
| `pct:X` | X percent from the entry |
| `swing` | just beyond the nearest 1-hour swing low (long) or high (short) that is at least half an ATR away |
| a price | your price |

| target | where it goes |
| --- | --- |
| `2R` (default; never under `min_reward_risk`) | the entry plus 2 x the risk |
| `1.5R`, `3R`, ... | any R multiple |
| `level` | just before the nearest 1-hour swing high (long) or low (short) at least 1R away |
| `atr:K` | K x the 15-minute ATR |
| a price | your price |

The stop rounds AWAY from the entry to the exchange's tick; the target
rounds to the nearest tick. Risk = the distance from entry to stop.

**Costs, in R.** Every plan shows what fees and slippage take: the taker fee
and slippage on the way in, the taker fee and slippage at a stop-out, the
maker fee at the target. Slippage is measured from the live order book (half
the spread plus the impact of a $10,000 market order, never under half a
basis point) unless `slippage_bps` is set. It also shows the funding a full
hold would pay or receive. From those it prints the **break-even share**:
how often the target must come first, of the trades that hit a side, just
to break even after costs. A 2R target needs about 35 to 38%; a 1R target
needs over 50%.

**The cost rule.** When fees and slippage would take more than `max_cost_r`
(0.2R by default) from a stop-out, crypto-risk blocks the order. The plan
says so and names the smallest stop distance that would pass. In the
published backtest, the trades whose stop-out cost more than 0.2R broke
about even before costs and lost 0.38R a trade after them: tight stops are
where costs do their damage. A wider stop is only worth it at a level that
means something; otherwise the answer is no trade.

**The backtest line.** A plan made from a scanner setup shows that setup
and side's record in the pack's walk-forward backtest
(`crypto-setup/track_record.json`, or your own run's copy in `crypto/`):
its average a trade after costs, then its average over the trades that
passed both checks (the cost rule, and the same setup making money on that
coin over its last 30 days). "Can't be told from zero" means the range
around that average, widened for testing 14 setup-and-side groups at once,
spans zero. It is the setup's history across 40 coins, not a forecast for
this trade.

**Refused, with the reason:** a stop on the wrong side of the entry, a stop
tighter than 0.05% of the price, a target on the wrong side, a price already
past the stop, a short with `allow_short` off, a setup with no stop of its
own and no other stop named, a squeeze with no side given.

**Warnings** (the plan is still written; say them): a stop inside half a
15-minute ATR (noise will hit it), a stop wider than three 1-hour ATRs,
fees and slippage over `max_cost_r` at a stop-out (crypto-risk will block
it; it names the stop distance that would pass), or over 0.2R when the rule
is off, funding of 30% a year or more against the side, a price that has moved a quarter of the risk from the trigger, a
target beyond the 7-day range, a swing level between the entry and the
target, reward:risk under `min_reward_risk` (crypto-risk will block it), a
setup that has already failed or run, a market entry more than an hour after
the trigger (a late entry: the setup's record is of entries at the trigger).

Without a hit and without `--side`, it asks for a side, and says whether
there is no scan yet or the last scan had no setup on that coin.

## Steps

1. Usually right after `/crypto-scanner` or `/crypto-coin`. With a hit on the
   coin, `plan <COIN>` uses it. Without one, ask the user for the side and
   use `--manual`.
2. Use the setup stop unless the user names another. For a manual plan,
   `atr:1.5` is the default; offer `swing` when there is a clear swing. On a
   quiet major a 1.5-ATR stop is often too tight for the cost rule; the
   warning names the distance that would pass.
3. Read back: entry and why, stop and its rule, target and its rule, risk a
   coin and as a percent, reward:risk before and after costs, the cost line,
   funding, the break-even share, the time limit, the backtest line, then
   every warning.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target, the other side). A stop too tight for the costs: skip it, or a
   wider stop at a real level, at a smaller size. Never loosen anything on
   your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/crypto-timemachine` to replay the plan, then
   `/crypto-budget` to size it.

## Example

From the pack's offline test market (synthetic prices, not real data): the
New York opening-range breakout that /crypto-entry caught on ORBX at 14:20,
planned 55 minutes later, so it enters at the market.

```
Exit plan | ORBX long | Binance ORBXUSDT | 2026-10-21 15:15 UTC
  Setup        opening-range breakout (New York open, 21.002 to 21.255), triggered 14:20 UTC, holding
  Entry        21.456  now, at the market (the trigger was 21.404 at 14:20 UTC)
  Stop         21.128  setup: the opening-range midpoint (21.255 + 21.002) / 2, rounded away from the entry
  Target       22.112  2R (2 x the risk)
  Risk         0.328 a coin (1.53% of the entry; 2.5 x the 15-minute ATR)
  Reward:risk  2.00 before costs; after fees and slippage a win nets +1.93R and a stop-out -1.11R
  Costs        taker 0.050%, maker 0.020%, slippage 3.1 bps, measured from the order book for a $10.0K order
  Funding      +0.0127%/8h (+13.9% a year), longs pay; a full hold crosses 3 payments: -0.02R for this side
  Break-even   the target must come first in 36.4% of trades that hit a side (fees and slippage included)
  Out by       2026-10-22 15:15 UTC (24h), win or lose
  Backtest     New York opening-range breakout longs: -0.22R a trade after costs over 1,177 trades (Binance, 40 coins, 29 Jul to 27 Oct 2026)
               with both checks (the cost rule, a positive 30-day record): -0.01R over 158 (can't be told from zero)
  Warning      The target is beyond the 7-day range (19.122 to 21.886).
  Warning      A 1-hour swing high at 21.886 sits between the entry and the target.
A stop is a plan, not a guarantee: a fast market can fill past it.
Next: /crypto-timemachine to replay this plan, then /crypto-budget to size it.
```

The two warnings say the 2R target asks for a price ORBX has not traded at
in a week, through a level where it turned before. The backtest line adds
the long view: New York opening-range longs lost 0.22R a trade over 1,177
backtested trades, and the ones that passed both checks came out about
even. A 1.5R target, or no trade, are both fair answers; the planner does
not pick for the user.

## Rules

- The stop, the target and the time limit are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only. The planner never prints a profit forecast.
- A 2R target is not a prediction that the price gets there.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
