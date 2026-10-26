# -*- coding: utf-8 -*-
"""Fake Binance USD-M and Bybit V5 accounts for the offline tests of live trading.

They check what the real exchanges check, in the real response formats: the HMAC
signature, the API key header, the timestamp window, the size step and price tick,
reduce-only rules, stops that would trigger at once, and (Binance) that conditional
orders go through the Algo Order API, not /fapi/v1/order. State lives per test
session (FAKE_SESSION). Tests move prices with move_price() to fire stops and targets.

Test keys: KEY / SECRET below. Nothing here touches a real exchange.
"""

import hashlib
import hmac
import json
import os
import threading
import urllib.parse

KEY = "test-key-0123456789"
SECRET = "test-secret-abcdefghijklmnopqrstuvwxyz"
_LOCK = threading.Lock()
_STATE = {}

BINANCE_PRIVATE = {"/fapi/v1/positionSide/dual", "/fapi/v1/marginType", "/fapi/v1/leverage", "/fapi/v1/order",
                   "/fapi/v1/algoOrder", "/fapi/v1/openAlgoOrders", "/fapi/v1/algoOpenOrders", "/fapi/v1/openOrders",
                   "/fapi/v1/allOpenOrders", "/fapi/v3/positionRisk", "/fapi/v3/balance", "/fapi/v1/userTrades",
                   "/fapi/v1/income"}
BYBIT_PRIVATE = {"/v5/account/info", "/v5/position/list", "/v5/position/set-leverage", "/v5/order/create",
                 "/v5/order/realtime", "/v5/order/amend", "/v5/order/cancel-all", "/v5/account/wallet-balance",
                 "/v5/position/closed-pnl"}


def is_private(host, path):
    return (host == "fapi.binance.com" and path in BINANCE_PRIVATE) or (host == "api.bybit.com" and path in BYBIT_PRIVATE)


def state():
    sid = os.environ.get("FAKE_SESSION", "")
    with _LOCK:
        st = _STATE.get(sid)
        if st is None:
            st = _STATE[sid] = {
                "binance": {"dual": False, "margin": {}, "lev": {}, "pos": {}, "orders": [], "algo": [], "fills": [],
                            "income": [], "balance": 1000.0, "refuse_algo": False, "refuse_close": False, "done": [], "next": 1000},
                "bybit": {"mode": "ISOLATED_MARGIN", "hedge": set(), "pos": {}, "orders": [], "closed": [], "lev": {},
                          "balance": 1000.0, "drop_stop": False, "refuse_close": False, "next": 5000},
                "calls": [], "price": {},
                "lose_reply": None,      # "after": the next order executes, then its answer is lost; "before": it never arrives
            }
        return st


def reset():
    with _LOCK:
        _STATE.pop(os.environ.get("FAKE_SESSION", ""), None)


def _mkt_coin(symbol):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    if base.startswith("1000"):
        return base[4:], 1000.0
    return base, 1.0


def price_of(symbol, mkt):
    st = state()
    if symbol in st["price"]:
        return st["price"][symbol]
    coin, mult = _mkt_coin(symbol)
    return mkt.path(coin)[-1][4] * mult


def steps_for(symbol, mkt):
    p = price_of(symbol, mkt)
    if p >= 10000:
        tick = 0.1
    elif p >= 1000:
        tick = 0.01
    elif p >= 10:
        tick = 0.001
    elif p >= 1:
        tick = 0.0001
    elif p >= 0.01:
        tick = 0.00001
    else:
        tick = 0.0000001
    return tick, (0.001 if p > 100 else 1.0)


def on_grid(x, step):
    q = float(x) / step
    return abs(q - round(q)) < 1e-6


