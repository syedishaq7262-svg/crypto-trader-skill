"""crypto-broker: the yes, paper fills and play-forward, and live Binance and Bybit against fake accounts
that check signatures, steps, ticks and the Algo Order rule like the real exchanges."""

import json
import os
import unittest

from _load import cc, load, Session, FAKE

BROKER = ("crypto-broker", "broker")
EX = load("crypto-broker", "exchanges")
FA = load("tests", "fake_accounts")          # for the test keys only
SOL = "SOLUSDT"


def fake_accounts():
    """The fake-accounts module instance the fake exchange really answers from (its state lives there)."""
    return cc._load_transport(FAKE).__globals__["fake_accounts"]


def ready_order(s, coin="SOL", venue="binance", side="long", stop="atr:1.0", target="1.5R", hold="8h", extra=()):
    s.run("crypto-exit", "exit", ["plan", coin, "--side", side, "--manual", "--venue", venue, "--stop", stop,
                                  "--target", target, "--hold", hold] + list(extra))
    code, out, err = s.run("crypto-budget", "size", [])
    assert code == 0, err
    code, out, err = s.run("crypto-risk", "check", [])
    o = cc.read_json(s.path("orders.json"))["orders"][0]
    return o


def place(s, o, *extra):
    return s.run(*BROKER, ["place", "--yes", cc.fingerprint(o)] + list(extra))


def at(s, when):
    os.environ["FAKE_NOW"] = when
    os.environ["CRYPTO_NOW"] = when
    cc.set_now(when)


def ledger(s, kind=None):
    return [e for e in cc.read_jsonl(s.path("ledger.jsonl")) if kind is None or e["type"] == kind]


class Yes(unittest.TestCase):
    def test_ticket_and_the_yes(self):
        with Session() as s:
            s.run("crypto-exit", "exit", ["plan", "SOL", "--side", "long", "--manual"])
            s.run("crypto-budget", "size", [])
            code, out, err = s.run(*BROKER, ["ticket"])
            self.assertEqual(code, 1)
            self.assertIn("NOT READY: crypto-risk has not passed it", out)
            s.run("crypto-risk", "check", [])
            o = cc.read_json(s.path("orders.json"))["orders"][0]
            code, out, err = s.run(*BROKER, ["ticket"])
            self.assertEqual(code, 0, out)
            self.assertIn("READY. Fingerprint %s" % cc.fingerprint(o), out)
            self.assertIn("only after you type yes", out)
            code, out, err = s.run(*BROKER, ["place", "--yes", "nope"])
            self.assertEqual(code, 1)
            self.assertIn("must be this order's fingerprint", err)
            code, out, err = place(s, o)
            self.assertEqual(code, 0, err)
            code, out, err = place(s, o)
            self.assertIn("already placed", err)

    def test_changed_or_stale_orders_are_not_ready(self):
        with Session() as s:
            o = ready_order(s)
            doc = cc.read_json(s.path("orders.json"))
            doc["orders"][0]["qty"] = o["qty"] * 2
            cc.write_json(s.path("orders.json"), doc)
            code, out, err = s.run(*BROKER, ["ticket"])
            self.assertIn("changed after crypto-risk checked it", out)
        with Session() as s:
            o = ready_order(s)
            at(s, "2026-10-21 15:31")
            code, out, err = place(s, o)
            self.assertIn("run /crypto-risk again", err)

    def test_live_needs_both_the_config_and_the_flag(self):
        with Session() as s:
            o = ready_order(s)
            code, out, err = place(s, o, "--live")
            self.assertIn("your config is paper", err)
        with Session(config={"paper": False}) as s:
            o = ready_order(s)
            code, out, err = place(s, o)
            self.assertIn("Add --live", err)


