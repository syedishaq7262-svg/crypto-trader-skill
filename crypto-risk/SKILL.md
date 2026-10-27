---
name: crypto-risk
description: >-
  Risk Manager for crypto perpetual futures: the gate between a proposal and
  the exchange. Checks every order in crypto/orders.json against the user's
  limits and stamps PASS or BLOCK into the file with a fingerprint of the
  exact order: the max loss per trade (fees, slippage and funding included,
  measured from the worse of the planned and the current price), the daily
  loss limit (today's net loss plus open risk plus this trade), trades per
  day, open positions, no adding to an open position, reward:risk, what fees
  and slippage take from a stop-out (max_cost_r, 0.2R), leverage,
  isolated margin, the margin cap, liquidation distance against the stop,
  exchange minimums, a price that moved since the plan, thin coins, and a
  cooldown after a losing streak. Getting out is never blocked. Use when the
  user says "check my order", "is this allowed", "risk check", "can I take
  this trade", "why was it blocked", "am I over my daily limit", or after
  /crypto-budget or /crypto-liquidation, before /crypto-broker.
argument-hint: "[--orders FILE] [--max-drift-r 0.5]"
---

# crypto-risk

The Risk Manager is the gate between a proposal and the exchange. Every
order written by crypto-budget, or a close written by crypto-broker, passes
through it. A BLOCK is final for that order: it is not shown to the user for
a yes and it never reaches the broker. A PASS only means the order may be
shown to the user; it is not a yes and it is not advice. These limits keep
each loss and each day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"               # crypto/orders.json -> a risk stamp on every order
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write    # look without stamping
```

Also `--orders FILE`, `--max-drift-r 0.5`, `--now`, `--offline`. Exit code 0
when every order passed, 1 when any was blocked.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/orders.json` | the proposed orders; each gets a `risk` block: verdict, reasons, warnings, the numbers, a fingerprint, valid for 15 minutes |
| `crypto/config.json` | the limits |
| `crypto/ledger.jsonl` | every open and close crypto-broker logged: today's entries, today's net result, the latest closed trades |
| `crypto/positions.json` | open positions: their margin and the loss still at risk to their stops |
| exchange data | the price now, funding, 24h volume, maintenance margin tiers |
| `crypto/timemachine.json` | the Time Machine run for this exact plan (matched by `plan_id`), for warnings |
| `crypto/risk_log.jsonl` | one line per verdict |

## Entries and exits

An **entry** opens a position. An **exit** only reduces one (`reduce_only`
or `action: close`). Getting out must always be possible, so an exit is
checked for format only; nothing else ever blocks it.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks an entry when |
| --- | --- |
| format | side not long or short, size not above zero or not on the exchange's step, a price off the tick, a limit order with no limit |
| blocked | the coin is on `blocked` |
| short | a short while `allow_short` is false |
| stop | no stop while `require_stop` is true; the price is already past the stop |
| target | the price is already past the target |
| moved | the price is more than `--max-drift-r` (0.5R) from the planned entry |
| loss | size x (the move to the stop + fees and slippage both ways + a full hold's funding when this side pays) over `max_loss_per_trade_usd` |
| reward:risk | from the price now, under `min_reward_risk` |
| costs | fees and slippage would take more than `max_cost_r` (0.2R) from a stop-out: a stop too tight for the coin's costs (`0` turns the rule off) |
| daily loss | today's net loss + the risk still open on positions + this trade's max loss over `max_daily_loss_usd` |
| leverage | over `max_leverage`, or over the venue's limit |
| margin | not isolated while `allow_cross_margin` is false; margin over `max_margin_pct` of the account; margin in use plus this over the account |
| liquidation | estimated liquidation closer than `min_liq_buffer` x the stop distance (it names the leverage that passes) |
| size | under the exchange's minimum size or minimum order |
| duplicate | a position on the same coin is already open (the pack does not add to positions) |
| positions | `max_open_positions` already open |
| trades | `max_trades_per_day` entries already today |
| cooldown | the last `cooldown_after_losses` closed trades all lost, and the last one closed under `cooldown_hours` ago |
| liquidity | 24h volume under a quarter of `min_quote_volume_usd` |
| time | the plan's time limit has passed |

**The entry it measures from:** the limit for a limit order; for a market
order, the plan's entry or the price now, whichever is worse for this side.
A plan written at 15:15 cannot understate the risk at 15:45 after the price
ran.

**The day** starts at midnight in `display_tz` (UTC by default; set
`"display_tz": "Australia/Melbourne"` and the day is the user's own day).

**Warnings** do not block, but say them: a thin coin, funding of 50% a year
or more against the side (with its cost a day at this size), a LIVE account,
no Time Machine run on this plan, a Time Machine history that lost money
over 10 or more trades, a limit through the price, no time limit.

## Steps

1. Run `check.py` right before showing an order. If the price may have
   moved, that is fine: it reads the price itself.
2. Tell the user the result order by order in plain words: the max loss at
   the stop against the limit, reward:risk, the liquidation buffer, and how
   much of today's limits it uses. For a BLOCK, give every reason and its
   fix (re-plan, a smaller size, a lower leverage, wait out the cooldown, or
   skip the trade; for costs, skip it or re-plan with a wider stop at a real
   level). Never change a limit or an order yourself.
3. If the order changes in any way, run the check again: crypto-broker
   matches each order to its stamp by fingerprint, and a stamp lasts 15
   minutes.

## Example

From the pack's offline test market (synthetic prices, not real data). The
ORBX order passes at 15:15, with the Time Machine's verdict as a warning:

```
PASS  #1 long ORBX 26, entry, 2x isolated
          entry 21.456 (the plan's entry), stop 21.128, target 22.112
          max loss $9.64 (limit $10.00), costs 0.11R at a stop-out (max 0.2R), reward:risk 2.00 (min 1.5), liquidation 11.009, 31.9x the stop distance
          today before this: 0 of 4 trades, $0.00 lost, $0.00 at risk open; daily limit $30.00
        ! history: the setup lost 0.89R a trade after costs over its last 11 trades here

1 passed, 0 blocked at 2026-10-21 15:15 UTC. A PASS lasts 15 minutes and only means the order may be shown to you; it is not a yes.
Next: /crypto-broker shows the ticket and asks for your yes.
```

At 16:00 the price is 21.494. The same 26 coins now lose more than $10 at
the stop, so the same order is blocked:

```
BLOCK #1 long ORBX 26, entry, 2x isolated
          entry 21.494 (the price now), stop 21.128, target 22.112
          max loss $10.70 (limit $10.00), costs 0.09R at a stop-out (max 0.2R), reward:risk 1.69 (min 1.5), liquidation 11.028, 28.6x the stop distance
          today before this: 0 of 4 trades, $0.00 lost, $0.00 at risk open; daily limit $30.00
        - loss: 26 x 0.412 at the stop (fees, slippage, funding included) = $10.70, over your $10.00 max loss per trade
        ! history: the setup lost 0.89R a trade after costs over its last 11 trades here

0 passed, 1 blocked at 2026-10-21 16:00 UTC. Nothing can be sent.
```

A SOL long planned with a 0.3% stop: fees and slippage would take 0.37R
from a stop-out, so it is blocked, whatever else it gets right:

```
BLOCK #1 long SOL 8.730, entry, 3x isolated
          max loss $6.15 (limit $10.00), costs 0.37R at a stop-out (max 0.2R), reward:risk 2.00 (min 1.5), liquidation 117.537, 105.2x the stop distance
        - costs: fees and slippage would take 0.37R from a stop-out, over your 0.2R limit (max_cost_r): the stop is too tight for this coin's costs
```

After two losing trades today and a BTC short still open, the 15:15 order is
blocked by the day and by the cooldown:

```
        - daily loss: $19.30 lost today + $8.00 at risk in open positions + $9.64 here = $36.94, over your $30.00 a day
        - cooldown: 2 losses in a row; no new entries until 2026-10-22 00:30 UTC. A pause beats a revenge trade.
```

## Rules

- A PASS only means the order may be shown to the user. It is not a yes.
- Every number comes from the config, the order, the trade log and the exchange.
- A stop can fill worse than its price in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks an order that only gets the user out, and
never treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
