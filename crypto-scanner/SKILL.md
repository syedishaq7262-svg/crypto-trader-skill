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
| `crypto/config.json` | `venue`, `session`, `min_quote_volume_usd`, `min_listing_days`, `exclude`, `allow_short`, `max_cost_r` |
| `crypto/universe.json` | written by `universe`: the coins that pass the filters, busiest first |
| `crypto/watchlist.json` | written by a scan: `as_of`, `setups`, `hits[]` with `symbol`, `coin`, `setup`, `side`, `status`, `trigger`, `level`, `stop_ref`, `rvol`, `metrics`, `stop_cost_r`, `too_tight` |

## The universe

Every USDT-margined perpetual that is trading, minus stablecoins, minus
coins listed under 14 days ago, minus coins with under $20M of 24h volume,
minus anything in `exclude`. Stock, ETF, commodity and forex contracts that
Binance and Bybit now list next to crypto are left out: they follow their
home market's hours, and the setups here assume a market that never closes. Busiest first, the top 150 (40 on Hyperliquid,
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
   reference. A coin that just broke out is listed by its break, not as
   coiled.
4. **trend** 1-hour 20, 50 and 200 averages stacked in one direction and the
   price on the right side of the 50; a 15-minute pullback of at least 0.5%
   touched the 15-minute 20 average and the last bar closed back over it.
   `extended` when that close is more than two 15-minute ATRs past the
   average: a chase, not a pullback entry.
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

**Stop cost.** Every hit with a stop shows what fees and slippage would take
from a stop-out, in R, entering at the trigger, with slippage read from that
coin's order book. `over` marks one past `max_cost_r` (0.2R): crypto-risk
would block it. In the pack's backtests such trades broke about even before
costs and lost 0.35R a trade after them.

**Backtest.** Each setup's title carries its average trade after costs in
the walk-forward backtests that ship with the pack (Binance, two 90-day
runs, 30 Apr to 27 Oct 2026; see the README), longs and shorts apart. It is the setup's long-run
record, not a judgment of today's hit.

## Steps

1. If there is no recent `crypto/universe.json` or the user asked about a
   different venue, the scan builds the universe itself; no need to run
   `universe` first.
2. Run the scan the user asked for. With no preference, run all setups.
3. Report the hits per setup as the tool prints them: coin, side, trigger,
   status, stop cost. Say the venue, the time, and how many coins were
   scanned, and each setup's backtest record when it is worth knowing (most
   lost money after costs).
4. Say once, plainly, that a scan is a list of charts that match a rule.
5. Offer the next step: `/crypto-coin <coin>` for the full picture of one
   hit, `/crypto-scout` to rank the hits by how each setup has tested on
   that coin's own history, or `/crypto-exit` to plan a stop and target.

## Example

From the pack's offline test market (synthetic prices, not real data), the
New York open, shortened:

```
Scan | Binance USD-M futures | 2026-10-21 15:15 UTC
26 coins scanned (left out: 1 stablecoin, 1 listed recently, 1 low volume). Closed bars only; the newest 5m bar closed at 15:15 UTC.

Opening-range breakout | New York open at 13:30 UTC | 30-minute range | backtest: longs -0.21R, shorts -0.14R a trade after costs
coin  side   range                   trigger (5m close)       now  rvol   stop cost  status
APT   short  12.378 to 12.564        12.342 @ 14:50 UTC    12.261  4.9x       0.20R  holding
ORBX  long   21.002 to 21.255        21.404 @ 14:20 UTC    21.456  4.8x       0.13R  holding
ADA   short  0.46017 to 0.46335     0.45902 @ 14:05 UTC   0.45662  4.6x  0.22R over  holding
  ... 4 more in crypto/watchlist.json
  Broke out, then fell back inside: INJ long, OP short, ARB short, APT long, SQZX short

Range breakout | backtest: longs -0.10R, shorts -0.23R a trade after costs
coin  side     level  15m close  beyond  rvol   stop cost  status
BRKX  long    2.9210     2.9623  +1.41%  3.5x       0.07R  fresh
BRKD  short   13.052     12.871  +1.39%  3.5x       0.10R  fresh
BONK  short  0.02550    0.02543  +0.25%  1.8x  0.52R over  fresh

Squeeze (volatility compression) | backtest of the break: longs +0.00R, shorts -0.16R a trade after costs
coin   band width  rank (30d)  lower band  upper band
FUNDX       1.66%          0%      30.044      30.547
  Broke out of a squeeze (first 1h close outside the bands):
coin   side               1h close      band  middle band   stop cost
ETH    short  3,444.24 @ 15:00 UTC  3,444.42     3,476.50       0.12R

Volume spike | backtest: longs -0.05R, shorts -0.17R a trade after costs
coin  side  15m close    move  rvol     size  stop cost
VOLX  long    0.68892  +2.62%  8.7x  3.3 ATR      0.04R

Relative strength vs BTC
coin  role        24h  BTC 24h  beta  vs expected
SOL   laggard  -7.54%   -0.85%  1.25       -6.48%
LINK  leader   +7.76%   -0.85%  1.07       +8.66%

Stop cost: what fees and slippage take from a stop-out when entering at the trigger, in R. Over 0.2R (max_cost_r) crypto-risk blocks the trade.
Backtest: each setup's average trade after costs in the walk-forward backtest (Binance, 30 Apr to 27 Oct 2026).
44 hits across 25 coins. A scan lists charts that match a rule; it is not a list of coins to buy.
Next: /crypto-coin <coin> for the full picture, /crypto-exit to set the stop and target.
```

APT shows twice: its long breakout fell back inside the range, and its short
breakout is holding. Both happened, so both are listed. ADA's short and
BONK's breakout have stops so close that fees and slippage would take more
than 0.2R at a stop-out, so crypto-risk would block them. Each setup's title
carries its record in the pack's walk-forward backtest: the New York
opening range lost 0.21R a trade on longs and 0.14R on shorts, after costs.

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
