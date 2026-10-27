---
name: crypto-entry
description: >-
  Entry Timer for crypto perpetual futures. Watches a setup bar by bar and
  says the moment to get in: the first CLOSED bar that confirms it, by the
  scanner's own rules. Opening-range breakouts after the UTC, Tokyo, London
  or New York open, range breakouts, trend pullbacks, volume spikes, RSI
  reversals, a squeeze breaking out of its bands, or a price level the user
  gives (--above, --below). Says waiting and how far, triggered with the
  time and price, stale (chasing is a different trade) or expired, for one
  coin or several, and writes crypto/entry.json for /crypto-exit. It never
  places an order. Use when the user says "tell me when to get in", "watch
  BTC", "has it broken out", "did it break the opening range", "entry
  signal", "time my entry", "alert me when ETH closes above 3,000", "wait
  for the squeeze to break", or after /crypto-scanner or /crypto-scout,
  before /crypto-exit.
argument-hint: "check|watch <COIN[,COIN]> [--setup orb|breakout|squeeze|trend|reversal|volume] [--above P | --below P]"
---

# crypto-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. A
price that pokes through a level in the middle of a bar is not an entry. A
bar that CLOSES through it is. The timer reads closed bars only, applies the
scanner's own rule to them, and says what it sees: still forming, waiting
(and how far away), triggered (the time and the price), stale (it fired
earlier) or expired. On a trigger it writes the hit down for the Exit
Planner and stops. It never buys or sells anything. Day trading loses most
people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check BTC                          # opening range after the open in your config
python3 "${CLAUDE_SKILL_DIR}/entry.py" check BTC,ETH,SOL --session ny     # several coins, the New York open
python3 "${CLAUDE_SKILL_DIR}/entry.py" check SQZX --setup squeeze         # the first close outside the squeezed bands
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ETH --setup breakout --side long
python3 "${CLAUDE_SKILL_DIR}/entry.py" check BTC --above 70000            # your level, 5-minute closes
python3 "${CLAUDE_SKILL_DIR}/entry.py" check SOL --below 140 --tf 15m --rvol 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch BTC --session ny --for 5h    # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch BTC --session ny --now "2026-10-21 13:20"   # replay a past session, no waiting
```

Flags: `--setup orb|breakout|squeeze|trend|reversal|volume` (default `orb`),
`--side long|short|both`, `--session utc|tokyo|london|ny` (default from the
config), `--or-minutes N` (default 30), `--above PRICE`, `--below PRICE`,
`--tf 5m|15m|1h` (for a level, default 5m), `--rvol X` (for a level: the
volume the crossing close needs, as a multiple of the median bar), `--for 4h`
and `--max-checks N` (watch), `--venue`, `--now`, `--offline`, `--record`,
`--json`, `--no-write`.

Exit codes: `check` 0 when something triggered, 1 otherwise. `watch` 0 on a
trigger, 2 when nothing is left to wait for (expired, already triggered
earlier, no setup), 3 when `--for` or `--max-checks` ran out first.

## What it reads and writes

| file | what |
| --- | --- |
| exchange data | closed 5-minute bars (opening range, levels), 15-minute bars built from them (breakout, trend, volume), 1-hour bars (reversal, squeeze, trend averages) |
| `crypto/config.json` | `session` (which open the range follows), `allow_short` |
| `crypto/entry.json` | on a trigger: the hit in the scanner's format (setup, side, trigger price and time, the setup's stop reference, the thresholds used), read by `/crypto-exit` for an hour |

## The rules

The trigger is the rule's first closed bar. The time is that bar's CLOSE
(the 14:15 five-minute bar closes at 14:20); the price is its close.

| setup | bars | triggers when |
| --- | --- | --- |
| `orb` | 5m | the first close beyond the opening range (the first 30 minutes after the open), inside the 4-hour entry window, on 1.5x the median volume of the 4 hours before the open. With both sides, the first side to break wins; a later break the other way is reported as a failed breakout. If the first close beyond is too thin, that side has no trade this session |
| `breakout` | 15m | a fresh close beyond the high or low of the last 24 hours on 1.5x volume (the bar before was still inside) |
| `trend` | 15m | 1-hour 20/50/200 averages stacked, a pullback to the 15-minute 20 average, a close back over it |
| `volume` | 15m | 4x the usual volume on a move of at least one ATR |
| `reversal` | 1h | 1-hour RSI at 25 or under (75 or over) in the last 4 hours, then a turn of 5 points |
| `squeeze` | 1h | the first close outside the Bollinger bands after the band width sat in the lowest 10% of 30 days in the 12 hours before. The stop reference is the middle band |
| `--above` / `--below` | 5m (`--tf`) | the first close beyond your level after a close on the near side; with `--rvol`, that crossing close needs the volume or it does not count |

**What it says.** `FORMING`: the opening range is still being drawn, or the
session has not opened yet (it says when, and skips New York weekends).
`WAITING`: the level or condition, how far the last close is from it, and
until when. `TRIGGERED`: the bar's time, close and volume; this is the
moment, if the user takes the trade. `STALE`: it fired earlier (it says
when, and where the price is now); chasing it is a worse entry for the same
stop. `EXPIRED`: the window closed with no trade. `NONE`: the setup is not
on for this coin (no squeeze, no stacked trend, a level already crossed for
the whole record).

**Watching.** `watch` wakes a few seconds after each bar closes, prints one
line per bar, and exits on the trigger (writing `crypto/entry.json`), when
nothing is left to wait for, or when `--for` runs out. With the clock pinned
(`--now`), it replays closed bars one at a time without waiting: a way to see
how it would have called a past session.

## Steps

1. Get the coin and the setup from the user, or from `/crypto-scout` or
   `/crypto-scanner` (a coiled squeeze, a coin near its range, the opening
   range before the open). Check `/crypto-news` for the coin first.
2. Run `entry.py check COIN --setup NAME`. Tell the user the status in one
   line: forming until when, waiting and how far, triggered at what time and
   price, stale since when, or expired.
3. To wait for it, Claude can run `entry.py watch COIN --setup NAME --for 4h`
   in the background and read the result when it exits. Keep the computer
   awake: a sleeping laptop pauses the watch, and it catches up late (it says
   how late).
4. When it triggers, say the time and the price, and that nothing was
   placed. If it is stale, say how old it is and how far the price has
   moved: a late entry is a different trade.
5. Hand off to `/crypto-exit COIN` to set the stop and the target BEFORE
   anything is bought; it picks up `crypto/entry.json`.

## Example

From the pack's offline test market (synthetic prices, not real data):
replaying the New York open on ORBX, a coin built to break out.

```
Watching ORBX (orb) on Binance USD-M futures from 2026-10-21 14:00 UTC, for up to 4h00m. Replaying closed bars on the pinned clock.
14:00 UTC  WAITING   ORBX: Waiting for a 5-minute close above 21.255 or below 21.002 on 1.5x volume, until 17:30 UTC. Last close 21.108 at 14:00 UTC: 0.70% under the high, 0.50% over the low.
14:05 UTC  WAITING   ORBX: Waiting for a 5-minute close above 21.255 or below 21.002 on 1.5x volume, until 17:30 UTC. Last close 21.129 at 14:05 UTC: 0.60% under the high, 0.60% over the low.
14:10 UTC  WAITING   ORBX: Waiting for a 5-minute close above 21.255 or below 21.002 on 1.5x volume, until 17:30 UTC. Last close 21.150 at 14:10 UTC: 0.50% under the high, 0.70% over the low.
14:15 UTC  WAITING   ORBX: Waiting for a 5-minute close above 21.255 or below 21.002 on 1.5x volume, until 17:30 UTC. Last close 21.171 at 14:15 UTC: 0.40% under the high, 0.80% over the low.
14:20 UTC  TRIGGERED ORBX: At 14:20 UTC the 5-minute bar closed at 21.404, above the range high 21.255, on 4.8x volume.
TRIGGERED | ORBX | New York open 13:30 UTC, 30-minute range | 2026-10-21 14:20 UTC
  At 14:20 UTC the 5-minute bar closed at 21.404, above the range high 21.255, on 4.8x volume.
  This is the moment to get in, if you take the trade. Nothing was placed.
  Saved crypto/entry.json. Next: /crypto-exit ORBX sets the stop and target BEFORE anything is bought.
```

The same check 55 minutes later:

```
STALE | ORBX | New York open 13:30 UTC, 30-minute range | 2026-10-21 15:15 UTC
  Triggered 55m ago: at 14:20 UTC the 5-minute bar closed at 21.404, above the range high 21.255, on 4.8x volume. The last close is 21.469 (+0.30%), still beyond the range.
  A late entry is a different trade: a worse price for the same stop.
```

## Rules

- Only closed bars count. Never call an entry early because a bar looks like
  it will close through.
- One trigger per setup per session for the opening range: the first close
  beyond. Later closes do not reset it.
- A signal is information, not an order and not a promise.
- Never move the level after the fact to make a trigger appear.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/crypto-exit`), never counts a bar that has not closed, and never runs
`/crypto-broker` from a background watch: nobody is there to type yes.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
