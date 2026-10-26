---
name: crypto-broker
description: >-
  Order executor for crypto perpetual futures, and the only skill in the pack
  that can place an order. Shows the order as a plain ticket with a
  fingerprint, and places it only after crypto-risk passed that exact order
  in the last 15 minutes AND the user typed an explicit yes for it in the
  chat. Paper by default: fills at live prices against the real order book
  and plays stops, targets, time limits, funding and liquidation forward on
  real 1-minute candles (no testnet). Live on Binance USD-M and Bybit with
  the user's own keys from environment variables: isolated margin, one-way
  mode, the stop on the exchange (mark price, reduce-only) the moment the
  entry fills, and the position closed at once if the stop cannot go on.
  Also syncs, lists positions, closes, tightens stops and shows the account.
  Use when the user says "place it", "buy it", "short it", "go", "yes" to a
  ticket, "take the trade", "close my SOL", "move my stop", "what is open",
  "how are my trades doing", "my balance", or after /crypto-risk passes.
argument-hint: "ticket | place --yes <FINGERPRINT> [--live] | sync | positions | close <COIN> --yes <COIN> | move-stop <COIN> <PRICE> | account"
---

# crypto-broker

Everything before this skill is a plan. This one acts on it, so it is
built to be hard to misuse. It places one order at a time, the one the user
just said yes to, exactly as crypto-risk checked it, with the stop going on
the exchange the moment the entry fills. Paper trading is the default and it
is not a toy: fills walk the live order book, and the stop, the target, the
time limit, funding and liquidation play out on real 1-minute candles. Day
trading loses most people money, and a broker that works well does not
change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/broker.py" ticket                    # the order in plain words, READY or NOT READY
python3 "${CLAUDE_SKILL_DIR}/broker.py" place --yes 1d24d046101b0622   # only after the user typed yes
python3 "${CLAUDE_SKILL_DIR}/broker.py" place --yes 1d24d046101b0622 --live   # real money: config live AND this flag
python3 "${CLAUDE_SKILL_DIR}/broker.py" sync                      # paper: play forward; live: reconcile with the exchange
python3 "${CLAUDE_SKILL_DIR}/broker.py" positions                 # P&L, R now, distance to stop, target, liquidation
python3 "${CLAUDE_SKILL_DIR}/broker.py" close SOL --yes SOL       # close at the market (exits are always allowed)
python3 "${CLAUDE_SKILL_DIR}/broker.py" move-stop SOL 170.90      # tighten a stop; it refuses to loosen one
python3 "${CLAUDE_SKILL_DIR}/broker.py" account                   # balance, realized P&L, margin in use
```

Flags: `--order N` (which order in `crypto/orders.json`, default 1),
`--yes`, `--live`, `--now`, `--json` (ticket, positions, account). Exit code 0 when it did
what was asked, 1 with one plain line when it refused or failed.

## What it reads and writes

| file | what |
| --- | --- |
| `crypto/orders.json` | the sized order with its risk stamp (from `/crypto-budget` and `/crypto-risk`); marked placed once it is |
| `crypto/config.json` | `paper` (true by default), `allow_cross_margin` (false), fees |
| environment | `BINANCE_API_KEY` and `BINANCE_API_SECRET`, or `BYBIT_API_KEY` and `BYBIT_API_SECRET`, for live orders only |
| `crypto/positions.json` | open positions and resting limit orders |
| `crypto/ledger.jsonl` | every open, close (P&L in dollars and R, the reason), stop move and cancel; `/crypto-journal` and `/crypto-risk` read it |
| `crypto/broker_log.jsonl` | every exchange answer the broker acted on, for checking later. Never keys |

## The yes

1. `broker.py ticket` prints the order: buy or sell short, size, leverage,
   margin, the stop and what it loses, the target and what it makes, the time
   limit, the liquidation estimate, warnings, and READY with a fingerprint,
   or NOT READY with the reason.
2. Claude shows the ticket and asks the user for an explicit yes for this
   order. "Sounds good" about a plan is not a yes to an order. A yes from
   earlier in the conversation is not a yes to this ticket.
3. Only then: `broker.py place --yes <fingerprint>`. It refuses when crypto-risk
   has not passed this order, the order changed after the check (the
   fingerprint covers venue, symbol, side, size, type, prices, leverage and
   margin mode), the check is over 15 minutes old, the price has run more
   than half the risk since, the price is past the stop, or a position on
   the same coin is already open.

## Paper

Market entries fill against the live order book, level by level, so a big
order pays the spread and the depth it eats. Limit entries rest until a
1-minute candle trades through them, or expire at the time limit. `sync`
walks 1-minute candles since the last sync: funding at each payment, then
liquidation if it sits closer than the stop, a stop (at the stop, or at the
open if the candle gapped past it), the target, and the time limit. A candle
that touches both the stop and the target counts as the stop.

## Live

Going live is the user's decision, made in their own config: `"paper": false`.
Every live order then also needs `--live` on the command line. Keys come from
environment variables only. Hyperliquid stays paper only (its orders need a
wallet signature this pack does not do).

| | Binance USD-M | Bybit (V5, linear) |
| --- | --- | --- |
| account checks | one-way position mode, no open position on the coin | isolated margin mode (unless `allow_cross_margin`), one-way, no open position |
| set first | isolated margin and the leverage, per symbol | the leverage |
| entry | market only | market or limit |
| stop | the moment the entry fills: a STOP_MARKET through the Algo Order API, reduce-only, triggered by the mark price | attached to the entry: market, triggered by the mark price |
| target | a reduce-only limit | attached: a limit at the target |
| if the stop is not on | the position is closed at once and recorded; the order is never placed again | the same |

If an order's answer is lost on the way back (a timeout, a server error),
the broker asks the exchange what happened instead of guessing: a fill gets
its stop, a request that never arrived is reported as nothing open. If the
exchange cannot be asked either, it says DANGER and tells the user to check
the app, because a position may be open without a stop.

`sync` on a live account records closes (by the stop, at the target, by hand
or another order) with P&L from the exchange's own fills and funding, cancels
whatever was left resting, warns when an open position has no stop, and says
when the time limit has passed. It never closes a live position on its own:
it asks.

**Keys.** Create API keys with futures trading on and withdrawals OFF, and an
IP restriction if the exchange offers one. Put them in the environment of the
shell that runs Claude (for example in `~/.zshrc`), never in the chat and
never in a file in this folder. The broker never prints them.

## Steps

1. Run `ticket`. If NOT READY, say why and the fix (usually: run
   `/crypto-risk` again).
2. Show the ticket and ask: "Place this order? Type yes to place it." Wait.
3. On an explicit yes, run `place --yes <fingerprint>` (add `--live` only if
   the config is live and the user asked for a real order). Report the fill,
   the stop and the target as printed.
4. Later: `positions` for how it is doing, `sync` to update (paper) or
   reconcile (live), `/crypto-alerts` to watch the stop, the target and the
   liquidation distance.
5. Closing: `close COIN --yes COIN` after the user asks to close. Moving a
   stop: only toward the price (`move-stop`), never away.

## Example

From the pack's offline test market (synthetic prices, not real data), paper:

```
Ticket #1 | PAPER | Binance SOLUSDT
  Buy 4.551 SOL at the market (about 171.809), 2x isolated, about $390.95 of margin.
  Stop-loss 169.800: a stop-market, reduce-only, triggered by the mark price. Lose about $10.00 there.
  Take-profit 175.827: a reduce-only limit. Make about $17.70 there.
  Out by 2026-10-21 23:15 UTC, win or lose. Estimated liquidation 88.152 (48.7% away).
READY. Fingerprint 1d24d046101b0622. The risk check is good until 15:30 UTC.
```

After the user typed yes:

```
Placed (paper): long 4.551 SOL filled at 171.810. Stop 169.800, target 175.827, 2x isolated, out by 2026-10-21 23:15 UTC.
```

A sync four hours later:

```
SOL long closed at 175.827 by the target: $17.88 (+1.95R).
Synced 2026-10-21 19:00 UTC: 0 open, 0 waiting to fill.
```

## Rules

- One order, one yes. Never place an order the user has not said yes to in
  this conversation, never reuse an old yes, never place from a background
  job, a loop, an alert or a scheduled run: nobody is there to say yes.
- Never loosen a stop, never remove one, never turn `paper` off, and never
  add `--live` unless the user's own config is live and they asked.
- Exits are always allowed: closing a position or tightening a stop needs no
  risk check.
- Report fills, P&L and errors exactly as printed.

## What it never does

It never asks for, prints or stores keys, never places an order without the
fingerprint of a passed risk check and the user's yes, never trades
Hyperliquid live, never uses cross margin unless the user turned it on, and
never leaves a live position without trying to put a stop on it.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
