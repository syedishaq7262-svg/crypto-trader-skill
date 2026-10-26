#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-broker: the only skill that can place an order, and never without your yes.

    python3 broker.py ticket                 the order in plain words, READY or NOT READY, and its fingerprint
    python3 broker.py place --yes FP         place it (paper by default) after you said yes to that exact order
    python3 broker.py place --yes FP --live  a real order (needs "paper": false and your keys)
    python3 broker.py sync                   play stops, targets, time limits and funding forward (paper),
                                             or reconcile with the exchange (live)
    python3 broker.py positions              open positions: P&L, R now, distance to stop, target, liquidation
    python3 broker.py close SOL --yes SOL    close at the market (an exit: always allowed)
    python3 broker.py move-stop SOL 131.90   tighten a stop (never loosen)
    python3 broker.py account                balance, equity and margin in use

Paper trading fills at live prices against the real order book, and plays the
stop, the target, the time limit and funding forward on real 1-minute candles.
"""

import argparse
import importlib.util
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, os.pardir, "crypto-setup")):
    if os.path.isfile(os.path.join(_d, "cryptocore.py")):
        sys.path.insert(0, os.path.abspath(_d))
        break
else:
    sys.stderr.write("Problem: cryptocore.py not found. Install the crypto-setup skill next to this one.\n")
    sys.exit(2)
import cryptocore as cc  # noqa: E402

_spec = importlib.util.spec_from_file_location("crypto_exchanges", os.path.join(HERE, "exchanges.py"))
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)

T, O, H, L, C = cc.T, cc.O, cc.H, cc.L, cc.C
M1 = 60000


# ---------------------------------------------------------------- the book of record

def load_book():
    d = cc.read_json(cc.wpath("positions.json"), None) or {}
    d.setdefault("positions", [])
    d.setdefault("pending", [])
    return d


def save_book(d):
    cc.write_json(cc.wpath("positions.json"), d)


def log(event):
    cc.append_jsonl(cc.wpath("ledger.jsonl"), event)


def audit(what, data):
    """Every exchange answer the broker acted on, for checking later. Never contains keys."""
    cc.append_jsonl(cc.wpath("broker_log.jsonl"), {"at": cc.now_ms(), "what": what, "data": data})


def load_orders():
    doc = cc.read_json(cc.wpath("orders.json"), None)
    if not doc or not doc.get("orders"):
        raise cc.CryptoError("no order to place. The chain is /crypto-exit, /crypto-budget, /crypto-risk, then this.")
    return doc


def pick_order(doc, n):
    if n < 1 or n > len(doc["orders"]):
        raise cc.CryptoError("there is no order #%d (orders.json holds %d)" % (n, len(doc["orders"])))
    return doc["orders"][n - 1]


def readiness(o, cfg):
    """(ready, reasons). Ready means: crypto-risk passed this exact order in the last 15 minutes."""
    why = []
    r = o.get("risk") or {}
    if o.get("placed"):
        why.append("already placed at %s" % cc.fmt_local(o["placed"]["at"]))
    if r.get("verdict") != "PASS":
        why.append("crypto-risk has not passed it" if not r else "crypto-risk blocked it: " + "; ".join(r.get("reasons") or []))
    elif r.get("fingerprint") != cc.fingerprint(o):
        why.append("the order changed after crypto-risk checked it; run /crypto-risk again")
    elif cc.now_ms() > r.get("valid_until", 0):
        why.append("the risk check is %s old (it lasts 15 minutes); run /crypto-risk again" % cc.fmt_dur(cc.now_ms() - r["checked_at"]))
    return (not why), why


# ---------------------------------------------------------------- paper fills

def walk_qty(levels, qty):
    """Average price for `qty` coins against one side of the book. (price, filled, worst level)."""
    left, cost, got, worst = qty, 0.0, 0.0, None
    for p, q in levels:
        take = min(q, left)
        cost += take * p
        got += take
        worst = p
        left -= take
        if left <= 1e-12:
            break
    return (cost / got if got else None), got, worst


def paper_market_fill(venue, m, side, qty):
    """Fill a market order against the live order book. Returns (price, note)."""
    bids, asks = venue.orderbook(m["symbol"], 500 if venue.name == "binance" else 200)
    levels = asks if side == "buy" else bids
    if not levels:
        raise cc.CryptoError("the %s order book is empty right now" % m["symbol"])
    avg, got, worst = walk_qty(levels, qty)
    note = None
    if got < qty - 1e-12:
        # the book we can see ran out: the rest fills a little past its worst level
        pen = 1.002 if side == "buy" else 0.998
        rest = qty - got
        avg = ((avg or worst) * got + worst * pen * rest) / qty
        note = "the visible book was thin; part of the fill is estimated past its last level"
    if m.get("tick"):
        avg = round(avg, cc.tick_decimals(m["tick"]) + 2)
    return avg, note


def open_position(o, fill, fee, at, paper, extra=None):
    sg = cc.side_sign(o["side"])
    risk_coin = abs(fill - o["stop"])
    costs = o.get("costs") or {}
    pos = {
        "id": "p%d-%s" % (at, o["symbol"]), "order_id": o["id"], "venue": o["venue"], "symbol": o["symbol"],
        "coin": o["coin"], "multiplier": o.get("multiplier", 1), "tick": o.get("tick"), "step": o.get("step"),
        "side": o["side"], "qty": o["qty"], "entry": fill, "stop": o["stop"], "target": o["target"],
        "exit_by": o.get("exit_by"), "leverage": o["leverage"], "margin": o["qty"] * fill / o["leverage"],
        "liq": cc.liq_price(fill, o["side"], o["leverage"], o.get("mmr") or 0.02, costs.get("taker") or 0.0005),
        "risk_usd": o["qty"] * risk_coin,
        "loss_at_stop_usd": o["qty"] * (risk_coin + ((costs.get("taker") or 0) + (costs.get("slippage") or 0)) * o["stop"]) + fee,
        "fee_in": fee, "funding_paid": 0.0, "opened_at": at, "last_sync": at, "paper": paper,
        "plan_id": o.get("plan_id"), "setup": o.get("setup"), "costs": costs,
        "funding_hours": o.get("funding_hours") or 8.0, "funding_rate": o.get("funding_rate") or 0.0,
    }
    if extra:
        pos.update(extra)
    log({"type": "open", "at": at, "id": pos["id"], "order_id": o["id"], "venue": o["venue"], "symbol": o["symbol"],
         "coin": o["coin"], "side": o["side"], "qty": o["qty"], "price": fill, "fee": fee, "leverage": o["leverage"],
         "stop": o["stop"], "target": o["target"], "exit_by": o.get("exit_by"), "risk_usd": pos["risk_usd"],
         "paper": paper, "plan_id": o.get("plan_id"), "setup": o.get("setup")})
    return pos


def close_record(pos, price, fee_out, at, reason, funding=None):
    sg = cc.side_sign(pos["side"])
    funding = pos.get("funding_paid", 0.0) if funding is None else funding
    gross = sg * (price - pos["entry"]) * pos["qty"]
    pnl = gross - pos.get("fee_in", 0.0) - fee_out - funding
    risk = pos.get("risk_usd") or 1e-9
    ev = {"type": "close", "at": at, "id": pos["id"], "venue": pos["venue"], "symbol": pos["symbol"], "coin": pos["coin"],
          "side": pos["side"], "qty": pos["qty"], "entry": pos["entry"], "price": price,
          "fee": pos.get("fee_in", 0.0) + fee_out, "funding": funding, "pnl_usd": pnl, "r": pnl / risk,
          "reason": reason, "hold_ms": at - pos["opened_at"], "paper": pos["paper"], "setup": pos.get("setup"),
          "plan_id": pos.get("plan_id"), "leverage": pos.get("leverage")}
    log(ev)
    return ev


# ---------------------------------------------------------------- commands

def ticket(a, cfg):
    doc = load_orders()
    o = pick_order(doc, a.order)
    ready, why = readiness(o, cfg)
    if a.json:
        cc.dump({"order": o, "paper": cfg.get("paper", True), "ready": ready, "why_not": why,
                 "fingerprint": cc.fingerprint(o) if ready else None})
        return 0 if ready else 1
    P = lambda x: cc.fmt_price(x, o.get("tick"))
    mode = "PAPER" if cfg.get("paper", True) else "LIVE (real money)"
    verb = "Buy" if o["side"] == "long" else "Sell short"
    unit = (" (= {:,.0f} {})".format(o["qty"] * o["multiplier"], o["coin"])) if o.get("multiplier", 1) != 1 else ""
    cc.out("Ticket #%d | %s | %s %s" % (a.order, mode, o["venue"].capitalize(), o["symbol"]))
    cc.out("  %s %s %s%s at %s, %dx isolated, about %s of margin." % (
        verb, cc.fmt_qty(o["qty"], o.get("step")), o["coin"], unit,
        "the market (about %s)" % P(o["entry"]) if o["type"] == "market" else "a limit of %s" % P(o["limit"]),
        o["leverage"], cc.fmt_money(o["margin"])))
    cc.out("  Stop-loss %s: a stop-market, reduce-only, triggered by the mark price. Lose about %s there." %
           (P(o["stop"]), cc.fmt_money(o["loss_at_stop_usd"])))
    cc.out("  Take-profit %s: a reduce-only limit. Make about %s there." % (P(o["target"]), cc.fmt_money(o["gain_at_target_usd"])))
    cc.out("  Out by %s, win or lose. Estimated liquidation %s." % (cc.fmt_local(o["exit_by"]),
                                                                    cc.fmt_liq(o["liq_est"], o["entry"], o.get("tick"))))
    r = o.get("risk") or {}
    for w in r.get("warnings") or []:
        cc.out("  ! " + w)
    fp = cc.fingerprint(o)
    if ready:
        cc.out("READY. Fingerprint %s. The risk check is good until %s." % (fp, cc.fmt_local(r["valid_until"], date=False)))
        cc.out("To place it: python3 broker.py place --yes %s%s" % (fp, " --live" if not cfg.get("paper", True) else ""))
        cc.out("Claude runs that only after you type yes for this exact order.")
        return 0
    cc.out("NOT READY: " + "; ".join(why) + ".")
    return 1


def place(a, cfg):
    doc = load_orders()
    o = pick_order(doc, a.order)
    ready, why = readiness(o, cfg)
    if not ready:
        raise cc.CryptoError("not placed: " + "; ".join(why))
    fp = cc.fingerprint(o)
    if a.yes != fp:
        raise cc.CryptoError("not placed: --yes must be this order's fingerprint (%s), given only after the user's yes" % fp)
    live = not cfg.get("paper", True)
    if live and not a.live:
        raise cc.CryptoError("not placed: your config is LIVE (paper is false). Add --live to confirm real money.")
    if a.live and not live:
        raise cc.CryptoError("not placed: --live was given but your config is paper. Set \"paper\": false yourself to go live.")
    venue = cc.get_venue(o["venue"])
    m = venue.market(o["symbol"])
    t = venue.tickers(fresh=True).get(m["symbol"]) or venue.ticker(m["symbol"])
    sg = cc.side_sign(o["side"])
    risk = abs(o["entry"] - o["stop"])
    drift = sg * (t["last"] - o["entry"]) / risk if risk else 0
    if drift > 0.5:
        raise cc.CryptoError("not placed: the price ran %.2fR since the risk check. Run /crypto-risk again." % drift)
    if sg * (t["last"] - o["stop"]) <= 0:
        raise cc.CryptoError("not placed: the price is past the stop")
    book = load_book()
    if any(p["symbol"] == o["symbol"] for p in book["positions"] + book["pending"]):
        raise cc.CryptoError("not placed: a %s position or order is already open" % o["symbol"])
    now = cc.now_ms()
    if live:
        try:
            pos = place_live(o, m, cfg)
        except cc.CryptoError:
            if o.get("placed"):              # the entry filled: never place this order twice
                cc.write_json(cc.wpath("orders.json"), doc)
            raise
    else:
        pos = place_paper(o, m, venue, now)
    if pos.get("pending"):
        book["pending"].append(pos)
    else:
        book["positions"].append(pos)
    save_book(book)
    o["placed"] = {"at": now, "position": pos["id"], "paper": not live}
    cc.write_json(cc.wpath("orders.json"), doc)
    P = lambda x: cc.fmt_price(x, o.get("tick"))
    if pos.get("pending"):
        cc.out("Placed (%s): a limit to %s %s %s at %s, waiting to fill. Stop %s and target %s go on when it fills." %
               ("paper" if not live else "LIVE", "buy" if o["side"] == "long" else "sell", cc.fmt_qty(o["qty"], o.get("step")),
                o["coin"], P(pos["limit"]), P(o["stop"]), P(o["target"])))
    else:
        cc.out("Placed (%s): %s %s %s filled at %s. Stop %s, target %s, %dx isolated, out by %s." %
               ("paper" if not live else "LIVE", o["side"], cc.fmt_qty(pos["qty"], o.get("step")), o["coin"], P(pos["entry"]),
                P(pos["stop"]), P(pos["target"]), o["leverage"], cc.fmt_local(pos["exit_by"])))
        if pos.get("fill_note"):
            cc.out("Note: " + pos["fill_note"])
    cc.out("Position %s. /crypto-alerts can watch it; broker.py sync %s." % (
        pos["id"], "plays it forward" if not live else "reconciles it with the exchange"))
    return 0


def place_paper(o, m, venue, now):
    costs = o.get("costs") or {}
    if o["type"] == "limit":
        sg = cc.side_sign(o["side"])
        last = venue.ticker(m["symbol"])["last"]
        if sg * (last - o["limit"]) > 0:      # not marketable: it rests
            return {"id": "w%d-%s" % (now, o["symbol"]), "pending": True, "order": o, "limit": o["limit"],
                    "symbol": o["symbol"], "placed_at": now, "last_sync": now, "expires": o["exit_by"], "paper": True}
    side = "buy" if o["side"] == "long" else "sell"
    fill, note = paper_market_fill(venue, m, side, o["qty"])
    if o["type"] == "limit":
        fill = min(fill, o["limit"]) if o["side"] == "long" else max(fill, o["limit"])
    fee = o["qty"] * fill * (costs.get("taker") or venue.taker_fee)
    pos = open_position(o, fill, fee, now, True)
    if note:
        pos["fill_note"] = note
    return pos


def emergency_close(acct, o, fill, filled, fee_in, cid, why):
    """The entry filled but no stop is on the exchange. Close at once, record it, never place the order again,
    and say exactly what happened, above all when the close fails too."""
    venue, sym = o["venue"], o["symbol"]
    exit_side = "sell" if o["side"] == "long" else "buy"
    now = cc.now_ms()
    try:
        if venue == "binance":
            res = acct.market(sym, exit_side, filled, reduce_only=True, client_id=cid + "x")
        else:
            res = acct.market(sym, exit_side, filled, reduce_only=True, link_id=cid + "x")
        audit("%s emergency close" % venue, res)
    except cc.CryptoError as e:
        audit("%s emergency close failed" % venue, str(e))
        o["placed"] = {"at": now, "position": None, "paper": False, "outcome": "OPEN WITHOUT A STOP: " + why}
        raise cc.CryptoError("DANGER: the entry filled but %s, and closing it failed too (%s). %s %s %s is OPEN on %s "
                             "WITHOUT A STOP. Close it in the %s app now." % (
                                 why, e, o["side"], cc.fmt_qty(filled, o.get("step")), o["coin"], venue.capitalize(),
                                 venue.capitalize()))
    try:
        acct.cancel_all(sym)                 # nothing should rest on a closed position: a stray stop or target goes too
    except cc.CryptoError as e:
        audit("%s cancel after emergency close failed" % venue, str(e))
    price = cc.fnum((res or {}).get("avgPrice")) if venue == "binance" else None
    if not price and venue == "bybit":
        try:
            rows = [r for r in acct.closed_pnl(now - 120000, None, sym) if r.get("symbol") == sym]
            price = cc.fnum(rows[0].get("avgExitPrice")) if rows else None
        except cc.CryptoError:
            price = None
    pos = open_position(dict(o, qty=filled), fill, fee_in, now, False)
    exit_px = price or fill
    close_record(pos, exit_px, filled * exit_px * ((o.get("costs") or {}).get("taker") or 0.0005), now,
                 "emergency close: no stop on the exchange")
    o["placed"] = {"at": now, "position": pos["id"], "paper": False, "outcome": "closed at once: " + why}
    raise cc.CryptoError("the entry filled but %s, so the position was closed at once for safety. Nothing is open. It "
                         "cost the fees and the spread (in crypto/ledger.jsonl)." % why)


def unknown_outcome(e):
    """True when an order request may or may not have gone through: the answer was lost on the way back,
    or the exchange says its own backend timed out (Binance -1007, Bybit 10000/10016, any HTTP 5xx)."""
    if isinstance(e, cc.NetError):
        return True
    code = getattr(e, "code", None)
    return code in (-1007, 10000, 10016) or (isinstance(code, int) and code >= 500)


def lost_entry(venue, sym, e, e2):
    return cc.CryptoError("DANGER: the entry's answer was lost (%s) and %s could not say whether it filled (%s). Open the "
                          "%s app now and check for a %s position: if there is one, it has NO STOP." % (
                              e, venue.capitalize(), e2, venue.capitalize(), sym))


def binance_entry(acct, sym, side, qty, cid):
    """A market entry. If its answer is lost, ask Binance what happened instead of guessing either way."""
    try:
        return acct.market(sym, side, qty, client_id=cid)
    except cc.CryptoError as e:
        if not unknown_outcome(e):
            raise
        audit("binance entry outcome unknown", str(e))
        for attempt in range(3):
            try:
                od = acct.order(sym, cid)
            except cc.CryptoError as e2:
                if getattr(e2, "code", None) != -2013:
                    raise lost_entry("binance", sym, e, e2)
                od = None
            if od is not None:
                audit("binance entry looked up", od)
                if (cc.fnum(od.get("executedQty"), 0.0) or 0.0) > 0:
                    return od
                raise cc.CryptoError("not placed: Binance shows the entry as %s with nothing filled. Nothing is open."
                                     % od.get("status"))
            if not cc.pinned():
                time.sleep(1.0)
        try:
            held = acct.positions(sym)
        except cc.CryptoError as e2:
            raise lost_entry("binance", sym, e, e2)
        if held:
            audit("binance entry found as a position", held)
            return {"avgPrice": held[0]["entry"], "executedQty": held[0]["qty"]}
        raise cc.CryptoError("not placed: the entry never reached Binance (%s). Nothing is open." % e)


def place_live(o, m, cfg):
    acct = ex.account(o["venue"])
    sym, qty = o["symbol"], o["qty"]
    entry_side = "buy" if o["side"] == "long" else "sell"
    exit_side = "sell" if o["side"] == "long" else "buy"
    taker = (o.get("costs") or {}).get("taker")
    cid = "cts%d" % (cc.now_ms() % 10 ** 12)
    if o["venue"] == "binance":
        if o["type"] != "market":
            raise cc.CryptoError("not placed: on Binance the pack places market entries only, so the stop goes on the "
                                 "moment it fills. Size it again without --limit.")
        if not acct.one_way():
            raise cc.CryptoError("not placed: your Binance futures account is in hedge mode. The pack needs one-way mode "
                                 "(Binance app: Futures > Preferences > Position Mode).")
        if any(p for p in acct.positions(sym)):
            raise cc.CryptoError("not placed: Binance already shows an open %s position" % sym)
        acct.isolated(sym)
        acct.leverage(sym, o["leverage"])
        res = binance_entry(acct, sym, entry_side, qty, cid + "e")
        audit("binance entry", res)
        fill = cc.fnum(res.get("avgPrice")) or o["entry"]
        filled = cc.fnum(res.get("executedQty"), qty)
        fee = filled * fill * (taker or 0.0005)
        try:
            st = acct.stop_market(sym, exit_side, filled, o["stop"], client_id=cid + "s")
            audit("binance stop", st)
        except cc.CryptoError as e:
            audit("binance stop failed", str(e))
            if not unknown_outcome(e) or not binance_stop_is_on(acct, sym, cid + "s"):
                emergency_close(acct, o, fill, filled, fee, cid, "the stop was refused (%s)" % e)
        tp_note = None
        try:
            tp = acct.limit(sym, exit_side, filled, o["target"], reduce_only=True, client_id=cid + "t")
            audit("binance target", tp)
        except cc.CryptoError as e:
            tp_note = ("the take-profit may not be on (%s); the stop is on. Check the app, and place the target there "
                       "if it is missing." % e)
        pos = open_position(dict(o, qty=filled), fill, fee, cc.now_ms(), False, {"stop_id": cid + "s", "target_id": cid + "t"})
        if tp_note:
            pos["fill_note"] = tp_note
        return pos
    # Bybit: the stop-loss and take-profit ride on the entry order itself
    mode = acct.margin_mode()
    if mode != "ISOLATED_MARGIN" and not cfg.get("allow_cross_margin"):
        raise cc.CryptoError("not placed: your Bybit account margin mode is %s. Switch it to isolated in the Bybit app "
                             "(Derivatives > Margin Mode) or set allow_cross_margin in your config." % mode)
    if not acct.one_way(sym):
        raise cc.CryptoError("not placed: %s is in hedge mode on Bybit; the pack needs one-way mode" % sym)
    if any(acct.positions(sym)):
        raise cc.CryptoError("not placed: Bybit already shows an open %s position" % sym)
    acct.leverage(sym, o["leverage"])
    otype = "Limit" if o["type"] == "limit" else "Market"
    try:
        res = acct.bracket(sym, entry_side, qty, o["stop"], o["target"], order_type=otype,
                           price=o.get("limit"), link_id=cid + "e")
    except cc.CryptoError as e:
        if not unknown_outcome(e):
            raise
        audit("bybit entry outcome unknown", str(e))
        try:
            held = acct.positions(sym)
            resting = [x for x in acct.open_orders(sym) if x.get("orderLinkId") == cid + "e"]
        except cc.CryptoError as e2:
            raise lost_entry("bybit", sym, e, e2)
        if not held and not resting:
            raise cc.CryptoError("not placed: the entry never reached Bybit (%s). Nothing is open." % e)
        res = {"orderId": (resting[0].get("orderId") if resting else None), "found_after_lost_answer": True}
    audit("bybit bracket", res)
    if otype == "Limit" and not any(acct.positions(sym)):
        return {"id": "w%d-%s" % (cc.now_ms(), sym), "pending": True, "order": o, "limit": o["limit"], "symbol": sym,
                "placed_at": cc.now_ms(), "last_sync": cc.now_ms(), "expires": o["exit_by"], "paper": False,
                "exchange_order": res.get("orderId")}
    pos_now = acct.positions(sym)
    if not pos_now:
        raise cc.CryptoError("not placed: Bybit took the order but shows no %s position. Check the Bybit app." % sym)
    fill, filled = pos_now[0]["entry"], pos_now[0]["qty"]
    fee = filled * fill * (taker or 0.00055)
    stops = [x for x in acct.open_orders(sym) if "StopLoss" in str(x.get("stopOrderType"))]
    if not stops:
        audit("bybit stop missing", acct.open_orders(sym))
        emergency_close(acct, o, fill, filled, fee, cid, "no stop-loss showed on Bybit")
    return open_position(dict(o, qty=filled), fill, fee, cc.now_ms(), False, {"exchange_order": res.get("orderId")})


def binance_stop_is_on(acct, sym, client_id):
    """After a stop request whose answer was lost: is it resting on Binance after all?"""
    try:
        plain, algo = acct.open_orders(sym)
    except cc.CryptoError:
        return False
    return any(x.get("clientAlgoId") == client_id for x in algo)


# ---------------------------------------------------------------- sync

def paper_sync(book, cfg, report):
    now = cc.now_ms()
    venues = {}

    def v_of(name):
        if name not in venues:
            venues[name] = cc.get_venue(name)
        return venues[name]
    still = []
    for w in book["pending"]:
        if not w.get("paper"):
            still.append(w)
            continue
        o = w["order"]
        v = v_of(o["venue"])
        bars = v.bars_between(o["symbol"], "1m", w["last_sync"], now)
        filled_at = None
        for b in bars:
            if b[T] < w["last_sync"]:
                continue
            if (o["side"] == "long" and b[L] <= w["limit"]) or (o["side"] == "short" and b[H] >= w["limit"]):
                filled_at = b[T]
                break
        if filled_at is not None and (not w.get("expires") or filled_at < w["expires"]):
            fee = o["qty"] * w["limit"] * ((o.get("costs") or {}).get("maker") or v.maker_fee)
            pos = open_position(o, w["limit"], fee, filled_at, True)
            pos["last_sync"] = filled_at
            book["positions"].append(pos)
            report.append("%s limit filled at %s (%s)." % (o["coin"], cc.fmt_price(w["limit"], o.get("tick")), cc.fmt_local(filled_at)))
        elif w.get("expires") and now >= w["expires"]:
            log({"type": "cancel", "at": now, "id": w["id"], "symbol": o["symbol"], "reason": "time limit, never filled", "paper": True})
            report.append("%s limit never filled; cancelled at its time limit." % o["coin"])
        else:
            w["last_sync"] = (now // M1) * M1
            still.append(w)
    book["pending"] = still
    keep = []
    for p in book["positions"]:
        if not p.get("paper"):
            keep.append(p)
            continue
        v = v_of(p["venue"])
        res = play_forward(p, v, now)
        if res is None:
            keep.append(p)
            continue
        price, reason, at, fee_out = res
        ev = close_record(p, price, fee_out, at, reason)
        report.append("%s %s closed at %s by the %s: %s (%+.2fR)." % (p["coin"], p["side"], cc.fmt_price(price, p.get("tick")),
                                                                      reason, cc.fmt_money(ev["pnl_usd"]), ev["r"]))
    book["positions"] = keep


def play_forward(p, v, now):
    """Walk 1-minute candles since the last sync. Returns (price, reason, at, fee_out) once closed, else None."""
    start = max(p["last_sync"], p["opened_at"])
    bars = v.bars_between(p["symbol"], "1m", start, now)
    costs = p.get("costs") or {}
    taker = costs.get("taker", v.taker_fee)
    maker = costs.get("maker", v.maker_fee)
    slip = costs.get("slippage", cc.DEFAULT_SLIPPAGE)
    hist = []
    try:
        hist = v.funding_history(p["symbol"], start, now)
    except cc.CryptoError:
        hist = []
    fund = sorted(((t // M1) * M1, r) for t, r in hist) or [(f, p.get("funding_rate") or 0.0)
                                                            for f in cc.funding_times(start, now, p.get("funding_hours") or 8.0)]
    fi = 0
    sg = cc.side_sign(p["side"])
    stop, target, liq = p["stop"], p["target"], p.get("liq")
    for b in bars:
        if b[T] < start:
            continue
        while fi < len(fund) and fund[fi][0] <= b[T]:
            if fund[fi][0] > p["opened_at"]:
                p["funding_paid"] = p.get("funding_paid", 0.0) + sg * fund[fi][1] * b[O] * p["qty"]
            fi += 1
        if p.get("exit_by") and b[T] >= p["exit_by"]:
            px = b[O] * (1 - sg * slip)
            return px, "time limit", b[T], p["qty"] * px * taker
        if liq and p["side"] == "long" and liq >= stop and b[L] <= liq:
            return liq, "liquidation", b[T] + M1, p["margin"] - p["qty"] * (p["entry"] - liq)
        if liq and p["side"] == "short" and liq <= stop and b[H] >= liq:
            return liq, "liquidation", b[T] + M1, p["margin"] - p["qty"] * (liq - p["entry"])
        if p["side"] == "long":
            if b[O] <= stop:
                if liq and b[O] <= liq:
                    return liq, "liquidation", b[T], p["margin"] - p["qty"] * (p["entry"] - liq) + 0.0
                px = b[O] * (1 - slip)
                return px, "stop", b[T], p["qty"] * px * taker
            if b[L] <= stop:
                px = stop * (1 - slip)
                return px, "stop", b[T] + M1, p["qty"] * px * taker
            if b[H] >= target:
                return target, "target", b[T] + M1, p["qty"] * target * maker
        else:
            if b[O] >= stop:
                if liq and b[O] >= liq:
                    return liq, "liquidation", b[T], p["margin"] - p["qty"] * (liq - p["entry"]) + 0.0
                px = b[O] * (1 + slip)
                return px, "stop", b[T], p["qty"] * px * taker
            if b[H] >= stop:
                px = stop * (1 + slip)
                return px, "stop", b[T] + M1, p["qty"] * px * taker
            if b[L] <= target:
                return target, "target", b[T] + M1, p["qty"] * target * maker
        p["last_sync"] = b[T] + M1
    return None


def live_sync(book, cfg, report):
    keep = []
    accts = {}
    for p in book["positions"]:
        if p.get("paper"):
            keep.append(p)
            continue
        acct = accts.get(p["venue"]) or accts.setdefault(p["venue"], ex.account(p["venue"]))
        now_pos = [x for x in acct.positions(p["symbol"]) if x["side"] == p["side"]]
        if now_pos:
            q = now_pos[0]["qty"]
            if abs(q - p["qty"]) > 1e-12:
                report.append("%s: the exchange shows %s left of %s (part of it closed)." % (p["coin"], q, p["qty"]))
                p["qty"] = q
            if p["venue"] == "binance":
                plain, algo = acct.open_orders(p["symbol"])
                if not algo:
                    report.append("WARNING %s: no stop-loss is on Binance for this position. Place one now." % p["coin"])
            else:
                if not [x for x in acct.open_orders(p["symbol"]) if "StopLoss" in str(x.get("stopOrderType"))]:
                    report.append("WARNING %s: no stop-loss is on Bybit for this position. Place one now." % p["coin"])
            if p.get("exit_by") and cc.now_ms() >= p["exit_by"]:
                report.append("%s: its time limit passed. Close it with: broker.py close %s --yes %s" % (p["coin"], p["coin"], p["coin"]))
            keep.append(p)
            continue
        # gone from the exchange: closed by the stop, the target or by hand
        price, pnl, fees, funding = exit_details(acct, p)
        acct.cancel_all(p["symbol"])
        reason = "closed"
        if price is not None:
            if abs(price - p["stop"]) <= 0.25 * abs(p["entry"] - p["stop"]):
                reason = "stop"
            elif abs(price - p["target"]) <= 0.25 * abs(p["entry"] - p["stop"]):
                reason = "target"
        ev = close_record(p, price if price is not None else p["entry"], fees, cc.now_ms(), reason, funding=funding)
        if pnl is not None:
            ev["pnl_usd_exchange"] = pnl
        how = {"stop": "by the stop", "target": "at the target"}.get(reason, "by hand or another order")
        report.append("%s %s closed on %s %s at %s: %s." % (p["coin"], p["side"], p["venue"].capitalize(), how,
                                                            cc.fmt_price(ev["price"], p.get("tick")), cc.fmt_money(ev["pnl_usd"])))
    book["positions"] = keep


def exit_details(acct, p):
    """(exit price, the exchange's realized P&L, fees out, funding) for a position that closed."""
    try:
        if p["venue"] == "binance":
            fills = acct.fills(p["symbol"], p["opened_at"])
            close_side = "SELL" if p["side"] == "long" else "BUY"
            out = [f for f in fills if f.get("side") == close_side and f.get("time", 0) >= p["opened_at"]]
            q = sum(cc.fnum(f.get("qty"), 0) for f in out)
            px = sum(cc.fnum(f.get("price"), 0) * cc.fnum(f.get("qty"), 0) for f in out) / q if q else None
            fees = sum(cc.fnum(f.get("commission"), 0) for f in out)
            pnl = sum(cc.fnum(f.get("realizedPnl"), 0) for f in out)
            inc = acct.income(p["opened_at"], kind="FUNDING_FEE")
            funding = -sum(cc.fnum(r.get("income"), 0) for r in inc if r.get("symbol") == p["symbol"])
            return px, pnl, fees, funding
        rows = acct.closed_pnl(p["opened_at"], symbol=p["symbol"])
        if rows:
            r = rows[0]
            return cc.fnum(r.get("avgExitPrice")), cc.fnum(r.get("closedPnl")), 0.0, 0.0
    except cc.CryptoError:
        pass
    return None, None, 0.0, 0.0


def sync(a, cfg):
    book = load_book()
    report = []
    paper_sync(book, cfg, report)
    if any(not p.get("paper") for p in book["positions"]):
        live_sync(book, cfg, report)
    save_book(book)
    for r in report:
        cc.out(r)
    cc.out("Synced %s: %d open, %d waiting to fill." % (cc.fmt_local(cc.now_ms()), len(book["positions"]), len(book["pending"])))
    return 0


# ---------------------------------------------------------------- the rest

def positions(a, cfg):
    book = load_book()
    if not book["positions"] and not book["pending"]:
        if a.json:
            cc.dump({"positions": [], "pending": []})
        else:
            cc.out("No open positions and no waiting orders.")
        return 0
    venues = {}
    rows = []
    for p in book["positions"]:
        v = venues.get(p["venue"]) or venues.setdefault(p["venue"], cc.get_venue(p["venue"]))
        t = v.ticker(p["symbol"])
        mark = t.get("mark") or t["last"]
        sg = cc.side_sign(p["side"])
        upnl = sg * (mark - p["entry"]) * p["qty"] - p.get("fee_in", 0) - p.get("funding_paid", 0)
        r_now = sg * (mark - p["entry"]) / abs(p["entry"] - p["stop"])
        rows.append(dict(p, mark=mark, unrealized_usd=upnl, r_now=r_now, stop_away=abs(mark - p["stop"]) / mark,
                         target_away=abs(p["target"] - mark) / mark))
        if a.json:
            continue
        P = lambda x: cc.fmt_price(x, p.get("tick"))
        cc.out("%s %s %s %s @ %s | mark %s | %s (%+.2fR) | %s" % (
            "PAPER" if p["paper"] else "LIVE", p["coin"], p["side"], cc.fmt_qty(p["qty"], p.get("step")), P(p["entry"]),
            P(mark), cc.fmt_money(upnl), r_now, cc.fmt_local(p["opened_at"])))
        cc.out("    stop %s (%s away), target %s (%s away), liquidation %s, out by %s" % (
            P(p["stop"]), cc.fmt_pct(abs(mark - p["stop"]) / mark, 2, False), P(p["target"]),
            cc.fmt_pct(abs(p["target"] - mark) / mark, 2, False), cc.fmt_liq(p.get("liq"), mark, p.get("tick")),
            cc.fmt_local(p["exit_by"])))
    if a.json:
        cc.dump({"positions": rows, "pending": book["pending"]})
        return 0
    for w in book["pending"]:
        o = w["order"]
        cc.out("%s waiting: limit %s %s at %s, placed %s, expires %s" % (
            "PAPER" if w["paper"] else "LIVE", o["side"], o["coin"], cc.fmt_price(w["limit"], o.get("tick")),
            cc.fmt_local(w["placed_at"]), cc.fmt_local(w["expires"])))
    return 0


def find_pos(book, coin):
    want = cc.clean_coin(coin)
    for p in book["positions"]:
        if p["coin"] == want or p["symbol"] == str(coin).upper():
            return p
    raise cc.CryptoError("no open position on %s" % coin)


def close(a, cfg):
    book = load_book()
    p = find_pos(book, a.target)
    if (a.yes or "").upper() != p["coin"].upper():
        raise cc.CryptoError("to close %s, pass --yes %s after the user said yes" % (p["coin"], p["coin"]))
    v = cc.get_venue(p["venue"])
    m = v.market(p["symbol"])
    side = "sell" if p["side"] == "long" else "buy"
    if p["paper"]:
        price, note = paper_market_fill(v, m, side, p["qty"])
        fee = p["qty"] * price * ((p.get("costs") or {}).get("taker") or v.taker_fee)
    else:
        acct = ex.account(p["venue"])
        if p["venue"] == "binance":
            res = acct.market(p["symbol"], side, p["qty"], reduce_only=True, client_id="cts%dc" % (cc.now_ms() % 10 ** 12))
            audit("binance close", res)
            price = cc.fnum(res.get("avgPrice")) or v.ticker(p["symbol"])["last"]
        else:
            res = acct.market(p["symbol"], side, p["qty"], reduce_only=True)
            audit("bybit close", res)
            price = v.tickers(fresh=True)[p["symbol"]]["last"]
        acct.cancel_all(p["symbol"])
        fee = p["qty"] * price * ((p.get("costs") or {}).get("taker") or v.taker_fee)
    ev = close_record(p, price, fee, cc.now_ms(), "manual")
    book["positions"] = [x for x in book["positions"] if x["id"] != p["id"]]
    save_book(book)
    cc.out("Closed %s %s at %s: %s (%+.2fR)." % (p["coin"], p["side"], cc.fmt_price(price, p.get("tick")), cc.fmt_money(ev["pnl_usd"]), ev["r"]))
    return 0


def move_stop(a, cfg):
    book = load_book()
    p = find_pos(book, a.target)
    try:
        new = float(a.price)
    except (TypeError, ValueError):
        raise cc.CryptoError("move-stop needs a price, like: broker.py move-stop SOL 131.90")
    sg = cc.side_sign(p["side"])
    if sg * (new - p["stop"]) <= 0:
        raise cc.CryptoError("a stop only moves toward the trade (%s than %s). Never further away." %
                             ("higher" if p["side"] == "long" else "lower", cc.fmt_price(p["stop"], p.get("tick"))))
    v = cc.get_venue(p["venue"])
    m = v.market(p["symbol"])
    if m.get("tick"):
        new = cc.round_to_tick(new, m["tick"])
    mark = v.ticker(p["symbol"])["mark"]
    if sg * (mark - new) <= 0:
        raise cc.CryptoError("the price %s is already past %s; that stop would fire at once" % (
            cc.fmt_price(mark, p.get("tick")), cc.fmt_price(new, p.get("tick"))))
    if not p["paper"]:
        acct = ex.account(p["venue"])
        side = "sell" if p["side"] == "long" else "buy"
        if p["venue"] == "binance":
            new_id = "cts%ds" % (cc.now_ms() % 10 ** 12)
            res = acct.stop_market(p["symbol"], side, p["qty"], new, client_id=new_id)
            audit("binance move stop", res)
            if p.get("stop_id"):
                try:
                    acct.cancel_algo(p["stop_id"])
                except cc.CryptoError as e:
                    cc.out("Note: the old stop could not be cancelled (%s); it is reduce-only, so it cannot add to the position." % e)
            p["stop_id"] = new_id
        else:
            stops = [x for x in acct.open_orders(p["symbol"]) if "StopLoss" in str(x.get("stopOrderType"))]
            if not stops:
                raise cc.CryptoError("no stop-loss found on Bybit for %s to move" % p["symbol"])
            audit("bybit move stop", acct.amend_trigger(p["symbol"], stops[0]["orderId"], new))
    log({"type": "stop_moved", "at": cc.now_ms(), "id": p["id"], "symbol": p["symbol"], "from": p["stop"], "to": new,
         "paper": p["paper"]})
    old = p["stop"]
    p["stop"] = new
    save_book(book)
    locked = sg * (new - p["entry"]) / abs(p["entry"] - old)
    cc.out("Stop on %s moved from %s to %s (%+.2fR from the entry)." % (p["coin"], cc.fmt_price(old, p.get("tick")),
                                                                     cc.fmt_price(new, p.get("tick")), locked))
    return 0


def account(a, cfg):
    led = cc.read_ledger()
    paper_closes = [e for e in led if e.get("type") == "close" and e.get("paper", True)]
    book = load_book()
    realized = sum(cc.fnum(e.get("pnl_usd"), 0.0) for e in paper_closes)
    start = float(cfg["account_usd"])
    margin = sum(p["margin"] for p in book["positions"] if p.get("paper"))
    n_open = len([p for p in book["positions"] if p.get("paper")])
    if a.json:
        d = {"paper": {"start_usd": start, "realized_usd": realized, "trades": len(paper_closes), "balance_usd": start + realized,
                       "margin_usd": margin, "open": n_open}}
        if not cfg.get("paper", True):
            venue = cfg.get("live_venue") or "binance"
            d["live"] = dict(ex.account(venue).balance(), venue=venue)
        cc.dump(d)
        return 0
    cc.out("Paper account: started %s, realized %s over %d trade%s, balance %s, %s in margin on %d open position%s." % (
        cc.fmt_money(start), cc.fmt_money(realized), len(paper_closes), "" if len(paper_closes) == 1 else "s",
        cc.fmt_money(start + realized), cc.fmt_money(margin), n_open, "" if n_open == 1 else "s"))
    if not cfg.get("paper", True):
        venue = cfg.get("live_venue") or "binance"
        b = ex.account(venue).balance()
        cc.out("Live %s: balance %s, available %s, unrealized %s." % (venue.capitalize(), cc.fmt_money(b["balance"]),
                                                                    cc.fmt_money(b["available"]), cc.fmt_money(b["unrealized"])))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="broker.py", description="Place, track and close orders: paper by default.")
    p.add_argument("command", choices=["ticket", "place", "sync", "positions", "close", "move-stop", "account"])
    p.add_argument("target", nargs="?", default=None, help="close / move-stop: the coin")
    p.add_argument("price", nargs="?", default=None, help="move-stop: the new stop")
    p.add_argument("--order", type=int, default=1, help="which order in crypto/orders.json (default 1)")
    p.add_argument("--yes", default=None, help="the order's fingerprint (place) or the coin (close), after the user's yes")
    p.add_argument("--live", action="store_true", help="confirm a real-money order (the config must be live too)")
    cc.add_common(p, venue=False)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    fn = {"ticket": ticket, "place": place, "sync": sync, "positions": positions, "close": close,
          "move-stop": move_stop, "account": account}[a.command]
    return fn(a, cfg)


if __name__ == "__main__":
    sys.exit(cc.run(main))
