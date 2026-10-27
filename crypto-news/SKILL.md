---
name: crypto-news
description: >-
  News Monitor for crypto day trading. Before a chart becomes a trade, checks
  what could move it: headlines from the big crypto outlets (CoinDesk,
  Cointelegraph, Decrypt, The Block, CryptoSlate, Bitcoin Magazine,
  BeInCrypto), tagged by coin and by kind (hack, depeg, outage, delisting,
  listing, unlock, regulatory, ETF, macro, liquidations), the same story
  from several outlets folded into one; Bybit's listing and delisting
  notices; the US economic calendar (CPI, FOMC, jobs, PCE) with a warning
  when a release lands inside a planned or open trade; and how far each
  watched coin has moved against its usual hour and day, so a big move with
  no story stands out. Shows only what is new since the last check. Use when
  the user says "any news on SOL", "why is BTC moving", "what's the news",
  "morning briefing", "is there CPI today", "when is the Fed", "any
  delistings", "check the news before I trade", or before /crypto-entry and
  /crypto-exit.
argument-hint: "[--coins BTC,SOL] [--hours 24] [--all] | macro | notices"
---

# crypto-news

A chart can look perfect an hour before a CPI print, an exchange hack or a
delisting notice. This skill reads the sources that carry those first and
tells you only what is new: the outlets' public RSS feeds, Bybit's public
announcements, and the week's economic calendar. It also prints how far each
coin you care about has moved against its own usual hour and day, so a
headline and a move sit side by side, and a move with no headline gets said
out loud. It reads and reports. It never trades on a headline.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                       # your coins (plan, positions, watchlist), market, macro, notices
python3 "${CLAUDE_SKILL_DIR}/news.py" --coins BTC,SOL       # these coins
python3 "${CLAUDE_SKILL_DIR}/news.py" --hours 6 --all       # the last 6 hours, including items already shown
python3 "${CLAUDE_SKILL_DIR}/news.py" macro                 # this week's US high-impact releases
python3 "${CLAUDE_SKILL_DIR}/news.py" macro --countries USD,EUR,CNY --impact medium
python3 "${CLAUDE_SKILL_DIR}/news.py" notices --days 14     # Bybit listings and delistings
```

Flags: `--coins`, `--hours` (default 24), `--all`, `--max N` (market
headlines, default 8), `--countries` (default USD), `--impact high|medium`,
`--days` (macro and notices), `--no-mark` (leave the seen list alone),
`--venue`, `--now`, `--offline`, `--record`, `--json`, `--no-write`. Exit
code 0 when something is new, 1 when nothing is.

## What it reads and writes

| source / file | what |
| --- | --- |
| outlet RSS and Atom feeds | CoinDesk `https://www.coindesk.com/arc/outboundfeeds/rss/`, Cointelegraph `https://cointelegraph.com/rss`, Decrypt `https://decrypt.co/feed`, The Block `https://www.theblock.co/rss.xml`, CryptoSlate `https://cryptoslate.com/feed/`, Bitcoin Magazine `https://bitcoinmagazine.com/feed`, BeInCrypto `https://beincrypto.com/feed/`. `news_feeds` in the config replaces the list. Each is read at most once every 5 minutes |
| Bybit announcements API | `GET /v5/announcements/index`, types `delistings` and `new_crypto`: public, no key. Read every 10 minutes at most. Stock and ETF ("TradFi") listings and promotions such as token splashes are left out |
| economic calendar | the Forex Factory weekly feed (`nfs.faireconomy.media`): free and unofficial, covers the current week, read once an hour at most |
| exchange data | 1-hour candles of each watched coin, for its move against its usual hour and day |
| `crypto/plan.json`, `crypto/positions.json`, `crypto/entry.json`, `crypto/scout.json`, `crypto/watchlist.json` | which coins to watch, and the time limits a macro release could land inside |
| `crypto/news.json` | everything from the last check, machine readable |
| `crypto/state/news_seen.json` | what has been shown (kept 7 days), so each item shows once |

News, the calendar and the notices are extras: each source gets one quick
try (10 seconds), and a source that just failed is not asked again for 10
minutes. A slow website never holds up a price, a plan or an order. When a
source is down, the last good copy (up to 7 days old) is shown with its
age, or the line says the source could not be read.

## How it reads a headline

- **Coins.** A coin's name ("Solana", "Ether", "Bitcoin Cash"), a ticker
  with a dollar sign (`$OP`), or a ticker of three letters or more in
  capitals (`SOL`), only for coins with a perpetual on the venue. Acronyms
  (SEC, ETF, CPI, USD), all-caps headlines and names that are also ordinary
  words (Near, Optimism, Stellar) are not read as coins.
- **Kinds.** hack, depeg, outage, delisting, listing, unlock, regulatory,
  ETF, macro, liquidations, network, deal, treasury, whale, opinion. A `!`
  marks a hack, a depeg, an outage, a delisting or an unlock, and anything
  regulatory about a coin you watch. Opinion pieces ("price prediction")
  never fill the market list.
- **One story, one line.** The same story from several outlets within 12
  hours shows once, with "+2 more".
