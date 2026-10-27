---
name: crypto-budget
description: >-
  Budget Manager for crypto perpetual futures. Turns the plan from
  crypto-exit into one exact order inside the user's money rules: the size
  from the most they will lose at the stop (fees, slippage and funding
  included), the lowest leverage that keeps the margin under their cap, and
  a leverage ceiling that keeps the liquidation price at least twice as far
  away as the stop. Isolated margin, a stop-market at the stop and a
  reduce-only limit at the target. Writes crypto/orders.json for
  crypto-liquidation, crypto-risk and crypto-broker. Use when the user says
  "size this trade", "how much should I buy", "how many coins", "position
  size", "what leverage", "how much margin", "risk 1% on this", "risk $20",
  or after /crypto-timemachine, before /crypto-risk.
argument-hint: "[--risk USD] [--leverage N] [--limit [PRICE]]"
---

# crypto-budget

The Budget Manager turns a plan ("long SOL at 133.20, stop 131.83") into an
exact order ("0.43 SOL, 1x isolated"), inside rules the user sets once. The
stop decides the size: the further away the stop, the smaller the position.
Leverage does not change what you lose at the stop. It only changes how much
margin the position locks up, and how close liquidation sits. So the pack
uses the least leverage that fits. It writes a proposal only: crypto-risk
checks it and crypto-broker asks the user yes or no. Most people who day
trade lose money; sizing to a max loss keeps each loss small, it does not
make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                    # crypto/plan.json + crypto/config.json -> crypto/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --risk 5           # risk $5 on this one (never above max_loss_per_trade_usd)
python3 "${CLAUDE_SKILL_DIR}/size.py" --leverage 2       # ask for 2x (it must still fit every cap)
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit            # a limit entry at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 132.80     # a limit entry at 132.80
```

Also `--plan FILE`, `--now`, `--offline`, `--record`, `--json`, `--no-write`.

## Step 1: set the rules once

If `crypto/config.json` does not exist, run `/crypto-setup` (`setup.py
init`) and ask the user for each money value in plain words:

- **account_usd**: the money in the futures account this is for. Only money they can afford to lose.
- **max_loss_per_trade_usd**: the most one trade may lose if the stop fills (default 10). A common rule is 0.5 to 1% of the account.
- **max_daily_loss_usd** (30), **max_trades_per_day** (4), **max_open_positions** (2): crypto-risk enforces these.
- **max_leverage** (3) and **max_margin_pct** (50: the most margin one position may lock up, as a share of the account).
- **min_liq_buffer** (2.0): liquidation must sit at least this many times further away than the stop.
- **min_reward_risk** (1.5), **max_hold_hours** (24), **allow_short** (true), **blocked** coins.
- Keep **paper** true until they have watched it work.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"
```

**The entry it sizes at.** A limit order: the limit. A market order: the
plan's entry, or the price now if it is worse by at least a tick (higher for
a long, lower for a short). A price that ran since the plan means a bigger
loss at the same stop, so a smaller size; a better price never inflates it.
It refuses when the price has run more than half the risk past the plan's
entry, or is already past the stop or the target.

**The size** is the smaller of two limits, rounded down to the exchange's step:

| limit | coins |
| --- | --- |
| max loss | `max_loss_per_trade_usd` / the loss a coin at the stop: the move to the stop, plus taker fee and slippage on the way in and at the stop, plus a full hold's funding when this side pays it |
| margin cap | `account_usd` x `max_margin_pct` x the leverage cap / the entry |

**The leverage cap** is the lowest of: your `max_leverage`, the venue's
limit, and the highest whole leverage whose estimated liquidation sits at
least `min_liq_buffer` times further away than the stop. **The leverage** is
then the lowest whole number that keeps the margin under the margin cap.

It refuses, and writes no order, when the size rounds to zero or under the
exchange's minimum size or minimum order (it says what the smallest order
would lose), the plan is over 12 hours old, the coin is blocked, or the
plan is a short with `allow_short` off.

**The order** (one per plan): the entry (market or limit), a stop-market at
the stop (reduce-only, triggered by the mark price, so a wick on one
exchange's last price does not trip it), and a reduce-only limit at the
target. Isolated margin. It carries the loss at the stop and the gain at the
target after costs, the estimated liquidation, the time limit, the
`plan_id`, and the Time Machine's verdict when it ran on this exact plan.

Coins priced per 1,000 (1000PEPE on Binance and Bybit, kPEPE on
Hyperliquid) are sized in those units, and the ticket says how many coins
that is.

## Step 3: show the user

Show the two limits, which one decides the size, the size in coins and
dollars, the leverage and why, the margin, the loss at the stop and the gain
at the target (after costs), the estimated liquidation and its distance
against the stop's, the time limit, and the Time Machine line. Say plainly
that a stop is an order, not a promise. Then hand over to
`/crypto-liquidation` and `/crypto-risk`.

## Example

From the pack's offline test market (synthetic prices, not real data): the
ORBX plan, a $1,000 account, $10 max loss, 3x max leverage.

```
Budget | ORBX long | Binance ORBXUSDT | 2026-10-21 15:15 UTC
Size: 26 ORBX, about $557.86 at about 21.456; 2x isolated, margin $278.93.
limit             the math                                                                  qty
max loss          $10.00 / 0.371 a coin (the stop 0.328 + fees, slippage, funding 0.043)  26.97
margin cap        50% of $1,000.00 = $500.00 of margin x 3x leverage / 21.456             69.91
exchange minimum  min size 1, min order $5.00                                                ok
The max loss decides it: 26 ORBX.
Leverage 2x: the lowest that keeps the margin under $500.00. Caps: your max 3x; liquidation at least 2x the stop distance allows up to 18x.
Stop 21.128 (stop-market, reduce-only, triggered by the mark price): lose about $9.64 there, fees, slippage and funding included.
Target 22.112 (limit, reduce-only): make about $16.27 there after fees. Reward:risk 2.00.
Estimated liquidation 11.009 (48.7% away); the stop is 1.53% away (Binance tier-1 estimate, maintenance 2.50%).
Out by 2026-10-22 15:15 UTC, win or lose.
Time Machine: no edge. The setup lost 0.89R a trade after costs over its last 11 trades here (random entries: -0.23R).
Paper trading. A stop is an order, not a promise: in a fast market it can fill past the stop.
Next: /crypto-liquidation to double-check the liquidation, then /crypto-risk.
```

The size rounds down to the exchange's step (26.97 becomes 26), so the loss
at the stop is $9.64, under the $10 limit. The Time Machine's verdict rides
along so it is in front of the user again at the moment of sizing.

## Rules

- The size comes from the max loss, never from a feeling about the trade.
- Never raise leverage to make a size fit; the caps are the user's.
- Every number comes from the plan, the config and the exchange.

## What it never does

It never places an order, never changes the plan, never raises a limit, and
never treats its proposal as the user's yes.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
