"""The README's worked example: the same chain on the test market, and every number it quotes."""

import os
import re
import unittest

from _load import ROOT, Session

# (what the README says, what a tool printed)
FACTS = [
    ("41 setups live in the scan", "41 setups live in the scan"),
    ("random\n   entries matched that only 0.2% of the time", "luck matches it 0.2% of the time"),
    ("the New York open, 1h45m ago, has a fresh one", "scout.py --session ny (the New York open, 1h45m ago)"),
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


if __name__ == "__main__":
    unittest.main()
