"""crypto-idealab: rules are read safely, indicators are right and never peek, trades follow the rules, and the
report says what the run found, the luck test and the count of rules tried included."""

import json
import math
import os
import random
import re
import unittest

from _load import ROOT, Session, cc, load

LAB = load("crypto-idealab", "lab")
STEP = 3600000
T0 = 1790000000000 // STEP * STEP


def walk(n, seed=7, start=100.0, vol=0.01):
    rng = random.Random(seed)
    bars, p = [], start
    for i in range(n):
        o = p
        c = o * math.exp(rng.gauss(0, vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        v = 1000 * (1 + rng.random())
        bars.append((T0 + i * STEP, o, h, lo, c, v, v * c))
        p = c
    return bars


def bar(i, o, h, lo, c, v=1000.0):
    return (T0 + i * STEP, o, h, lo, c, v, v * c)


COSTS = {"taker": 0.0005, "maker": 0.0002, "slippage": 0.0001}


class Reading(unittest.TestCase):
    def test_good_rules_read_back_the_same(self):
        for text, back in (("rsi(14) < 30 and close > ema(200)", "rsi(14) < 30 and close > ema(200)"),
                           ("RSI < 30 AND price > ema(200)", None),
                           ("crosses_above(ema(9), ema(21))", "crosses_above(ema(9), ema(21))"),
                           ("btc_close > ema(btc_close, 200)", "btc_close > ema(btc_close, 200)"),
                           ("vol_ratio > 3", "vol_ratio(20) > 3"),
                           ("not (hour < 13) and weekday <= 4", "not (hour < 13) and weekday <= 4"),
                           ("close - open > 2 * atr(14)", "close - open > 2 * atr(14)"),
                           ("funding = 0", "funding == 0"),
                           ("20 < rsi < 40", "20 < rsi(14) < 40")):
            if back is None:
                with self.assertRaises(LAB.RuleError):                  # RSI in capitals is not a word
                    LAB.Rule(text)
                continue
            self.assertEqual(LAB.Rule(text).text, back, text)
        self.assertEqual(LAB.Rule("rsi < 30 AND price > ema(200)").text, "rsi(14) < 30 and close > ema(200)")

    def test_nothing_but_the_words_gets_in(self):
        for bad in ("__import__('os').system('echo hi')", "close.__class__", "open('x')", "(lambda: 1)() > 0",
                    "[x for x in close] > 0", "close[0] > 1", "'a' < 'b'", "exec('1') > 0", "ema(n=5) > close",
                    "sma(close) > 1", "rsi(14)", "1 < 2", "rsi(14) and close", "ema(close, close) > 1",
                    "ema(2.5) > close", "rsi(1) > 3", "macd(26, 12) > 0", "sma(*[5]) > 1", "x" * 500,
                    "close > " + " + ".join(["1"] * 200), "close >", "True", "close > True", "close > 1\x00"):
            with self.assertRaises(LAB.RuleError, msg=bad):
                LAB.Rule(bad)

    def test_the_errors_say_what_to_do(self):
        with self.assertRaises(LAB.RuleError) as e:
            LAB.Rule("rsx(14) < 30")
        self.assertIn("Did you mean rsi?", str(e.exception))
        with self.assertRaises(LAB.RuleError) as e:
            LAB.Rule("close() > 1")
        self.assertIn("close is a value, not a function", str(e.exception))
        with self.assertRaises(LAB.RuleError) as e:
            LAB.Rule("sma(close) > 1")
        self.assertIn("sma needs its n", str(e.exception))

    def test_what_a_rule_needs(self):
        r = LAB.Rule("funding < 0 and btc_close > ema(btc_close, 50) and close > prev_day_high")
        self.assertEqual(r.needs, {"funding", "btc", "day"})
        self.assertEqual(LAB.Rule("ema(200) < close").lookback(STEP), 600)
        self.assertEqual(LAB.Rule("close > highest(20)").lookback(STEP), 21)
        self.assertGreaterEqual(LAB.Rule("close > prev_day_high").lookback(STEP), 48)


class Indicators(unittest.TestCase):
    def setUp(self):
        self.bars = walk(400)
        self.f = LAB.Frame(self.bars, STEP)
        self.close = [b[4] for b in self.bars]

    def val(self, text):
        k, ir = LAB.Rule("%s > -1e30" % text).ir[2][0], None
        return self.f.series(k)

    def test_averages_match_the_core(self):
        self.assertEqual(self.val("ema(20)"), cc.ema(self.close, 20))
        self.assertEqual(self.val("sma(20)"), cc.sma(self.close, 20))
        self.assertEqual(self.val("rsi(14)"), cc.rsi(self.close, 14))
        self.assertEqual(self.val("atr(14)"), cc.atr(self.bars, 14))
        mid, up, lo, _ = cc.bollinger(self.close, 20, 2.0)
        for a, b in zip(self.val("bb_upper(20, 2)"), up):
            if b is None:
                self.assertIsNone(a)
            else:
                self.assertAlmostEqual(a, b, places=8)

    def test_highest_leaves_out_the_bar_itself(self):
        hi = self.val("highest(20)")
        for i in range(20, len(self.bars)):
            self.assertEqual(hi[i], max(b[2] for b in self.bars[i - 20:i]))
        self.assertIsNone(hi[19])
        lo = self.val("lowest(5)")
        self.assertEqual(lo[30], min(b[3] for b in self.bars[25:30]))

    def test_change_ago_and_crosses(self):
        ch = self.val("change(3)")
        self.assertAlmostEqual(ch[10], (self.close[10] / self.close[7] - 1) * 100)
        ago = self.val("ago(close, 2)")
        self.assertEqual(ago[5], self.close[3])
        self.assertIsNone(ago[1])
        up = self.f.series(LAB.Rule("crosses_above(ema(5), ema(20))").ir)
        e5, e20 = cc.ema(self.close, 5), cc.ema(self.close, 20)
        for i in range(21, len(self.close)):
            self.assertEqual(up[i], e5[i - 1] <= e20[i - 1] and e5[i] > e20[i])

    def test_vol_ratio_and_macd(self):
        vol = [b[5] for b in self.bars]
        vr = self.val("vol_ratio(10)")
        self.assertAlmostEqual(vr[50], vol[50] / (sum(vol[40:50]) / 10))
        line = self.val("macd(12, 26)")
        e12, e26 = cc.ema(self.close, 12), cc.ema(self.close, 26)
        self.assertAlmostEqual(line[100], e12[100] - e26[100])
        hist = self.val("macd_hist(12, 26, 9)")
        sig = self.val("macd_signal(12, 26, 9)")
        self.assertAlmostEqual(hist[200], line[200] - sig[200])

    def test_a_signal_never_depends_on_a_later_bar(self):
        rules = ["rsi(14) < 40 and close > ema(50)", "crosses_above(macd(12, 26), macd_signal(12, 26, 9))",
                 "close > highest(20) and vol_ratio(20) > 1.2", "change(5) < -2 or bb_width(20, 2) < 3",
                 "close < bb_lower(20, 2) and ago(rsi(14), 1) < rsi(14)", "stdev(10) > 0.5 and close > prev_day_high"]
        full = walk(600, seed=3)
        for cut in (150, 300, 451):
            changed = full[:cut] + walk(600 - cut, seed=99, start=full[cut - 1][4] * 1.3)
            changed = changed[:cut] + [(full[cut + i][0],) + tuple(changed[cut + i][1:]) for i in range(600 - cut)]
            for text in rules:
                a = LAB.Frame(full, STEP).signals(LAB.Rule(text))
                b = LAB.Frame(changed, STEP).signals(LAB.Rule(text))
                self.assertEqual(a[:cut], b[:cut], "%s peeked past bar %d" % (text, cut))

    def test_funding_is_the_last_payment_by_the_close(self):
        bars = walk(30)
        pays = [(T0 + 8 * STEP, 0.0001), (T0 + 16 * STEP, -0.0002)]
        f = LAB.Frame(bars, STEP, funding=pays)
        fund = f.value("funding")
        self.assertIsNone(fund[6])                       # closes at 7:00, before the first payment
        self.assertAlmostEqual(fund[7], 0.01)            # closes at 8:00, as the payment lands: 0.01%
        self.assertAlmostEqual(fund[15], -0.02)
        self.assertAlmostEqual(fund[29], -0.02)

    def test_yesterday_comes_from_whole_days_only(self):
        start = (T0 // 86400000 + 1) * 86400000 + 3 * STEP          # the data starts three hours into a day
        bars = [(start + i * STEP,) + tuple(b[1:]) for i, b in enumerate(walk(80))]
        f = LAB.Frame(bars, STEP)
        hi, d_open = f.value("prev_day_high"), f.value("day_open")
        first_full = 21                                               # the first bar of the next whole day
        self.assertTrue(all(x is None for x in hi[:first_full + 24]))  # the partial first day never counts
        self.assertIsNone(d_open[0])
        self.assertEqual(d_open[first_full], bars[first_full][1])
        k = first_full + 24 + 5
        self.assertEqual(hi[k], max(b[2] for b in bars[first_full:first_full + 24]))


class Trades(unittest.TestCase):
    def play(self, bars, side="long", stop=("pct", 2.0), target=("r", 2.0), hold=24, exit_sig=None, i=0,
             funding=None):
        f = LAB.Frame(bars, STEP, funding=funding)
        ts = [t for t, _ in f.funding]
        rs = [r for _, r in f.funding]
        return LAB.play(f, i, side, stop, target, hold, exit_sig, cc.atr(bars, 14), COSTS, ts, rs)

    def test_in_at_the_next_open_out_at_the_target(self):
        bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 101, 99.5, 100.5), bar(2, 100.5, 104.5, 100, 104)]
        t = self.play(bars)
        self.assertEqual((t["entry"], t["stop"], t["target"], t["exit"], t["outcome"]), (100, 98, 104, 104, "target"))
        self.assertAlmostEqual(t["gross_r"], 2.0)
        self.assertAlmostEqual(t["r"], cc.net_r("long", 100, 104, "target", 2.0, COSTS, 0.0))
        self.assertEqual(t["bars"], 2)

    def test_a_bar_that_touches_both_is_a_stop(self):
        bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 104.5, 97.5, 101)]
        t = self.play(bars)
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 98))

    def test_a_gap_past_the_stop_fills_at_the_open(self):
        bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 100.5, 99, 99.5), bar(2, 96, 97, 95, 96.5)]
        t = self.play(bars)
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 96))
        self.assertLess(t["gross_r"], -1.0)

    def test_shorts_mirror_longs(self):
        bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 100.5, 95.5, 96)]
        t = self.play(bars, side="short")
        self.assertEqual((t["stop"], t["target"], t["outcome"], t["exit"]), (102, 96, "target", 96))

    def test_time_limit_and_exit_rule(self):
        bars = [bar(i, 100, 100.4, 99.6, 100) for i in range(10)]
        t = self.play(bars, hold=3)
        self.assertEqual((t["outcome"], t["bars"], t["exit_at"]), ("time", 3, T0 + 4 * STEP))
        sig = [False] * 10
        sig[2] = True
        t = self.play(bars, exit_sig=sig, hold=8)
        self.assertEqual((t["outcome"], t["exit_at"], t["exit"]), ("rule", T0 + 3 * STEP, 100))
        self.assertEqual(self.play(bars[:3], hold=8), "unfinished")

    def test_no_honest_stop_no_trade(self):
        bars = [bar(i, 100, 100.01, 99.99, 100) for i in range(30)]
        self.assertEqual(self.play(bars, stop=("pct", 0.01)), "skip")
        self.assertEqual(self.play(bars, stop=("swing", 5), i=2), "skip")             # not enough bars behind it
        self.assertEqual(self.play(bars, stop=("swing", 5), i=10), "skip")            # the swing low is the entry

    def test_funding_paid_while_open(self):
        bars = [bar(i, 100, 100.5, 99.5, 100) for i in range(12)]
        pays = [(T0 + 1 * STEP, 0.01), (T0 + 4 * STEP, 0.001), (T0 + 6 * STEP, 0.002), (T0 + 20 * STEP, 0.5)]
        t = self.play(bars, hold=5, funding=pays)                  # in at 1:00, out at the 6:00 close
        self.assertAlmostEqual(t["funding_r"], -(0.001 + 0.002) * 100 / 2.0)
        s = self.play(bars, side="short", hold=5, funding=pays)
        self.assertAlmostEqual(s["funding_r"], (0.001 + 0.002) * 100 / 2.0)

    def test_one_trade_at_a_time(self):
        bars = [bar(i, 100, 100.3, 99.7, 100) for i in range(40)]
        m = {"coin": "X", "symbol": "XUSDT"}
        data = {"bars": bars, "funding": [], "btc": None}
        rule = LAB.Rule("hour >= 0")                                                  # true on every bar
        w_start, w_end = T0 + 2 * STEP, T0 + 30 * STEP
        r = LAB.run_coin((m, data, rule, None, "long", ("pct", 2.0), ("r", 2.0), 5, STEP, w_start, w_end, COSTS, 19, 0))
        entries = [t["entry_at"] for t in r["trades"]]
        self.assertEqual(entries[0], T0 + 2 * STEP)                                   # the first bar that closes inside
        for a, b in zip(r["trades"], r["trades"][1:]):
            self.assertGreaterEqual(b["entry_at"], a["exit_at"])
        self.assertEqual(r["fired"], len(r["trades"]) + r["overlap"] + r["unfinished"] + r["skipped"])


