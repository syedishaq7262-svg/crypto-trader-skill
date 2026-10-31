"""crypto-pulse: the biggest moves, volume against a usual day, what open interest says, the headlines naming them,
and the backtest's record for chasing, on the test exchange; and it keeps going when a source fails."""

import json
import os
import unittest

from _load import ROOT, Session, cc, load

PULSE = load("crypto-pulse", "pulse")


def run_json(s, argv=()):
    code, out, err = s.run("crypto-pulse", "pulse", ["--json"] + list(argv))
    assert code == 0, err
    return json.loads(out)


class Pulse(unittest.TestCase):
    def test_the_skill_example_is_this_run(self):
        with open(os.path.join(ROOT, "crypto-pulse", "SKILL.md"), encoding="utf-8") as fh:
            doc = fh.read()
        block = doc[doc.index("## Example"):]
        block = block[block.index("```\n") + 4:]
        block = block[:block.index("```")]
        with Session() as s:
            code, out, err = s.run("crypto-pulse", "pulse", [])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), block.strip())

    def test_the_movers_are_the_biggest_moves(self):
        with Session() as s:
            d = run_json(s, ["--venue", "binance", "--n", "4"])
            v = cc.get_venue("binance")
            uni, _ = cc.build_universe(v, cc.load_config(), top=60)
            ch = sorted(((t["change"], m["coin"]) for m, t in uni), reverse=True)
        self.assertEqual([r["coin"] for r in d["up"]], [c for x, c in ch if x > 0][:4])
        self.assertEqual([r["coin"] for r in d["down"]], [c for x, c in sorted(ch) if x < 0][:4])
        self.assertEqual(d["coins"], len(uni))
        self.assertEqual(d["mood"]["up"], sum(1 for x, _ in ch if x > 0))
        self.assertTrue(os.path.basename(cc.wpath("pulse.json")))

    def test_volume_against_a_usual_day(self):
        with Session() as s:
            d = run_json(s, ["--venue", "binance"])
            v = cc.get_venue("binance")
            row = d["up"][0]
            bars = v.bars(row["symbol"], "1d", 9)
            usual = sum(b[cc.QV] for b in bars[-7:]) / 7.0
            t = v.ticker(row["symbol"])
        self.assertAlmostEqual(row["rvol"], t["quote_volume"] / usual)
        self.assertAlmostEqual(row["ch7d"], t["last"] / bars[-8][cc.C] - 1.0)

    def test_what_open_interest_says(self):
        with Session() as s:
            d = run_json(s, ["--venue", "binance"])
        for r in d["up"] + d["down"]:
            want = cc.crowding(r["funding_apr"], r["oi_24h"], None, r["change"]) or \
                cc.flow(r["change"], r["oi_24h"], r["funding_apr"])
            self.assertEqual(r["read"], want, r["coin"])
        crowded = dict((r["coin"], r["crowding"]) for r in d["crowded"])
        self.assertEqual(crowded.get("FUNDX"), "crowded longs")             # the test market's crowded coins
        self.assertEqual(crowded.get("SHRTX"), "crowded shorts")

    def test_headlines_new_listings_and_notices(self):
        with Session() as s:
            d = run_json(s, ["--venue", "binance"])
        sol = [r for r in d["down"] if r["coin"] == "SOL"][0]
        self.assertEqual(sol["headline"]["title"], "SOL Climbs as Solana Funds Draw Fresh Inflows")
        self.assertEqual(sol["stories"], 2)
        most = dict((c, k) for c, k in d["news"]["most"])
        self.assertEqual(most.get("BTC"), 2)                                # one story told by two outlets counts once
        self.assertEqual([x["coin"] for x in d["new_listings"]], ["NEWX"])
        kinds = sorted(n["kind"] for n in d["notices"])
        self.assertEqual(kinds, ["delistings", "new_crypto"])
        self.assertEqual([e["title"] for e in d["macro"]][:2], ["CPI m/m", "FOMC Statement"])     # PCE came out 3h ago

    def test_the_chase_record_is_the_track_record(self):
        with Session() as s:
            d = run_json(s, ["--no-news"])
        for r in d["chase"]["rows"]:
            e = cc.track_entry(r["setup"], r["side"])
            self.assertEqual((r["n"], r["avg_r"]), (e["n"], e["avg_r"]))
        lines = PULSE.chase_lines(d)
        self.assertTrue(lines[0].startswith("What is moving is not a reason to buy it."))
        self.assertIn("buying volume spikes -0.05R a trade over 4,494 (can't be told from zero)", lines[0])

    def test_it_keeps_going_when_sources_fail(self):
        with Session(mode="macro_down") as s:
            code, out, err = s.run("crypto-pulse", "pulse", ["--venue", "binance"])
        self.assertEqual(code, 0, err)
        self.assertIn("Calendar: not read just now", out)
        self.assertIn("Up most in 24h", out)
        with Session() as s:
            code, out, err = s.run("crypto-pulse", "pulse", ["--no-news", "--venue", "bybit"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Headlines naming them", out)
        self.assertNotIn("Calendar", out)
        self.assertIn("What is moving is not a reason to buy it.", out)

    def test_a_venue_without_open_interest_history(self):
        with Session() as s:
            code, out, err = s.run("crypto-pulse", "pulse", ["--venue", "hyperliquid"])
        self.assertEqual(code, 0, err)
        self.assertIn("This venue keeps no open-interest history", out)
        self.assertIn("the 27 busiest perpetuals", out)

    def test_limits_and_files(self):
        with Session() as s:
            for argv, words in ((["--top", "2"], "--top must be between"), (["--n", "50"], "--n must be between")):
                code, out, err = s.run("crypto-pulse", "pulse", argv)
                self.assertEqual(code, 1)
                self.assertIn(words, err)
            code, out, err = s.run("crypto-pulse", "pulse", ["--no-news"])
            self.assertEqual(code, 0, err)
            saved = cc.read_json(s.path("pulse.json"))
            for k in ("mood", "up", "down", "surges", "crowded", "new_listings", "chase"):
                self.assertIn(k, saved)
            code, out, err = s.run("crypto-pulse", "pulse", ["--no-news", "--no-write", "--n", "2"])
            self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
