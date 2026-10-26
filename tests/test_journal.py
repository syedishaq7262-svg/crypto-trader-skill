"""crypto-journal: exact stats on a known ledger, honest ranges, patterns, notes, and read-only imports."""

import json
import os
import unittest

from _load import cc, load, Session, FAKE

J = ("crypto-journal", "journal")
JM = load(*J)
FA = load("tests", "fake_accounts")
BASE = cc.parse_when("2026-10-10 12:00")
HOUR = 3600000


def fake_accounts():
    return cc._load_transport(FAKE).__globals__["fake_accounts"]


def trade(s, i, r, hours_after=6, hold_h=2, coin="SOL", side="long", setup="orb", reason=None, at=None):
    t = at if at is not None else BASE + i * hours_after * HOUR
    oid = "t%d" % i
    cc.append_jsonl(s.path("ledger.jsonl"), {"type": "open", "at": t, "id": oid, "coin": coin, "symbol": coin + "USDT",
                                             "side": side, "setup": setup, "risk_usd": 10.0, "paper": True})
    cc.append_jsonl(s.path("ledger.jsonl"), {"type": "close", "at": t + hold_h * HOUR, "id": oid, "coin": coin,
                                             "symbol": coin + "USDT", "side": side, "setup": setup, "pnl_usd": r * 10.0, "r": r,
                                             "fee": 0.5, "funding": 0.1, "reason": reason or ("target" if r > 0 else "stop"),
                                             "hold_ms": hold_h * HOUR, "paper": True})
    return t + hold_h * HOUR


class Stats(unittest.TestCase):
    def test_numbers_on_a_known_ledger(self):
        with Session(now="2026-10-21 15:15") as s:
            rs = [2.0, -1.0, -1.0, 2.0, -1.0, -1.0, -1.0, 2.0, 2.0, -1.0]
            for i, r in enumerate(rs):
                trade(s, i, r, hold_h=1 if r > 0 else 3)
            code, out, err = s.run(*J, ["--days", "0"])
            self.assertEqual(code, 0, err)
            self.assertIn("10 trades: 4 won, 6 lost. Win rate 40% (95% range 17% to 69%).", out)
            self.assertIn("10 trades are too few to judge", out)
            self.assertIn("Average +0.20R a trade, +$2.00 a trade; total +$20.00. Profit factor 1.33.", out)
            self.assertIn("Break-even at your average win and loss: 33%. Your win rate is above it, and the range includes it.", out)
            self.assertIn("Max drawdown -$30.00 (-3.0R)", out)                     # the three losses in a row
            self.assertIn("Longest losing streak 3; now 1 loss in a row.", out)
            self.assertIn("Costs: fees $5.00, funding $1.00", out)
            self.assertIn("Losers were held 3.0x longer than winners", out)
            d = cc.read_json(s.path("journal.json"))
            self.assertAlmostEqual(d["stats"]["avg_r"], 0.2)
            self.assertAlmostEqual(d["stats"]["profit_factor"], 80.0 / 60.0)

    def test_filters_and_breakdowns(self):
        with Session(now="2026-10-21 15:15") as s:
            trade(s, 0, 2.0, setup="orb", coin="BTC")
            trade(s, 1, -1.0, setup="breakout", coin="ETH", side="short")
            trade(s, 2, -1.0, setup=None, coin="ETH")
            code, out, err = s.run(*J, ["--days", "0"])
            self.assertIn("By setup:", out)
            self.assertIn("manual 1 (0 won) -1.00R", out)
            self.assertIn("By side:", out)
            code, out, err = s.run(*J, ["--days", "0", "--coin", "ETH", "--json"])
            self.assertEqual(json.loads(out)["stats"]["n"], 2)
            code, out, err = s.run(*J, ["--days", "0", "--setup", "orb", "--json"])
            self.assertEqual(json.loads(out)["stats"]["n"], 1)
            code, out, err = s.run(*J, ["--days", "3"])
            self.assertEqual(code, 1)
            self.assertIn("No closed trades in that window", out)
            code, out, err = s.run(*J, ["--paper", "--live"])
            self.assertIn("pick one", err)

    def test_a_trade_right_after_a_loss_is_flagged(self):
        with Session(now="2026-10-21 15:15") as s:
            end = trade(s, 0, -1.0)
            trade(s, 1, -1.2, at=end + 10 * 60000)
            trade(s, 2, 2.0, at=end + 20 * HOUR)
            code, out, err = s.run(*J, ["--days", "0"])
            self.assertIn("! 1 trade opened within 30 minutes of a loss: -1.20R on average (the rest: +0.50R).", out)

    def test_notes(self):
        with Session(now="2026-10-21 15:15") as s:
            code, out, err = s.run(*J, ["note", "x"])
            self.assertIn("no closed trade", err)
            trade(s, 0, 2.0)
            trade(s, 1, -1.0)
            code, out, err = s.run(*J, ["note", "chased it"])
            self.assertIn("Noted on SOL long -1.00R", out)
            code, out, err = s.run(*J, ["note", "patient", "--trade", "t0"])
            self.assertIn("+2.00R", out)
            code, out, err = s.run(*J, ["trades", "--days", "0"])
            self.assertIn('"chased it"', out)
            self.assertIn('"patient"', out)


