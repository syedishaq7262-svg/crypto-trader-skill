"""crypto-coin: the card is complete, clean and honest on every venue."""

import json
import unittest

from _load import cc, Session

COIN = ("crypto-coin", "coin")


class Card(unittest.TestCase):
    def test_card_on_every_venue(self):
        for venue in ("binance", "bybit", "hyperliquid"):
            with Session() as s:
                code, out, err = s.run(*COIN, ["SOL", "--venue", venue])
                self.assertEqual(code, 0, venue + err)
                for line in ("Price ", "Range ", "Trend ", "Momentum ", "Volatility ", "Levels ", "Liquidity ",
                             "Funding ", "Open int. ", "Market ", "7 days ", "Setups now "):
                    self.assertIn(line, out, venue)
                self.assertIn("Context, not a call", out)
                for word in ("buy now", "sell now", "bullish", "bearish", "will rise", "will fall"):
                    self.assertNotIn(word, out.lower())

    def test_live_setups_show_their_backtest(self):
        with Session() as s:
            code, out, err = s.run(*COIN, ["SOL", "--venue", "binance"])
            self.assertIn("Backtest   UTC-day opening-range breakout longs: -0.37R a trade after costs over 2,195 trades; "
                          "within the cost rule -0.06R over 745 (can't be told from zero)", out)
            self.assertIn("(the walk-forward backtest: Binance, 30 Apr to 27 Oct 2026)", out)

    def test_json_has_what_the_planner_needs(self):
        with Session(config={"session": "ny"}) as s:
            code, out, err = s.run(*COIN, ["orbx", "--venue", "binance", "--json"])
            d = json.loads(out)
            for k in ("price", "high_24h", "low_24h", "support", "resistance", "atr_1h_pct", "atr_1d_pct",
                      "funding", "open_interest", "book", "trend", "rsi", "setups", "notes"):
                self.assertIn(k, d)
            self.assertTrue(any(h["setup"] == "orb" and h["side"] == "long" for h in d["setups"]))
            self.assertTrue(all(x > d["price"] for x in d["resistance"]))
            self.assertTrue(all(x < d["price"] for x in d["support"]))
            self.assertIsNotNone(cc.read_json(s.path("coins", "ORBXUSDT.json")))

    def test_priced_per_thousand_is_said_out_loud(self):
        with Session() as s:
            code, out, err = s.run(*COIN, ["bonk", "--venue", "hyperliquid"])
            self.assertIn("kBONK", out)
            self.assertIn("Priced per 1,000 coins", out)

    def test_new_listing_and_crowding_notes(self):
        with Session() as s:
            code, out, err = s.run(*COIN, ["NEWX", "--venue", "binance"])
            self.assertIn("Listed 3d", out)
            code, out, err = s.run(*COIN, ["FUNDX", "--venue", "binance"])
            self.assertIn("crowded longs", out)
            self.assertIn("top 5% of the last 30 days", out)

    def test_unknown_coin(self):
        with Session() as s:
            code, out, err = s.run(*COIN, ["doge2", "--venue", "binance"])
            self.assertEqual(code, 1)
            self.assertIn("Close matches: DOGE", err)

    def test_a_missing_section_is_listed_never_invented(self):
        with Session() as s:
            v = cc.get_venue("binance")
            orig = v.orderbook
            mod = __import__("_load").load(*COIN)
            def broken(*a, **k):
                raise cc.NetError("could not reach fapi.binance.com: timed out")
            v.orderbook = broken
            d = mod.build(v, "SOL", cc.load_config())
            self.assertIn("book", d["errors"])
            self.assertNotIn("book", d)

    def test_headlines_macro_and_notices_on_the_card(self):
        with Session() as s:
            code, out, err = s.run(*COIN, ["SOL", "--venue", "binance"])
            self.assertIn('News       2 headlines in 24h; latest: "SOL Climbs as Solana Funds Draw Fresh Inflows" (CoinDesk', out)
            self.assertIn("Macro      US CPI m/m in 2h45m", out)
            code, out, err = s.run(*COIN, ["THINX", "--venue", "binance"])
            self.assertIn("News       no headline naming THINX in 24h", out)
            self.assertIn("! Bybit announced a delisting", out)
            code, out, err = s.run(*COIN, ["SOL", "--venue", "binance", "--no-news", "--json"])
            d = json.loads(out)
            self.assertNotIn("news", d)
            self.assertNotIn("macro", d)

    def test_a_dead_calendar_costs_one_quick_try(self):
        for mode in ("macro_down", "macro_ratelimit"):
            with Session(mode=mode) as s:
                code, out, err = s.run(*COIN, ["SOL", "--venue", "binance"])
                self.assertEqual(code, 0, err)
                self.assertIn("Macro      the calendar could not be read just now", out)
                self.assertNotIn("Missing    macro", out)
                asked = cc.HTTP.stats["by_host"]["nfs.faireconomy.media"]["requests"]
                self.assertEqual(asked, 1, mode)                                  # asked once: no retries
                code, out, err = s.run(*COIN, ["ETH", "--venue", "binance"])
                self.assertIn("the calendar could not be read just now", out)
                self.assertNotIn("nfs.faireconomy.media", cc.HTTP.stats["by_host"])   # remembered: not asked again

    def test_btc_card_has_no_btc_comparison(self):
        with Session() as s:
            code, out, err = s.run(*COIN, ["btc", "--venue", "bybit", "--json"])
            d = json.loads(out)
            self.assertNotIn("beta", d)


if __name__ == "__main__":
    unittest.main()
