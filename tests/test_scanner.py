"""crypto-scanner on the fake exchange: it must find every coin shaped on purpose, on every venue,
and say plainly when it cannot run a setup."""

import json
import unittest

from _load import cc, Session, WED

SCAN = ("crypto-scanner", "scan")


def hits_of(s):
    return cc.read_json(s.path("watchlist.json"))["hits"]


def find(hits, coin, setup, side=None):
    return [h for h in hits if h["coin"] == coin and h["setup"] == setup and (side is None or h["side"] == side)]


class PlantedSetups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {}
        for venue in ("binance", "bybit", "hyperliquid"):
            with Session() as s:
                code, out, err = s.run(*SCAN, ["--venue", venue, "--session", "ny", "--now", WED])
                cls.results[venue] = (code, out, err, hits_of(s))

    def test_runs_everywhere(self):
        for venue, (code, out, err, hits) in self.results.items():
            self.assertEqual(code, 0, venue + err)
            self.assertIn("Scan |", out)
            self.assertIn("not a list of coins to buy", out)

    def test_orb_long_trigger_bar(self):
        for venue, (_, _, _, hits) in self.results.items():
            h = find(hits, "ORBX", "orb", "long")
            self.assertEqual(len(h), 1, venue)
            self.assertEqual(h[0]["at_utc"], "2026-10-21 14:20 UTC")       # the 14:15 bar closed at 14:20
            self.assertEqual(h[0]["status"], "holding")
            self.assertGreater(h[0]["rvol"], 2.0)
            m = h[0]["metrics"]
            self.assertLess(m["or_low"], m["or_high"])
            self.assertGreater(h[0]["trigger"], m["or_high"])

    def test_breakouts_both_ways(self):
        for venue, (_, _, _, hits) in self.results.items():
            up = find(hits, "BRKX", "breakout", "long")
            dn = find(hits, "BRKD", "breakout", "short")
            self.assertEqual(len(up), 1, venue)
            self.assertEqual(len(dn), 1, venue)
            self.assertEqual(up[0]["status"], "fresh")
            self.assertGreater(up[0]["trigger"], up[0]["level"])
            self.assertLess(dn[0]["trigger"], dn[0]["level"])

    def test_squeeze_trend_reversal_volume(self):
        for venue, (_, _, _, hits) in self.results.items():
            self.assertTrue(find(hits, "SQZX", "squeeze"), venue)
            self.assertTrue(find(hits, "TRDX", "trend", "long"), venue)
            rv = find(hits, "RSIX", "reversal", "long")
            self.assertTrue(rv, venue)
            self.assertLessEqual(rv[0]["metrics"]["rsi_min"], 25)
            self.assertGreaterEqual(rv[0]["metrics"]["rsi_now"], 30)
            vs = find(hits, "VOLX", "volume", "long")
            self.assertTrue(vs, venue)
            self.assertGreaterEqual(vs[0]["rvol"], 4.0)

    def test_a_reclaim_far_past_the_average_is_extended(self):
        from _load import load
        scan = load(*SCAN)
        with Session():
            v = cc.get_venue("binance")
            m = v.market("TRDX")
            x = scan.Ctx(m, {}, v.bars(m["symbol"], "5m", 900), v.bars(m["symbol"], "1h", 260))
            hits = scan.setup_trend(x, scan.args_from({}), ["long"])
            self.assertEqual([h["status"] for h in hits], ["reclaimed"])
            self.assertLessEqual(hits[0]["metrics"]["beyond_atr"], scan.TREND_MAX_ATR)
            x.atr15 = x.atr15 / 50.0                       # the same close, now far past the average in ATR terms
            hits = scan.setup_trend(x, scan.args_from({}), ["long"])
            self.assertEqual([h["status"] for h in hits], ["extended"])

    def test_relative_strength(self):
        for venue, (_, _, _, hits) in self.results.items():
            rs = [h for h in hits if h["setup"] == "rs"]
            self.assertTrue(rs, venue)
            self.assertFalse(find(hits, "BTC", "rs"))
            for h in rs:
                ex = h["metrics"]["excess_24h"]
                self.assertGreaterEqual(ex if h["side"] == "long" else -ex, 0.03, venue)
                self.assertEqual(h["status"], "leader" if h["side"] == "long" else "laggard")
                expected = h["metrics"]["move_24h"] - h["metrics"]["beta"] * h["metrics"]["btc_24h"]
                self.assertAlmostEqual(ex, expected, places=2)

    def test_universe_leaves_out_the_new_the_thin_and_the_stable(self):
        for venue, (_, _, _, hits) in self.results.items():
            coins = set(h["coin"] for h in hits)
            self.assertNotIn("NEWX", coins)
            self.assertNotIn("THINX", coins)
            self.assertNotIn("USDC", coins)

    def test_every_hit_has_the_fields_the_next_skills_read(self):
        for venue, (_, _, _, hits) in self.results.items():
            for h in hits:
                for k in ("symbol", "coin", "setup", "side", "status", "trigger", "stop_ref", "at", "metrics"):
                    self.assertIn(k, h)
                self.assertIn(h["side"], ("long", "short", "both"))


