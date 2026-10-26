"""crypto-budget: the size from the max loss, the least leverage that fits, liquidation kept clear of the stop."""

import json
import math
import unittest

from _load import cc, Session

EXIT = ("crypto-exit", "exit")
SIZE = ("crypto-budget", "size")


def order_of(s):
    return cc.read_json(s.path("orders.json"))["orders"][0]


class Sizing(unittest.TestCase):
    def test_the_size_obeys_every_limit(self):
        for venue in ("binance", "bybit", "hyperliquid"):
            with Session() as s:
                s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual", "--venue", venue])
                code, out, err = s.run(*SIZE, [])
                self.assertEqual(code, 0, venue + err)
                o = order_of(s)
                cfg = cc.load_config()
                self.assertLessEqual(o["loss_at_stop_usd"], cfg["max_loss_per_trade_usd"] + 1e-9)
                self.assertLessEqual(o["margin"], cfg["account_usd"] * cfg["max_margin_pct"] / 100.0 + 1e-9)
                self.assertLessEqual(o["leverage"], cfg["max_leverage"])
                self.assertEqual(o["margin_mode"], "isolated")
                liq_d = abs(o["entry"] - o["liq_est"]) / o["entry"]
                stop_d = abs(o["entry"] - o["stop"]) / o["entry"]
                self.assertGreaterEqual(liq_d / stop_d, cfg["min_liq_buffer"])
                # the least leverage: one less would not fit the margin cap
                if o["leverage"] > 1:
                    self.assertGreater(o["notional"] / (o["leverage"] - 1), cfg["account_usd"] * cfg["max_margin_pct"] / 100.0)
                # one more step of size would break the max loss (when the max loss decides it)
                if o["limits"]["decided_by"] == "max loss":
                    self.assertGreater((o["qty"] + (o["step"] or 0)) * o["limits"]["unit_loss"], cfg["max_loss_per_trade_usd"])
                self.assertIn("A stop is an order, not a promise", out)

    def test_margin_cap_decides_on_a_small_account(self):
        with Session(config={"account_usd": 200, "max_loss_per_trade_usd": 10}) as s:
            s.run(*EXIT, ["plan", "BTC", "--side", "long", "--manual", "--venue", "binance", "--stop", "pct:0.5"])
            code, out, err = s.run(*SIZE, [])
            self.assertEqual(code, 0, err)
            o = order_of(s)
            self.assertEqual(o["limits"]["decided_by"], "margin cap")
            self.assertEqual(o["leverage"], 3)
            self.assertLess(o["loss_at_stop_usd"], 10)

    def test_risk_flag_and_its_cap(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            s.run(*SIZE, ["--risk", "4"])
            self.assertLessEqual(order_of(s)["loss_at_stop_usd"], 4.0 + 1e-9)
            code, out, err = s.run(*SIZE, ["--risk", "50"])
            self.assertIn("your max per trade is $10.00", out)
            self.assertLessEqual(order_of(s)["loss_at_stop_usd"], 10.0 + 1e-9)

    def test_leverage_flag(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            code, out, err = s.run(*SIZE, ["--leverage", "5"])
            self.assertEqual(code, 1)
            self.assertIn("over the 3x", err)
            code, out, err = s.run(*SIZE, ["--leverage", "3"])
            self.assertEqual(code, 0, err)
            self.assertEqual(order_of(s)["leverage"], 3)

    def test_too_small(self):
        with Session(config={"max_loss_per_trade_usd": 0.05}) as s:
            s.run(*EXIT, ["plan", "BTC", "--side", "long", "--manual", "--venue", "binance"])
            code, out, err = s.run(*SIZE, [])
            self.assertEqual(code, 1)
            self.assertIn("too small to trade", err)
            self.assertIsNone(cc.read_json(s.path("orders.json")))

    def test_a_price_that_ran_shrinks_the_size_and_too_far_refuses(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "ORBX", "--side", "long", "--manual", "--venue", "binance"])
            plan = cc.read_json(s.path("plan.json"))
            # a plan made 30 minutes earlier at a lower price, same stop
            plan["entry"] = cc.round_to_tick(plan["entry"] - 0.2 * plan["risk"], plan["tick"])
            plan["risk"] = plan["entry"] - plan["stop"]
            cc.write_json(s.path("plan.json"), plan)
            code, out, err = s.run(*SIZE, [])
            self.assertEqual(code, 0, err)
            o = order_of(s)
            self.assertGreater(o["entry"], plan["entry"])
            self.assertIn("Sized from the price now", out)
            plan["entry"] = cc.round_to_tick(plan["entry"] - 0.6 * plan["risk"], plan["tick"])
            plan["risk"] = plan["entry"] - plan["stop"]
            cc.write_json(s.path("plan.json"), plan)
            code, out, err = s.run(*SIZE, [])
            self.assertEqual(code, 1)
            self.assertIn("Re-plan", err)

    def test_limit_order(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            s.run(*SIZE, ["--limit"])
            o = order_of(s)
            self.assertEqual(o["type"], "limit")
            self.assertEqual(o["limit"], cc.read_json(s.path("plan.json"))["entry"])

    def test_time_machine_is_linked_only_to_its_own_plan(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            s.run("crypto-timemachine", "timemachine", ["run", "--replays", "300", "--no-baseline"])
            s.run(*SIZE, [])
            self.assertIsNotNone(order_of(s)["timemachine"])
            # another plan made in the same minute must not inherit that verdict
            s.run(*EXIT, ["plan", "ETH", "--side", "long", "--manual"])
            code, out, err = s.run(*SIZE, [])
            self.assertIsNone(order_of(s)["timemachine"])
            self.assertIn("Time Machine: not run for this plan", out)

    def test_refusals(self):
        with Session(config={"blocked": ["SOL"]}) as s:
            code, out, err = s.run(*SIZE, [])
            self.assertIn("no plan to size", err)
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            code, out, err = s.run(*SIZE, [])
            self.assertIn("blocked list", err)
        with Session() as s:
            s.run(*EXIT, ["plan", "SOL", "--side", "long", "--manual"])
            plan = cc.read_json(s.path("plan.json"))
            plan["created_at"] -= 13 * 3600000
            cc.write_json(s.path("plan.json"), plan)
            code, out, err = s.run(*SIZE, [])
            self.assertIn("old", err)

    def test_thousand_unit_contracts(self):
        with Session() as s:
            s.run(*EXIT, ["plan", "BONK", "--side", "short", "--manual", "--venue", "binance", "--stop", "pct:1.2"])
            code, out, err = s.run(*SIZE, [])
            self.assertEqual(code, 0, err)
            o = order_of(s)
            self.assertEqual(o["symbol"], "1000BONKUSDT")
            self.assertIn("1000BONK (= ", out)
            self.assertIn(" BONK)", out)

    def test_per_coin_math(self):
        mod = __import__("_load").load(*SIZE)
        c = {"taker": 0.0005, "maker": 0.0002, "slippage": 0.0001}
        self.assertAlmostEqual(mod.per_coin_loss("long", 100.0, 98.0, c, 0.0001, 3), 2.0 + 0.0006 * 198 + 0.03)
        self.assertAlmostEqual(mod.per_coin_loss("short", 100.0, 102.0, c, 0.0001, 3), 2.0 + 0.0006 * 202)   # shorts receive
        self.assertAlmostEqual(mod.per_coin_gain("long", 100.0, 104.0, c, 0.0, 0), 4.0 - 0.06 - 0.0208)


if __name__ == "__main__":
    unittest.main()
