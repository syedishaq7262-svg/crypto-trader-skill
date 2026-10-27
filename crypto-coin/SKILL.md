---
name: crypto-coin
description: >-
  Coin Report. One clean card for any crypto coin with a perpetual on
  Binance, Bybit or Hyperliquid: price and 24h/7d/30d moves, ranges, trend on
  15m/1h/4h/1d, RSI, volatility, support and resistance, VWAP, liquidity
  (spread, order-book depth, the cost of a $10,000 market order), funding and
  its 30-day rank, open interest, long/short positioning, how it moves
  against BTC, Fear & Greed, the latest headlines naming it, the next
  high-impact US release, exchange delisting notices, and any scanner setup
  live on it right now. Use
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

Flags: `--no-setups`, `--no-news`, `--venue`, `--now`, `--offline`,
`--record`, `--json`, `--no-write`, `--stats`. Any spelling works: `btc`, `BTCUSDT`, `BTC/USDT`,
`BTC-PERP`. An unknown name fails in one line with the closest matches.

## What it reads and writes

| file | what |
| --- | --- |
| exchange data | 5m, 1h and 1d candles, the 24h ticker, 30 days of funding, open interest and its history, long/short and taker ratios, the order book, Fear & Greed |
| `crypto/regime.json` | if crypto-derivs wrote one in the last 6 hours, its label is shown |
| `crypto-scanner` | if installed next to it, its six single-coin setups run on this coin |
| `crypto-news` | if installed next to it: headlines naming the coin in the last 24 hours, high-impact US releases in the next 24 hours, Bybit listing and delisting notices |
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
- **News**: how many headlines named the coin in the last 24 hours, and the
  latest with its source, age and tags; or that none did, and how many
  feeds were read.
- **Macro**: high-impact US releases in the next 24 hours (CPI, jobs, the
  Fed), which can move every coin at once, or that the calendar could not
  be read just now. It is an extra: one quick try, never a long wait.
- **Setups now**: the scanner's single-coin setups that match right now.
- **Backtest**: each live setup and side's record in the pack's walk-forward
  backtest (Binance, 40 coins, 90 days): the average trade after costs, and
  the average over the trades that passed both checks (the cost rule, and a
  positive 30-day record on that coin). "Can't be told from zero" means the
  range around the average, widened for testing 14 setup-and-side groups at
  once, spans zero. Most setups lost money after costs; say so when it
  applies.
- **Notes**: plain warnings when they apply (new listing, thin volume,
  extreme funding, at the 24h high or low, a wild week, a wide spread,
  priced per 1,000 coins, an exchange delisting or listing it).

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

## Example

From the pack's offline test market (synthetic prices, made-up headlines,
not real data):

```
SOL | Binance SOLUSDT perpetual | 2026-10-21 15:15 UTC
Price      171.809   24h -7.44%   7d +7.15%   30d +12.21%
Range      24h 170.320 to 188.226  |  7d 154.489 to 188.226
Trend      15m down, 1h mixed, 4h up, 1d -   (price against its 20 and 50 averages)
Momentum   RSI 1h 39, 4h 56, 1d 55
Volatility typical hour 1.64% | typical day 7.4% | realized 98% a year this week vs 101% this month
Levels     resistance 172.246, 174.065  |  support 170.320, 168.265   (1h swing points, 14 days)
           VWAP since 00:00 UTC 174.353  |  yesterday 172.163 to 188.226  |  20-day average 166.032
Liquidity  24h $2.30B (1.1x its 7-day average)  |  spread 0.0012%, $6.3M bids and $6.3M asks within 0.25%
           a $10,000 market buy would fill about 0.0039% above the mid
Funding    -0.0237%/8h (-26.0% a year), next in 45m  |  30-day average +14% a year
Open int.  $798.9M, -9.9% in 24h  |  58% of accounts long
           long unwinding (price down, open interest down)
Market     BTC 24h -0.8% | moves 1.3x BTC | -6.4% vs what BTC implied | Fear & Greed 64 (Greed) | regime Risk-on
7 days     ▁▁▁▂▃▄▃▃▂▁▁▁▂▃▃▃▄▃▃▄▄▆▆▆█▄▄▄
News       2 headlines in 24h; latest: "SOL Climbs as Solana Funds Draw Fresh Inflows" (CoinDesk, 35m ago) [ETF]  /crypto-news has the rest
Macro      US CPI m/m in 2h45m
Setups now opening-range breakout long (back inside)
Backtest   UTC-day opening-range breakout longs: -0.37R a trade after costs over 1,674 trades; with both checks -0.21R over 117 (can't be told from zero)
           (the walk-forward backtest: Binance, 40 coins, 29 Jul to 27 Oct 2026)
Note       Funding is in the bottom 5% of the last 30 days: holding a short is unusually expensive.
Context, not a call. Next: /crypto-exit SOL to plan a stop and target.
```

The 1d trend shows `-` because the test market keeps only 35 days, too few
for a 50-day average; real exchanges have years of daily candles.

The opening-range breakout on SOL has already failed (back inside), and the
backtest line says what the UTC-day opening range did over 90 days on real
prices: it lost 0.37R a trade on longs after costs.

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