class Behaviour(unittest.TestCase):
    def test_new_york_range_is_skipped_at_weekends(self):
        with Session(now="2026-10-24 16:00") as s:
            code, out, err = s.run(*SCAN, ["--venue", "binance", "--setups", "orb", "--session", "ny"])
            self.assertIn("US stocks do not open at weekends", out)
            self.assertEqual(code, 1)

    def test_range_still_forming(self):
        with Session(now="2026-10-21 13:50") as s:
            code, out, err = s.run(*SCAN, ["--venue", "binance", "--setups", "orb", "--session", "ny"])
            self.assertIn("still forming", out)

    def test_symbols_and_side_filters(self):
        with Session() as s:
            code, out, err = s.run(*SCAN, ["--venue", "binance", "--symbols", "ORBX,BRKD", "--side", "long",
                                           "--setups", "orb,breakout", "--session", "ny"])
            hits = hits_of(s)
            self.assertTrue(all(h["side"] == "long" for h in hits))
            self.assertEqual(set(h["coin"] for h in hits), {"ORBX"})

    def test_universe_command(self):
        with Session() as s:
            code, out, err = s.run(*SCAN, ["universe", "--venue", "bybit"])
            self.assertEqual(code, 0)
            u = cc.read_json(s.path("universe.json"))
            self.assertEqual(u["venue"], "bybit")
            self.assertEqual(u["coins"][0]["coin"], "BTC")
            self.assertIn("left out", out)

    def test_json_output_and_no_write(self):
        with Session() as s:
            code, out, err = s.run(*SCAN, ["--venue", "binance", "--setups", "volume", "--json", "--no-write"])
            d = json.loads(out)
            self.assertEqual(d["setups"], ["volume"])
            self.assertIsNone(cc.read_json(s.path("watchlist.json")))

    def test_bad_input_is_one_plain_line(self):
        with Session() as s:
            code, out, err = s.run(*SCAN, ["--venue", "binance", "--setups", "moonshot"])
            self.assertEqual(code, 1)
            self.assertTrue(err.startswith("Problem: unknown setup"))

    def test_geoblocked_auto_says_which_venue_it_used(self):
        with Session(mode="binance_geoblock") as s:
            code, out, err = s.run(*SCAN, ["--setups", "volume"])
            self.assertIn("Using Bybit linear perpetuals", out)
            self.assertEqual(cc.read_json(s.path("watchlist.json"))["venue"], "bybit")

    def test_a_coin_that_cannot_be_read_is_listed_not_guessed(self):
        with Session(mode="garbage") as s:
            code, out, err = s.run(*SCAN, ["--venue", "binance", "--setups", "volume,breakout"])
            d = cc.read_json(s.path("watchlist.json"))
            self.assertIn("ADAUSDT", [f["symbol"] for f in d["failed"]])
            self.assertIn("Could not read", out)


if __name__ == "__main__":
    unittest.main()