class Paper(unittest.TestCase):
    def test_open_r_counts_costs_like_a_close(self):
        with Session() as s:
            o = ready_order(s)
            code, out, err = place(s, o)
            self.assertEqual(code, 0, err)
            code, out, err = s.run(*BROKER, ["positions", "--json"])
            row = json.loads(out)["positions"][0]
            self.assertAlmostEqual(row["r_now"], row["unrealized_usd"] / row["risk_usd"])
            self.assertEqual(row["r_now"] > 0, row["unrealized_usd"] > 0)          # never "-$0.53 (+0.04R)"

    def test_target_stop_and_time_limit_play_forward(self):
        with Session() as s:
            o = ready_order(s)
            code, out, err = place(s, o)
            self.assertEqual(code, 0, err)
            pos = cc.read_json(s.path("positions.json"))["positions"][0]
            self.assertTrue(pos["paper"])
            self.assertAlmostEqual(pos["fee_in"], pos["qty"] * pos["entry"] * o["costs"]["taker"])
            # read the real path ahead, then set the stop and target so each case is known
            v = cc.get_venue("binance")
            at(s, "2026-10-21 18:00")
            cc.reset_http()
            path = v.bars_between(SOL, "1m", pos["opened_at"], cc.now_ms())
            lo = min(b[3] for b in path)
            hi = max(b[2] for b in path)
            first_low = next(b for b in path if b[3] <= lo + 1e-12)
            book = cc.read_json(s.path("positions.json"))
            book["positions"][0].update(stop=lo + (pos["entry"] - lo) * 0.001, target=hi * 1.5, liq=lo * 0.5)
            cc.write_json(s.path("positions.json"), book)
            code, out, err = s.run(*BROKER, ["sync"])
            self.assertIn("closed", out)
            close = ledger(s, "close")[-1]
            self.assertEqual(close["reason"], "stop")
            self.assertLessEqual(close["at"], first_low[0] + 60000)
            self.assertLess(close["pnl_usd"], 0)

    def test_time_limit(self):
        with Session() as s:
            o = ready_order(s, hold="1h")
            place(s, o)
            book = cc.read_json(s.path("positions.json"))
            p = book["positions"][0]
            p.update(stop=p["entry"] * 0.5, target=p["entry"] * 1.5, liq=p["entry"] * 0.1)
            cc.write_json(s.path("positions.json"), book)
            at(s, "2026-10-21 17:00")
            s.run(*BROKER, ["sync"])
            close = ledger(s, "close")[-1]
            self.assertEqual(close["reason"], "time limit")
            self.assertEqual(close["at"], p["exit_by"])

    def test_funding_is_charged_across_a_payment(self):
        with Session() as s:
            o = ready_order(s, hold="24h")
            place(s, o)
            book = cc.read_json(s.path("positions.json"))
            p = book["positions"][0]
            p.update(stop=p["entry"] * 0.5, target=p["entry"] * 1.5, liq=p["entry"] * 0.1)
            cc.write_json(s.path("positions.json"), book)
            at(s, "2026-10-21 16:30")                                     # Binance pays at 16:00 UTC
            s.run(*BROKER, ["sync"])
            p = cc.read_json(s.path("positions.json"))["positions"][0]
            self.assertNotEqual(p["funding_paid"], 0.0)

    def test_limit_waits_then_fills_or_expires(self):
        with Session() as s:
            v = cc.get_venue("binance")
            px = v.ticker(SOL)["last"]
            s.run("crypto-exit", "exit", ["plan", "SOL", "--side", "long", "--manual", "--entry", str(round(px * 0.999, 3)),
                                          "--stop", "pct:1", "--hold", "6h"])
            s.run("crypto-budget", "size", ["--limit"])
            s.run("crypto-risk", "check", [])
            o = cc.read_json(s.path("orders.json"))["orders"][0]
            code, out, err = place(s, o)
            self.assertIn("waiting to fill", out)
            book = cc.read_json(s.path("positions.json"))
            self.assertEqual(len(book["pending"]), 1)
            at(s, "2026-10-21 21:30")
            code, out, err = s.run(*BROKER, ["sync"])
            self.assertTrue("limit filled" in out or "never filled" in out, out)

    def test_move_stop_only_tightens(self):
        with Session() as s:
            o = ready_order(s)
            place(s, o)
            p = cc.read_json(s.path("positions.json"))["positions"][0]
            code, out, err = s.run(*BROKER, ["move-stop", "SOL", str(p["stop"] - 1)])
            self.assertIn("Never further away", err)
            code, out, err = s.run(*BROKER, ["move-stop", "SOL", str(p["entry"] * 1.2)])
            self.assertIn("already past", err)
            new = round((p["stop"] + p["entry"]) / 2.0, 3)
            code, out, err = s.run(*BROKER, ["move-stop", "SOL", str(new)])
            self.assertEqual(code, 0, err)
            self.assertAlmostEqual(cc.read_json(s.path("positions.json"))["positions"][0]["stop"], new)
            self.assertEqual(ledger(s, "stop_moved")[-1]["to"], new)

    def test_close_needs_the_coin_as_the_yes(self):
        with Session() as s:
            o = ready_order(s)
            place(s, o)
            code, out, err = s.run(*BROKER, ["close", "SOL"])
            self.assertIn("pass --yes SOL", err)
            code, out, err = s.run(*BROKER, ["close", "SOL", "--yes", "SOL"])
            self.assertEqual(code, 0, err)
            self.assertEqual(ledger(s, "close")[-1]["reason"], "manual")
            self.assertEqual(cc.read_json(s.path("positions.json"))["positions"], [])
            code, out, err = s.run(*BROKER, ["positions"])
            self.assertIn("No open positions", out)

    def test_one_position_a_coin(self):
        with Session() as s:
            o = ready_order(s)
            place(s, o)
            o2 = ready_order(s)
            doc = cc.read_json(s.path("orders.json"))
            doc["orders"][0]["risk"] = dict(o2["risk"], verdict="PASS", reasons=[], fingerprint=cc.fingerprint(o2))
            cc.write_json(s.path("orders.json"), doc)
            code, out, err = place(s, o2)
            self.assertIn("already open", err)


