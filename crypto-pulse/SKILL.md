---
name: crypto-pulse
description: >-
  Market Pulse for crypto perpetual futures: what is moving today and what is
  behind it, on one screen, free and with no key. The market's mood (BTC, how
  many coins are up, median funding, Fear & Greed), the next big US releases,
  the coins up and down the most in 24 hours with their volume against a
  usual day, the change in open interest and what it says (new longs, short
  covering, new shorts, long unwinding, crowded trades), funding, the
  headlines naming them from seven newsrooms, volume surges, the coins most
  in the news, new listings and Bybit's listing and delisting notices. Ends
  with the pack's backtest record for chasing moves, because what is moving
  is not a reason to buy it. Use when the user asks "what's hot", "what's
  moving", "what's pumping", "top gainers", "biggest losers", "why is X
  pumping", "why is X dumping", "what's happening in crypto", "market today",
  "market pulse", "anything new listed", or wants a quick look at the market
  before the Scout or the Scanner.
argument-hint: "[--top 60] [--n 5] [--no-news] [--venue binance|bybit|hyperliquid]"
---

# crypto-pulse

The front page of the pack. One run answers "what is going on today?": what
moved most, whether new money came in behind it or old positions left, who
is paying to hold, what the newsrooms are saying, what is new on the
exchange, and what big US releases are coming.

It shows what is moving, not what to buy. Chasing moves is what most people
do with a list like this, and in the pack's own backtest it lost money after
costs, so the pulse ends by saying so, with the numbers.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/pulse.py"               # the 60 busiest perpetuals -> crypto/pulse.json
python3 "${CLAUDE_SKILL_DIR}/pulse.py" --top 100     # more coins
python3 "${CLAUDE_SKILL_DIR}/pulse.py" --n 8         # longer lists
python3 "${CLAUDE_SKILL_DIR}/pulse.py" --no-news     # prices only: no headlines, calendar or notices
```

Flags: `--top` (default 60; 30 on Hyperliquid, which allows fewer requests),
`--n` (coins in each list, default 5), `--no-news`, `--venue`, `--now`,
`--offline`, `--record`, `--json`, `--no-write`, `--stats`.

## What it reads and writes

| source | what |
| --- | --- |
| exchange data | every perpetual's 24h ticker; each coin's last 9 daily candles; open-interest history for the coins it lists |
| `crypto-news` (installed next to this skill) | seven newsrooms' headlines, the US macro calendar and Bybit's listing notices, cached like `/crypto-news` caches them |
| alternative.me | the Fear & Greed index |
| `crypto-setup/track_record.json` | the backtest's record for the setups that chase a move |
| `crypto/pulse.json` | everything it found, for the other skills and for follow-up questions |

## How it works

- **The coins.** The busiest perpetuals that pass your config's filters
  (`min_quote_volume_usd`, `min_listing_days`, `exclude`): moves in thin
  coins are noise.
- **Mood.** BTC's price, 24h and 7-day change; how many of the coins rose
  in 24 hours; the median funding of the 20 busiest, in yearly terms; Fear &
  Greed now and a week ago. `/crypto-derivs regime` has the full weather.
- **Calendar.** High-impact US releases from 2 hours ago to 48 hours ahead.
- **Up and down most.** The biggest 24-hour moves each way, with:
  - *volume vs usual*: the last 24 hours' volume over the average of the 7
    whole days before; 2x or more is a surge;
  - *open int. 24h*: the change in open interest, where the venue keeps it
    (Binance and Bybit; Hyperliquid does not);
  - *what it says*, from price against open interest and funding, the same
    rules as `/crypto-derivs`: new longs (up, open interest up), short
    covering (up, open interest down), new shorts (down, open interest up),
    long unwinding (down, open interest down), or a crowded trade. Blank
    when the moves are too small to read.
- **Headlines naming them.** Stories from the last 24 hours that name the
  coin or its ticker, each story once, newest first. A headline next to a
  move is not proof it caused the move.
- **Volume surges** not already listed, **crowded trades** (funding and open
  interest), **the coins most in the news**, **new listings** on this venue
  (the last 14 days) and **Bybit's notices** of the last 72 hours.
- **The chase record.** The walk-forward backtest's results for buying
  volume spikes and breakouts, and shorting them, after costs.

## Steps

1. Run `pulse.py`. It takes a few seconds.
2. Tell the user, briefly: the mood line, the calendar, the top movers each
   way with what open interest says about them, the headlines that name
   them, then anything crowded, new or noticed. Keep it to what stands out.
3. End with the closing lines as printed: what is moving is not a reason to
   buy it, and the backtest's numbers for chasing.
4. If the user wants to act on a mover, do not treat the list as a pick.
   Offer `/crypto-coin` for that coin's full card, `/crypto-idealab` to test
   the chase as an idea first, or `/crypto-scout` for setups with records.
   Any trade still goes through the plan, the checks and the user's typed
   yes.

## Example

From the pack's offline test market (synthetic prices and synthetic
headlines, not real data):

```
Market Pulse | Binance USD-M futures | 2026-10-21 15:15 UTC | the 26 busiest perpetuals
Mood: BTC 79,051.7, -0.9% in 24h, +9.2% in 7 days; 10 of the 26 coins up in 24h; median funding +5% a year (longs pay shorts); Fear & Greed 64, Greed (52 a week ago).
Calendar: US CPI m/m in 2h45m; US FOMC Statement in 1d 3h.

