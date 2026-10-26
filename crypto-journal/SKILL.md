---
name: crypto-journal
description: >-
  Trading Journal for crypto perpetual futures. Turns every closed trade
  (paper and live, recorded by /crypto-broker, or imported read-only from
  Binance or Bybit) into an honest record: win rate with its 95% range, the
  break-even win rate at the user's own average win and loss, average R and
  dollars per trade, profit factor, average win and loss, max drawdown,
  losing streaks, and what fees and funding cost. Breaks it down by setup,
  coin, side, exit and hour, and flags patterns worth a look: trades opened
  right after a loss, losers held longer than winners, exits by hand, losses
  past the stop. Notes can be added to any trade. Use when the user says
  "how am I doing", "my stats", "review my trades", "what's my win rate",
  "which setup works for me", "journal this trade", "am I revenge trading",
  "import my Bybit trades", or at the end of a day or a week.
argument-hint: "[--days 30] [--paper|--live] [--setup X] | trades | note \"text\" | import binance|bybit"
---

# crypto-journal

The journal reads the trade log and says what it shows, with the honesty a
small sample needs. Twelve trades at a 42% win rate could come from a real
win rate anywhere between 19% and 68%, so every win rate comes with its 95%
range, and the break-even win rate at the user's own average win and loss
sits next to it. It also looks for the habits that cost day traders the
most: jumping back in right after a loss, letting losers run longer than
winners, closing by hand ahead of the plan.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/journal.py"                          # last 30 days, paper and live
python3 "${CLAUDE_SKILL_DIR}/journal.py" --days 0 --live          # all time, live only (--paper for paper)
python3 "${CLAUDE_SKILL_DIR}/journal.py" --setup orb --coin SOL   # one setup, one coin
python3 "${CLAUDE_SKILL_DIR}/journal.py" trades                   # the latest trades, one line each, with notes
python3 "${CLAUDE_SKILL_DIR}/journal.py" note "chased the entry"  # on the last closed trade (--trade ID for another)
python3 "${CLAUDE_SKILL_DIR}/journal.py" import bybit --days 30   # closed trades made on the exchange itself
```

Flags: `--days N` (default 30, 0 for all time), `--since "2026-10-01"`,
`--paper`, `--live`, `--setup`, `--coin`, `--recent N`, `--trade ID`,
`--now`, `--json`, `--no-write`. Exit code 0 when there are trades in the
window, 1 when there are none.

## What it reads and writes

| file / source | what |
| --- | --- |
| `crypto/ledger.jsonl` | every open and close /crypto-broker recorded: price, size, fees, funding, P&L in dollars and in R, the exit reason, the setup |
| `crypto/imported.jsonl` | trades read from the exchange with `import`; a trade the broker already recorded is counted once |
| `crypto/journal_notes.jsonl` | the user's notes, by trade |
| Bybit `GET /v5/position/closed-pnl` | `import bybit`: closed positions, 7 days a call (fees included in Bybit's figure) |
| Binance `GET /fapi/v1/income` and `/fapi/v1/userTrades` | `import binance`: realized profit, commissions and funding, grouped into closes; the side from the closing fill |
| `crypto/journal.json` | the last report's numbers, for other skills |

Imports are read-only: a key with reading rights only is enough, and nothing
is changed on the exchange. Keys come from the same environment variables as
the broker's, never from the chat. Imported trades have no planned stop, so
they count in dollars, not in R.

## What it measures

- **Win rate** with a Wilson 95% range. Under 20 trades it says the sample
  is too small to judge; under 50 it says the range is wide.
- **Break-even win rate** at the average win and loss in R, and whether the
  win rate is clearly above it, clearly under it, or still undecided.
- **Average R a trade** (the expectancy) and dollars a trade, total, **profit
  factor** (dollars won over dollars lost), average win and loss, best and
  worst trade.
- **Max drawdown** of the running total, in dollars and R, with its dates;
  the longest losing streak and the current streak.
- **Costs**: fees and funding, and their share of the profit before costs.
- **Breakdowns** by setup, coin, side, exit (stop, target, time limit, by
  hand), the hour the trade was opened (in `display_tz`), and paper or live.
- **Worth a look**: trades opened within 30 minutes of a loss (and how they
  did against the rest), losers held 1.5 times longer than winners, stops
  moved, closes by hand, losses bigger than 1.25R.

## Steps

1. Run `journal.py` (add `--live` when the user means real money). Read back
   the win rate WITH its range and the break-even line, the average R, the
   drawdown, and the costs.
2. Read the "Worth a look" lines as questions, not verdicts: "three of your
   trades came within half an hour of a loss, and they did worse; was that
   on purpose?"
3. For breakdowns, say how many trades sit behind each number. A setup with
   three trades has no record yet.
4. After a trade, offer `note` so the user can write down what they saw and
   felt; the notes come back in `trades`.
5. Never turn the journal into a forecast. A good month says little about the
   next one at these sample sizes.

## Example

From a made-up ledger of 12 paper trades:

```
Journal | paper and live | all time | 2026-10-21 15:15 UTC
12 trades: 5 won, 7 lost. Win rate 42% (95% range 19% to 68%).
  12 trades are too few to judge a win rate or an edge: the range above is how wide the truth could be.
Average +0.23R a trade, +$2.33 a trade; total +$28.00. Profit factor 1.39.
Average win +1.98R (+$19.80), average loss -1.01R (-$10.14). Best +$20.00 SOL, worst -$11.00 BTC.
Break-even at your average win and loss: 34%. Your win rate is above it, and the range includes it.
Max drawdown -$30.00 (-3.0R), 2026-10-10 to 2026-10-12. Longest losing streak 3; now 1 loss in a row.
Costs: fees $6.00, funding $1.20: 20% of the profit before costs.

By setup:              orb 5 (3 won) +0.78R | breakout 3 (0 won) -1.00R | manual 2 (0 won) -1.05R | trend 2 (2 won) +2.00R
By exit:               stop 7 (0 won) -1.01R | target 5 (5 won) +1.98R

Worth a look:
  ! Losers were held 3.0x longer than winners (3h00m against 1h00m).
```

## Rules

- Always give the trade count and the range with a win rate.
- Call a pattern a pattern, never a diagnosis of the user.
- The journal's numbers are the ledger's. Never round a loss away or leave a
  trade out to make a period look better.

## What it never does

It never places or changes an order, never edits the ledger, never writes
to an exchange account, and never predicts results.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
