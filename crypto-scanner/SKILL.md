---
name: crypto-scanner
description: >-
  Chart Scanner for crypto perpetual futures. Scans every liquid USDT
  perpetual on Binance, Bybit or Hyperliquid for seven setups: session
  opening-range breakouts (UTC, Tokyo, London or the New York stock open),
  24-hour range breakouts on volume, volatility squeezes, trend pullbacks,
  RSI reversals, volume spikes and relative strength against BTC. Long and
  short. Closed candles only, free public data, no key. Writes
  crypto/watchlist.json for crypto-scout, crypto-coin and crypto-entry. Use
  when the user says "scan crypto", "scan the market", "find setups", "what
  is breaking out", "any breakouts", "opening range breakouts", "which coins
  are squeezing", "what is moving", "volume spikes", "strongest coins vs
  BTC", "build my watchlist", "what should I look at", or at the start of a
  crypto trading session.
argument-hint: "[scan | universe | orb] [--setups ...] [--session utc|tokyo|london|ny]"
---

# crypto-scanner

The first step of a day trade. It reads every liquid perpetual's candles and
lists the ones that match a rule. That is all it does. A scan is a list of
charts that match a rule, not a list of coins to buy, and a setup is not a
prediction. Most people who day trade lose money; the scanner does not
change that. It saves flipping through 150 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                    # the coins worth scanning -> crypto/universe.json
python3 "${CLAUDE_SKILL_DIR}/scan.py"                             # all seven setups -> crypto/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --session ny            # opening-range breakouts after the US stock open
python3 "${CLAUDE_SKILL_DIR}/scan.py" --setups breakout,volume --side long
python3 "${CLAUDE_SKILL_DIR}/scan.py" --symbols BTC,ETH,SOL,PEPE
```

Flags: `--setups` (orb, breakout, squeeze, trend, reversal, volume, rs),
`--side both|long|short`, `--session utc|tokyo|london|ny` (default from
config, utc), `--or-minutes 30`, `--entry-hours 4`, `--lookback-hours 24`,
`--min-rvol 1.5`, `--spike-rvol 4`, `--squeeze-pct 10`, `--rsi-low 25`,
`--rsi-high 75`, `--rs-min 3`, `--min-volume` (USD, default 20 million),
`--top N`, `--venue auto|binance|bybit|hyperliquid`, `--now "YYYY-MM-DD
HH:MM"` (UTC) to pin the clock, `--offline DIR` and `--record DIR` to replay
or save a session, `--json`, `--no-write`, `--stats`. Exit code 0 when
something matched, 1 when nothing did or it could not run (one plain line
says why).

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/config.json` | `venue`, `session`, `min_quote_volume_usd`, `min_listing_days`, `exclude`, `allow_short` |
| `crypto/universe.json` | written by `universe`: the coins that pass the filters, busiest first |
| `crypto/watchlist.json` | written by a scan: `as_of`, `setups`, `hits[]` with `symbol`, `coin`, `setup`, `side`, `status`, `trigger`, `level`, `stop_ref`, `rvol`, `metrics` |

## The universe

Every USDT-margined perpetual that is trading, minus stablecoins, minus
coins listed under 14 days ago, minus coins with under $20M of 24h volume,
minus anything in `exclude`. Busiest first, the top 150 (40 on Hyperliquid,
whose candle limit is tight). Coins priced per 1,000 (1000PEPE on Binance,
kPEPE on Hyperliquid) are the same coin: `--symbols PEPE` finds them.

## The seven setups

Each one uses closed candles only, never the bar still forming.

1. **orb** Opening-range breakout. The range is the first `--or-minutes`
   (30) after the session opens: 00:00 UTC (`utc`, `tokyo`), London 08:00
   or New York 09:30 local, daylight saving handled. A hit is the first
   5-minute close beyond the range within `--entry-hours` (4) of the open,
   on at least 1.5x the median volume of the 4 hours before the open.
   `holding` if price is still beyond the range, `back inside` if it fell
   back. The New York range skips weekends.
2. **breakout** The last closed 15-minute bar closes beyond the high or low
   of the previous 24 hours on at least 1.5x its median volume. `fresh` if
   the bar before was still inside.
3. **squeeze** The 1-hour Bollinger band width (20, 2) is in the lowest 10%
   of the last 30 days: `coiled`, no side yet (`/crypto-entry COIN --setup
   squeeze` waits for the break). The break itself is a hit with a side: the
   first 1-hour close outside the bands after a squeeze in the 12 hours
   before, `broke up` or `broke down`, with the middle band as its stop
   reference.
4. **trend** 1-hour 20, 50 and 200 averages stacked in one direction and the
   price on the right side of the 50; a 15-minute pullback of at least 0.5%
   touched the 15-minute 20 average and the last bar closed back over it.
5. **reversal** 1-hour RSI(14) reached 25 or below (75 or above) in the four
   hours before the last one, the last hour's RSI is back 5 points inside,
   and the last close turned.
6. **volume** The last 15-minute bar traded 4x its median volume and moved
   at least one ATR. The side is the direction of the move.
7. **rs** Relative strength: each coin's 24h move minus what BTC's 24h move
   predicts for it (beta from 7 days of hourly returns). The top and bottom
   10% with at least 3 points of excess are leaders and laggards.

`stop_ref` on each hit is where the setup is plainly wrong (the range
middle, a pullback low, the spike bar's far end). It is a reference for
crypto-exit, not a stop order.

## Steps

1. If there is no recent `crypto/universe.json` or the user asked about a
   different venue, the scan builds the universe itself; no need to run
   `universe` first.
2. Run the scan the user asked for. With no preference, run all setups.
3. Report the hits per setup as the tool prints them: coin, side, trigger,
   status. Say the venue, the time, and how many coins were scanned.
4. Say once, plainly, that a scan is a list of charts that match a rule.
5. Offer the next step: `/crypto-coin <coin>` for the full picture of one
   hit, `/crypto-scout` to rank the hits by how each setup has tested on
   that coin's own history, or `/crypto-exit` to plan a stop and target.

## Rules

- Every number comes from the tool. Never add a coin the scan did not list.
- Never rank hits as "best" or "most likely to win". The scan does not know
  that. crypto-scout tests them; until then they are only matches.
- Crypto never closes. The sessions are where volume gathers, not opens of a
  market. Weekends are thinner.
- Free exchange data can lag or rate-limit. A coin that could not be read is
  listed at the end; never guess its numbers.

## What it never does

It never places an order, never sizes a position, never says buy or sell,
and never reads a candle that has not closed.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
