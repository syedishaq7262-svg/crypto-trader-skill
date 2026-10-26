"""crypto-liquidation: the price formula, the buffer, the odds, the blocks."""

import json
import unittest

from _load import cc, load, Session

LIQ = load("crypto-liquidation", "liq")
EXIT = ("crypto-exit", "exit")
SIZE = ("crypto-budget", "size")


class Formula(unittest.TestCase):
    def test_margin_equals_maintenance_at_the_liquidation_price(self):
        for side in ("long", "short"):
            for lev in (2, 5, 10, 25, 100):
                E, mmr, fee, q = 100.0, 0.005, 0.0005, 3.0
                P = cc.liq_price(E, side, lev, mmr, fee)
                margin = q * E / lev
                pnl = q * (P - E) if side == "long" else q * (E - P)
                self.assertAlmostEqual(margin + pnl, (mmr + fee) * q * P, places=9, msg="%s %dx" % (side, lev))

    def test_deduction_and_known_values(self):
        self.assertAlmostEqual(cc.liq_price(100.0, "long", 10, 0.005, 0.0005), 90.0 / 0.9945)
        self.assertAlmostEqual(cc.liq_price(100.0, "short", 10, 0.005, 0.0005), 110.0 / 1.0055)
        with_ded = cc.liq_price(100.0, "long", 10, 0.01, 0.0, deduction=50.0, qty=10.0)
        self.assertAlmostEqual(with_ded, (90.0 - 5.0) / 0.99)
        self.assertEqual(cc.liq_price(100.0, "long", 1, 0.005, 0.0005), 0.0)
        # margin under maintenance: liquidated at the entry, never a price on the wrong side
        self.assertTrue(cc.instant_liquidation(50, 0.025, 0.0005))
        self.assertEqual(cc.liq_price(100.0, "long", 50, 0.025, 0.0005), 100.0)
        self.assertEqual(cc.liq_price(100.0, "short", 50, 0.025, 0.0005), 100.0)

    def test_max_safe_leverage_is_the_edge(self):
        for stop in (99.5, 98.5, 95.0):
            L = cc.max_safe_leverage(100.0, stop, "long", 0.01, 0.0005, 2.0)
            d = lambda lev: (100.0 - cc.liq_price(100.0, "long", lev, 0.01, 0.0005)) / 100.0
            self.assertGreaterEqual(d(L), 2.0 * (100.0 - stop) / 100.0)
            self.assertLess(d(L + 1), 2.0 * (100.0 - stop) / 100.0)

    def test_adverse_moves(self):
        bars = [(i, 100, 100, 100, 100, 1, 1) for i in range(30)]
        bars[10] = (10, 100, 103, 90, 100, 1, 1)
        moves = LIQ.adverse_moves(bars, "long", 5)
        self.assertAlmostEqual(max(moves), 0.10)
        self.assertEqual(LIQ.share_at_least(moves, 0.10), 5 / float(len(moves)))
        self.assertAlmostEqual(max(LIQ.adverse_moves(bars, "short", 5)), 0.03)


class Check(unittest.TestCase):
    def sized(self, s, *argv):
        s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual", "--venue", "bybit"] + list(argv))
        s.run(*SIZE, [])

    def test_pass(self):
        with Session() as s:
            self.sized(s)
            code, out, err = s.run("crypto-liquidation", "liq", ["check"])
            self.assertEqual(code, 0, err)
            o = cc.read_json(s.path("orders.json"))["orders"][0]
            self.assertEqual(o["liq"]["verdict"], "PASS")
            self.assertGreaterEqual(o["liq"]["buffer"], 2.0)
            self.assertIn("Bybit risk tiers", out)
            self.assertIn("the one that counts", out)

    def test_blocks(self):
        with Session(config={"min_liq_buffer": 60}) as s:
            self.sized(s)
            code, out, err = s.run("crypto-liquidation", "liq", ["check"])
            # with a 60x buffer the budget already drops leverage to what fits; tighten the order by hand
            doc = cc.read_json(s.path("orders.json"))
            doc["orders"][0]["leverage"] = 3
            cc.write_json(s.path("orders.json"), doc)
            code, out, err = s.run("crypto-liquidation", "liq", ["check"])
            self.assertEqual(code, 1)
            self.assertIn("BLOCK", out)
            self.assertIn("or less passes", out)
        with Session() as s:
            self.sized(s)
            doc = cc.read_json(s.path("orders.json"))
            doc["orders"][0]["margin_mode"] = "cross"
            doc["orders"][0]["stop"] = None
            cc.write_json(s.path("orders.json"), doc)
            code, out, err = s.run("crypto-liquidation", "liq", ["check"])
            self.assertEqual(code, 1)
            self.assertIn("cross margin is off", out)
            self.assertIn("no stop", out)

    def test_no_orders(self):
        with Session() as s:
            code, out, err = s.run("crypto-liquidation", "liq", ["check"])
            self.assertEqual(code, 1)
            self.assertIn("no orders to check", err)


class At(unittest.TestCase):
    def test_ladder(self):
        with Session() as s:
            code, out, err = s.run("crypto-liquidation", "liq", ["at", "SOL", "--side", "long", "--leverage", "10",
                                                                 "--venue", "binance", "--json"])
            self.assertEqual(code, 0, err)
            d = json.loads(out)
            dists = [x["dist"] for x in d["ladder"]]
            self.assertEqual(dists, sorted(dists, reverse=True))
            ps = [x["p"] for x in d["ladder"]]
            self.assertEqual(ps, sorted(ps))                      # more leverage, reached more often
            self.assertLess(d["liq"], d["entry"])

    def test_with_a_stop_names_the_leverage_that_passes(self):
        with Session() as s:
            code, out, err = s.run("crypto-liquidation", "liq", ["at", "BTC", "--side", "short", "--leverage", "50",
                                                                 "--stop", "68500", "--venue", "binance"])
            self.assertEqual(code, 0, err)
            self.assertIn("Highest leverage that passes: 20x", out)

    def test_instant_liquidation_is_refused(self):
        with Session() as s:
            code, out, err = s.run("crypto-liquidation", "liq", ["at", "SOL", "--leverage", "50", "--venue", "binance"])
            self.assertEqual(code, 1)
            self.assertIn("liquidation on the first tick", err)
            code, out, err = s.run("crypto-liquidation", "liq", ["at", "SOL", "--leverage", "10", "--venue", "binance"])
            self.assertIn("From 50x up the margin would not cover", out)

    def test_over_the_venue_max(self):
        with Session() as s:
            code, out, err = s.run("crypto-liquidation", "liq", ["at", "SOL", "--leverage", "90", "--venue", "hyperliquid"])
            self.assertEqual(code, 1)
            self.assertIn("at most", err)


if __name__ == "__main__":
    unittest.main()