class Report(unittest.TestCase):
    def test_the_verdicts(self):
        s = {"n": 200, "avg_r": -0.2, "lo": -0.3, "hi": -0.1}
        self.assertEqual(LAB.verdict(s, None, ({}, {}))[0], "no edge")
        s = {"n": 200, "avg_r": 0.05, "lo": -0.05, "hi": 0.15}
        self.assertEqual(LAB.verdict(s, None, ({}, {}))[0], "no edge shown")
        s = {"n": 200, "avg_r": 0.2, "lo": 0.1, "hi": 0.3}
        self.assertEqual(LAB.verdict(s, {"luck_p": 0.2}, ({}, {}))[0], "no edge from the rule")
        h_good = ({"n": 90, "avg_r": 0.1}, {"n": 110, "avg_r": 0.3})
        h_bad = ({"n": 90, "avg_r": 0.5}, {"n": 110, "avg_r": -0.05})
        self.assertEqual(LAB.verdict(s, {"luck_p": 0.01}, h_bad)[0], "not consistent")
        label, line = LAB.verdict(s, {"luck_p": 0.01}, h_good)
        self.assertEqual(label, "worth a paper trial")
        self.assertIn("not money", line)
        self.assertEqual(LAB.verdict({"n": 12, "avg_r": 2.0, "lo": 1.0, "hi": 3.0}, {"luck_p": 0.001}, h_good)[0],
                         "too few trades")
        self.assertEqual(LAB.verdict({"n": 0}, None, ({}, {}))[0], "nothing to judge")

    def test_luck_test_finds_no_edge_where_there_is_none(self):
        rng = random.Random(5)
        trades = [{"r": rng.gauss(0, 1)} for _ in range(300)]
        groups = [[rng.gauss(0, 1) for _ in range(50)] for _ in trades]
        lk = LAB.luck_test(trades, groups, 19)
        self.assertGreater(lk["luck_p"], 0.05)
        self.assertLess(lk["edge_lo"], 0)
        self.assertGreater(lk["edge_hi"], 0)
        better = [{"r": t["r"] + 0.5} for t in trades]
        self.assertLess(LAB.luck_test(better, groups, 19)["luck_p"], 0.01)


