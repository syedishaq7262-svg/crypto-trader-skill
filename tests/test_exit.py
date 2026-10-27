"""crypto-exit: stops, targets, entries, costs and refusals on the fake exchange."""

import json
import math
import unittest

from _load import cc, Session

EXIT = ("crypto-exit", "exit")
SCAN = ("crypto-scanner", "scan")


def plan_of(s):
    return cc.read_json(s.path("plan.json"))


class FromAHit(unittest.TestCase):
    def test_orb_hit_plan(self):
        with Session() as s:
            s.run(*SCAN, ["--venue", "binance", "--session", "ny"])
            code, out, err = s.run(*EXIT, ["plan", "ORBX"])
            self.assertEqual(code, 0, err)
            p = plan_of(s)
            mt = p["setup"]["metrics"]
            mid = (mt["or_high"] + mt["or_low"]) / 2.0
            self.assertEqual(p["setup"]["name"], "orb")
            self.assertEqual(p["entry_rule"], "market")                  # the trigger is 55 minutes old
            self.assertLessEqual(p["stop"], mid + 1e-9)                   # rounded away from the entry
            self.assertLess(mid - p["stop"], p["tick"] + 1e-9)
            self.assertAlmostEqual(p["target"], cc.round_to_tick(p["entry"] + 2 * p["risk"], p["tick"]))
            self.assertAlmostEqual(p["target_r"], abs(p["target"] - p["entry"]) / p["risk"], places=3)
            self.assertEqual(p["exit_by"] - p["entry_time"], 24 * 3600000)
            self.assertEqual(len(p["plan_id"]), 12)
            c = p["costs"]
            self.assertIn("measured from the order book", c["slippage_source"])
            be = cc.breakeven_share(p["target_r"], p["entry"], p["stop"], p["target"], p["risk"], c)
            self.assertAlmostEqual(p["breakeven_share"], be)
            self.assertEqual(p["setup"]["params"]["session"], "ny")
            for line in ("Entry", "Stop", "Target", "Risk", "Reward:risk", "Costs", "Funding", "Break-even", "Out by",
                         "A stop is a plan, not a guarantee"):
                self.assertIn(line, out)

    def test_fresh_trigger_is_the_entry_and_a_short_is_mirrored(self):
        with Session() as s:
            s.run(*SCAN, ["--venue", "binance", "--setups", "breakout"])
            code, out, err = s.run(*EXIT, ["plan", "BRKD"])
            self.assertEqual(code, 0, err)
            p = plan_of(s)
            self.assertEqual(p["side"], "short")
            self.assertEqual(p["entry_rule"], "trigger")
            self.assertGreater(p["stop"], p["entry"])
            self.assertLess(p["target"], p["entry"])

    def test_a_squeeze_break_has_a_side_and_a_coil_needs_one(self):
        with Session() as s:
            s.run(*SCAN, ["--venue", "binance", "--setups", "squeeze"])
            w = cc.read_json(s.path("watchlist.json"))
            sq = [h for h in w["hits"] if h["coin"] == "SQZX"]
            self.assertEqual([h["status"] for h in sq], ["broke down"])    # broken out, so no longer listed as coiled
            self.assertEqual([h["status"] for h in w["hits"] if h["coin"] == "FUNDX"], ["coiled"])
            code, out, err = s.run(*EXIT, ["plan", "SQZX"])
            self.assertEqual(code, 0, err)                                # the break: a side and a setup stop
            p = plan_of(s)
            self.assertEqual((p["side"], p["stop_spec"]), ("short", "setup"))
            self.assertIn("the middle band", p["stop_rule"])
            code, out, err = s.run(*EXIT, ["plan", "FUNDX"])
            self.assertEqual(code, 1)
            self.assertIn("which side", err)
            self.assertIn("a squeeze has no side until a band breaks", err)
            code, out, err = s.run(*EXIT, ["plan", "FUNDX", "--side", "long"])
            self.assertEqual(code, 0, err)                                # no setup stop: atr:1.5 by default
            self.assertTrue(plan_of(s)["stop_spec"].startswith("atr:"))

    def test_named_setup_missing(self):
        with Session() as s:
            s.run(*SCAN, ["--venue", "binance", "--setups", "volume"])
            code, out, err = s.run(*EXIT, ["plan", "BTC", "--setup", "orb"])
            self.assertEqual(code, 1)
            self.assertIn("no orb hit on BTC", err)