- **Moves.** MOVING when the last hour moved 3x the coin's usual hour (and
  at least 1%), or the last 24 hours moved 2x its usual day (and at least 3%),
  measured on its last 8 days.
- **Safety.** A feed with a DOCTYPE is refused (entity tricks), anything over
  5 MB is skipped, and one source failing never stops the others.

## Steps

1. Run `news.py` (or `--coins` for specific coins). Exit code 1 means
   nothing new since the last check.
2. Read the `!` items first, then the macro calendar: a high-impact US
   release in the next hours, especially one that lands inside a planned or
   open trade, comes before any setup.
3. Write a short brief: per coin, what happened, the outlet, the move, and
   one line on why it matters to the trade (for example: "CPI lands in 2h45m,
   inside the plan's time limit; a 2% candle can jump the stop"). When a coin
   has no news, say so plainly. A big move with no headline is worth saying
   too.
4. Before telling the user something happened, open the story's link and
   read it. Never summarise a story you could not open: say so.
5. Then `/crypto-entry` or `/crypto-exit`. Never act on a headline alone.

During a session: `/loop 15m /crypto-news` shows only what is new each time.

## Example

From the pack's offline test feeds: made-up headlines in the outlets' real
feed formats, a made-up calendar, synthetic prices. None of it is real news.
`news.py --coins SOL,THINX,WIF` while a SOL long plan is open:

```
News | 2026-10-21 15:15 UTC | the last 24h

Macro calendar (USD, high impact, next 48h):
  12:15 UTC     out 3h00m ago USD Core PCE Price Index m/m (forecast 0.2%, previous 0.3%)
  18:00 UTC     in 2h45m      USD CPI m/m (forecast 0.3%, previous 0.4%)
    ! lands inside your SOL long plan (out by 2026-10-21 23:15 UTC): a big candle can jump a stop.
  Thu 18:15 UTC in 1d 3h      USD FOMC Statement

Exchange notices (Bybit):
  ! 2h00m ago Delisting of THINXUSDT Perpetual Contract [THINX]
    5h00m ago New Listing: NEWXUSDT Perpetual Contract, with up to 25x leverage [NEWX]

Your coins:
  SOL (asked)  -7.5% 24h, -2.4% 1h  MOVING: the last hour moved 4.5x its usual hour
    35m ago   SOL Climbs as Solana Funds Draw Fresh Inflows [ETF]  (CoinDesk)
    1h10m ago SOL Price Prediction: Could Solana Hit $300 This Month? [opinion]  (BeInCrypto)
  THINX (asked)  +2.1% 24h, -0.2% 1h
    no new headline in the last 24h
    ! an exchange is delisting it: positions get closed at the delisting time.
  WIF (asked)  -0.3% 24h, +4.9% 1h  MOVING: the last hour moved 5.3x its usual hour
    no new headline in the last 24h: a move with no story is worth saying out loud

Market:
  ! 1h00m ago Hackers Drain $40 Million From Lending Protocol on Arbitrum [hack] ARB  (Cointelegraph)
    50m ago   Dogecoin Whales Move $200 Million to Exchanges [whale] DOGE  (Decrypt)
    1h15m ago THE NEW ERA FOR CRYPTO IS HERE AND ONE BIG TOP IS IN  (BeInCrypto)
    1h30m ago Strategy Buys Another 5,000 BTC [treasury] BTC  (Bitcoin Magazine)
    2h10m ago Bitcoin Dips as Traders Brace for Fed Rate Decision [macro] BTC  (Cointelegraph +1 more)
    3h00m ago BRKX Breaks Out as Exchange Lists New Perpetual [listing] BRKX  (Cointelegraph)
    4h00m ago SEC Sues Crypto Lender Over Unregistered Securities [regulatory]  (Decrypt)

Read 5 of 7 feeds (The Block: www.theblock.co answered HTTP 403; CryptoSlate: the feed has a DOCTYPE, so it was not read (a safety rule)). 9 new items.
Headlines are leads, not facts: open the story before acting on one.
```

`news.py macro`:

```
Macro calendar | 2026-10-21 15:15 UTC | USD, high impact | the feed covers this week
  released 3h00m ago  2026-10-21 12:15 UTC  USD Core PCE Price Index m/m  forecast 0.2%, previous 0.3%
  in 2h45m            2026-10-21 18:00 UTC  USD CPI m/m  forecast 0.3%, previous 0.4%
  in 1d 3h            2026-10-22 18:15 UTC  USD FOMC Statement
Unofficial feed: check the time on the agency's own calendar before trading around it.
```

## Rules

- Headlines are leads, not facts. Open the story before repeating it, quote
  the outlet and give the link.
- Never invent or embellish a story, a quote, a number or a release time.
  The calendar feed is unofficial: for a trade timed around a release, check
  the time on the agency's own calendar.
- No predictions. Report what was published and how far the price moved.
  Never say what a coin will do because of a headline.
- A delisting of a coin with an open position comes first, before anything else.

## What it never does

It never trades on news, never tells the user to buy or sell because of a
headline, never hammers a source (each one is cached), and never parses a
feed it does not trust.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
