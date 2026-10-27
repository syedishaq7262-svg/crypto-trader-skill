"""tools/backtest.py: a walk-forward replay that never peeks ahead, honest statistics, the pack's two checks, the
paper account's rules, and the published results rebuilt from the published trades."""

import csv
import gzip
import json
import os
import shutil
import tempfile
import unittest

from _load import cc, load, ROOT, Session

BT = load("tools", "backtest")
SCAN = load("crypto-scanner", "scan")
TM = load("crypto-timemachine", "timemachine")
DAY = 86400000
HOUR = 3600000


def trade(coin, setup, side, at_h, r, hold_h=2, cost=0.1):
    at = 1000 * DAY + at_h * HOUR
    return {"coin": coin, "setup": setup, "side": side, "at": at, "exit_at": at + hold_h * HOUR, "r": r,
            "gross_r": r + 0.1, "outcome": "target" if r > 0 else "stop", "stop_cost_r": cost}


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

    def test_the_cost_rule_is_judged_as_shown(self):
        start = 1000 * DAY
        ts = [trade("A", "trend", "long", 24 * d, 1.0, cost=c) for d, c in ((31, 0.204), (32, 0.206), (33, 0.1))]
        g = BT.check_groups(ts, start - 30 * DAY, 0.2)
        self.assertEqual([t["stop_cost_r"] for t in g["cost"]], [0.204, 0.1])         # 0.204 prints as 0.20R
        self.assertEqual(len(BT.check_groups(ts, start - 30 * DAY, None)["cost"]), 3)  # the rule off: all pass
        split = BT.cost_split(ts, 0.2)
        self.assertEqual((split["over"]["n"], split["within"]["n"]), (1, 2))

    def test_the_halves(self):
        start = 1000 * DAY                     # a 60-day window: judged from day 30, halves split at day 45
        ts = [trade("A", "trend", "long", 24 * d, r) for d, r in ((31, -1.0), (35, -1.0), (50, 2.0), (55, 2.0))]
        tab = BT.check_table(BT.check_groups(ts, start, 0.2), start, start + 60 * DAY)
        self.assertEqual(tab["mid"], start + 45 * DAY)
        self.assertEqual(tab["every"]["first_half"], {"n": 2, "avg_r": -1.0})
        self.assertEqual(tab["every"]["second_half"], {"n": 2, "avg_r": 2.0})

    def test_paper_runs_shuffle_the_order_and_the_day(self):
        cfg = cc.DEFAULTS
        # five signals at the same moment every day: which ones get the two slots is luck, so runs differ
        ts = [trade(c, "trend", "long", 24 * d + 1, (2.0 if c in "AB" else -1.0), hold_h=3)
              for d in range(20) for c in "ABCDE"]
        pr = BT.paper_runs(ts, cfg, runs=30, seed=19)
        self.assertEqual(pr["runs"], 30)
        self.assertLessEqual(pr["min"], pr["lo"])
        self.assertLessEqual(pr["lo"], pr["median"])
        self.assertLessEqual(pr["median"], pr["hi"])
        self.assertLess(pr["min"], pr["max"])
        self.assertEqual(pr, BT.paper_runs(ts, cfg, runs=30, seed=19))                # the same seed, the same runs
        # the trading day: four entries a day, counted from the day's start
        late = [trade("A", "trend", "long", h, 1.0, hold_h=0.1) for h in (22, 22.5, 23, 23.5, 24.5)]
        self.assertEqual(BT.paper_account(late, cfg, {})["trades"], 5)                 # 4 before midnight UTC, 1 after
        self.assertEqual(BT.paper_account(late, cfg, {}, day_start_ms=22 * HOUR)["trades"], 4)   # one day from 22:00

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
                             "By setup", "judge the trades after the first 30 days", "Survivorship", "track_record.json"):
                    self.assertIn(want, text)
                d = cc.read_json(os.path.join(out, "backtest.json"))
                with open(os.path.join(out, "backtest_trades.csv")) as fh:
                    rows = list(csv.DictReader(fh))
                self.assertEqual(len(rows), d["all"]["n"])
                self.assertGreater(d["all"]["n"], 50)
                self.assertEqual(d["coins"], 4)
                win = d["from"]
                for r in rows:
                    self.assertGreaterEqual(cc.parse_when(r["signal_utc"]), win)
                    self.assertLess(cc.parse_when(r["signal_utc"]), d["to"])
                    self.assertLessEqual(float(r["r_after_costs"]), float(r["r_before_costs"]) + 1e-9)   # costs only cost
                    self.assertTrue(0 < float(r["stop_cost_r"]) < 50, r)
                with open(os.path.join(out, "backtest_report.txt")) as fh:
                    self.assertIn("Fake money only.", fh.read())
                tr = cc.read_json(os.path.join(out, "track_record.json"))
                self.assertEqual(sum(e["n"] for e in tr["setups"].values()), d["all"]["n"])
                self.assertEqual(tr["cost_limit_r"], 0.2)
                self.assertFalse(tr["costs_estimated"])
                # read the same trades back: the same numbers, nothing downloaded
                code, again, err = s.run("tools", "backtest", ["--trades", os.path.join(out, "backtest_trades.csv"),
                                                              "--venue", "binance", "--end", cc.fmt_utc(d["to"]),
                                                              "--days", "20", "--out", out + "-again"])
                self.assertEqual(code, 0, err)
                self.assertIn("trades re-read from backtest_trades.csv", again)
                again = cc.read_json(os.path.join(out + "-again", "backtest.json"))["all"]
                for k, v in d["all"].items():                      # the file keeps R to 4 decimals
                    self.assertAlmostEqual(again[k], v, places=3, msg=k)
                # a window that ends in the past
                code, text, err = s.run("tools", "backtest", ["--coins", "2", "--days", "10", "--end", "2026-10-14 12:00",
                                                             "--workers", "1", "--venue", "binance", "--out", out + "-end"])
                self.assertEqual(code, 0, err)
                e = cc.read_json(os.path.join(out + "-end", "backtest.json"))
                self.assertEqual((e["from"], e["to"]), (cc.parse_when("2026-10-04 12:00"), cc.parse_when("2026-10-14 12:00")))
                code, text, err = s.run("tools", "backtest", ["--end", "2030-01-01", "--venue", "binance"])
                self.assertIn("is in the future", err)
                code, text, err = s.run("tools", "backtest", ["--venue", "hyperliquid"])
                self.assertEqual(code, 1)
                self.assertIn("too few to backtest", err)
        finally:
            for x in ("", "-again", "-end"):
                shutil.rmtree(out + x, ignore_errors=True)

    def test_costs_rebuilt_for_a_file_without_the_column(self):
        out = tempfile.mkdtemp(prefix="crypto-bt-")
        try:
            with Session() as s:
                code, text, err = s.run("tools", "backtest", ["--coins", "4", "--days", "20", "--workers", "1",
                                                             "--venue", "binance", "--out", out, "--paper-runs", "5"])
                self.assertEqual(code, 0, err)
                with open(os.path.join(out, "backtest_trades.csv")) as fh:
                    rows = list(csv.DictReader(fh))
                true = [float(r.pop("stop_cost_r")) for r in rows]
                old = os.path.join(out, "old.csv")
                with open(old, "w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
                trades, estimated = BT.read_trades(old)
                self.assertTrue(estimated)
                errs = sorted(abs(t["stop_cost_r"] - c) / c for t, c in zip(trades, true))
                self.assertLess(errs[len(errs) // 2], 0.1)        # the typical trade's cost, within 10%
        finally:
            shutil.rmtree(out, ignore_errors=True)


BACKTEST = os.path.join(ROOT, "backtest")
FIRST = os.path.join(BACKTEST, "2026-10-27-binance-90d-trades.csv.gz")     # 29 Jul to 27 Oct, 40 coins
SECOND = os.path.join(BACKTEST, "2026-07-29-binance-90d-trades.csv.gz")    # 30 Apr to 29 Jul, 14 coins, out of sample


class Published(unittest.TestCase):
    """Every published report, and the track record that ships with the pack, is what the tool makes of the
    published trades."""

    def rebuild(self, s, trades, end, out):
        code, text, err = s.run("tools", "backtest", ["--trades", trades, "--venue", "binance", "--end", end, "--out", out])
        self.assertEqual(code, 0, err)
        with open(os.path.join(out, "backtest_report.txt"), encoding="utf-8") as fh:
            return fh.read()

    def published(self, name):
        with open(os.path.join(BACKTEST, name), encoding="utf-8") as fh:
            return fh.read()

    def test_rebuilt_from_the_published_trades(self):
        out = tempfile.mkdtemp(prefix="crypto-bt-")
        try:
            with Session() as s:
                first = self.rebuild(s, FIRST, "2026-10-27 06:00", os.path.join(out, "1"))
                second = self.rebuild(s, SECOND, "2026-07-29 00:00", os.path.join(out, "2"))
                pooled = self.rebuild(s, FIRST + "," + SECOND, "2026-10-27 06:00", os.path.join(out, "p"))
            self.assertEqual(self.published("2026-10-27-binance-90d-checks.txt"), first)
            self.assertEqual(self.published("2026-10-27-binance-180d-pooled-report.txt"), pooled)
            # the second window's report came from a full run on the user's computer: everything a trades file can
            # rebuild (all but the random entries) matches it line for line
            sent = self.published("2026-07-29-binance-90d-report.txt")
            self.assertIn("crypto-trader-skill 1.1.0, code 0be267c0", sent.splitlines()[0])
            self.assertEqual(sent[sent.index("The pack's two checks"):], second[second.index("The pack's two checks"):])
            for line in second.splitlines()[5:10]:
                self.assertIn(line, sent)
            made = cc.read_json(os.path.join(out, "p", "track_record.json"))
            shipped = cc.read_json(os.path.join(ROOT, "crypto-setup", "track_record.json"))
            self.assertEqual(json.dumps(made, sort_keys=True), json.dumps(shipped, sort_keys=True))
            self.assertTrue(shipped["costs_estimated"])
            self.assertEqual([r["trades"] for r in shipped["runs"]], [9835, 27566])         # oldest first
            self.assertEqual((shipped["coins"], shipped["all"]["n"]), (40, 37401))
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