class Manual(unittest.TestCase):
    def test_stop_rules(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual", "--venue", "bybit", "--stop", "atr:1.5"])
            p = plan_of(s)
            self.assertAlmostEqual(p["stop"], cc.round_to_tick(p["entry"] - 1.5 * p["atr15"], p["tick"], "down"))
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual", "--venue", "bybit", "--stop", "pct:2"])
            p = plan_of(s)
            self.assertAlmostEqual(p["stop"], cc.round_to_tick(p["entry"] * 0.98, p["tick"], "down"))
            code, out, err = s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual", "--venue", "bybit", "--stop", "swing"])
            if code == 0:
                p = plan_of(s)
                self.assertGreaterEqual(p["entry"] - p["stop"], 0.5 * p["atr15"])
            else:
                self.assertIn("no 1-hour swing", err)
            px = cc.get_venue("bybit").ticker("SOLUSDT")["last"]
            stop = round(px * 1.05, 3)
            s.run(*EXIT, ["plan", "SOL", "--side", "short", "--manual", "--venue", "bybit", "--stop", str(stop), "--target", "3R"])
            p = plan_of(s)
            self.assertEqual(p["stop"], stop)
            self.assertAlmostEqual(p["target_r"], 3.0, places=2)

    def test_target_rules_and_hold(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "ETH", "--side", "long", "--manual", "--venue", "binance", "--target", "atr:4", "--hold", "8h"])
            p = plan_of(s)
            self.assertAlmostEqual(p["target"], cc.round_to_tick(p["entry"] + 4 * p["atr15"], p["tick"]))
            self.assertEqual(p["exit_by"] - p["entry_time"], 8 * 3600000)
            code, out, err = s.run(*EXIT, ["plan", "ETH", "--side", "long", "--manual", "--venue", "binance", "--target", "level"])
            if code == 0:
                self.assertGreaterEqual(plan_of(s)["target_r"], 0.95)
            else:
                self.assertIn("at least 1R", err)

    def test_a_late_entry_says_so(self):
        with Session() as s:
            s.run(*SCAN, ["--venue", "binance", "--setups", "orb"])               # the UTC-day range, 15 hours old
            code, out, err = s.run(*EXIT, ["plan", "LINK"])
            self.assertEqual(code, 0, err)
            self.assertIn("so this is a late entry", out)
            p = plan_of(s)
            self.assertEqual(p["entry_rule"], "market")

    def test_refusals(self):
        with Session() as s:
            cases = [
                (["plan", "BTC", "--side", "long", "--manual", "--stop", "999999"], "is not under the entry"),
                (["plan", "BTC", "--side", "long", "--manual", "--stop", "pct:0.01"], "tighter than the spread"),
                (["plan", "BTC", "--side", "long", "--manual", "--target", "1000"], "is not over the entry"),
                (["plan", "BTC", "--manual"], "which side"),
                (["plan", "ORBX"], "or run /crypto-scanner first to plan from a setup"),       # no scan saved yet
                (["plan", "NOPE", "--side", "long", "--manual"], "no USDT perpetual"),
                (["plan", "BTC", "--side", "long", "--manual", "--stop", "fancy"], "unknown stop"),
                (["plan", "BTC", "--side", "long", "--manual", "--hold", "forever"], "hold time"),
                (["plan"], "which coin"),
            ]
            for argv, msg in cases:
                code, out, err = s.run(*EXIT, argv)
                self.assertEqual(code, 1, argv)
                self.assertIn(msg, err, argv)
            s.run(*SCAN, ["--venue", "binance", "--setups", "volume"])
            code, out, err = s.run(*EXIT, ["plan", "ORBX"])
            self.assertIn("the last scan has no setup on ORBX", err)

    def test_shorts_off(self):
        with Session(config={"allow_short": False}) as s:
            code, out, err = s.run(*EXIT, ["plan", "BTC", "--side", "short", "--manual"])
            self.assertEqual(code, 1)
            self.assertIn("allow_short", err)

    def test_warnings(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "FUNDX", "--side", "long", "--manual", "--venue", "binance", "--stop", "pct:0.3", "--target", "1R"])
            w = " ".join(plan_of(s)["warnings"])
            self.assertIn("Funding is +104% a year against this side", w)
            self.assertIn("under your 1.5 minimum", w)
            self.assertIn("tight for the costs", w)

    def test_priced_per_thousand(self):
        with Session() as s:
            code, out, err = s.run(*EXIT, ["plan", "bonk", "--side", "long", "--manual", "--venue", "binance"])
            self.assertEqual(plan_of(s)["symbol"], "1000BONKUSDT")
            self.assertIn("per 1,000 coins", out)

    def test_show(self):
        with Session() as s:
            code, out, err = s.run(*EXIT, ["show"])
            self.assertEqual(code, 1)
            self.assertIn("no plan yet", err)
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            code, out, err = s.run(*EXIT, ["show"])
            self.assertEqual(code, 0)
            self.assertIn("Exit plan | SOL long", out)

    def test_fixed_slippage_from_config(self):
        with Session(config={"slippage_bps": 7}) as s:
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            self.assertAlmostEqual(plan_of(s)["costs"]["slippage"], 0.0007)


if __name__ == "__main__":
    unittest.main()
