"""crypto-alerts: levels caught between checks, each alert once, plans and positions, the macro calendar."""

import json
import os
import unittest

from _load import cc, load, Session

ALERTS = ("crypto-alerts", "alerts")
A = load(*ALERTS)
MIN = 60000


def at(when):
    os.environ["FAKE_NOW"] = when
    os.environ["CRYPTO_NOW"] = when
    cc.set_now(when)


def rules(s):
    return dict((r["id"], r) for r in cc.read_json(s.path("alerts.json"))["rules"])


class StubVenue(object):
    """Just enough of a venue: a ticker and 1-minute candles the test controls."""

    def __init__(self, last, candles=(), mark=None, funding=None):
        self.last, self.candles, self.mark, self.funding = last, list(candles), mark, funding

    def tickers(self, fresh=False):
        return {"XUSDT": {"last": self.last, "mark": self.mark or self.last, "funding": self.funding, "funding_hours": 8.0}}

    def bars_between(self, symbol, interval, start, end):
        return [b for b in self.candles if start <= b[0] <= end]


def rule(kind, **kw):
    r = {"id": kind, "coin": "X", "symbol": "XUSDT", "venue": "stub", "kind": kind, "tick": 0.01, "source": "manual",
         "created_at": cc.parse_when("2026-10-21 12:00"), "expires_at": None, "rung_at": None, "rearm": False}
    r.update(kw)
    return r


class Rules(unittest.TestCase):
    def setUp(self):
        self.now = cc.parse_when("2026-10-21 12:30")
        cc.set_now(self.now)

    def tearDown(self):
        cc.set_now(None)

    def run_once(self, rs, venue, last_check=None, d=None):
        d = d or {"rules": rs, "macro_rung": {}}
        return A.evaluate(d, rs, {"stub": venue}, self.now, last_check)

    def test_a_touch_between_checks_rings_even_if_the_price_came_back(self):
        r = rule("above", price=105.0)
        candles = [[self.now - 20 * MIN, 100, 101, 99, 100], [self.now - 10 * MIN, 100, 105.5, 99.5, 101]]
        rung = self.run_once([r], StubVenue(101.0, candles), last_check=self.now - 30 * MIN)
        self.assertEqual(len(rung), 1)
        self.assertIn("touched 105.50 at 12:21 UTC; now 101.00", rung[0][1])
        self.assertEqual(self.run_once([r], StubVenue(106.0, candles)), [])            # once only

    def test_a_touch_before_the_rule_existed_does_not_count(self):
        r = rule("above", price=105.0, created_at=self.now - 5 * MIN)
        candles = [[self.now - 10 * MIN, 100, 106, 99, 100]]
        self.assertEqual(self.run_once([r], StubVenue(101.0, candles)), [])

    def test_rearm_needs_the_price_clearly_back(self):
        r = rule("above", price=100.0, rearm=True)
        self.assertEqual(len(self.run_once([r], StubVenue(100.5))), 1)
        self.run_once([r], StubVenue(99.9))                                              # inside the 0.25% gap: still rung
        self.assertIsNotNone(r["rung_at"])
        self.run_once([r], StubVenue(99.5))                                              # clearly back: re-armed, silent
        self.assertIsNone(r["rung_at"])
        self.assertEqual(len(self.run_once([r], StubVenue(100.2))), 1)

    def test_moves_funding_time(self):
        r = rule("pct", pct=3.0, ref=100.0, direction="down")
        self.assertEqual(self.run_once([r], StubVenue(97.5)), [])
        self.assertIn("-3.10%", self.run_once([r], StubVenue(96.9))[0][1])
        f = rule("funding", dir="above", apr=50.0)
        self.assertEqual(self.run_once([f], StubVenue(100, funding=0.0001)), [])          # about 11% a year
        self.assertIn("longs pay", self.run_once([f], StubVenue(100, funding=0.0006))[0][1])
        t = rule("time", at=self.now - 20 * MIN, label="look at the trade", symbol=None)
        msg = self.run_once([t], StubVenue(100))[0][1]
        self.assertIn("12:10 UTC: look at the trade (rang 20m late", msg)

    def test_past_the_stop_and_near_liquidation(self):
        p = rule("past_stop", stop=95.0, entry=100.0, side="long", source="position")
        self.assertEqual(self.run_once([p], StubVenue(94.0, mark=94.0)), [])            # 0.2R past: a stop can slip
        msg = self.run_once([p], StubVenue(93.5, mark=93.5))[0][1]
        self.assertIn("0.30R past the stop", msg)
        self.assertIn("may not be on the exchange", msg)
        q = rule("near_liq", liq=60.0, entry=100.0, side="long", source="position")
        self.assertEqual(self.run_once([q], StubVenue(71.0, mark=71.0)), [])
        self.assertIn("has only 22% of the distance to liquidation 60.00 left", self.run_once([q], StubVenue(69.0, mark=69.0))[0][1])


