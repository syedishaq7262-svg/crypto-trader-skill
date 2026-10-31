---
name: crypto-idealab
description: >-
  Idea Lab for crypto perpetual futures: tests a trading idea of the user's
  own on the past before any money is at risk. Claude turns the idea, said in
  plain words, into a rule the lab can read (for example crosses_above(ema(9),
  ema(21)) for a fast average crossing a slow one) and shows it for the user
  to confirm. The lab replays it walk-forward on real Binance, Bybit or
  Hyperliquid candles: read at each bar's close, in at the next open, one
  trade at a time per coin, out at the stop, target, time limit or an exit
  rule, after fees, slippage and the funding charged. Reports the average R
  with a 90% range before and after costs, both halves, the cost rule, a luck
  test against random entries with the same exits, how many rules were tried,
  and one plain verdict. Use when the user says "test my idea", "would this
  have worked", "backtest this rule", "what if I bought every time", "idea
  lab", "let's work on an idea", or describes a trading rule of their own.
argument-hint: "[test | check | words | list] --when \"rule\" --side long|short [--tf 1h] [--stop atr:2] [--target r:2] [--hold 24h] [--coins top:10] [--days 180] [--end DATE]"
---

# crypto-idealab

Everyone who trades has ideas: buy the dip in an uptrend, fade crowded
funding, ride a breakout on big volume. Almost nobody tests them honestly
before putting money on them, with fees, slippage and funding counted, on
enough trades to mean something, against what luck alone would have done.
The Idea Lab does that in seconds, and it is where the user and Claude work
on ideas together: the user brings the idea, Claude turns it into a rule,
the lab says what really happened.

