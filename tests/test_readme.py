"""The README's worked example: the same chain on the test market, and every number it quotes."""

import csv
import gzip
import io
import os
import re
import unittest

from _load import ROOT, Session

# (what the README says, what a tool printed)
FACTS = [
    ("41 setups live in the scan", "41 setups live in the scan"),
    ("a stop-out would cost 0.50R", "BONK short range breakout 0.50R"),
    ("A squeeze break on SHRTX comes first", " 1. SHRTX long, squeeze break"),
    ("+0.06R a trade, which can't be told from zero",
     "squeeze break longs within the cost rule +0.06R a trade after costs over 713 trades (can't be told from zero)"),
    ("random entries matched only 0.2% of the time", "luck matches it 0.2% of the time"),
    ("the New York open, 1h45m ago, has a fresh one", "scout.py --session ny (the New York open, 1h45m ago)"),
    ("nothing on it has shown an edge", "Nothing on this board has shown an edge"),
    ("21.002 to 21.255", "above 21.255 or below 21.002"),
    ("closes\n   at 21.404, above the range, on 4.8x volume", "closed at 21.404, above the range high 21.255, on 4.8x volume"),
    ("the market: 21.456", "Entry        21.456  now, at the market"),
    ("midpoint, 21.128, a risk\n   of 0.328 a coin", "Risk         0.328 a coin"),
    ("2R, 22.112", "Target       22.112  2R"),
    ("36.4% of trades to\n   break even", "first in 36.4% of trades that hit a side"),
    ("1-hour swing high at 21.886", "A 1-hour swing high at 21.886 sits between the entry and the target."),
    ("target is beyond the 7-day range", "The target is beyond the 7-day range"),
    ("target first\n   29.1%, stop first 61.0%", "target first   29.1%"),
    ("stop first 61.0%", "stop first     61.0%"),
    ("it fired 11 times and hit its target once", "fired 11 times; 11 became trades"),
    ("losing\n   0.89R a trade after costs", "The setup lost 0.89R a trade after costs over its last 11 trades here"),
    ("Verdict: no edge.", "Verdict: no edge."),
    ("26 ORBX at 2x isolated, $278.93 of margin", "Size: 26 ORBX, about $557.86 at about 21.456; 2x isolated, margin $278.93."),
    ("The loss at the\n   stop is $9.64", "lose about $9.64 there, fees, slippage and funding included"),
    ("11.009, is 48.7%\n   away, 31.9 times further than the stop", "Estimated liquidation 11.009 (48.7% away). The stop 21.128 is 1.53% away: liquidation is 31.9x further"),
    ("buy 26 ORBX, stop 21.128 on the exchange's mark price, target 22.112", "Buy 26 ORBX at the market"),
]


def readme_example():
    with open(os.path.join(ROOT, "README.md")) as fh:
        text = fh.read()
    a = text.index("An example, from the pack's offline test market")
    return text[a:text.index("## The tools", a)]


def squash(s):
    return re.sub(r"\s+", " ", s)


class ReadmeExample(unittest.TestCase):
    def test_every_number_in_the_example(self):
        example = squash(readme_example())
        outputs = []
        with Session() as s:
            def run(folder, name, argv):
                code, out, err = s.run(folder, name, argv)
                self.assertIn(code, (0, 1), err)
                outputs.append(out)
                return out
            run("crypto-scout", "scout", ["--venue", "binance"])
            run("crypto-entry", "entry", ["watch", "ORBX", "--session", "ny", "--now", "2026-10-21 14:00", "--venue", "binance"])
            run("crypto-exit", "exit", ["plan", "ORBX"])
            run("crypto-timemachine", "timemachine", ["run"])
            run("crypto-budget", "size", [])
            run("crypto-liquidation", "liq", ["check"])
            risk = run("crypto-risk", "check", [])
            self.assertTrue(risk.startswith("PASS"), risk)
            ticket = run("crypto-broker", "broker", ["ticket"])
            self.assertIn("READY.", ticket)
        printed = squash("\n".join(outputs))
        for said, shown in FACTS:
            self.assertIn(squash(said), example, "the README no longer says: %s" % said)
            self.assertIn(squash(shown), printed, "the tools no longer print: %s" % shown)


BT_REPORT = os.path.join(ROOT, "backtest", "2026-10-27-binance-90d-report.txt")
BT_SECOND = os.path.join(ROOT, "backtest", "2026-07-29-binance-90d-report.txt")
BT_CHECKS = os.path.join(ROOT, "backtest", "2026-10-27-binance-90d-checks.txt")
BT_TRADES = os.path.join(ROOT, "backtest", "2026-10-27-binance-90d-trades.csv.gz")
CHECK_ROWS = (("every signal", "every signal"), ("the cost rule", "cost rule"), ("the 30-day check", "30-day check"),
              ("both", "both checks"))


