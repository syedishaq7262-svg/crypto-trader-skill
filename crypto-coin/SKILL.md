---
name: crypto-coin
description: >-
  Coin Report. One clean card for any crypto coin with a perpetual on
  Binance, Bybit or Hyperliquid: price and 24h/7d/30d moves, ranges, trend on
  15m/1h/4h/1d, RSI, volatility, support and resistance, VWAP, liquidity
  (spread, order-book depth, the cost of a $10,000 market order), funding and
  its 30-day rank, open interest, long/short positioning, how it moves
  against BTC, Fear & Greed, and any scanner setup live on it right now. Use
  whenever the user asks about one specific coin: "tell me about SOL", "what
  is BTC doing", "how is PEPE looking", "analyze DOGE", "should I look at
  WIF", "price of ETH", "is SUI liquid enough", "what's the funding on X",
  "where is support on X", "give me the info on X", or names a ticker and
  wants to know about it.
argument-hint: "<COIN> [--venue binance|bybit|hyperliquid]"
---

# crypto-coin

When the user asks about a coin, this is the answer: one card, built from
the exchange's own data in a couple of seconds, with every number they need
to understand the coin right now and nothing they do not. It is context,
not a call. The card never says buy or sell, and neither does Claude.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/coin.py" BTC
python3 "${CLAUDE_SKILL_DIR}/coin.py" pepe          # 1000PEPE on Binance, kPEPE on Hyperliquid: same coin
python3 "${CLAUDE_SKILL_DIR}/coin.py" SOL --venue bybit
python3 "${CLAUDE_SKILL_DIR}/coin.py" ETH --json
```

Flags: `--no-setups`, `--venue`, `--now`, `--offline`, `--record`, `--json`,
`--no-write`, `--stats`. Any spelling works: `btc`, `BTCUSDT`, `BTC/USDT`,
`BTC-PERP`. An unknown name fails in one line with the closest matches.

## What it reads and writes

| file | what |
| --- | --- |
| exchange data | 5m, 1h and 1d candles, the 24h ticker, 30 days of funding, open interest and its history, long/short and taker ratios, the order book, Fear & Greed |
| `crypto/regime.json` | if crypto-derivs wrote one in the last 6 hours, its label is shown |
| `crypto-scanner` | if installed next to it, its six single-coin setups run on this coin |
| `crypto/coins/<SYMBOL>.json` | everything on the card, for crypto-exit and crypto-scout |

## The card, line by line

- **Price**: last price; change over 24 hours, 7 days and 30 days.
- **Range**: 24-hour and 7-day low to high.
- **Trend**: per timeframe, `up` when price is above a rising stack of its
  20 and 50 averages, `down` when below a falling one, `mixed` otherwise.
- **Momentum**: RSI(14) on 1h, 4h and 1d.
- **Volatility**: the typical hour and day (ATR as a % of price) and realized
  volatility this week against this month. Stops tighter than a typical
  hour get hit by noise.
- **Levels**: the nearest 1-hour swing highs above and swing lows below,
  from 14 days; VWAP since 00:00 UTC; yesterday's range; the 20-day average.
- **Liquidity**: 24h volume against its 7-day average, the spread, resting
  size within 0.25% of the mid, and how far a $10,000 market buy would fill
  from the mid.
- **Funding**: the rate per payment and per year, time to the next payment,
  the 30-day average.
- **Open int.**: size, 24h change, share of accounts long; a crowding or
  flow read when there is one (the same rules as crypto-derivs).
- **Market**: BTC's 24h move, how strongly the coin moves with BTC (beta),
  its 24h move against what BTC implied, Fear & Greed, the regime.
- **7 days**: an hourly sparkline.
- **Setups now**: the scanner's single-coin setups that match right now.
- **Notes**: plain warnings when they apply (new listing, thin volume,
  extreme funding, at the 24h high or low, a wild week, a wide spread,
  priced per 1,000 coins).

## Steps

1. Run `coin.py <COIN>`. If the user named an exchange, pass `--venue`.
2. Present the card cleanly. On a phone or in chat, lead with one line (price,
   24h move, trend in a few words), then the card's facts grouped the same
   way, in the tool's numbers. Keep every number; drop nothing material;
   add no number the tool did not print.
3. Repeat any Note lines. They are the warnings that matter.
4. If a setup is live, name it with its status and offer `/crypto-exit` to
   plan a stop and target, or `/crypto-timemachine` to test it.
5. If the user asks "should I buy it", answer with what the card shows on
   both sides, then point to the planning chain. Do not give a verdict.

## Rules

- No buy, sell, bullish or bearish verdicts. Describe; do not predict.
- A level is a place price turned before, not a place it must turn again.
- If a section is missing (an exchange endpoint failed), the card lists it
  under Missing. Say so; never fill the gap from memory.

## What it never does

It never places an order, never sizes a position, never forecasts a price.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
