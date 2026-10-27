"""crypto-derivs: crowding, flow, cross-venue funding and the regime, on the fake exchange."""

import json
import unittest

from _load import cc, Session

DERIVS = ("crypto-derivs", "derivs")


class Radar(unittest.TestCase):
    def test_crowded_longs_and_shorts(self):
        for venue in ("binance", "bybit"):
            with Session() as s:
                code, out, err = s.run(*DERIVS, ["--venue", venue])
                self.assertEqual(code, 0, err)
                rows = dict((r["coin"], r) for r in cc.read_json(s.path("derivs.json"))["rows"])
                self.assertEqual(rows["FUNDX"]["crowding"], "crowded longs", venue)
                self.assertEqual(rows["SHRTX"]["crowding"], "crowded shorts", venue)
                self.assertGreater(rows["FUNDX"]["funding_apr"], 0.9)
                self.assertGreater(rows["FUNDX"]["oi_24h"], 0.10)
                self.assertIn("squeeze-down risk", out)
                self.assertIn("short-squeeze fuel", out)

    def test_cross_venue_spread(self):
        with Session() as s:
            s.run(*DERIVS, ["--venue", "binance"])
            sp = cc.read_json(s.path("derivs.json"))["spreads"]
            self.assertEqual(sp[0]["coin"], "FUNDX")
            self.assertEqual(set(sp[0]["aprs"]), {"binance", "bybit", "hyperliquid"})

    def test_hyperliquid_has_no_history_and_says_so(self):
        with Session() as s:
            code, out, err = s.run(*DERIVS, ["--venue", "hyperliquid"])
            self.assertEqual(code, 0, err)
            self.assertIn("keeps no open-interest history", out)
            rows = cc.read_json(s.path("derivs.json"))["rows"]
            self.assertTrue(all(r["oi_24h"] is None for r in rows))

    def test_flow_reads_funding(self):
        self.assertEqual(cc.flow(0.03, 0.10), "new longs")
        self.assertEqual(cc.flow(0.03, 0.10, apr=-0.4), "shorts adding into a rise")
        self.assertEqual(cc.flow(-0.03, 0.10, apr=0.5), "longs adding into a drop")
        self.assertEqual(cc.flow(-0.03, -0.10), "long unwinding")
        self.assertEqual(cc.flow(0.03, -0.10), "short covering")
        self.assertIsNone(cc.flow(0.005, 0.10))

    def test_crowding_thresholds(self):
        self.assertEqual(cc.crowding(0.35, 0.12, 0.65, 0.01), "crowded longs")
        self.assertIsNone(cc.crowding(0.35, 0.12, 0.65, 0.08))          # price ran: not stuck
        self.assertEqual(cc.crowding(-0.15, 0.12, 0.45, 0.02), "crowded shorts")
        self.assertEqual(cc.crowding(0.7, 0.0, None, 0.0), "hot funding")
        self.assertIsNone(cc.crowding(0.11, 0.0, 0.55, 0.0))


class OneCoin(unittest.TestCase):
    def test_coin_deep_dive(self):
        with Session() as s:
            code, out, err = s.run(*DERIVS, ["coin", "fundx", "--venue", "binance", "--json"])
            d = json.loads(out)
            self.assertEqual(d["crowding"], "crowded longs")
            self.assertGreaterEqual(d["funding"]["rank"], 0.95)
            self.assertEqual(d["funding"]["n"], 90)                      # 30 days of 8-hour payments
            self.assertIsNotNone(d["positioning"]["top_long_share"])
            code, out, err = s.run(*DERIVS, ["coin", "fundx", "--venue", "binance"])
            self.assertIn("Next payment in 45m", out)
            self.assertIn("Read: crowded longs", out)

    def test_four_hour_funding_is_annualised_correctly(self):
        with Session() as s:
            code, out, err = s.run(*DERIVS, ["coin", "WIF", "--venue", "binance", "--json"])
            d = json.loads(out)
            self.assertEqual(d["funding"]["hours"], 4)
            self.assertAlmostEqual(d["funding"]["apr"], d["funding"]["rate"] * 6 * 365)

    def test_missing_coin_argument(self):
        with Session() as s:
            code, out, err = s.run(*DERIVS, ["coin", "--venue", "binance"])
            self.assertEqual(code, 1)
            self.assertIn("which coin", err)


class Regime(unittest.TestCase):
    def test_regime_reads_and_writes(self):
        with Session() as s:
            code, out, err = s.run(*DERIVS, ["regime", "--venue", "bybit"])
            self.assertEqual(code, 0, err)
            r = cc.read_json(s.path("regime.json"))
            self.assertIn(r["label"], ("Risk-on", "Risk-off", "Mixed", "Overheated", "Fearful"))
            self.assertEqual(r["fng"], 64)
            self.assertIsNone(r["btc_ema50"])                  # the test market has 35 days, too few for a 50-day average,
            self.assertEqual(r["trend"], 1)                    # so the rising 20-day average decides the trend
            self.assertIn("uptrend (above a rising 20-day average)", out)
            self.assertIsNotNone(r["breadth"])
            self.assertIn("not a forecast", out)

    def test_regime_survives_fear_and_greed_being_down(self):
        with Session() as s:
            import os
            os.environ["FAKE_EXCHANGE_MODE"] = ""
            cc.reset_http()
            v = cc.get_venue("binance")
            orig = cc.fear_greed
            try:
                def boom(limit=31):
                    raise cc.NetError("could not reach api.alternative.me")
                cc.fear_greed = boom
                r = cc.market_regime(v)
            finally:
                cc.fear_greed = orig
            self.assertIsNone(r["fng"])
            self.assertIn("alternative.me", r["fng_error"])


if __name__ == "__main__":
    unittest.main()
