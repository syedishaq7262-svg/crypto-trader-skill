"""crypto-scout: the board lists what passes the cost rule, least bad record first, never calls anything an edge,
and picks one."""

import json
import unittest

from _load import cc, load, Session

SCOUT = ("crypto-scout", "scout")
SC = load("crypto-scout", "scout")
FAST = ["--venue", "binance", "--replays", "300"]


class Board(unittest.TestCase):
    def test_the_board(self):
        with Session() as s:
            code, out, err = s.run(*SCOUT, FAST + ["--top", "6"])
            self.assertIn(code, (0, 1), err)
            self.assertIn("Scout | Binance USD-M futures | 2026-10-21 15:15 UTC", out)
            self.assertIn("Regime: ", out)
            self.assertIn("tested on each coin's own last 30 days, after fees, slippage and funding", out)
            self.assertIn("coiled squeeze", out)                                 # FUNDX: no side until a band breaks
            self.assertIn("A list of what the rules found, not advice.", out)
            self.assertIn("Nothing on this board has shown an edge: in the pack's backtests (37,401 trades, Binance, "
                          "30 Apr to 27 Oct 2026) no setup made money after costs clearly enough to tell from luck, and "
                          "a setup's last 30 days on a coin did not predict its next trade.", out)
            for word in ("evidence", "Worth watching", "Has the"):
                self.assertNotIn(word, out)
            self.assertIn("Testing 1 of", err)                                   # progress goes to stderr
            d = cc.read_json(s.path("scout.json"))
            c = d["candidates"]
            self.assertTrue(0 < len(c) <= 6)
            self.assertEqual([x["rank"] for x in c], list(range(1, len(c) + 1)))
            self.assertEqual(c, sorted(c, key=SC.board_order))                  # groups in order, best record first
            for x in c:
                self.assertIn(x["side"], ("long", "short"))
                self.assertIn(x["group"], SC.GROUPS)
                self.assertEqual(x["fdr_tests"], len([y for y in c if y["tm"]["baseline"]]))
                self.assertEqual(x["group"] == "gone", bool(x["flags"]["hard"]))
                p = x["plan"]
                sg = cc.side_sign(p["side"])
                self.assertGreater(sg * (p["target"] - p["entry"]), 0)
                self.assertGreater(sg * (p["entry"] - p["stop"]), 0)
                lines = SC.line_for(x["rank"], x)
                self.assertTrue(lines[1].startswith("     backtest: "), lines)      # the record leads
                self.assertTrue(lines[2].startswith("     this coin, last 30 days: "), lines)
                self.assertIn("a stop-out costs %.2fR" % (p["costs"]["entry_r"] + p["costs"]["stop_exit_r"]), lines[3])
            self.assertEqual(code, 0 if any(x["group"] == "listed" for x in c) else 1)
            self.assertNotIn("FUNDX", [x["coin"] for x in c if x["setup"] == "squeeze"])
            # the UTC-day opening range closed its 4h entry window at 04:00: those breakouts are old news
            self.assertFalse([x for x in c if x["setup"] == "orb"])
            self.assertIn("past their 4h00m entry window", out)
            self.assertIn("For a fresh range: scout.py --session ny (the New York open, 1h45m ago).", out)

    def test_stops_too_tight_for_the_costs_are_left_out_and_replaced(self):
        with Session() as s:
            code, out, err = s.run(*SCOUT, FAST)
            self.assertIn("Left out, stops too tight for the costs (fees and slippage over 0.2R at a stop-out, which "
                          "crypto-risk blocks): BONK short range breakout 0.50R.", out)
            self.assertIn("Left out: BONK short breakout, a stop-out would cost 0.50R", err)
            d = cc.read_json(s.path("scout.json"))
            self.assertEqual(d["skipped"]["stops too tight for the costs"], ["BONK short range breakout 0.50R"])
            self.assertEqual(d["tested"], 10)                       # the next candidate took its place
            for x in d["candidates"]:
                c = x["plan"]["costs"]
                self.assertLessEqual(round(c["entry_r"] + c["stop_exit_r"], 2), 0.2)
            self.assertIn("Within the cost rule, best long-run record first (10)", out)
            self.assertIn("     backtest: range breakout shorts within the cost rule -0.18R a trade after costs over 2,127 "
                          "trades; all of them -0.23R over 3,534\n", out)
            self.assertEqual([x["coin"] for x in d["candidates"]][:2], ["SHRTX", "VOLX"])   # squeeze, then volume longs
            records = [x["record_r"] for x in d["candidates"]]
            self.assertEqual(records, sorted(records, reverse=True))
        with Session(config={"max_cost_r": 0}) as s:                # the rule off: tested, with a note
            code, out, err = s.run(*SCOUT, FAST)
            self.assertNotIn("too tight for the costs", out)
            self.assertIn("costs 0.50R at a stop-out", out)
            self.assertIn("Best long-run record first (", out)

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


class Rules(unittest.TestCase):
    def test_benjamini_hochberg(self):
        self.assertEqual(SC.bh_passes([]), set())
        self.assertEqual(SC.bh_passes([0.001, 0.02, 0.03, 0.5]), {0, 1, 2})
        self.assertEqual(SC.bh_passes([0.06, 0.07]), {0, 1})                     # step-up: the larger one lifts both
        self.assertEqual(SC.bh_passes([0.06, 0.5]), set())
        self.assertEqual(SC.bh_passes([0.04] + [0.9] * 9), set())                # 0.04 is not rare among ten tests

    def test_groups_and_order(self):
        def cand(setup="breakout", side="long", hard=(), note=None, cost=0.1, record=None):
            hist = {"setup": None if note else setup, "n": 12, "note": note}
            return {"setup": setup, "side": side, "session": None, "flags": {"hard": list(hard), "soft": []},
                    "tm": {"history": hist}, "plan": {"costs": {"entry_r": cost / 2, "stop_exit_r": cost / 2}},
                    "record_r": record}
        self.assertEqual(SC.group_of(cand()), "listed")
        self.assertEqual(SC.group_of(cand(hard=["the price is past the stop"])), "gone")
        self.assertEqual(SC.group_of(cand(setup="rs", note="no fixed entry rule")), "untested")
        board = [cand(record=-0.2), cand(record=0.05, cost=0.15), cand(record=0.05, cost=0.05), cand(record=None)]
        for c in board:
            c["group"] = SC.group_of(c)
        got = sorted(board, key=SC.board_order)
        self.assertEqual([(c["record_r"], round(c["plan"]["costs"]["entry_r"] * 2, 2)) for c in got],
                         [(0.05, 0.05), (0.05, 0.15), (-0.2, 0.1), (None, 0.1)])      # best record, cheaper stop-out
        self.assertEqual(SC.record_r("breakout", "long", None), 0.004)             # within the cost rule
        self.assertIsNone(SC.record_r("orb", "long", "tokyo"))


if __name__ == "__main__":
    unittest.main()