class OnTheFakeExchange(unittest.TestCase):
    def test_plan_alerts_through_the_day(self):
        with Session() as s:
            s.run("crypto-exit", "exit", ["plan", "SOL", "--side", "long", "--manual", "--venue", "binance",
                                          "--stop", "atr:1.5", "--hold", "8h"])
            code, out, err = s.run(*ALERTS, ["add", "--from-plan"])
            self.assertEqual(code, 0, err)
            self.assertEqual(out.count("Added"), 3)
            rs = rules(s)
            kinds = sorted(r["kind"] for r in rs.values())
            self.assertEqual(kinds, ["above", "below", "time"])
            code, out, err = s.run(*ALERTS, ["add", "--macro", "15"])
            code, out, err = s.run(*ALERTS, ["check"])
            self.assertEqual(code, 1)
            self.assertIn("Nothing rang", out)
            at("2026-10-21 17:50")
            code, out, err = s.run(*ALERTS, ["check"])
            self.assertEqual(code, 0)
            self.assertIn("SOL reached 175.827 (the plan's target): touched", out)
            self.assertIn("/crypto-broker sync", out)
            self.assertIn("USD CPI m/m at 2026-10-21 18:00 UTC, in 10m", out)
            rs = rules(s)
            stop = [r for r in rs.values() if r["kind"] == "below"][0]
            self.assertEqual(stop["retired"], "the plan's target rang")
            at("2026-10-21 17:55")
            code, out, err = s.run(*ALERTS, ["check"])
            self.assertEqual(code, 1)                                                     # CPI rang once, not twice
            code, out, err = s.run(*ALERTS, ["list"])
            self.assertIn("watching (last rang 17:50 UTC)", out)

    def test_position_alerts_retire_when_it_closes(self):
        with Session() as s:
            s.run("crypto-exit", "exit", ["plan", "SOL", "--side", "long", "--manual", "--venue", "binance",
                                          "--stop", "atr:1.5", "--hold", "8h"])
            s.run("crypto-budget", "size", [])
            s.run("crypto-risk", "check", [])
            o = cc.read_json(s.path("orders.json"))["orders"][0]
            code, out, err = s.run("crypto-broker", "broker", ["place", "--yes", cc.fingerprint(o)])
            self.assertEqual(code, 0, err)
            code, out, err = s.run(*ALERTS, ["add", "--from-positions"])
            self.assertEqual(code, 0, err)
            kinds = sorted(r["kind"] for r in rules(s).values())
            self.assertEqual(kinds, ["above", "below", "near_liq", "past_stop", "time"])
            code, out, err = s.run(*ALERTS, ["add", "--from-positions"])
            self.assertIn("already has its alerts", out)
            at("2026-10-21 19:00")
            s.run("crypto-broker", "broker", ["sync"])                                    # the target fills at 17:47
            code, out, err = s.run(*ALERTS, ["check"])
            self.assertTrue(all(r.get("retired") == "the position closed" for r in rules(s).values()))

    def test_manual_rules_and_refusals(self):
        with Session() as s:
            code, out, err = s.run(*ALERTS, ["add", "BTC", "--above", "1", "--venue", "binance"])
            self.assertIn("already above", err)
            code, out, err = s.run(*ALERTS, ["add", "BTC", "--venue", "binance"])
            self.assertIn("what should ring", err)
            code, out, err = s.run(*ALERTS, ["add", "BTC", "--pct", "0", "--venue", "binance"])
            self.assertIn("between 0 and 90", err)
            code, out, err = s.run(*ALERTS, ["check"])
            self.assertIn("no alerts set", err)
            code, out, err = s.run(*ALERTS, ["add", "BTC", "--above", "90000", "--venue", "binance"])
            self.assertIn("Added btc-above-90000: BTC above 90,000.0", out)
            code, out, err = s.run(*ALERTS, ["add", "--at", "2026-10-21 15:20", "--label", "five minutes"])
            code, out, err = s.run(*ALERTS, ["watch", "--every", "60", "--for", "1h"])
            self.assertEqual(code, 0)
            self.assertIn("2026-10-21 15:20 UTC: five minutes", out)
            code, out, err = s.run(*ALERTS, ["reset", "btc-above-90000"])
            self.assertIn("Re-armed", out)
            code, out, err = s.run(*ALERTS, ["remove", "btc-above-90000"])
            self.assertIn("Removed", out)
            code, out, err = s.run(*ALERTS, ["clear", "--all"])
            self.assertIn("Cleared 1 rule", out)
            code, out, err = s.run(*ALERTS, ["quote", "BTC,BONK", "--venue", "binance", "--json"])
            q = json.loads(out)["quotes"]
            self.assertEqual([x["coin"] for x in q], ["BTC", "BONK"])


if __name__ == "__main__":
    unittest.main()
