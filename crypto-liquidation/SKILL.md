---
name: crypto-liquidation
description: >-
  Liquidation Guard for crypto perpetual futures. Estimates where a leveraged
  position gets liquidated (isolated margin, the exchange's own maintenance
  margin tiers where public), checks that the liquidation sits at least twice
  as far away as the stop, and measures how often the coin really moved that
  far against the position within the holding time over the last 90 days. A
  ladder shows the same for 2x to 125x. Blocks an order whose liquidation is
  too close to its stop, that has no stop, or that uses cross margin when the
  config does not allow it. Use when the user asks "where is my liquidation",
  "liquidation price at 10x", "how much leverage is safe", "will I get
  liquidated", "is 20x too much", "what leverage for this stop", or after
  /crypto-budget, before /crypto-risk.
argument-hint: "[check | at <COIN> --side long|short --leverage N [--stop P] [--hold 24h]]"
---

# crypto-liquidation

On a leveraged perpetual, a stop is the exit you planned and liquidation is
the exit you did not. The exchange closes the position when its margin is
nearly gone, and it keeps that margin. A normal wick should take out the
stop long before it gets anywhere near the liquidation price. This skill
checks that it does, with numbers from the coin's own recent moves. It also
answers the question most leveraged traders never ask out loud: how often
has this coin moved far enough to liquidate me?

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/liq.py" check                                     # every order in crypto/orders.json
python3 "${CLAUDE_SKILL_DIR}/liq.py" at SOL --side long --leverage 10          # the price, the odds, a 2x to 125x ladder
python3 "${CLAUDE_SKILL_DIR}/liq.py" at BTC --side short --leverage 5 --stop 68500 --hold 8h
```

Flags for `at`: `--entry` (default the price now), `--stop`, `--qty` (for
tiered maintenance margin on big sizes), `--hold` (24h), `--days` (90),
`--venue`. Common: `--now`, `--offline`, `--record`, `--json`, `--no-write`.
`check` exits 0 when every order passed, 1 when any was blocked.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/orders.json` | the order from crypto-budget; `check` adds a `liq` block to each order |
| exchange data | hourly candles (90 days), maintenance margin tiers (Bybit public risk limits, Hyperliquid margin tables) |
| `crypto/config.json` | `min_liq_buffer` (2.0), `allow_cross_margin` (false) |

## How it works

**The price.** For an isolated position of size q at entry E and leverage L,
liquidation comes when the margin plus the loss falls to the maintenance
margin plus the fee to close:

    long : P = (E (1 - 1/L) - d/q) / (1 - mmr - fee)
    short: P = (E (1 + 1/L) + d/q) / (1 + mmr + fee)

`mmr` is the maintenance margin rate for the position's size and `d` the
tier's deduction. Bybit and Hyperliquid publish theirs. Binance needs an API
key for its brackets, so the pack uses the tier-1 rates for BTC (0.4%) and
ETH (0.5%) and a cautious 2.5% for every other coin. A higher rate moves the
estimate closer, which makes the guard stricter, not looser. The exchange's
own liquidation price is the one that counts; say so.

**The buffer.** Liquidation distance over stop distance. Under
`min_liq_buffer` (2.0) the order is blocked, and the tool names the highest
leverage that would pass.

**The odds.** From every hourly close in the last 90 days, the worst move
against the position within the holding time (the order's time limit, or
`--hold`). The share of those that reached the liquidation distance is how
often that leverage would have been liquidated from a random hour, and the
same for the stop. It is the recent past, not a forecast: the worst
liquidation cascades are rarer than 90 days.

**Blocks:** liquidation closer than `min_liq_buffer` x the stop distance; no
stop (then liquidation is the stop, and it costs the whole margin); cross
margin while `allow_cross_margin` is false (with cross margin one position
can drain the whole account).

## Steps

1. After `/crypto-budget`, run `liq.py check`. Report each order: PASS or
   BLOCK, the estimated liquidation and its distance, the stop's distance,
   the buffer, the maintenance margin source, and the odds line.
2. For a BLOCK, give the fix the tool prints (a lower leverage, which means
   more margin for the same size) and never change the order yourself.
3. When the user asks about leverage on its own, run `at` with their coin,
   side and leverage, and read the ladder back. If their leverage was
   reached often in the last 90 days, say so plainly.
4. Then hand over to `/crypto-risk`.

## Example

From the pack's offline test market (synthetic prices, not real data):

```
Liquidation | SOL long at 10x isolated | Bybit SOLUSDT | 2026-10-21 15:15 UTC
Entry 171.809 (now). Estimated liquidation 155.491: 9.50% below.
Maintenance margin 0.50% (Bybit risk tiers). The exchange's own liquidation price is the one that counts.
Last 42 days, within 24h of any hourly close: a 9.5% move against a long happened 5.8% of the time. The worst was 16.3%.

leverage  liquidation   away  how often a move that big came within 24h
      2x       86.384  49.7%                                      never
      3x      115.178  33.0%                                      never
      5x      138.214  19.6%                                      never
     10x      155.491   9.5%                                       5.8%
     20x      164.129   4.5%                                        32%
     25x      165.857   3.5%                                        41%
     50x      169.312   1.5%                                        71%

Isolated margin: a liquidation costs this position's margin, not the rest of the account.
```

At 10x, a move big enough to liquidate this long came within a day 5.8% of
the time over the last 42 days; at 20x, about one day in three. The
pack's own cap is 3x unless the user raises it.

## Rules

- Liquidation prices here are estimates; the exchange's number counts.
- Never suggest more leverage to free up margin.
- A move that never happened in 90 days can still happen.

## What it never does

It never places an order, never changes leverage, never edits an order to
make it pass, and never calls a liquidation price safe.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
