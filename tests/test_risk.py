"""crypto-risk: every block, every warning, batches, exits that are never blocked."""

import json
import unittest

from _load import cc, Session

EXIT = ("crypto-exit", "exit")
SIZE = ("crypto-budget", "size")
RISK = ("crypto-risk", "check")
LATER = "2026-10-21 15:15"


def ready(s, coin="SOL", venue="binance", side="long", *extra):
    s.run(*EXIT, ["plan", coin, "--side", side, "--manual", "--venue", venue] + list(extra))
    s.run(*SIZE, [])


def edit(s, **changes):
    doc = cc.read_json(s.path("orders.json"))
    doc["orders"][0].update(changes)
    cc.write_json(s.path("orders.json"), doc)
    return doc["orders"][0]


def check(s, *argv):
    code, out, err = s.run(*RISK, list(argv))
    o = cc.read_json(s.path("orders.json"))["orders"][0]
    return code, out, err, o.get("risk") or {}


def log_trades(s, pnls, start="2026-10-21 12:00", symbol="ADAUSDT"):
    t = cc.parse_when(start)
    for i, pnl in enumerate(pnls):
        cc.append_jsonl(s.path("ledger.jsonl"), {"type": "open", "at": t + i * 3600000, "symbol": symbol})
        cc.append_jsonl(s.path("ledger.jsonl"), {"type": "close", "at": t + i * 3600000 + 1800000, "symbol": symbol, "pnl_usd": pnl})


