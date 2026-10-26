---
name: crypto-derivs
description: >-
  Derivatives Radar for crypto perpetuals: funding rates, open interest,
  long/short positioning, taker flow, basis, cross-venue funding (Binance vs
  Bybit vs Hyperliquid) and the overall market regime (BTC trend and
  volatility, breadth, average funding, Fear & Greed). Flags crowded longs
  (squeeze-down risk), crowded shorts (short-squeeze fuel), extreme funding,
  and reads price against open interest (new longs, short covering, new
  shorts, long unwinding). Writes crypto/derivs.json and crypto/regime.json.
  Use when the user asks "what is funding on X", "is BTC crowded", "where
  are the shorts", "short squeeze candidates", "open interest", "long short
  ratio", "who is paying funding", "funding arbitrage", "basis", "is the
  market overheated", "fear and greed", "market regime", "risk on or risk
  off", "what's the mood", or before trading a coin with high funding.
argument-hint: "[radar | coin <COIN> | regime] [--table]"
---

# crypto-derivs

Perpetual futures have a cost of holding: funding, paid every 1, 4 or 8
hours by one side to the other. Together with open interest (how many
contracts are open) and the long/short split, it shows who is crowded into
a trade and paying to stay there. This skill reads all of it, free and with
no key, and says it in plain words. Positioning is context, not a signal:
crowded trades can stay crowded for days.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/derivs.py"                 # radar: the 40 busiest coins -> crypto/derivs.json
python3 "${CLAUDE_SKILL_DIR}/derivs.py" radar --table   # plus every coin in one table
python3 "${CLAUDE_SKILL_DIR}/derivs.py" coin SOL        # one coin in depth
python3 "${CLAUDE_SKILL_DIR}/derivs.py" regime          # the market's weather -> crypto/regime.json
```

Flags: `--top N`, `--symbols`, `--min-volume`, `--min-spread 20` (points a
year before a cross-venue gap is listed), `--venue`, `--now`, `--offline`,
`--record`, `--json`, `--no-write`, `--stats`.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/derivs.json` | radar rows: funding (per payment and a year), open interest and its 24h and 7d change, long share, taker ratio, crowding, flow, cross-venue spreads |
| `crypto/regime.json` | BTC trend, 7d and 30d moves, realized volatility, breadth, average funding, Fear & Greed, a label and one line on what it means |

## How to read it

**Funding** is shown per payment and as a yearly rate, so a 1-hour
Hyperliquid rate and an 8-hour Binance rate compare directly. The usual
resting rate is about +11% a year (0.01% every 8 hours). Positive: longs
pay shorts. Negative: shorts pay longs.

**Crowding** (all from the tool):
- *crowded longs*: funding at least +30% a year, open interest up at least
  10% in 24h, at least 60% of accounts long, and price within 5%: longs
  are paying to hold a trade that is not moving. A drop can cascade
  through their stops.
- *crowded shorts*: funding at or below -10% a year, open interest up at
  least 10%, at most 50% of accounts long: shorts are paying while new
  positions pile in. A push up can force them to buy.
- *hot funding* (+60% a year or more) and *deeply negative funding* (-30%
  or less) without the rest.

**Flow** (price against open interest over 24h, moves of at least 1.5% and
5%): new longs, short covering, new shorts, long unwinding, plus *shorts
adding into a rise* or *longs adding into a drop* when funding says the
new money is on the other side.

**Across venues**: one public Hyperliquid request returns the next funding
on Binance, Bybit and Hyperliquid for every coin. A large gap is the raw
material of a funding carry trade; the radar lists gaps of 20 points a year
or more. It does not account for fees, transfer time or price risk.

**Regime**: BTC above a rising 20-day average or below a falling one,
breadth (share of the 30 busiest coins above their own 20-day average),
average funding and Fear & Greed give one label: Risk-on, Risk-off, Mixed,
Overheated or Fearful, with one line on what it means for day trades. A
rough read of conditions, never a forecast.

Hyperliquid publishes no open-interest history and no long/short ratio, so
those columns are blank there. Bybit open interest is in coins; the 24h
change is still exact.

## Steps

1. For "what is the market doing" run `regime`, then the radar.
2. For one coin run `coin <COIN>`. Report funding (now, yearly, rank in 30
   days, next payment), open interest and its changes, positioning, basis,
   other venues and the Read line, as printed.
3. For crowding or squeeze questions run the radar and report the Crowded
   trades block first, with the one-line reads exactly as printed.
4. Funding paid over a long hold is a real cost. When the user is planning a
   trade on a coin with hot funding, say what holding costs per day at their
   size, from the yearly rate.

## Rules

- Every number comes from the tool; repeat the reads, do not embellish them.
- Never present crowding as a timing signal. "Crowded" says who is exposed,
  not when it breaks.
- A cross-venue gap is not free money. Say so whenever you mention one.

## What it never does

It never places an order, never sizes a position, never says buy or sell.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