def now_ms():
    from datetime import datetime, timezone
    v = os.environ.get("FAKE_NOW") or os.environ.get("CRYPTO_NOW")
    v = v.replace("T", " ").replace("Z", "")
    fmt = "%Y-%m-%d %H:%M" if len(v) <= 16 else "%Y-%m-%d %H:%M:%S"
    return int(datetime.strptime(v, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)


def sig(payload):
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- Binance

def b_err(code, msg, status=400):
    return status, {"code": code, "msg": msg}


def handle_binance(method, path, query, headers, mkt):
    st = state()
    b = st["binance"]
    if headers.get("X-MBX-APIKEY") != KEY:
        return b_err(-2015, "Invalid API-key, IP, or permissions for action.", 401)
    if "&signature=" not in query:
        return b_err(-1102, "Mandatory parameter 'signature' was not sent.")
    body, signature = query.rsplit("&signature=", 1)
    if sig(body) != signature:
        return b_err(-1022, "Signature for this request is not valid.")
    q = dict(urllib.parse.parse_qsl(body))
    ts = int(q.get("timestamp", 0))
    if abs(ts - now_ms()) > int(q.get("recvWindow", 5000)):
        return b_err(-1021, "Timestamp for this request is outside of the recvWindow.")
    st["calls"].append(("binance", method, path, q))
    sym = q.get("symbol")
    now = now_ms()
    if path == "/fapi/v1/positionSide/dual":
        return 200, {"dualSidePosition": b["dual"]}
    if path == "/fapi/v1/marginType":
        if b["margin"].get(sym) == q.get("marginType"):
            return b_err(-4046, "No need to change margin type.")
        b["margin"][sym] = q.get("marginType")
        return 200, {"code": 200, "msg": "success"}
    if path == "/fapi/v1/leverage":
        b["lev"][sym] = int(q["leverage"])
        return 200, {"leverage": int(q["leverage"]), "maxNotionalValue": "1000000", "symbol": sym}
    if path == "/fapi/v3/balance":
        upnl = sum(_b_upnl(s, p, mkt) for s, p in b["pos"].items())
        return 200, [{"accountAlias": "x", "asset": "USDT", "balance": "%.8f" % b["balance"], "crossWalletBalance": "%.8f" % b["balance"],
                      "crossUnPnl": "%.8f" % upnl, "availableBalance": "%.8f" % b["balance"], "maxWithdrawAmount": "%.8f" % b["balance"],
                      "marginAvailable": True, "updateTime": now}]
    if path == "/fapi/v3/positionRisk":
        rows = []
        for s, p in b["pos"].items():
            if sym and s != sym or not p["amt"]:
                continue
            mark = price_of(s, mkt)
            rows.append({"symbol": s, "positionSide": "BOTH", "positionAmt": "%.6f" % p["amt"], "entryPrice": "%.8f" % p["entry"],
                         "breakEvenPrice": "%.8f" % p["entry"], "markPrice": "%.8f" % mark,
                         "unRealizedProfit": "%.8f" % _b_upnl(s, p, mkt), "liquidationPrice": "%.8f" % (p["entry"] * 0.6),
                         "isolatedMargin": "%.8f" % (abs(p["amt"]) * p["entry"] / b["lev"].get(s, 20)),
                         "notional": "%.8f" % (p["amt"] * mark), "marginAsset": "USDT", "updateTime": now})
        return 200, rows
    if path == "/fapi/v1/openOrders":
        return 200, [o for o in b["orders"] if o["symbol"] == sym and o["status"] == "NEW"]
    if path == "/fapi/v1/openAlgoOrders":
        return 200, [o for o in b["algo"] if o["symbol"] == sym and o["algoStatus"] == "NEW"]
    if path == "/fapi/v1/allOpenOrders":
        for o in b["orders"]:
            if o["symbol"] == sym and o["status"] == "NEW":
                o["status"] = "CANCELED"
        return 200, {"code": 200, "msg": "The operation of cancel all open order is done."}
    if path == "/fapi/v1/algoOpenOrders":
        for o in b["algo"]:
            if o["symbol"] == sym and o["algoStatus"] == "NEW":
                o["algoStatus"] = "CANCELED"
        return 200, {"code": 200, "msg": "success"}
    if path == "/fapi/v1/algoOrder" and method == "DELETE":
        for o in b["algo"]:
            if o["clientAlgoId"] == q.get("clientAlgoId") and o["algoStatus"] == "NEW":
                o["algoStatus"] = "CANCELED"
                return 200, {"algoId": o["algoId"], "clientAlgoId": o["clientAlgoId"], "code": "200", "msg": "success"}
        return b_err(-2011, "Unknown order sent.")
    if path == "/fapi/v1/userTrades":
        s0, e0 = int(q.get("startTime", 0)), int(q.get("endTime", now))
        if e0 - s0 > 7 * 86400000:
            return b_err(-1127, "More than 7 days between startTime and endTime.")
        return 200, [f for f in b["fills"] if f["symbol"] == sym and s0 <= f["time"] <= e0]
    if path == "/fapi/v1/income":
        kind = q.get("incomeType")
        return 200, [r for r in b["income"] if (not kind or r["incomeType"] == kind) and
                     int(q.get("startTime", 0)) <= r["time"] <= int(q.get("endTime", now))][:int(q.get("limit", 100))]
    if path == "/fapi/v1/order" and method == "GET":
        for o in b["orders"] + b["done"]:
            if o["symbol"] == sym and o["clientOrderId"] == q.get("origClientOrderId"):
                return 200, o
        return b_err(-2013, "Order does not exist.")
    tick, step = steps_for(sym, mkt) if sym else (None, None)
    if path == "/fapi/v1/order" and method == "POST":
        typ = q.get("type")
        if typ in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET"):
            return b_err(-4120, "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.")
        qty = float(q["quantity"])
        if not on_grid(qty, step):
            return b_err(-1111, "Precision is over the maximum defined for this asset.")
        side = q["side"]
        reduce = q.get("reduceOnly") == "true"
        p = b["pos"].setdefault(sym, {"amt": 0.0, "entry": 0.0})
        signed = qty if side == "BUY" else -qty
        if reduce and (p["amt"] == 0 or (p["amt"] > 0) == (signed > 0) or (b["refuse_close"] and typ == "MARKET")):
            return b_err(-2022, "ReduceOnly Order is rejected.")
        b["next"] += 1
        oid = b["next"]
        px = price_of(sym, mkt)
        if typ == "LIMIT":
            price = float(q["price"])
            if not on_grid(price, tick):
                return b_err(-4014, "Price not increased by tick size.")
            marketable = (side == "BUY" and price >= px) or (side == "SELL" and price <= px)
            o = {"orderId": oid, "symbol": sym, "status": "NEW", "clientOrderId": q.get("newClientOrderId", "x%d" % oid),
                 "price": q["price"], "avgPrice": "0", "origQty": q["quantity"], "executedQty": "0", "type": "LIMIT",
                 "reduceOnly": reduce, "side": side, "positionSide": "BOTH", "timeInForce": "GTC", "updateTime": now}
            b["orders"].append(o)
            if marketable:
                _b_fill(sym, side, qty, price, now, oid, mkt, maker=False)
                o["status"], o["executedQty"], o["avgPrice"] = "FILLED", q["quantity"], q["price"]
            return 200, o
        if typ != "MARKET":
            return b_err(-1116, "Invalid orderType.")
        if not reduce and qty * px < 5:
            return b_err(-4164, "Order's notional must be no smaller than 5 (unless you choose reduce only).")
        fill = px * (1.0001 if side == "BUY" else 0.9999)
        _b_fill(sym, side, qty, fill, now, oid, mkt, maker=False)
        o = {"orderId": oid, "symbol": sym, "status": "FILLED", "clientOrderId": q.get("newClientOrderId", "x%d" % oid),
             "price": "0", "avgPrice": "%.8f" % fill, "origQty": q["quantity"], "executedQty": q["quantity"],
             "cumQuote": "%.8f" % (fill * qty), "timeInForce": "GTC", "type": "MARKET", "reduceOnly": reduce,
             "side": side, "positionSide": "BOTH", "updateTime": now}
        b["done"].append(o)
        return 200, o
    if path == "/fapi/v1/algoOrder" and method == "POST":
        if b["refuse_algo"]:
            return b_err(-4045, "Reach max stop order limit.")
        if q.get("algoType") != "CONDITIONAL":
            return b_err(-1102, "Mandatory parameter 'algoType' was not sent, was empty/null, or malformed.")
        if q.get("type") not in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET"):
            return b_err(-1116, "Invalid orderType.")
        trig = float(q["triggerPrice"])
        if not on_grid(trig, tick):
            return b_err(-4014, "Price not increased by tick size.")
        if not on_grid(float(q["quantity"]), step):
            return b_err(-1111, "Precision is over the maximum defined for this asset.")
        mark = price_of(sym, mkt)
        if q["type"] == "STOP_MARKET" and ((q["side"] == "SELL" and trig >= mark) or (q["side"] == "BUY" and trig <= mark)):
            return b_err(-2021, "Order would immediately trigger.")
        b["next"] += 1
        o = {"algoId": b["next"], "clientAlgoId": q.get("clientAlgoId") or "a%d" % b["next"], "algoType": "CONDITIONAL",
             "orderType": q["type"], "symbol": sym, "side": q["side"], "positionSide": "BOTH", "timeInForce": "GTC",
             "quantity": q["quantity"], "algoStatus": "NEW", "triggerPrice": q["triggerPrice"], "price": "0",
             "workingType": q.get("workingType", "CONTRACT_PRICE"), "reduceOnly": q.get("reduceOnly") == "true",
             "createTime": now, "updateTime": now}
        b["algo"].append(o)
        return 200, o
    return b_err(-5000, "Path %s, Method %s is invalid" % (path, method), 404)


def _b_upnl(sym, p, mkt):
    return (price_of(sym, mkt) - p["entry"]) * p["amt"] if p["amt"] else 0.0


def _b_fill(sym, side, qty, price, now, oid, mkt=None, maker=False):
    b = state()["binance"]
    p = b["pos"].setdefault(sym, {"amt": 0.0, "entry": 0.0})
    signed = qty if side == "BUY" else -qty
    realized = 0.0
    if p["amt"] and (p["amt"] > 0) != (signed > 0):
        closing = min(abs(signed), abs(p["amt"]))
        realized = (price - p["entry"]) * closing * (1 if p["amt"] > 0 else -1)
        p["amt"] += signed
        if abs(p["amt"]) < 1e-12:
            p["amt"], p["entry"] = 0.0, 0.0
    else:
        new = p["amt"] + signed
        p["entry"] = (p["entry"] * abs(p["amt"]) + price * qty) / abs(new)
        p["amt"] = new
    fee = qty * price * (0.0002 if maker else 0.0005)
    b["balance"] += realized - fee
    tid = len(b["fills"]) + 1
    b["income"].append({"symbol": sym, "incomeType": "COMMISSION", "income": "%.8f" % -fee, "asset": "USDT", "info": "",
                        "time": now, "tranId": 9000 + tid, "tradeId": str(tid)})
    if realized:
        b["income"].append({"symbol": sym, "incomeType": "REALIZED_PNL", "income": "%.8f" % realized, "asset": "USDT",
                            "info": "", "time": now, "tranId": 8000 + tid, "tradeId": str(tid)})
    b["fills"].append({"buyer": side == "BUY", "commission": "%.8f" % fee, "commissionAsset": "USDT", "id": len(b["fills"]) + 1,
                       "maker": maker, "orderId": oid, "price": "%.8f" % price, "qty": "%.6f" % qty,
                       "quoteQty": "%.8f" % (qty * price), "realizedPnl": "%.8f" % realized, "side": side,
                       "positionSide": "BOTH", "symbol": sym, "time": now})


# ---------------------------------------------------------------- Bybit

def y_ok(result):
    return 200, {"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {}, "time": now_ms()}


def y_err(code, msg):
    return 200, {"retCode": code, "retMsg": msg, "result": {}, "retExtInfo": {}, "time": now_ms()}


def handle_bybit(method, path, query, body, headers, mkt):
    st = state()
    y = st["bybit"]
    if headers.get("X-BAPI-API-KEY") != KEY:
        return y_err(10003, "API key is invalid.")
    ts, recv = headers.get("X-BAPI-TIMESTAMP", "0"), headers.get("X-BAPI-RECV-WINDOW", "5000")
    payload = query if method == "GET" else (body or b"").decode("utf-8")
    if sig(ts + KEY + recv + payload) != headers.get("X-BAPI-SIGN"):
        return y_err(10004, "error sign! origin_string[%s]" % (ts + KEY + recv + payload))
    if abs(int(ts) - now_ms()) > int(recv):
        return y_err(10002, "invalid request, please check your server timestamp or recv_window param")
    q = dict(urllib.parse.parse_qsl(query)) if method == "GET" else json.loads(payload or "{}")
    st["calls"].append(("bybit", method, path, q))
    sym = q.get("symbol")
    now = now_ms()
    if path == "/v5/account/info":
        return y_ok({"marginMode": y["mode"], "unifiedMarginStatus": 5, "isMasterTrader": False, "updatedTime": str(now)})
    if path == "/v5/account/wallet-balance":
        upnl = sum((price_of(s, mkt) - p["avg"]) * p["size"] * (1 if p["side"] == "Buy" else -1) for s, p in y["pos"].items() if p["size"])
        return y_ok({"list": [{"accountType": "UNIFIED", "totalEquity": "%.4f" % (y["balance"] + upnl),
                               "totalWalletBalance": "%.4f" % y["balance"], "totalAvailableBalance": "%.4f" % y["balance"],
                               "totalPerpUPL": "%.4f" % upnl, "coin": [{"coin": "USDT", "walletBalance": "%.4f" % y["balance"]}]}]})
    if path == "/v5/position/list":
        rows = []
        for s, p in y["pos"].items():
            if sym and s != sym:
                continue
            mark = price_of(s, mkt)
            rows.append({"positionIdx": 1 if s in y["hedge"] else 0, "symbol": s, "side": p["side"] if p["size"] else "",
                         "size": "%.6f" % p["size"], "avgPrice": "%.8f" % p["avg"], "markPrice": "%.8f" % mark,
                         "liqPrice": "%.8f" % (p["avg"] * (0.6 if p["side"] == "Buy" else 1.4)), "leverage": str(y["lev"].get(s, 10)),
                         "unrealisedPnl": "%.8f" % ((mark - p["avg"]) * p["size"] * (1 if p["side"] == "Buy" else -1)),
                         "positionIM": "%.8f" % (p["size"] * p["avg"] / float(y["lev"].get(s, 10))), "tpslMode": "Partial",
                         "createdTime": str(now), "updatedTime": str(now)})
        if sym and not rows:
            rows.append({"positionIdx": 1 if sym in y["hedge"] else 0, "symbol": sym, "side": "", "size": "0", "avgPrice": "0"})
        return y_ok({"category": "linear", "list": rows, "nextPageCursor": ""})
    if path == "/v5/position/set-leverage":
        if y["lev"].get(sym) == float(q["buyLeverage"]):
            return y_err(110043, "Set leverage not modified")
        y["lev"][sym] = float(q["buyLeverage"])
        return y_ok({})
    if path == "/v5/order/realtime":
        return y_ok({"list": [o for o in y["orders"] if o["symbol"] == sym and o["orderStatus"] in ("New", "Untriggered")],
                     "nextPageCursor": "", "category": "linear"})
    if path == "/v5/order/cancel-all":
        n = 0
        for o in y["orders"]:
            if o["symbol"] == sym and o["orderStatus"] in ("New", "Untriggered"):
                o["orderStatus"] = "Cancelled"
                n += 1
        return y_ok({"list": [], "success": "1"})
    if path == "/v5/order/amend":
        for o in y["orders"]:
            if o["orderId"] == q.get("orderId") and o["orderStatus"] in ("New", "Untriggered"):
                if q.get("triggerPrice"):
                    o["triggerPrice"] = q["triggerPrice"]
                return y_ok({"orderId": o["orderId"], "orderLinkId": o.get("orderLinkId", "")})
        return y_err(110001, "order not exists or too late to replace")
    if path == "/v5/position/closed-pnl":
        rows = [r for r in y["closed"] if (not sym or r["symbol"] == sym) and int(r["updatedTime"]) >= int(q.get("startTime", 0))]
        return y_ok({"list": sorted(rows, key=lambda r: -int(r["updatedTime"])), "nextPageCursor": "", "category": "linear"})
    if path == "/v5/order/create":
        tick, step = steps_for(sym, mkt)
        if q.get("category") != "linear":
            return y_err(10001, "params error: category")
        qty = float(q["qty"])
        if not on_grid(qty, step):
            return y_err(10001, "Qty invalid")
        side = q["side"]
        px = price_of(sym, mkt)
        p = y["pos"].setdefault(sym, {"side": "", "size": 0.0, "avg": 0.0})
        if q.get("reduceOnly"):
            if not p["size"] or p["side"] == side or y["refuse_close"]:
                return y_err(110017, "current position is zero, cannot fix reduce-only order qty")
        if q.get("stopLoss"):
            sl = float(q["stopLoss"])
            if not on_grid(sl, tick):
                return y_err(10001, "StopLoss price invalid")
            if (side == "Buy" and sl >= px) or (side == "Sell" and sl <= px):
                return y_err(10001, "StopLoss:%s set for %s position should %s than base_price:%s" % (
                    q["stopLoss"], side, "lower" if side == "Buy" else "higher", px))
            if q.get("tpslMode") == "Partial" and q.get("tpOrderType") == "Limit" and not q.get("tpLimitPrice"):
                return y_err(10001, "tpLimitPrice is required")
        y["next"] += 1
        oid = "y%d" % y["next"]
        if q["orderType"] == "Limit":
            price = float(q["price"])
            y["orders"].append({"orderId": oid, "orderLinkId": q.get("orderLinkId", ""), "symbol": sym, "side": side,
                                "orderType": "Limit", "price": q["price"], "qty": q["qty"], "orderStatus": "New",
                                "stopOrderType": "", "triggerPrice": "", "reduceOnly": False, "_tpsl": q})
            return y_ok({"orderId": oid, "orderLinkId": q.get("orderLinkId", "")})
        fill = px * (1.0001 if side == "Buy" else 0.9999)
        _y_fill(sym, side, qty, fill, now)
        if q.get("stopLoss") and not y["drop_stop"]:
            _y_brackets(sym, side, qty, q)
        return y_ok({"orderId": oid, "orderLinkId": q.get("orderLinkId", "")})
    return y_err(10404, "not found")


def _y_brackets(sym, side, qty, q):
    y = state()["bybit"]
    exit_side = "Sell" if side == "Buy" else "Buy"
    y["next"] += 1
    y["orders"].append({"orderId": "y%d" % y["next"], "symbol": sym, "side": exit_side, "orderType": "Market", "qty": "%g" % qty,
                        "triggerPrice": q["stopLoss"], "triggerBy": q.get("slTriggerBy", "LastPrice"),
                        "stopOrderType": "PartialStopLoss", "orderStatus": "Untriggered", "reduceOnly": True})
    if q.get("takeProfit"):
        y["next"] += 1
        y["orders"].append({"orderId": "y%d" % y["next"], "symbol": sym, "side": exit_side, "orderType": "Limit",
                            "qty": "%g" % qty, "price": q.get("tpLimitPrice") or q["takeProfit"], "triggerPrice": q["takeProfit"],
                            "stopOrderType": "PartialTakeProfit", "orderStatus": "Untriggered", "reduceOnly": True})


def _y_fill(sym, side, qty, price, now):
    y = state()["bybit"]
    p = y["pos"].setdefault(sym, {"side": "", "size": 0.0, "avg": 0.0, "opened": now})
    if p["size"] and p["side"] != side:
        closing = min(qty, p["size"])
        sgn = 1 if p["side"] == "Buy" else -1
        pnl = (price - p["avg"]) * closing * sgn - closing * price * 0.00055
        y["closed"].append({"symbol": sym, "orderId": "c%d" % len(y["closed"]), "side": side, "qty": "%g" % closing,
                            "orderPrice": "%.8f" % price, "closedSize": "%g" % closing, "avgEntryPrice": "%.8f" % p["avg"],
                            "avgExitPrice": "%.8f" % price, "closedPnl": "%.8f" % pnl, "leverage": "3",
                            "createdTime": str(p.get("opened", now)), "updatedTime": str(now)})
        y["balance"] += pnl
        p["size"] -= closing
        if p["size"] <= 1e-12:
            p.update({"side": "", "size": 0.0, "avg": 0.0})
    else:
        p["avg"] = (p["avg"] * p["size"] + price * qty) / (p["size"] + qty)
        p["size"] += qty
        p["side"] = side
        p["opened"] = now
        y["balance"] -= qty * price * 0.00055


# ---------------------------------------------------------------- tests drive the market

def move_price(symbol, price):
    """Set a price and let resting stops and targets on both fake accounts fire, as the exchange would."""
    st = state()
    st["price"][symbol] = price
    now = now_ms()
    b = st["binance"]
    for o in b["algo"]:
        if o["symbol"] == symbol and o["algoStatus"] == "NEW" and o["orderType"] == "STOP_MARKET":
            trig = float(o["triggerPrice"])
            if (o["side"] == "SELL" and price <= trig) or (o["side"] == "BUY" and price >= trig):
                p = b["pos"].get(symbol)
                if p and p["amt"]:
                    _b_fill(symbol, o["side"], min(float(o["quantity"]), abs(p["amt"])), trig, now, o["algoId"])
                o["algoStatus"] = "FINISHED"
    for o in b["orders"]:
        if o["symbol"] == symbol and o["status"] == "NEW":
            lim = float(o["price"])
            if (o["side"] == "SELL" and price >= lim) or (o["side"] == "BUY" and price <= lim):
                p = b["pos"].get(symbol)
                if o["reduceOnly"] and (not p or not p["amt"]):
                    o["status"] = "EXPIRED"
                    continue
                _b_fill(symbol, o["side"], float(o["origQty"]), lim, now, o["orderId"], None, maker=True)
                o["status"] = "FILLED"
    y = st["bybit"]
    for o in y["orders"]:
        if o["symbol"] != symbol or o["orderStatus"] not in ("New", "Untriggered"):
            continue
        p = y["pos"].get(symbol) or {"size": 0}
        trig = float(o["triggerPrice"]) if o.get("triggerPrice") else None
        if o["stopOrderType"] == "PartialStopLoss":
            if (o["side"] == "Sell" and price <= trig) or (o["side"] == "Buy" and price >= trig):
                if p["size"]:
                    _y_fill(symbol, o["side"], float(o["qty"]), trig, now)
                o["orderStatus"] = "Triggered"
        elif o["stopOrderType"] == "PartialTakeProfit":
            if (o["side"] == "Sell" and price >= trig) or (o["side"] == "Buy" and price <= trig):
                if p["size"]:
                    _y_fill(symbol, o["side"], float(o["qty"]), float(o["price"]), now)
                o["orderStatus"] = "Filled"
        elif o["orderType"] == "Limit" and not o["stopOrderType"]:
            lim = float(o["price"])
            if (o["side"] == "Buy" and price <= lim) or (o["side"] == "Sell" and price >= lim):
                _y_fill(symbol, o["side"], float(o["qty"]), lim, now)
                o["orderStatus"] = "Filled"
                if o.get("_tpsl", {}).get("stopLoss"):
                    _y_brackets(symbol, o["side"], float(o["qty"]), o["_tpsl"])


def handle(method, host, path, query, data, headers, mkt):
    if host == "fapi.binance.com":
        return handle_binance(method, path, query, headers or {}, mkt)
    return handle_bybit(method, path, query, data, headers or {}, mkt)