Most ideas come out close to break-even after costs, or worse. That is the
useful answer: it is far cheaper to learn it here than in an account.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/lab.py" words                      # the words a rule can use
python3 "${CLAUDE_SKILL_DIR}/lab.py" check --when "rsi(14) < 30 and close > ema(200)"
python3 "${CLAUDE_SKILL_DIR}/lab.py" test --when "rsi(14) < 30 and close > ema(200)" --side long
python3 "${CLAUDE_SKILL_DIR}/lab.py" test --when "funding < -0.01" --side long --coins SOL,ETH --stop pct:3 --hold 3d
python3 "${CLAUDE_SKILL_DIR}/lab.py" test --idea crypto/ideas/ema-9-21-cross.json --end 2026-06-30   # the same idea, an earlier window
python3 "${CLAUDE_SKILL_DIR}/lab.py" list                       # every idea tested so far and how it did
```

Flags for `test`: `--when` (the entry rule), `--side long|short`, `--tf`
(bars: 5m, 15m, 1h by default, 4h, 1d), `--stop` (`atr:2` by default,
`pct:2`, `swing:10`), `--target` (`r:2` by default, `pct:3`, `none`),
`--hold` (time limit, `24h` by default, up to `14d`), `--exit-when` (an exit
rule), `--coins` (`top:10` by default, the busiest perpetuals, or a list like
`SOL,ETH`), `--days` (default by bars: 30 on 5m, 60 on 15m, 180 on 1h, 365
on 4h, 730 on 1d), `--end` (end the window earlier, for data the idea has
not seen), `--name`, `--idea FILE`, `--seed 19`, `--no-luck`, `--no-cache`,
`--venue`, `--json`, `--no-write`.

## What it reads and writes

| file | what |
| --- | --- |
| exchange data | candles at the rule's bar size, from before the window (so averages have settled) to after it (so the last trades can finish), the funding history, and each coin's order book today for slippage |
| `crypto/cache/lab/` | the candles and funding already downloaded; closed bars never change, so the next test only fetches what is missing |
| `crypto/ideas/<name>.json` | the idea, its rule and exits, the result and the verdict |
| `crypto/ideas/<name>-trades.csv` | every trade: times, entry, stop, target, exit, outcome, R after and before costs, funding |
| `crypto/ideas/tried.jsonl` | every distinct rule tested, once each: the count behind the luck warning |
| `crypto/idealab.json` | the last run in full |

## Turning words into a rule

A rule is a condition read at the close of each bar, written with these
words. Run `lab.py words` for the full list with examples.

| values | |
| --- | --- |
| `open` `high` `low` `close` (or `price`) `volume` | the bar's own |
| `hour`, `weekday` | when the bar opened, UTC (0 to 23; 0 Monday to 6 Sunday) |
| `funding` | the last funding rate charged, in percent as exchanges show it (`0.01` is 0.01%) |
| `btc_close` | BTC's close on the same bar |
| `day_open`, `prev_day_high`, `prev_day_low`, `prev_day_close` | today's open and yesterday's range (UTC days) |

| functions | |
| --- | --- |
| `sma(n)` `ema(n)` `rsi(n)` `stdev(n)` `change(n)` | on the close unless a value is put first: `ema(btc_close, 200)`, `sma(volume, 20)`; `change` is in percent |
| `highest(n)` `lowest(n)` | the high (low) of the n bars before this one, not counting it |
| `bb_upper(n, k)` `bb_lower(n, k)` `bb_width(n, k)` | Bollinger bands; the width in percent of the middle band |
| `macd(fast, slow)` `macd_signal(fast, slow, signal)` `macd_hist(...)` | MACD |
| `atr(n)`, `vol_ratio(n)` | this coin's average true range; this bar's volume over the n bars before |
| `ago(value, n)` | a value n bars ago |
| `crosses_above(a, b)` `crosses_below(a, b)` | true on the bar the cross happens |
| `abs` `min` `max` | as usual |

Join with `< <= > >= == !=`, `and`, `or`, `not`, `+ - * /` and brackets.
Nothing in a rule is ever run as code: the lab reads it word by word and
refuses anything else.

How common phrases translate (say which reading you chose when the user's
words were vague):

| the user says | rule |
| --- | --- |
| RSI under 30, oversold | `rsi(14) < 30` |
| above the 200 EMA, in an uptrend | `close > ema(200)` |
| golden cross | `crosses_above(sma(50), sma(200))` |
| breaks the 20-bar high | `close > highest(20)` |
| volume spike, 3x volume | `vol_ratio(20) > 3` |
| funding negative, shorts paying | `funding < 0` |
| dumped 5% in a day (on 1h bars) | `change(24) < -5` |
| while BTC is bullish | `btc_close > ema(btc_close, 200)` |
| in the US session | `hour >= 13 and hour < 21` |
| at weekends | `weekday >= 5` |
| breaks yesterday's high | `close > prev_day_high` |
| MACD crosses up | `crosses_above(macd(12, 26), macd_signal(12, 26, 9))` |
| tags the lower Bollinger band | `low < bb_lower(20, 2)` |

## How a test works

1. **No peeking.** The rule is read at each bar's close from that bar and
   the ones before it. Averages get their history before the window starts.
2. **In at the next open.** When the rule is true and no trade is open on
   that coin, a trade opens at the next bar's open, at the market. Signals
   while a trade is open are counted and skipped.
3. **Out, bar by bar.** The stop (`atr:2` is 2 ATR(14) from the entry,
   `pct:2` is 2%, `swing:10` is beyond the lowest low or highest high of the
   last 10 bars), the target (`r:2` is twice the risk, `pct:3` is 3%), the
   time limit, or the exit rule (read at a bar's close, out at the next
   open). A bar that touches both the stop and the target counts as the
   stop; a bar that opens past the stop fills at its open. A stop closer
   than 0.05% of the price is no stop, and the signal is skipped.
4. **Costs.** The exchange's fees (taker in, maker at the target, taker
   otherwise), slippage measured from each coin's order book today, and the
   funding really charged at each payment while the trade was open.
5. **The honest part.** The average R after costs with its 90% range, and
   before costs; both halves of the window; the trades within the cost rule
   (a stop-out costs at most `max_cost_r`, 0.2R) against the rest; 50
   random entries for each trade at the same hour of day on the same coin,
   with the same stop, target, time limit and exit rule, and the share of
   2,000 random histories that did as well as the rule (the luck test).
6. **The count.** Every distinct rule tested is noted once. Luck alone lets
   about 1 in 20 rules with no edge pass the luck test, so the report says
   how many rules have been tried.
7. **The verdict**, in this order: nothing to judge (no trades); too few
   trades (under 30); no edge (even the top of the range is below zero); no
   edge shown (the range spans zero); no edge from the rule (it made money
   but random entries with the same exits did about as well, luck matching
   it more than 5% of the time); not consistent (it beat luck but lost in
   one half); worth a paper trial, not money (it made money after costs,
   beat random entries and made money in both halves).

## Steps

1. Get the idea in the user's words. Ask only for what changes it: long or
   short, and anything about the bars, the exits or the coins they care
   about. Use the defaults for the rest and say so.
2. Translate it into a rule and run `lab.py check --when "..."`. Fix any
   problem it reports.
3. Show the user the rule and its exits in one or two plain lines and ask
   whether that is the idea. One question, then go.
4. Run `lab.py test` with the rule. It takes seconds, or a minute or two the
   first time a long window downloads.
5. Report, in this order: the trades and how they ended, the average after
   and before costs with what costs took, the halves, the cost-rule line,
   the luck-test lines, then the verdict line and the count line exactly as
   printed. Say the seed.
6. Working on the idea together: change one thing at a time, for a reason
   the user can say ("only when BTC is in an uptrend", "a wider stop because
   it kept getting stopped out"), not to fish for a better number. Each
   change is another rule tried, and the count says so. If a version comes
   out *worth a paper trial*, test it as it is, unchanged, on a window it has
   not seen (`--end` a few months back) and on other coins before anything
   else.
7. The lab never trades. If the user wants to try an idea, paper trade it:
   each time the rule fires, plan it with `/crypto-exit`, check it with
   `/crypto-timemachine`, size it with `/crypto-budget`, and send it through
   `/crypto-risk` and `/crypto-broker` on paper.

## Example

From the pack's offline test market (synthetic prices, not real data): a
fast average crossing a slow one, on 1-hour bars.

```
Idea Lab | EMA 9/21 cross | long | 1h bars | Binance USD-M futures | 8 coins | 21 Sep to 21 Oct 2026 (30 days)
Rule: go long at the next bar's open when  crosses_above(ema(9), ema(21))
Exits: stop 2 ATR(14) below the entry; target 2R; out after 24h.
Costs: fees 0.05% a side (0.02% at the target), slippage median 1.2 bps a side, measured from each coin's order book today, and the funding charged while each trade was open.

