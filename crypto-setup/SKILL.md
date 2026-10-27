---
name: crypto-setup
description: >-
  Setup and Doctor for the crypto trader pack. Checks which exchanges answer
  from the user's location (Binance USD-M futures, Bybit linear perpetuals,
  Hyperliquid), how fast each one answers, whether the computer's clock is
  close enough to the exchange's for signed orders, and writes
  crypto/config.json from the example. Holds cryptocore.py, the shared code
  every other crypto-* skill imports, so it must stay installed. Use when
  the user says "set up the crypto skills", "check my connection", "which
  exchange works here", "Binance is blocked", "why is it slow", "is my clock
  right", "make a config", "show my settings", "change my risk limits", or
  the first time any crypto-* skill runs in a project, or when a skill says
  an exchange refused the connection.
argument-hint: "[doctor | init | config]"
---

# crypto-setup

The first thing to run, and the place to come back to when something will
not connect. It does not trade, scan or advise. It answers three questions:
which exchanges can I reach from here, how fast, and what settings are in
force.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/setup.py" doctor     # reachability, speed, clock, keys present or not
python3 "${CLAUDE_SKILL_DIR}/setup.py" init       # crypto/config.json from config.example.json (never overwrites)
python3 "${CLAUDE_SKILL_DIR}/setup.py" init --force
python3 "${CLAUDE_SKILL_DIR}/setup.py" config     # every setting, and whether it is the user's or a default
```

Needs Python 3.8 or newer and nothing else: no pip install, no API key.
Everything is the standard library.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/config.json` | written by `init` from `config.example.json`, read by every skill |
| `crypto/cache/venue.json` | written by `doctor`: the venue that answered from here, used by `"venue": "auto"` for six hours |
| `cryptocore.py` | the shared library: exchange clients, clock, indicators, formatting |

## The exchanges

All three serve free public market data with no key.

| venue | data | notes |
| --- | --- | --- |
| Binance USD-M futures | candles, 24h tickers, funding, open interest and its history, long/short ratios, taker flow, order book | the deepest market; refuses some countries, including the US (HTTP 451) |
| Bybit linear perpetuals | the same, plus public risk tiers for exact liquidation prices | refuses some countries, including the US (HTTP 403) |
| Hyperliquid | candles, funding, open interest now, order book, every wallet's positions | open from anywhere; no open-interest history and no long/short ratio; tighter rate limit, so scans default to the 40 busiest coins |

`"venue": "auto"` (the default) tries them in that order and keeps the
first one that answers. A blocked exchange is the exchange's rule for that
location, not a fault in the pack. Never suggest a VPN to get around it.

## Steps

1. Run `doctor`. Report each line as printed: which venues answered, how
   fast, the clock offset, and the venue `auto` will use.
2. If no `crypto/config.json` exists and the user is going to plan trades,
   run `init` and tell them the money settings it printed. Ask them for
   their real account size and the most they will lose on one trade and in
   one day, then edit those keys in `crypto/config.json` for them.
3. If the clock is more than a second off, suggest syncing it: on Windows,
   Settings > Time & language > Date & time > Sync now; elsewhere, turn on
   setting the time automatically. The pack allows for the difference: the
   broker signs orders by the exchange's clock, and the market tools read
   candles by it.
4. If the display time zone line says `unknown`, the computer has no time
   zone database for that name: suggest an offset such as `"+11:00"` in
   `display_tz` instead.
5. If nothing answers, say so plainly and stop. Do not retry in a loop.

## Example

From the pack's offline test exchange, with Binance answering as it does
from a blocked country (the test exchange builds its made-up market on the
first request, hence the slow answers):

```
Doctor | 2026-10-21 15:15 UTC | Python 3.13.17 on Linux
  Binance USD-M futures    blocked  refuses connections from this location
  Bybit linear perpetuals  ok       28 markets, answered in 1903 ms
  Hyperliquid perps        ok       27 markets, answered in 1887 ms
  Fear & Greed index       ok       64 (Greed)
  News feed (CoinDesk)     ok       answered in 0 ms
  Macro calendar           ok       answered in 0 ms
  Bybit announcements      ok       answered in 0 ms
  Work folder              ok       /home/you/trading/crypto
  Display time zone        ok       UTC (UTC+0 now)
  Config                   default  no crypto/config.json yet (python3 setup.py init writes one)
  Exchange keys            none (fine: every skill except a live crypto-broker works without keys)
Best venue from here: Bybit linear perpetuals. With "venue": "auto" every skill uses it.
Blocked here: Binance USD-M futures. That is the exchange's rule for this location, not a fault.
```

## Settings worth knowing

`account_usd`, `max_loss_per_trade_usd`, `max_daily_loss_usd`,
`max_trades_per_day`, `max_open_positions`, `min_reward_risk`,
`max_cost_r` (0.2: the most fees and slippage may take from a stop-out, in
R; 0 turns the rule off), `max_leverage` (3), `margin_mode` (isolated), `allow_cross_margin` (false),
`min_liq_buffer` (2.0: liquidation at least twice as far as the stop),
`cooldown_after_losses` (2) and `cooldown_hours` (12), `paper` (true),
`live_venue`, `display_tz` ("+11:00" or "Australia/Melbourne"), `session`
for opening ranges (utc, tokyo, london, ny), `min_quote_volume_usd`
(20 million), `min_listing_days` (14), `exclude`.

## Rules

- Never ask for, print, store or echo API keys. `doctor` only says whether
  the environment variables exist.
- Never loosen a risk setting the user did not ask to loosen.
- Say the venue in use whenever a result depends on it.
