---
name: crypto-alerts
description: >-
  Price Alerts for crypto perpetual futures. Rings when a level, a stop, a
  target, a time limit or a macro release is reached, each alert once. Turns
  the trade plan into its stop, target and time-limit alerts in one go, and
  every open position into stop, target, time limit, past-the-stop (the
  stop order may be missing) and near-liquidation alerts. Also levels above
  or below, a percent move from now or inside a window, funding above or
  below a yearly rate, a clock time, and a warning minutes before each
  high-impact US release. A level counts as hit when any 1-minute candle
  since the last check touched it, so a wick between two checks is not
  missed. Use when the user says "alert me when", "tell me if BTC hits",
  "watch my stop", "watch my trade", "ping me before CPI", "warn me if
  funding goes crazy", "get me a price", or right after /crypto-exit or
  /crypto-broker.
argument-hint: "add [COIN] [--above P | --below P | --pct X | --from-plan | --from-positions | --macro 15] | check | watch | list"
---

# crypto-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart, around the clock, because crypto never closes. An alert is
information, never an order: when a stop alert rings, nothing is sold by
this skill. The stop order on the exchange (or the paper broker) does the
work; the alert says what happened and what to check.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan              # the plan's stop, target and time limit
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-positions         # every open position, including past-stop and near-liquidation
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add BTC --above 70000        # or --below; --rearm to ring again after a pullback
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add SOL --pct 3              # 3% either way from the price now (--direction up|down)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add ETH --pct 2 --within 1h  # 2% inside any hour
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add BTC --funding-above 50   # funding above 50% a year (--funding-below -20)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --macro 15               # 15 minutes before each high-impact US release
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --in 2h --label "check the trade"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                        # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" watch --for 8h               # checks every minute, exits on the first ring
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset btc-above-70000        # or remove ID, clear (--all)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote BTC,SOL                # last, mark, 24h change, funding, volume
```

Flags: `--label`, `--rearm`, `--expires 7d` (manual rules), `--at "2026-10-21 18:00"`,
`--countries USD,EUR` and `--impact high|medium` (with `--macro`), `--every 60`,
`--for 8h`, `--keep` and `--max-checks N` (watch), `--venue`, `--now`,
`--json`, `--no-write`. Exit codes: `check` 0 when something rang, 1 when
nothing did; `watch` 0 on a ring, 2 when nothing is left to watch, 3 when
`--for` runs out.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/plan.json` | the plan from /crypto-exit, for `--from-plan` |
| `crypto/positions.json` | open positions from /crypto-broker, for `--from-positions`; a position's alerts retire when it closes |
| exchange data | one ticker read per venue per check (last and mark price, funding), 1-minute candles since the last check |
| `crypto/cache/macro.json` | the economic calendar crypto-news keeps (needed for `--macro`) |
| `crypto/alerts.json` | the rules, which rang and when |
| `crypto/alerts_log.jsonl` | every ring, with its message |

## The rules

| rule | rings when | again |
| --- | --- | --- |
| `--above` / `--below` | any 1-minute candle since the last check touched the level, or the price is there now | with `--rearm`, after the price is 0.25% back on the other side |
| `--pct X` | the price moved X% from where it was when the rule was set (candle highs and lows count) | `--rearm` |
| `--pct X --within 1h` | the move over the last hour (or any window) is X% or more | after it calms down |
| `--funding-above` / `--funding-below` | the yearly funding rate crosses the level | `--rearm` |
| `--at` / `--in` | the clock passes the time (it says how late if no check ran sooner) | never |
| `--macro N` | N minutes before each high-impact release in the calendar, once per release | every release |
| plan | the stop, the target, the time limit; when the stop or the target rings, the plan's other alerts go quiet | never |
| position | stop, target, time limit; **past the stop**: the mark price is a quarter of the risk past the stop and the position is still open, so the stop order may be missing; **near liquidation**: under a quarter of the distance from the entry to the liquidation price is left | never |

Manual rules expire after 7 days unless `--expires` says otherwise. Plan
rules expire at the plan's time limit.

## Steps

1. Right after /crypto-exit writes a plan, run `add --from-plan`; right after
   /crypto-broker places an order, run `add --from-positions`. Confirm every
   new rule back to the user: coin, level, what it is for.
2. To keep watching, Claude can run `alerts.py watch --for 8h` in the
   background, or `alerts.py check` on a `/loop` or a scheduled task every
   few minutes. Each check reads the latest candles, so a wick between two
   checks still counts.
3. Tell the user what rang in one line each, exactly as printed. A stop or
   target alert means the order on the exchange has probably filled: run
   `/crypto-broker sync`. A past-the-stop alert is urgent: the stop may not
   be on the exchange. A time-limit alert means close what is still open:
   `/crypto-broker close COIN --yes COIN` after the user's yes. Never act
   for the user.

## Example

From the pack's offline test market (synthetic prices, made-up calendar,
not real data): the SOL long from /crypto-broker, placed at 15:15 with stop
169.800, target 175.827 and out by 23:15.

```
$ alerts.py add --from-plan
Added sol-plan-4ed5-stop: SOL below 169.800 (the plan's stop), until 2026-10-21 23:15 UTC.
Added sol-plan-4ed5-target: SOL above 175.827 (the plan's target), until 2026-10-21 23:15 UTC.
Added sol-plan-4ed5-time: at 2026-10-21 23:15 UTC: the plan's time limit: out, win or lose.
alerts.py check rings them; alerts.py watch keeps checking. Alerts never place or close anything.
$ alerts.py add --macro 15
Added macro-15: 15 minutes before each USD high-impact release, until 2026-10-28 15:15 UTC.
alerts.py check rings them; alerts.py watch keeps checking. Alerts never place or close anything.
```

A check at 17:00, and the one at 17:50:

```
Nothing rang at 2026-10-21 17:00 UTC. Watching 4 rules: SOL below 169.800 (1.70% away); SOL above 175.827 (1.79% away).
```

```
ALERT  SOL reached 175.827 (the plan's target): touched 175.868 at 17:47 UTC; now 175.365. If the take-profit order is on the exchange it has probably filled: /crypto-broker sync.
ALERT  USD CPI m/m at 2026-10-21 18:00 UTC, in 10m: a big candle can come with it.
```

The price touched the target at 17:47 and fell back before the check; the
1-minute candle still counts. The plan's stop and time-limit alerts went
quiet with it.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- A check reads the exchange's latest candles; between checks nothing is
  watched, and a level can be touched and left again (the next check still
  sees the candle).
- Confirm every new rule back to the user.

## What it never does

It never places, cancels or changes an order, never claims to be a live
feed, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