Trades: 118 on 8 coins. The rule was true 134 times; 14 came while a trade was open, 0 had no honest stop, 2 ran past the end of the data.
  target first 34, stopped 47, out at the time limit 37.
  Won (above zero after costs) 50.8% (95% range 41.9% to 59.7%).
  Average +0.27R a trade after costs (90% range +0.07R to +0.46R); +0.33R before costs, so costs took 0.06R.
  Profit factor 1.59. Funding -0.01R a trade on average. Deepest drawdown 25.5R; longest losing streak 16.
Halves: 21 Sep to 6 Oct +0.58R (58 trades); 6 Oct to 21 Oct -0.03R (60 trades).
Within the cost rule: every trade (no stop-out cost more than 0.2R).
Random entries at the same hours, with the same stop, target and exits (5,900): +0.17R a trade.
  The rule beat them by 0.09R a trade (90% range of the difference -0.10R to +0.28R); luck matched it 20% of the time.

By coin (after costs):
  coin  trades    won   avg R
  ETH       17  47.1%  +0.18R
  ADA       16  43.8%  +0.10R
  XRP       16  56.2%  +0.21R
  DOGE      15  53.3%  +0.62R
  LINK      15  46.7%  +0.02R
  BTC       14  42.9%  +0.40R
  SOL       13  53.8%  +0.15R
  SUI       12  66.7%  +0.52R

Verdict: no edge from the rule. It made +0.27R a trade after costs, but random entries at the same hours with the same exits did about as well (luck matched it 20% of the time): the exits or the market's direction did the work, not the rule.
This is the first rule tested in this lab.
The coins are today's; ones that faded or were delisted are missing. A test on the past with fake money, not a forecast. Seed 19.
Saved in your crypto folder: ideas/ema-9-21-cross.json, and every trade in ideas/ema-9-21-cross-trades.csv.
```

It made money, and a quick look would stop there. The luck test is why it
is not an edge: random entries at the same hours with the same stop and
target made +0.17R too, so the rising market and the 2R exit did the work,
and all of the profit came in the first half.

## Rules

- Show the rule before testing it, and say how vague words were read.
- Every number comes from the run. The verdict line and the count line are
  printed by the tool: repeat them, do not soften them.
- Never call a result an edge, a sure thing or proven. The best the lab says
  is *worth a paper trial, not money*.
- One change at a time, each for a reason. Never run variations in a loop
  looking for the best number: that finds luck, not an edge.
- Say the seed, so any run can be repeated.

## What it never does

It never places an order, never reads an account, never uses a bar from
after a signal to decide that signal, and never hides a losing result.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