Up most in 24h
  coin     price    24h  volume vs usual  open int. 24h  funding/yr  what it says
  LINK    19.304  +7.2%             0.9x          +6.9%        +39%  new longs
  ARB     1.0176  +6.0%             1.1x          +4.6%        +34%
  INJ     25.689  +4.5%             1.0x          +5.1%        +27%  new longs
  BRKX    2.9638  +3.0%             1.0x          -2.6%        +19%
  SHRTX  0.11885  +2.5%             0.9x         +30.1%        -68%  crowded shorts

Down most in 24h
  coin    price    24h  volume vs usual  open int. 24h  funding/yr  what it says
  SOL   171.809  -7.4%             1.1x          -9.9%        -26%  long unwinding
  BONK  0.02543  -5.1%             0.9x          -4.1%        -29%
  RSIX  0.67150  -3.5%             1.1x          -5.6%         -9%  long unwinding
  TIA    5.0068  -3.4%             0.9x          +4.1%         -9%
  SUI    2.3589  -3.3%             1.0x          -1.1%         -7%

Headlines naming them, last 24h
  ARB    "Hackers Drain $40 Million From Lending Protocol on Arbitrum" (Cointelegraph, 1h00m ago)
  BRKX   "BRKX Breaks Out as Exchange Lists New Perpetual" (Cointelegraph, 3h00m ago)
  SOL    "SOL Climbs as Solana Funds Draw Fresh Inflows" (CoinDesk, 35m ago) +1 more
  No headline names LINK, INJ, SHRTX, BONK, RSIX, TIA, SUI: a move with no news is often flows, liquidations or a thin book.

Volume far above a usual day: none beyond the coins above.
Crowded: FUNDX, crowded longs (funding +104% a year, open interest +27.4% in 24h, price -0.4%). Longs pay to hold while price goes nowhere. A drop can cascade through their stops (squeeze-down risk).
Crowded: SHRTX, crowded shorts (funding -68% a year, open interest +30.1% in 24h, price +2.5%). Shorts pay to hold while open interest grows. A push up can force them to buy (short-squeeze fuel).
Most in the news, 24h (5 of 7 newsrooms read): BTC 2 stories, SOL 2 stories, ARB 1 story, BRKX 1 story, DOGE 1 story.
New on Binance: NEWX, listed 3d 0h ago, +4.8% in 24h, $269.3M traded.
Exchange notices (Bybit, last 72h): Delisting of THINXUSDT Perpetual Contract (2h00m ago); New Listing: NEWXUSDT Perpetual Contract, with up to 25x leve... (5h00m ago).

What is moving is not a reason to buy it. In the pack's walk-forward backtest (Binance, 30 Apr to 27 Oct 2026), after costs: buying volume spikes -0.05R a trade over 4,494 (can't be told from zero); buying range breakouts -0.10R a trade over 4,453; shorting volume spikes down -0.17R a trade over 4,833; shorting breakdowns -0.23R a trade over 3,534.
To test a chase of your own before risking anything: /crypto-idealab, for example change(24) > 10 on 1h bars (up 10% in a day).
```

Read it like this: LINK and INJ rose on new money (open interest up with
price), SOL fell as longs left, and SHRTX is up while shorts pile in and pay
68% a year to stay: fuel if it keeps rising. ARB's headline is about a hack
on Arbitrum while ARB went up: a headline that names a coin is not always
the reason it moved. Nothing here says which way any of them goes next.

## Rules

- Report what the run printed. Never add a coin, a reason or a headline it
  did not show.
- Say "a headline naming it", never "because of", unless the user asks what
  the headline says and it plainly describes the move.
- Never call a mover a buy, a sell or a pick. Repeat the closing lines.
- The coins are the busiest ones that pass the filters; a quieter coin
  moving a lot is not on the list by design.

## What it never does

It never places an order, never ranks coins by what to buy, never uses a key,
and never reads an account.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