class Imports(unittest.TestCase):
    def live(self, venue):
        s = Session(config={"paper": False}).__enter__()
        names = {"binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET"), "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET")}[venue]
        os.environ[names[0]], os.environ[names[1]] = FA.KEY, FA.SECRET
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in names])
        self.addCleanup(lambda: s.__exit__(None, None, None))
        s.run("crypto-exit", "exit", ["plan", "SOL", "--side", "long", "--manual", "--venue", venue, "--stop", "atr:1.0",
                                      "--target", "1.5R", "--hold", "8h"])
        s.run("crypto-budget", "size", [])
        s.run("crypto-risk", "check", [])
        o = cc.read_json(s.path("orders.json"))["orders"][0]
        code, out, err = s.run("crypto-broker", "broker", ["place", "--yes", cc.fingerprint(o), "--live"])
        self.assertEqual(code, 0, err)
        return s, o

    def test_bybit_closed_pnl_and_no_double_count(self):
        s, o = self.live("bybit")
        fake_accounts().move_price("SOLUSDT", o["target"] * 1.001)
        s.run("crypto-broker", "broker", ["sync"])
        code, out, err = s.run(*J, ["import", "bybit", "--days", "7"])
        self.assertEqual(code, 0, err)
        self.assertIn("Read 1 closed trade from Bybit", out)
        self.assertIn("Nothing was changed on the exchange", out)
        code, out, err = s.run(*J, ["import", "bybit", "--days", "7"])
        self.assertIn("0 new, 1 already imported", out)
        code, out, err = s.run(*J, ["--live", "--json"])
        self.assertEqual(json.loads(out)["stats"]["n"], 1)                      # the broker's record and the import are one trade

    def test_binance_income_grouped_into_a_trade(self):
        s, o = self.live("binance")
        code, out, err = s.run("crypto-broker", "broker", ["close", "SOL", "--yes", "SOL"])
        self.assertEqual(code, 0, err)
        os.remove(s.path("ledger.jsonl"))                                       # as if traded elsewhere: only the import knows
        code, out, err = s.run(*J, ["import", "binance", "--days", "7"])
        self.assertEqual(code, 0, err)
        self.assertIn("Read 1 closed trade from Binance", out)
        t = cc.read_jsonl(s.path("imported.jsonl"))[0]
        self.assertEqual((t["coin"], t["side"], t["r"]), ("SOL", "long", None))
        self.assertAlmostEqual(t["fee"], 0.001 * o["qty"] * o["entry"], delta=0.05)  # the opening and the closing commission
        code, out, err = s.run(*J, ["--live"])
        self.assertIn("1 imported trade have no planned stop, so no R.", out.replace("trades have", "trade have"))

    def test_import_refusals(self):
        with Session() as s:
            code, out, err = s.run(*J, ["import", "kraken"])
            self.assertIn("binance or bybit", err)
            code, out, err = s.run(*J, ["import", "bybit"])
            self.assertIn("never in the chat", err)


if __name__ == "__main__":
    unittest.main()