class EndToEnd(unittest.TestCase):
    ARGS = ["--when", "crosses_above(ema(9), ema(21))", "--side", "long", "--coins", "top:8", "--days", "30",
            "--venue", "binance", "--name", "EMA 9/21 cross"]

    def test_the_skill_example_is_this_run(self):
        with open(os.path.join(ROOT, "crypto-idealab", "SKILL.md"), encoding="utf-8") as fh:
            doc = fh.read()
        block = doc[doc.index("## Example"):]
        block = block[block.index("```\n") + 4:]
        block = block[:block.index("```")]
        with Session() as s:
            code, out, err = s.run("crypto-idealab", "lab", ["test"] + self.ARGS)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), block.strip())

    def test_runs_files_list_and_the_count(self):
        with Session() as s:
            code, out, err = s.run("crypto-idealab", "lab", ["test"] + self.ARGS)
            self.assertEqual(code, 0, err)
            self.assertIn("This is the first rule tested in this lab.", out)
            saved = cc.read_json(s.path("ideas", "ema-9-21-cross.json"))
            self.assertEqual(saved["rule"], "crosses_above(ema(9), ema(21))")
            self.assertEqual(saved["verdict"], "no edge from the rule")
            with open(s.path("ideas", "ema-9-21-cross-trades.csv"), encoding="utf-8") as fh:
                rows = fh.read().strip().splitlines()
            self.assertEqual(len(rows) - 1, saved["result"]["n"])
            code, again, err = s.run("crypto-idealab", "lab", ["test"] + self.ARGS)          # the same rule again
            self.assertIn("This is the first rule tested in this lab.", again)
            self.assertEqual(out, again)                                                   # seeded: the same run
            code, other, err = s.run("crypto-idealab", "lab", ["test", "--idea", s.path("ideas", "ema-9-21-cross.json"),
                                                              "--stop", "atr:3"])
            self.assertEqual(code, 0, err)
            self.assertIn("Rules tested in this lab so far: 2.", other)
            self.assertIn("stop 3 ATR(14) below the entry", other)
            code, listed, err = s.run("crypto-idealab", "lab", ["list"])
            self.assertIn("1 idea saved, 2 distinct rules tested", listed)
            self.assertIn("EMA 9/21 cross", listed)
            code, js, err = s.run("crypto-idealab", "lab", ["test", "--when", "rsi(14) > 60", "--side", "short", "--tf",
                                                           "15m", "--coins", "BTC,ETH", "--days", "10", "--venue", "bybit",
                                                           "--json", "--exit-when", "rsi(14) < 50"])
            self.assertEqual(code, 0, err)
            d = json.loads(js)
            self.assertEqual(d["coins"], ["BTC", "ETH"])
            self.assertEqual(d["exit_rule"], "rsi(14) < 50")
            self.assertEqual(d["tried"], 3)
            for t in d["trades"]:
                self.assertIn(t["outcome"], ("target", "stop", "time", "rule"))
                self.assertLess(t["entry"], t["stop"])

    def test_problems_are_plain(self):
        with Session() as s:
            for argv, words in ((["test", "--when", "rsi(14) < 30"], "say which side"),
                                (["test", "--side", "long"], "say when to enter"),
                                (["test", "--when", "rsi(14) < 30", "--side", "long", "--stop", "2%"], "could not read the stop"),
                                (["test", "--when", "rsi(14) < 30", "--side", "long", "--tf", "2h"], "the bars must be one of"),
                                (["test", "--when", "rsi(14) < 30", "--side", "long", "--days", "5000"], "between 7 and"),
                                (["test", "--when", "rsi(14) < 30", "--side", "long", "--hold", "30d"], "hold"),
                                (["check", "--when", "import os"], "could not read the rule")):
                code, out, err = s.run("crypto-idealab", "lab", argv)
                self.assertEqual(code, 1, argv)
                self.assertIn(words, err, argv)
                self.assertNotIn("Traceback", err)

    def test_words_and_check(self):
        with Session() as s:
            code, out, err = s.run("crypto-idealab", "lab", ["words"])
            self.assertEqual(code, 0)
            for name in list(LAB.VALUES) + list(LAB.FUNCS):
                self.assertIn(name, out)
            for rule, _ in LAB.EXAMPLES:
                self.assertIn(rule, out)
                LAB.Rule(rule)                                    # every example reads
            code, out, err = s.run("crypto-idealab", "lab", ["check", "--when", "funding < 0 and close > prev_day_high"])
            self.assertEqual(code, 0, err)
            self.assertIn("the funding history", out)
            self.assertIn("Rule OK.", out)

    def test_the_skill_doc_phrases_read(self):
        with open(os.path.join(ROOT, "crypto-idealab", "SKILL.md"), encoding="utf-8") as fh:
            doc = fh.read()
        table = doc[doc.index("| the user says | rule |"):doc.index("## How a test works")]
        rules = re.findall(r"\| `([^`]+)` \|", table)
        self.assertGreaterEqual(len(rules), 12)
        for r in rules:
            LAB.Rule(r)


if __name__ == "__main__":
    unittest.main()