class ReadmeBacktest(unittest.TestCase):
    """The README's backtest numbers are the published report's, and the report's are the published trades'."""

    def section(self):
        with open(os.path.join(ROOT, "README.md")) as fh:
            text = fh.read()
        a = text.index("## Backtest: 27,566 trades on real past prices")
        return text[a:text.index("## The tools", a)]

    def test_the_numbers_are_the_reports(self):
        with open(BT_REPORT) as fh:
            report = fh.read()
        sec = self.section()
        for x in ("27,566", "8,669", "17,300", "1,597", "35.1%", "34.6%", "35.7%", "-0.18R", "-0.19R", "-0.16R",
                  "+0.02R", "0.20R", "0.77", "+0.06R", "5,426", "-0.05R", "+0.07R", "342", "+0.01R", "694", "-0.37R", "-0.50R"):
            self.assertIn(x, sec, x)
            self.assertIn(x, report, x)

    def test_the_lessons_are_the_checks_report(self):
        """The checks report is rebuilt from the published trades by test_backtest.Published; the README quotes it."""
        with open(BT_CHECKS) as fh:
            checks = fh.read()
        table, paper = checks.split("A $1,000 paper account")
        sec = self.section()
        def row(text, label):
            k = len(label.split())
            return [l.split()[k:] for l in text.splitlines()
                    if l.startswith("  " + label + " ") and re.match(r"^[0-9,]+$", l.split()[k])][0]
        for words, label in CHECK_ROWS:
            n, first, second = row(table, label)[:3]
            self.assertIn("| %s | %s | %s | %s |" % (words, first, second, n), sec)
            r = row(paper, label)
            median, lo, hi, lost = r[2], r[3], r[5], r[7]
            self.assertIn("| %s | %s | %s to %s | %s |" % (words, median, lo, hi, lost), sec)
            if label == "every signal":
                self.assertIn("lost money in %s of 100 runs; the median run ended at %s" % (lost, median), sec)
        self.assertIn("the 10,209 trades whose stop-out would have cost more\n  than 0.2R were even before costs (-0.01R) "
                      "and lost 0.38R a trade after\n  them.", sec)
        self.assertIn("the 10,209 trades whose stop-out cost more than 0.2R averaged -0.38R after costs (-0.01R before)", checks)
        self.assertIn("Halves split at 2026-09-27 06:00 UTC", checks.replace("halves split", "Halves split"))

    def test_the_second_window_is_its_report(self):
        with open(BT_SECOND) as fh:
            report = fh.read()
        table = report.split("A $1,000 paper account")[0]
        sec = self.section()
        part = sec[sec.index("### Then 90 days it had never seen"):]

        def row(label):
            k = len(label.split())
            return [l.split()[k:] for l in table.splitlines()
                    if l.startswith("  " + label + " ") and re.match(r"^[0-9,]+$", l.split()[k])][0]
        for words, label in CHECK_ROWS:
            n, first, second = row(label)[:3]
            self.assertIn("| %s | %s | %s | %s |" % (words, first, second, n), part)
        self.assertIn("The halves split at 2026-06-29 00:00 UTC.", report)
        self.assertIn("| average a trade, after costs | 30 May to 29 Jun | 29 Jun to 29 Jul | trades |", part)
        for said, shown in (("9,835 trades", "9,835 trades"), ("-0.15R a trade after costs, +0.04R\n  before",
                                                                "Average -0.15R a trade after costs"),
                            ("random entries (+0.08R)", "the setups' edge over them +0.08R"),
                            ("lost 0.26R a trade,\n  against 0.08R for the rest", "averaged -0.26R after costs"),
                            ("the two rules together\n  lost 0.14R a trade", "  both checks    1,493      -0.06R       -0.30R  -0.14R")):
            self.assertIn(said, part)
            self.assertIn(shown, report)
        self.assertIn("averaged -0.08R (+0.02R before)", report)
        for line in table.splitlines()[14:28]:                  # no setup and side made money after costs
            if line.strip():
                self.assertTrue(line.split()[-5].startswith("-"), line)       # avg R after costs

    def test_the_report_and_the_table_come_from_the_trades(self):
        with gzip.open(BT_TRADES, "rt", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(io.StringIO(fh.read())))
        self.assertEqual(len(rows), 27566)
        r = [float(x["r_after_costs"]) for x in rows]
        self.assertEqual("%+.2fR" % (sum(r) / len(r)), "-0.18R")
        self.assertEqual("%.1f%%" % (100.0 * sum(1 for v in r if v > 0) / len(r)), "35.1%")
        self.assertEqual("%+.2fR" % (sum(float(x["r_before_costs"]) for x in rows) / len(rows)), "+0.02R")
        groups = [(0, 0.25, "under 0.25%"), (0.25, 0.5, "0.25% to 0.5%"), (0.5, 1, "0.5% to 1%"), (1, 2, "1% to 2%"),
                  (2, 1e9, "2% or more")]
        sec = self.section()
        for lo, hi, label in groups:
            g = [x for x in rows if lo <= float(x["stop_distance_pct"]) < hi]
            line = "| %s | %s | %+.2fR | %+.2fR |" % (label, "{:,}".format(len(g)),
                                                    sum(float(x["r_before_costs"]) for x in g) / len(g),
                                                    sum(float(x["r_after_costs"]) for x in g) / len(g))
            self.assertIn(line, sec)


if __name__ == "__main__":
    unittest.main()
