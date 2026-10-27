"""crypto-scout: the board ranks by evidence, corrects for testing many setups at once, and picks one."""

import json
import unittest

from _load import cc, load, Session

SCOUT = ("crypto-scout", "scout")
SC = load("crypto-scout", "scout")
FAST = ["--venue", "binance", "--replays", "300"]


def tm_data(n=12, avg=0.4, edge=0.3, decided=10, share_hi=0.8, breakeven=0.37, note=None):
    return {"history": {"setup": "orb", "n": n, "avg_r": avg, "decided": decided, "share_hi": share_hi, "note": note},
            "baseline": {"edge": edge} if edge is not None else None, "mc": {"breakeven": breakeven}}


class Board(unittest.TestCase):
    def test_the_board(self):
        with Session() as s:
            code, out, err = s.run(*SCOUT, FAST + ["--top", "6"])
            self.assertIn(code, (0, 1), err)
            self.assertIn("Scout | Binance USD-M futures | 2026-10-21 15:15 UTC", out)
            self.assertIn("Regime: ", out)
            self.assertIn("tested on each coin's own last 30 days, after fees, slippage and funding", out)
            self.assertIn("coiled squeeze", out)                                 # FUNDX: no side until a band breaks
            self.assertIn("Evidence, not a promise or advice.", out)
            self.assertIn("Testing 1 of", err)                                   # progress goes to stderr
            d = cc.read_json(s.path("scout.json"))
            c = d["candidates"]
            self.assertTrue(0 < len(c) <= 6)
            self.assertEqual([x["rank"] for x in c], list(range(1, len(c) + 1)))
            order = [SC.TIERS.index(x["tier"]) for x in c]
            self.assertEqual(order, sorted(order))                               # tiers in order, best first
            for x in c:
                self.assertIn(x["side"], ("long", "short"))
                self.assertEqual(x["fdr_tests"], len([y for y in c if y["tm"]["baseline"]]))
                if x["tier"] == "edge":                                          # the bar for "has the evidence"
                    self.assertGreaterEqual(x["tm"]["history"]["n"], 10)
                    self.assertTrue(x["passes_fdr"])
                    self.assertFalse(x["flags"]["hard"] + x["flags"]["soft"])
                p = x["plan"]
                sg = cc.side_sign(p["side"])
                self.assertGreater(sg * (p["target"] - p["entry"]), 0)
                self.assertGreater(sg * (p["entry"] - p["stop"]), 0)
            self.assertEqual(code, 0 if any(x["tier"] == "edge" for x in c) else 1)
            if not any(x["tier"] == "edge" for x in c):
                self.assertIn("Nothing has the evidence today", out)
            self.assertNotIn("FUNDX", [x["coin"] for x in c if x["setup"] == "squeeze"])
            # the UTC-day opening range closed its 4h entry window at 04:00: those breakouts are old news
            self.assertFalse([x for x in c if x["setup"] == "orb"])
            self.assertIn("past their 4h00m entry window", out)
            self.assertIn("For a fresh range: scout.py --session ny (the New York open, 1h45m ago).", out)

    def test_a_fresh_session_keeps_its_breakouts(self):
        with Session() as s:
            code, out, err = s.run(*SCOUT, FAST + ["--top", "8", "--session", "ny", "--setups", "orb", "--json"])
            d = json.loads(out)
            self.assertTrue(d["candidates"])
            self.assertEqual(set(x["setup"] for x in d["candidates"]), {"orb"})
            self.assertFalse(d["skipped"]["past the entry window"])

    def test_side_and_symbols(self):
        with Session() as s:
            code, out, err = s.run(*SCOUT, FAST + ["--top", "4", "--side", "long", "--json"])
            d = json.loads(out)
            self.assertTrue(d["candidates"])
            self.assertEqual(set(x["side"] for x in d["candidates"]), {"long"})
            code, out, err = s.run(*SCOUT, FAST + ["--top", "4", "--symbols", "ORBX,VOLX", "--json"])
            d = json.loads(out)
            self.assertTrue(d["candidates"])
            self.assertTrue(set(x["coin"] for x in d["candidates"]) <= {"ORBX", "VOLX"})
            self.assertFalse(d["watchlist_reused"])                              # a coin list scans afresh

    def test_pick(self):
        with Session() as s:
            s.run(*SCOUT, FAST + ["--top", "3"])
            d = cc.read_json(s.path("scout.json"))
            code, out, err = s.run(*SCOUT, ["pick", "1"])
            self.assertEqual(code, 0, err)
            self.assertIn("Picked 1: ", out)
            self.assertIn("Next: /crypto-budget", out)
            self.assertEqual(cc.read_json(s.path("plan.json")), d["candidates"][0]["plan"])
            self.assertTrue(cc.read_json(s.path("timemachine.json"))["from_scout"])
            code, out, err = s.run(*SCOUT, ["pick", "99"])
            self.assertEqual(code, 1)
            self.assertIn("the board has no entry 99", err)
            code, out, err = s.run(*SCOUT, ["pick", "1", "--now", "2026-10-21 15:50"])
            self.assertEqual(code, 1)
            self.assertIn("old; prices have moved", err)

    def test_pick_before_any_board(self):
        with Session() as s:
            code, out, err = s.run(*SCOUT, ["pick", "1"])
            self.assertEqual(code, 1)
            self.assertIn("no scout board yet", err)


class Evidence(unittest.TestCase):
    def test_benjamini_hochberg(self):
        self.assertEqual(SC.bh_passes([]), set())
        self.assertEqual(SC.bh_passes([0.001, 0.02, 0.03, 0.5]), {0, 1, 2})
        self.assertEqual(SC.bh_passes([0.06, 0.07]), {0, 1})                     # step-up: the larger one lifts both
        self.assertEqual(SC.bh_passes([0.06, 0.5]), set())
        self.assertEqual(SC.bh_passes([0.04] + [0.9] * 9), set())                # 0.04 is not rare among ten tests

    def test_tiers(self):
        none = {"hard": [], "soft": []}
        self.assertEqual(SC.tier_of(tm_data(), {}, none, True)[0], "edge")
        self.assertEqual(SC.tier_of(tm_data(), {}, none, False)[0], "watch")      # did not survive the correction
        self.assertEqual(SC.tier_of(tm_data(n=6), {}, none, True)[0], "watch")    # too few trades
        self.assertEqual(SC.tier_of(tm_data(), {}, {"hard": [], "soft": ["joins crowded longs"]}, True)[0], "watch")
        self.assertEqual(SC.tier_of(tm_data(), {}, {"hard": ["the price is past the stop"], "soft": []}, True)[0], "watch")
        self.assertEqual(SC.tier_of(tm_data(avg=-0.1), {}, none, True)[0], "no_edge")
        self.assertEqual(SC.tier_of(tm_data(share_hi=0.30), {}, none, True)[0], "no_edge")   # even the top is under break-even
        self.assertEqual(SC.tier_of(tm_data(n=0, avg=None), {}, none, False)[0], "no_edge")
        self.assertEqual(SC.tier_of(tm_data(note="no fixed rule"), {}, none, False)[0], "untested")
        a = SC.tier_of(tm_data(n=12, edge=0.3), {}, none, True)[1]
        b = SC.tier_of(tm_data(n=40, edge=0.3), {}, none, True)[1]
        self.assertLess(a, b)                                                    # a longer record ranks higher


if __name__ == "__main__":
    unittest.main()