class Gate(unittest.TestCase):
    def test_clean_pass_is_stamped(self):
        with Session() as s:
            ready(s)
            code, out, err, r = check(s)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(r["verdict"], "PASS")
            o = cc.read_json(s.path("orders.json"))["orders"][0]
            self.assertEqual(r["fingerprint"], cc.fingerprint(o))
            self.assertEqual(r["valid_until"] - r["checked_at"], 15 * 60000)
            self.assertIn("not replayed", " ".join(r["warnings"]))
            self.assertIn("it is not a yes", out)
            self.assertEqual(len(cc.read_jsonl(s.path("risk_log.jsonl"))), 1)

    def assertBlocked(self, r, word):
        self.assertEqual(r.get("verdict"), "BLOCK", r)
        self.assertTrue(any(x.startswith(word) for x in r["reasons"]), (word, r["reasons"]))

    def test_each_rule_blocks(self):
        cases = [
            ({"qty_mult": 2.0}, "loss"),
            ({"leverage": 5}, "leverage"),
            ({"margin_mode": "cross"}, "margin"),
            ({"stop": None}, "stop"),
            ({"target_near": True}, "reward:risk"),
            ({"exit_by": cc.parse_when("2026-10-21 10:00")}, "time"),
            ({"qty_offstep": True}, "format"),
            ({"stop_offtick": True}, "format"),
        ]
        for change, word in cases:
            with Session() as s:
                ready(s)
                o = cc.read_json(s.path("orders.json"))["orders"][0]
                upd = {}
                if "qty_mult" in change:
                    upd["qty"] = cc.floor_step(o["qty"] * change["qty_mult"], o["step"])
                elif "target_near" in change:
                    upd["target"] = cc.round_to_tick(o["entry"] + 0.5 * (o["entry"] - o["stop"]), o["tick"])
                elif "qty_offstep" in change:
                    upd["qty"] = o["qty"] + o["step"] / 3.0
                elif "stop_offtick" in change:
                    upd["stop"] = o["stop"] - o["tick"] / 3.0
                else:
                    upd = change
                edit(s, **upd)
                code, out, err, r = check(s)
                self.assertEqual(code, 1, (change, out))
                self.assertBlocked(r, word)

    def test_blocked_list_and_shorts(self):
        with Session() as s:
            ready(s)
            cfg = {"blocked": ["sol"], "allow_short": False}
            cc.write_json(s.path("config.json"), cfg)
            edit(s, side="short", stop=None)
            code, out, err, r = check(s)
            self.assertBlocked(r, "blocked")
            self.assertBlocked(r, "short")

    def test_price_moved(self):
        with Session() as s:
            ready(s, "SOL")
            o = cc.read_json(s.path("orders.json"))["orders"][0]
            risk = o["entry"] - o["stop"]
            # an order planned when the price was 0.8R lower, same stop: the price has run since
            edit(s, entry=cc.round_to_tick(o["entry"] - 0.8 * risk, o["tick"]), stop=cc.round_to_tick(o["stop"] - 0.8 * risk, o["tick"]),
                 target=cc.round_to_tick(o["target"] - 0.8 * risk, o["tick"]))
            code, out, err, r = check(s)
            self.assertBlocked(r, "moved")
            self.assertIn("(the price now)", out)

    def test_daily_loss_trades_and_cooldown(self):
        with Session() as s:
            ready(s)
            log_trades(s, [-12.0, -10.0])                               # $22 lost + $10 here > $30
            code, out, err, r = check(s)
            self.assertBlocked(r, "daily loss")
            self.assertBlocked(r, "cooldown")
        with Session(config={"max_trades_per_day": 2}) as s:
            ready(s)
            log_trades(s, [5.0, 6.0])
            code, out, err, r = check(s)
            self.assertBlocked(r, "trades")
            self.assertFalse(any(x.startswith("cooldown") for x in r["reasons"]))
        with Session() as s:
            ready(s)
            log_trades(s, [-4.0, -4.0], start="2026-10-20 10:00")      # yesterday: not today's loss; cooldown over
            code, out, err, r = check(s)
            self.assertEqual(r["verdict"], "PASS", r)

    def test_open_positions(self):
        with Session(config={"max_open_positions": 1}) as s:
            ready(s)
            cc.write_json(s.path("positions.json"), {"positions": [{"symbol": "SOLUSDT", "margin": 100, "loss_at_stop_usd": 5}]})
            code, out, err, r = check(s)
            self.assertBlocked(r, "duplicate")
            self.assertBlocked(r, "positions")

    def test_liquidation_buffer(self):
        with Session(config={"max_leverage": 50, "min_liq_buffer": 2.0, "max_margin_pct": 100}) as s:
            ready(s, "BTC", "binance", "long", "--stop", "pct:3")
            edit(s, leverage=40)
            code, out, err, r = check(s)
            self.assertBlocked(r, "liquidation")
            self.assertIn("or less passes", " ".join(r["reasons"]))

    def test_liquidity(self):
        with Session() as s:
            vol = cc.get_venue("binance").ticker("SOLUSDT")["quote_volume"]
            ready(s)
            cc.write_json(s.path("config.json"), {"min_quote_volume_usd": vol * 5})     # volume is under a quarter
            code, out, err, r = check(s)
            self.assertBlocked(r, "liquidity")
            cc.write_json(s.path("config.json"), {"min_quote_volume_usd": vol * 2})     # under the floor, over a quarter
            code, out, err, r = check(s)
            self.assertEqual(r["verdict"], "PASS")
            self.assertIn("thin:", " ".join(r["warnings"]))

    def test_exits_are_never_blocked(self):
        with Session() as s:
            ready(s)
            cc.write_json(s.path("config.json"), {"max_trades_per_day": 0, "blocked": ["SOL"]})
            cc.write_json(s.path("positions.json"), {"positions": [{"symbol": "SOLUSDT", "margin": 100, "loss_at_stop_usd": 5}]})
            log_trades(s, [-20.0, -20.0])
            edit(s, reduce_only=True, action="close", side="short", stop=None, leverage=99)
            code, out, err, r = check(s)
            self.assertEqual(r["verdict"], "PASS", r)
            self.assertIn("exit", out)

    def test_a_batch_is_checked_as_if_each_pass_filled(self):
        with Session(config={"max_daily_loss_usd": 15}) as s:
            ready(s, "SOL")
            first = cc.read_json(s.path("orders.json"))["orders"][0]
            ready(s, "ETH")
            second = cc.read_json(s.path("orders.json"))["orders"][0]
            cc.write_json(s.path("orders.json"), {"orders": [first, second]})
            code, out, err = s.run(*RISK, [])
            doc = cc.read_json(s.path("orders.json"))["orders"]
            self.assertEqual(doc[0]["risk"]["verdict"], "PASS")
            self.assertEqual(doc[1]["risk"]["verdict"], "BLOCK")
            self.assertTrue(any(x.startswith("daily loss") for x in doc[1]["risk"]["reasons"]))

    def test_warnings(self):
        with Session(config={"paper": False}) as s:
            ready(s, "FUNDX")
            code, out, err, r = check(s)
            w = " ".join(r["warnings"])
            self.assertIn("funding: +104% a year against this side", w)
            self.assertIn("LIVE", w)

    def test_no_orders(self):
        with Session() as s:
            code, out, err = s.run(*RISK, [])
            self.assertEqual(code, 1)
            self.assertIn("no orders to check", err)


if __name__ == "__main__":
    unittest.main()