class LiveBinance(unittest.TestCase):
    def setUp(self):
        self.s = Session(config={"paper": False}).__enter__()
        os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"] = FA.KEY, FA.SECRET
        self.fa = fake_accounts()

    def tearDown(self):
        for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
            os.environ.pop(k, None)
        self.s.__exit__(None, None, None)

    def test_signature_matches_binances_own_example(self):
        q = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
        self.assertEqual(EX.hmac_hex("NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j", q),
                         "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_bracket_goes_on_and_the_stop_uses_the_algo_api(self):
        o = ready_order(self.s)
        code, out, err = place(self.s, o, "--live")
        self.assertEqual(code, 0, err + out)
        self.assertIn("Placed (LIVE)", out)
        b = self.fa.state()["binance"]
        self.assertAlmostEqual(b["pos"][SOL]["amt"], o["qty"])
        self.assertEqual(b["margin"][SOL], "ISOLATED")
        self.assertEqual(b["lev"][SOL], o["leverage"])
        stops = [x for x in b["algo"] if x["algoStatus"] == "NEW"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(float(stops[0]["triggerPrice"]), o["stop"])
        self.assertEqual(stops[0]["workingType"], "MARK_PRICE")
        self.assertTrue(stops[0]["reduceOnly"])
        tps = [x for x in b["orders"] if x["type"] == "LIMIT" and x["status"] == "NEW"]
        self.assertEqual(float(tps[0]["price"]), o["target"])
        paths = [c[2] for c in self.fa.state()["calls"]]
        self.assertNotIn(("POST", "/fapi/v1/order", "STOP_MARKET"), [(c[1], c[2], c[3].get("type")) for c in self.fa.state()["calls"]])
        pos = cc.read_json(self.s.path("positions.json"))["positions"][0]
        self.assertFalse(pos["paper"])

    def test_stop_refused_means_the_position_is_closed_at_once(self):
        o = ready_order(self.s)
        self.fa.state()["binance"]["refuse_algo"] = True
        code, out, err = place(self.s, o, "--live")
        self.assertEqual(code, 1)
        self.assertIn("closed at once for safety", err)
        self.assertEqual(self.fa.state()["binance"]["pos"][SOL]["amt"], 0.0)
        self.assertEqual(cc.read_json(self.s.path("positions.json")), None)
        close = ledger(self.s, "close")[-1]                                   # the round trip is on the record
        self.assertEqual(close["reason"], "emergency close: no stop on the exchange")
        self.assertLess(close["pnl_usd"], 0)
        self.assertGreater(close["pnl_usd"], -0.0015 * o["qty"] * o["entry"])  # fees and the spread, nothing more
        code, out, err = place(self.s, o, "--live")                          # and the order is never placed twice
        self.assertIn("already placed", err)

    def test_a_failed_emergency_close_says_danger(self):
        o = ready_order(self.s)
        b = self.fa.state()["binance"]
        b["refuse_algo"] = b["refuse_close"] = True
        code, out, err = place(self.s, o, "--live")
        self.assertEqual(code, 1)
        self.assertIn("DANGER", err)
        self.assertIn("OPEN on Binance WITHOUT A STOP", err)
        self.assertNotIn("Nothing is open", err)
        self.assertAlmostEqual(b["pos"][SOL]["amt"], o["qty"])               # it really is open: the words match
        code, out, err = place(self.s, o, "--live")
        self.assertIn("already placed", err)

    def test_a_lost_answer_is_looked_up_not_guessed(self):
        o = ready_order(self.s)
        st = self.fa.state()
        st["lose_reply"] = "after"                                            # it filled; the answer never came back
        code, out, err = place(self.s, o, "--live")
        self.assertEqual(code, 0, err)
        self.assertIn("Placed (LIVE)", out)
        self.assertAlmostEqual(st["binance"]["pos"][SOL]["amt"], o["qty"])
        self.assertEqual(len([x for x in st["binance"]["algo"] if x["algoStatus"] == "NEW"]), 1)   # the stop went on
        audit = [e["what"] for e in cc.read_jsonl(self.s.path("broker_log.jsonl"))]
        self.assertIn("binance entry outcome unknown", audit)
        self.assertIn("binance entry looked up", audit)

    def test_a_lost_request_is_not_called_a_fill(self):
        o = ready_order(self.s)
        st = self.fa.state()
        st["lose_reply"] = "before"                                           # it never arrived
        code, out, err = place(self.s, o, "--live")
        self.assertEqual(code, 1)
        self.assertIn("never reached Binance", err)
        self.assertIn("Nothing is open", err)
        self.assertFalse(st["binance"]["pos"].get(SOL, {}).get("amt"))

    def test_hedge_mode_is_refused(self):
        o = ready_order(self.s)
        self.fa.state()["binance"]["dual"] = True
        code, out, err = place(self.s, o, "--live")
        self.assertIn("hedge mode", err)
        self.assertNotIn(SOL, self.fa.state()["binance"]["pos"])

    def test_sync_sees_the_stop_fill_and_cleans_up(self):
        o = ready_order(self.s)
        place(self.s, o, "--live")
        self.fa.move_price(SOL, o["stop"] * 0.999)
        code, out, err = self.s.run(*BROKER, ["sync"])
        self.assertIn("closed on Binance by the stop", out)
        close = ledger(self.s, "close")[-1]
        self.assertEqual(close["reason"], "stop")
        self.assertFalse(close["paper"])
        b = self.fa.state()["binance"]
        self.assertFalse([x for x in b["orders"] if x["status"] == "NEW"])          # the target was cancelled

    def test_move_stop_places_the_new_one_first(self):
        o = ready_order(self.s)
        place(self.s, o, "--live")
        p = cc.read_json(self.s.path("positions.json"))["positions"][0]
        new = round((p["stop"] + p["entry"]) / 2.0, 3)
        code, out, err = self.s.run(*BROKER, ["move-stop", "SOL", str(new)])
        self.assertEqual(code, 0, err)
        live = [x for x in self.fa.state()["binance"]["algo"] if x["algoStatus"] == "NEW"]
        self.assertEqual([float(x["triggerPrice"]) for x in live], [new])

    def test_close_and_wrong_keys(self):
        o = ready_order(self.s)
        place(self.s, o, "--live")
        code, out, err = self.s.run(*BROKER, ["close", "SOL", "--yes", "SOL"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fa.state()["binance"]["pos"][SOL]["amt"], 0.0)
        os.environ["BINANCE_API_SECRET"] = "wrong"
        acct = EX.account("binance")
        with self.assertRaises(EX.ExchangeError) as e:
            acct.balance()
        self.assertEqual(e.exception.code, -1022)

    def test_no_keys(self):
        os.environ.pop("BINANCE_API_KEY")
        o = ready_order(self.s)
        code, out, err = place(self.s, o, "--live")
        self.assertIn("no Binance keys", err)
        self.assertIn("never in the chat", err)


class LiveBybit(unittest.TestCase):
    def setUp(self):
        self.s = Session(config={"paper": False, "live_venue": "bybit"}).__enter__()
        os.environ["BYBIT_API_KEY"], os.environ["BYBIT_API_SECRET"] = FA.KEY, FA.SECRET
        self.fa = fake_accounts()

    def tearDown(self):
        for k in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
            os.environ.pop(k, None)
        self.s.__exit__(None, None, None)

    def test_bracket_rides_on_the_entry(self):
        o = ready_order(self.s, venue="bybit")
        code, out, err = place(self.s, o, "--live")
        self.assertEqual(code, 0, err + out)
        y = self.fa.state()["bybit"]
        self.assertAlmostEqual(y["pos"][SOL]["size"], o["qty"])
        kinds = sorted(x["stopOrderType"] for x in y["orders"] if x["orderStatus"] == "Untriggered")
        self.assertEqual(kinds, ["PartialStopLoss", "PartialTakeProfit"])
        create = [c[3] for c in self.fa.state()["calls"] if c[2] == "/v5/order/create"][0]
        self.assertEqual(create["slTriggerBy"], "MarkPrice")
        self.assertEqual(create["tpslMode"], "Partial")
        self.assertEqual(create["tpLimitPrice"], create["takeProfit"])

    def test_cross_margin_account_is_refused(self):
        o = ready_order(self.s, venue="bybit")
        self.fa.state()["bybit"]["mode"] = "REGULAR_MARGIN"
        code, out, err = place(self.s, o, "--live")
        self.assertIn("margin mode is REGULAR_MARGIN", err)

    def test_missing_stop_means_closed_at_once(self):
        o = ready_order(self.s, venue="bybit")
        self.fa.state()["bybit"]["drop_stop"] = True
        code, out, err = place(self.s, o, "--live")
        self.assertIn("closed at once for safety", err)
        self.assertEqual(self.fa.state()["bybit"]["pos"][SOL]["size"], 0.0)
        close = ledger(self.s, "close")[-1]
        self.assertEqual(close["reason"], "emergency close: no stop on the exchange")
        self.assertNotEqual(close["price"], close["entry"])                  # the real exit price, from closed P&L

    def test_lost_answers_on_bybit(self):
        o = ready_order(self.s, venue="bybit")
        st = self.fa.state()
        st["lose_reply"] = "after"
        code, out, err = place(self.s, o, "--live")
        self.assertEqual(code, 0, err)
        self.assertAlmostEqual(st["bybit"]["pos"][SOL]["size"], o["qty"])
        self.assertIn("Placed (LIVE)", out)
        with Session(config={"paper": False}) as s2:
            o2 = ready_order(s2, venue="bybit")
            fake_accounts().state()["lose_reply"] = "before"
            code, out, err = place(s2, o2, "--live")
            self.assertIn("never reached Bybit", err)

    def test_target_fill_is_seen_by_sync_and_move_stop_amends(self):
        o = ready_order(self.s, venue="bybit")
        place(self.s, o, "--live")
        p = cc.read_json(self.s.path("positions.json"))["positions"][0]
        new = round((p["stop"] + p["entry"]) / 2.0, 3)
        code, out, err = self.s.run(*BROKER, ["move-stop", "SOL", str(new)])
        self.assertEqual(code, 0, err)
        sl = [x for x in self.fa.state()["bybit"]["orders"] if x["stopOrderType"] == "PartialStopLoss"][0]
        self.assertEqual(float(sl["triggerPrice"]), new)
        self.fa.move_price(SOL, o["target"] * 1.001)
        code, out, err = self.s.run(*BROKER, ["sync"])
        self.assertIn("at the target", out)

    def test_hyperliquid_is_paper_only(self):
        o = ready_order(self.s, venue="hyperliquid")
        code, out, err = place(self.s, o, "--live")
        self.assertIn("stays paper only", err)


if __name__ == "__main__":
    unittest.main()
