"""tools/backtest.py: a walk-forward replay that never peeks ahead, honest statistics, and the paper account's rules."""

import csv
import json
import os
import shutil
import tempfile
import unittest

from _load import cc, load, Session

BT = load("tools", "backtest")
SCAN = load("crypto-scanner", "scan")
TM = load("crypto-timemachine", "timemachine")
DAY = 86400000
HOUR = 3600000


def trade(coin, setup, side, at_h, r, hold_h=2):
    at = 1000 * DAY + at_h * HOUR
    return {"coin": coin, "setup": setup, "side": side, "at": at, "exit_at": at + hold_h * HOUR, "r": r,
            "gross_r": r + 0.1, "outcome": "target" if r > 0 else "stop"}


class NoPeeking(unittest.TestCase):
    def test_a_signal_never_depends_on_a_later_candle(self):
        """Cut the past off at a moment: every signal found before it is the same as with the whole past."""
        specs = [("%s|%s" % (k, sd), {"name": n, "params": p}, sd) for k, n, p in BT.SETUPS for sd in ("long", "short")]
        with Session():
            v = cc.get_venue("binance")
            for coin in ("ORBX", "TRDX", "SQZX", "VOLX", "RSIX", "BTC"):
                m = v.market(coin)
                bars = v.bars_between(m["symbol"], "5m", cc.now_ms() - 30 * DAY, cc.now_ms())
                full = TM.history_events_many(SCAN, specs, m, bars)
                for cut_days in (6, 13):
                    cut = bars[-1][0] - cut_days * DAY
                    part = TM.history_events_many(SCAN, specs, m, [b for b in bars if b[0] < cut])
                    for key in full:
                        before = lambda evs: [(e["at"], e["side"], e["trigger"], e.get("stop_ref")) for e in evs
                                              if e["at"] <= cut - 5 * HOUR]
                        self.assertEqual(before(full[key]), before(part[key]), (coin, key, cut_days))
                        self.assertTrue(all(e["at"] <= cut for e in part[key]), (coin, key))


class Statistics(unittest.TestCase):
    def test_summary(self):
        ts = [trade("A", "orb-ny", "long", i * 3, r) for i, r in enumerate([2.0, -1.0, -1.0, 2.0, -1.0])]
        s = BT.summary(ts)
        self.assertEqual((s["n"], s["wins"], s["target"], s["stop"]), (5, 2, 2, 3))
        self.assertAlmostEqual(s["avg_r"], 0.2)
        self.assertAlmostEqual(s["profit_factor"], 4.0 / 3.0)
        self.assertAlmostEqual(s["max_dd_r"], 2.0)
        self.assertEqual(s["worst_losing_streak"], 2)
        self.assertLess(s["avg_r_lo"], 0.2)
        self.assertGreater(s["avg_r_hi"], 0.2)
        self.assertEqual(BT.summary([]), {"n": 0})

    def test_the_scout_test_uses_only_what_had_closed(self):
        start = 1000 * DAY
        good = [trade("A", "trend", "long", 24 * d, 1.0) for d in range(0, 40, 2)]          # a setup that kept winning
        bad = [trade("B", "trend", "long", 24 * d, -1.0) for d in range(0, 40, 2)]
        judged, taken = BT.evidence_filter(good + bad, start)
        self.assertTrue(all(t["at"] >= start + 30 * DAY for t in judged))
        self.assertEqual(set(t["coin"] for t in taken), {"A"})
        lone = [trade("C", "trend", "long", 24 * 31, 1.0, hold_h=48), trade("C", "trend", "long", 24 * 32, 1.0)]
        judged, taken = BT.evidence_filter(lone, start)
        self.assertEqual(taken, [])                       # fewer than 5 closed trades: no record, no trade

    def test_the_paper_account_keeps_the_pack_limits(self):
        cfg = cc.DEFAULTS
        ts = [trade(c, "trend", "long", 1, -1.0, hold_h=5) for c in "ABCDEF"]               # six at once
        pa = BT.paper_account(ts, cfg, {})
        self.assertEqual(pa["trades"], 2)                                                     # two open at most
        self.assertAlmostEqual(pa["end"], 1000 - 20)
        ts = [trade("A", "trend", "long", h, -1.0, hold_h=1) for h in (0, 2, 4, 6, 8)]       # losses in a row
        pa = BT.paper_account(ts, cfg, {})
        self.assertEqual(pa["trades"], 2)                                                     # then a 12-hour pause
        ts = [trade("A", "trend", "long", h, 1.0, hold_h=1) for h in range(0, 12, 2)]        # winners, one day
        pa = BT.paper_account(ts, cfg, {})
        self.assertEqual(pa["trades"], 4)                                                     # four a day
        self.assertAlmostEqual(pa["end"], 1040)


class EndToEnd(unittest.TestCase):
    def test_a_small_run_on_the_test_exchange(self):
        out = tempfile.mkdtemp(prefix="crypto-bt-")
        try:
            with Session() as s:
                code, text, err = s.run("tools", "backtest", ["--coins", "4", "--days", "20", "--workers", "1",
                                                             "--venue", "binance", "--out", out])
                self.assertEqual(code, 0, err)
                for want in ("Walk-forward", "trades: target first", "after costs", "Random entries at the same times",
                             "By setup", "A $1,000 paper account", "Survivorship"):
                    self.assertIn(want, text)
                d = cc.read_json(os.path.join(out, "backtest.json"))
                with open(os.path.join(out, "backtest_trades.csv")) as fh:
                    rows = list(csv.DictReader(fh))
                self.assertEqual(len(rows), d["all"]["n"])
                self.assertGreater(d["all"]["n"], 50)
                self.assertEqual(d["coins"], 4)
                win = d["from"]
                for r in rows:
                    self.assertGreaterEqual(cc.parse_when(r["signal_utc"].replace(" UTC", "")), win)
                    self.assertLessEqual(float(r["r_after_costs"]), float(r["r_before_costs"]) + 1e-9)   # costs only cost
                with open(os.path.join(out, "backtest_report.txt")) as fh:
                    self.assertIn("Fake money only.", fh.read())
                code, text, err = s.run("tools", "backtest", ["--venue", "hyperliquid"])
                self.assertEqual(code, 1)
                self.assertIn("too few to backtest", err)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
