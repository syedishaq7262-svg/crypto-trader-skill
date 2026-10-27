---
name: crypto-whales
description: >-
  Whale Watch for crypto perpetual futures, from Hyperliquid's public chain,
  where every trader's positions are visible. Reads the leaderboard (the
  best traders this day, week, month or all time, with their account size
  and whether they are up across week, month and all time), shows who among
  the top traders holds a coin right now (how many long and short, how big,
  at what entry and leverage, and how many are in profit), one wallet's
  positions and latest fills, and what the wallets the user follows opened,
  closed, added to, cut or flipped since the last look. Context, not a
  signal: the skill says so every time. Use when the user says "what are
  the whales doing", "who is long BTC", "smart money on SOL", "top traders
  on Hyperliquid", "show me this wallet", "follow this trader", "what did
  the whales change", or wants positioning context before a trade.
argument-hint: "top [--window month] | coin <COIN> | wallet <0x...> | follow <0x...> [--name X] | changes"
---

# crypto-whales

On Hyperliquid every account is public: its positions, its entries, its
fills. This skill reads that for the traders at the top of the leaderboard
and for wallets the user chooses to follow. It answers "who is long, who is
short, how big" for a coin, and "what did they change" for a wallet.

It is context, not a signal, and the skill says so in every answer. These
traders entered earlier and at other prices, with other sizes and time
frames, and some hedge on other exchanges. The leaderboard only shows the
ones still winning (the losers drop off it), and the biggest accounts
include market makers. Copying a position without its plan is a different
trade.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/whales.py" top                          # best this month, accounts over $100K
python3 "${CLAUDE_SKILL_DIR}/whales.py" top --window allTime --min-account 1000000 --n 30
python3 "${CLAUDE_SKILL_DIR}/whales.py" coin BTC                     # who among the top 50 holds BTC ($10K or more)
python3 "${CLAUDE_SKILL_DIR}/whales.py" wallet 0xABC...              # positions and the last 7 days of fills
python3 "${CLAUDE_SKILL_DIR}/whales.py" follow 0xABC... --name ace   # unfollow, following
python3 "${CLAUDE_SKILL_DIR}/whales.py" changes                      # opened, closed, added to, cut, flipped
```

Flags: `--window day|week|month|allTime` (default month), `--min-account`
(USD, default 100,000), `--n` (top: 20, coin: 50), `--days` (wallet fills,
default 7), `--name`, `--now`, `--offline`, `--record`, `--json`,
`--no-write`. `changes` exits 0 when something changed, 1 when nothing did.

## What it reads and writes

| source / file | what |
| --- | --- |
| `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` | the public leaderboard (the one the Hyperliquid site shows): account value and profit, return and volume by window. Large; read at most every 6 hours and cached |
| `https://api.hyperliquid.xyz/info` | `clearinghouseState` (a wallet's open positions, entries, leverage, liquidation price, unrealized profit) and `userFillsByTime` (its fills). Public, no key |
| `crypto/cache/hl_leaderboard.json`, `crypto/cache/hl_state/` | the cached leaderboard (top rows only) and wallet states (2 minutes) |
| `crypto/whales.json` | the last `coin` answer |
| `crypto/whales_follow.json`, `crypto/state/whale_snaps.json` | followed wallets and their last positions, for `changes` |

## How it reads them

- **Top traders.** Ranked by profit in the window, among accounts over
  `--min-account` with a profit in that window. "steady" marks a trader up
  over the week, the month and all time.
- **A coin.** The top `--n` traders' open positions in that coin: how many
  are long and short, the size on each side, the share long by size, the
  median leverage, how many are in profit at the mark price, and the biggest
  holders with entry, unrealized profit, leverage and liquidation price.
  Hyperliquid's own funding rate is shown with it. Builder-deployed markets
  (names like `dex:XYZ`) and spot are left out.
- **Changes.** A position counts as added to or cut when its size moved by a
  fifth or more; smaller changes are noise. The first look at a wallet only
  saves its positions.
- **Odd data.** Rows the leaderboard sends without a valid address, account
  value or profit history are skipped, never guessed.

## Steps

1. For positioning on a coin: `whales.py coin COIN`. Read back the counts,
   the sizes, the share long by size and how many are in profit, then name
   the two or three biggest holders with their entries and leverage.
2. Say what it does not show: why they hold it, their stops, their other
   positions elsewhere. Then say it is context, not a signal.
3. For a trader: `wallet 0x...`, and `follow` it if the user wants updates.
   `changes` on a `/loop` (every 10 to 30 minutes) shows what they do.
4. If the user wants to trade the same way, that is a new plan:
   `/crypto-exit` sets its own stop and target, `/crypto-timemachine` tests
   it, `/crypto-budget` sizes it to the user's limits, not the whale's.

## Example

From the pack's offline test market (synthetic wallets, not real traders):

```
Whales on BTC | the top 12 traders by month profit (accounts over $100.0K) | 2026-10-21 15:15 UTC
4 of 12 hold it: 3 long ($5.2M), 1 short ($2.0M). By size, 72% long. Median leverage 7.5x.
Mark 79,051.7: 4 of 4 are in profit on it.
rank  trader        side      size     entry  unrealized  leverage      liquidation
   1  steady-eddie  long     $4.1M  77,470.7      $82.0K  5x cross      65,075.4
   2  0x0000..3232  short    $2.0M  80,632.8      $40.0K  10x isolated  87,083.4
   3  night-owl     long   $900.0K  77,470.7      $18.0K  4x cross      61,976.6
   5  0x0000..fd7d  long   $150.0K  77,470.7       $3.0K  20x isolated  74,371.9
Funding on Hyperliquid +4.3% a year: longs pay.
Context, not a signal: they entered at other prices, with other sizes and time frames, and some hedge elsewhere.
```

## Rules

- Never call a whale's position a reason to buy or sell. Never say "smart
  money is long, so go long".
- Report sizes, entries and counts as printed. Never guess a trader's
  intent, plan, identity or other holdings.
- An address is public data, but treat it as a pseudonym: never try to find
  out who is behind it.
- Copying needs its own plan, its own stop and its own size, through the
  same chain as any other trade.

## What it never does

It never places or copies an order, never follows a wallet the user did not
ask to follow, and never presents leaderboard profit as proof of skill.

## Safety

- Education and tooling, not financial advice. The pack does not know the user's situation or the future. Most people who day trade lose money, and leverage makes losses arrive faster.
- Paper trading by default (`"paper": true`): simulated fills at live prices against the real order book. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except crypto-broker, and only when all of these hold: crypto-risk passed that exact order, the user typed an explicit yes for that one order in the chat, and, for a live order, the user's own keys are in environment variables. Never ask for keys in the chat.
- Every order carries a stop. Isolated margin and a 3x leverage cap by default; cross margin stays off unless the user turns it on.
- Claude reports what the tools print. It never invents a price, a level, a probability or a funding rate, and never calls a setup a sure thing.
