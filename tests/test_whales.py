"""crypto-whales: the leaderboard read defensively, who holds a coin, one wallet, and what followed wallets changed."""

import json
import os
import unittest

from _load import cc, load, Session
import fake_exchange as FX

WH = ("crypto-whales", "whales")
W = load(*WH)


def at(when):
    os.environ["FAKE_NOW"] = when
    os.environ["CRYPTO_NOW"] = when
    cc.set_now(when)


class Board(unittest.TestCase):
    def test_odd_rows_are_skipped_not_fatal(self):
        rows = W.parse_board(FX.leaderboard())
        addrs = set(r["address"] for r in rows)
        self.assertIn(FX.whale_address(1), addrs)
        self.assertNotIn(FX.whale_address(900), addrs)                 # account value NaN
        self.assertNotIn(FX.whale_address(901), addrs)                 # no window performances
        self.assertEqual(len(rows), len(FX.WHALES) + 40)
        with self.assertRaises(cc.CryptoError):
            W.parse_board("<html>")

    def test_ranking(self):
        rows = W.parse_board(FX.leaderboard())
        month = W.ranked(rows, "month", 100000)
        names = [r["name"] for r in month]
        self.assertEqual(names[0], "steady-eddie")
        self.assertNotIn("too-small", names)                           # under $100K
        self.assertNotIn("flat-month", names)                          # lost money this month
        self.assertTrue(month[0]["steady"])
        self.assertFalse([r for r in month if r["name"] == "night-owl"][0]["steady"])   # down on the week
        alltime = [r["name"] for r in W.ranked(rows, "allTime", 1e6)]
        self.assertIn("flat-month", alltime)

    def test_diff(self):
        a = [{"coin": "BTC", "side": "long", "size": 10.0}, {"coin": "ETH", "side": "short", "size": 5.0},
             {"coin": "SOL", "side": "long", "size": 100.0}, {"coin": "DOGE", "side": "long", "size": 1000.0}]
        b = [{"coin": "BTC", "side": "long", "size": 12.5}, {"coin": "ETH", "side": "long", "size": 5.0},
             {"coin": "SOL", "side": "long", "size": 90.0}, {"coin": "ARB", "side": "short", "size": 7.0}]
        got = dict(((p or q)["coin"], what) for what, p, q in W.diff(a, b))
        self.assertEqual(got, {"BTC": "added to", "ETH": "flipped", "ARB": "opened", "DOGE": "closed"})   # SOL -10%: noise


class Commands(unittest.TestCase):
    def test_top(self):
        with Session() as s:
            code, out, err = s.run(*WH, ["top", "--n", "5"])
            self.assertEqual(code, 0, err)
            self.assertIn("1  steady-eddie    $8.2M    $1.4M", out)
            self.assertIn("Context, not a signal", out)
            self.assertEqual(out.count("\n"), 9)

    def test_who_holds_a_coin(self):
        with Session() as s:
            code, out, err = s.run(*WH, ["coin", "BTC"])
            self.assertEqual(code, 0, err)
            self.assertIn("4 of 12 hold it: 3 long ($5.2M), 1 short ($2.0M). By size, 72% long.", out)
            self.assertIn("4 of 4 are in profit on it", out)
            d = cc.read_json(s.path("whales.json"))
            self.assertEqual(len(d["holders"]), 4)
            self.assertAlmostEqual(d["long_share"], 5.15 / 7.15, places=3)
            code, out, err = s.run(*WH, ["coin", "BONK", "--json"])
            d = json.loads(out)
            self.assertEqual([h["coin"] for h in d["holders"]], ["BONK"])           # kBONK read as BONK
            code, out, err = s.run(*WH, ["coin", "DOGE"])
            self.assertIn("None of them holds DOGE right now.", out)

    def test_one_wallet(self):
        with Session() as s:
            code, out, err = s.run(*WH, ["wallet", "0x123"])
            self.assertIn("40 hex characters", err)
            code, out, err = s.run(*WH, ["wallet", FX.whale_address(4), "--json"])
            d = json.loads(out)
            self.assertEqual([p["coin"] for p in d["positions"]], ["BONK"])        # the HIP-3 market is left out
            at("2026-10-21 18:00")
            code, out, err = s.run(*WH, ["wallet", FX.whale_address(3)])
            self.assertIn("SOL   long", out)
            self.assertIn("Close Short", out)
            self.assertIn("Closed P&L over those 2 fills, after fees: $14.9K.", out)

    def test_following_and_changes(self):
        with Session() as s:
            code, out, err = s.run(*WH, ["changes"])
            self.assertIn("not following anyone", err)
            s.run(*WH, ["follow", FX.whale_address(3), "--name", "owl"])
            code, out, err = s.run(*WH, ["follow", FX.whale_address(3)])
            self.assertIn("Already following", out)
            code, out, err = s.run(*WH, ["changes"])
            self.assertEqual(code, 1)
            self.assertIn("First look at owl", out)
            at("2026-10-21 17:30")
            code, out, err = s.run(*WH, ["changes"])
            self.assertEqual(code, 0, err)
            self.assertIn("owl opened a SOL long, $400.0K", out)
            self.assertIn("owl closed its ETH short", out)
            code, out, err = s.run(*WH, ["changes"])
            self.assertEqual(code, 1)
            self.assertIn("No changes since the last look.", out)
            code, out, err = s.run(*WH, ["unfollow", FX.whale_address(3)])
            self.assertIn("Stopped following", out)
            code, out, err = s.run(*WH, ["following"])
            self.assertIn("Not following anyone.", out)


if __name__ == "__main__":
    unittest.main()
