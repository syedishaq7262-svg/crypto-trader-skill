# -*- coding: utf-8 -*-
"""Signed account access for crypto-broker (and read-only imports for crypto-journal).

Binance USD-M futures and Bybit V5 linear perpetuals, standard library only.
Keys come from environment variables and are never printed, logged or saved:

    BINANCE_API_KEY, BINANCE_API_SECRET
    BYBIT_API_KEY,   BYBIT_API_SECRET

Use keys with trading allowed and withdrawals OFF, and an IP allowlist if you can.
Every call that places, changes or cancels an order is sent once (never retried
blindly): a timeout is followed by a lookup, not a second order.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, os.pardir, "crypto-setup")):
    if os.path.isfile(os.path.join(_d, "cryptocore.py")):
        sys.path.insert(0, os.path.abspath(_d))
        break
import cryptocore as cc  # noqa: E402

RECV_WINDOW = 5000


class ExchangeError(cc.CryptoError):
    """The exchange refused a signed request. .code holds its error code."""

    def __init__(self, venue, code, msg):
        cc.CryptoError.__init__(self, "%s refused it (code %s): %s" % (venue, code, msg))
        self.code, self.msg = code, msg


def keys_for(venue):
    names = {"binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET"), "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET")}
    if venue not in names:
        raise cc.CryptoError("live orders work on Binance and Bybit. Hyperliquid needs a wallet signature this pack "
                             "does not do; it stays paper only.")
    k, s = names[venue]
    key, secret = os.environ.get(k), os.environ.get(s)
    if not key or not secret:
        raise cc.CryptoError("no %s keys: set %s and %s in your environment (never in the chat). "
                             "Trading on, withdrawals off." % (venue.capitalize(), k, s))
    return key, secret


def hmac_hex(secret, payload):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def num(x):
    """A number as the exchanges want it: no exponent, no trailing zeros."""
    if isinstance(x, str):
        return x
    s = ("%.12f" % x).rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


class Account(object):
    venue = "?"

    def __init__(self, key=None, secret=None):
        if key is None:
            key, secret = keys_for(self.venue)
        self.key, self.secret = key, secret
        self._offset = None

    def now(self):
        if self._offset is None:
            self._offset = 0
            if not cc.pinned():
                try:
                    t0 = time.time()
                    st = self.server_time()
                    t1 = time.time()
                    self._offset = int(st - (t0 + t1) / 2.0 * 1000)
                except cc.CryptoError:
                    self._offset = 0
        return cc.now_ms() + self._offset


class BinanceAccount(Account):
    """USD-M futures. One-way position mode only (the pack refuses hedge mode)."""

    venue = "binance"
    BASE = "https://fapi.binance.com"

    def server_time(self):
        r = cc.HTTP.request("GET", self.BASE + "/fapi/v1/time")
        return int(r["serverTime"])

    def call(self, method, path, params=None, change=False):
        q = []
        for k, v in (params or {}).items():
            if v is None:
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            q.append((k, num(v) if isinstance(v, (int, float)) else v))
        q.append(("recvWindow", RECV_WINDOW))
        q.append(("timestamp", self.now()))
        query = urllib.parse.urlencode(q)
        query += "&signature=" + hmac_hex(self.secret, query)
        status, data = cc.HTTP.request(method, self.BASE + path, query=query, headers={"X-MBX-APIKEY": self.key},
                                       errors="return", retry=not change, private=True, weight=5)
        if status >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0):
            code = data.get("code") if isinstance(data, dict) else status
            msg = (data.get("msg") if isinstance(data, dict) else None) or ("HTTP %s" % status)
            raise ExchangeError("Binance", code, msg)
        return data

    # -- account
    def one_way(self):
        return not self.call("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition")

    def balance(self):
        rows = self.call("GET", "/fapi/v3/balance")
        for r in rows or []:
            if r.get("asset") == "USDT":
                return {"balance": cc.fnum(r.get("balance")), "available": cc.fnum(r.get("availableBalance")),
                        "unrealized": cc.fnum(r.get("crossUnPnl"), 0.0)}
        return {"balance": 0.0, "available": 0.0, "unrealized": 0.0}

    def positions(self, symbol=None):
        rows = self.call("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
        out = []
        for r in rows or []:
            amt = cc.fnum(r.get("positionAmt"), 0.0)
            if not amt:
                continue
            out.append({"symbol": r["symbol"], "side": "long" if amt > 0 else "short", "qty": abs(amt),
                        "entry": cc.fnum(r.get("entryPrice")), "mark": cc.fnum(r.get("markPrice")),
                        "liq": cc.fnum(r.get("liquidationPrice")), "upnl": cc.fnum(r.get("unRealizedProfit"), 0.0),
                        "margin": cc.fnum(r.get("isolatedMargin")) or cc.fnum(r.get("initialMargin"))})
        return out

    def open_orders(self, symbol):
        plain = self.call("GET", "/fapi/v1/openOrders", {"symbol": symbol}) or []
        algo = self.call("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}) or []
        return plain, algo

    # -- setup
    def isolated(self, symbol):
        try:
            self.call("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, change=True)
        except ExchangeError as e:
            if e.code != -4046:          # "No need to change margin type."
                raise

    def leverage(self, symbol, lev):
        return self.call("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(lev)}, change=True)

    # -- orders
    def market(self, symbol, side, qty, reduce_only=False, client_id=None):
        return self.call("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": "BUY" if side == "buy" else "SELL", "type": "MARKET", "quantity": qty,
            "reduceOnly": True if reduce_only else None, "newClientOrderId": client_id, "newOrderRespType": "RESULT"},
            change=True)

    def limit(self, symbol, side, qty, price, reduce_only=False, client_id=None):
        return self.call("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": "BUY" if side == "buy" else "SELL", "type": "LIMIT", "timeInForce": "GTC",
            "quantity": qty, "price": price, "reduceOnly": True if reduce_only else None, "newClientOrderId": client_id},
            change=True)

    def stop_market(self, symbol, side, qty, trigger, client_id=None):
        """A reduce-only stop-market, triggered by the mark price, through the Algo Order API
        (Binance moved conditional orders off /fapi/v1/order)."""
        return self.call("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": "BUY" if side == "buy" else "SELL",
            "type": "STOP_MARKET", "quantity": qty, "triggerPrice": trigger, "workingType": "MARK_PRICE",
            "reduceOnly": True, "clientAlgoId": client_id}, change=True)

    def cancel_algo(self, client_id):
        return self.call("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_id}, change=True)

    def cancel_all(self, symbol):
        self.call("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, change=True)
        self.call("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, change=True)

    def order(self, symbol, client_id):
        return self.call("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})

    # -- history
    def fills(self, symbol, start, end=None):
        out = []
        end = end or self.now()
        s = start
        while s <= end:
            e = min(end, s + 7 * cc.DAY_MS - 1)          # the endpoint takes at most 7 days a call
            rows = self.call("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": s, "endTime": e, "limit": 1000}) or []
            out.extend(rows)
            s = e + 1
        out.sort(key=lambda r: r.get("time", 0))
        return out

    def income(self, start, end=None, kind=None):
        rows = self.call("GET", "/fapi/v1/income", {"incomeType": kind, "startTime": start, "endTime": end or self.now(),
                                                   "limit": 1000}) or []
        return rows


class BybitAccount(Account):
    """V5 unified account, linear USDT perpetuals, one-way mode (positionIdx 0)."""

    venue = "bybit"
    BASE = "https://api.bybit.com"

    def server_time(self):
        r = cc.HTTP.request("GET", self.BASE + "/v5/market/time")
        return int(r["result"]["timeNano"]) // 1000000

    def call(self, method, path, params=None, change=False, ok_codes=()):
        ts = str(self.now())
        recv = str(RECV_WINDOW)
        if method == "GET":
            items = [(k, v) for k, v in (params or {}).items() if v is not None]
            payload = urllib.parse.urlencode(items)
            sign = hmac_hex(self.secret, ts + self.key + recv + payload)
            hdr = {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sign}
            status, data = cc.HTTP.request("GET", self.BASE + path, query=payload, headers=hdr, errors="return",
                                           retry=True, private=True)
        else:
            body = dict((k, v) for k, v in (params or {}).items() if v is not None)
            payload = json.dumps(body, separators=(",", ":"))
            sign = hmac_hex(self.secret, ts + self.key + recv + payload)
            hdr = {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sign,
                   "Content-Type": "application/json"}
            status, data = cc.HTTP.request("POST", self.BASE + path, data=payload, headers=hdr, errors="return",
                                           retry=not change, private=True)
        if status >= 400 or not isinstance(data, dict):
            raise ExchangeError("Bybit", status, (data or {}).get("retMsg") if isinstance(data, dict) else "HTTP %s" % status)
        code = data.get("retCode")
        if str(code) != "0" and code not in ok_codes and str(code) not in [str(c) for c in ok_codes]:
            raise ExchangeError("Bybit", code, data.get("retMsg"))
        return data.get("result") or {}

    def margin_mode(self):
        return (self.call("GET", "/v5/account/info") or {}).get("marginMode")

    def one_way(self, symbol):
        rows = self.call("GET", "/v5/position/list", {"category": "linear", "symbol": symbol}).get("list") or []
        return all(int(r.get("positionIdx") or 0) == 0 for r in rows)

    def balance(self):
        rows = self.call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}).get("list") or []
        if not rows:
            return {"balance": 0.0, "available": 0.0, "unrealized": 0.0}
        r = rows[0]
        return {"balance": cc.fnum(r.get("totalWalletBalance") or r.get("totalEquity"), 0.0),
                "available": cc.fnum(r.get("totalAvailableBalance"), 0.0),
                "unrealized": cc.fnum(r.get("totalPerpUPL"), 0.0)}

    def positions(self, symbol=None):
        params = {"category": "linear", "symbol": symbol} if symbol else {"category": "linear", "settleCoin": "USDT"}
        rows = self.call("GET", "/v5/position/list", params).get("list") or []
        out = []
        for r in rows:
            size = cc.fnum(r.get("size"), 0.0)
            if not size:
                continue
            out.append({"symbol": r["symbol"], "side": "long" if r.get("side") == "Buy" else "short", "qty": size,
                        "entry": cc.fnum(r.get("avgPrice")), "mark": cc.fnum(r.get("markPrice")),
                        "liq": cc.fnum(r.get("liqPrice")), "upnl": cc.fnum(r.get("unrealisedPnl"), 0.0),
                        "margin": cc.fnum(r.get("positionIM")) or cc.fnum(r.get("positionBalance"))})
        return out

    def open_orders(self, symbol):
        return self.call("GET", "/v5/order/realtime", {"category": "linear", "symbol": symbol}).get("list") or []

    def leverage(self, symbol, lev):
        return self.call("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": symbol,
                                                               "buyLeverage": num(lev), "sellLeverage": num(lev)},
                         change=True, ok_codes=(110043,))       # 110043: leverage not modified

    def bracket(self, symbol, side, qty, stop, target, order_type="Market", price=None, link_id=None):
        """Entry with its stop-loss (market, mark price) and take-profit (limit) attached."""
        return self.call("POST", "/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": "Buy" if side == "buy" else "Sell", "orderType": order_type,
            "qty": num(qty), "price": num(price) if price is not None else None,
            "timeInForce": "GTC" if order_type == "Limit" else None, "positionIdx": 0, "orderLinkId": link_id,
            "stopLoss": num(stop), "slTriggerBy": "MarkPrice", "slOrderType": "Market",
            "takeProfit": num(target), "tpTriggerBy": "LastPrice", "tpOrderType": "Limit", "tpLimitPrice": num(target),
            "tpslMode": "Partial"}, change=True)

    def market(self, symbol, side, qty, reduce_only=False, link_id=None):
        return self.call("POST", "/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": "Buy" if side == "buy" else "Sell", "orderType": "Market",
            "qty": num(qty), "positionIdx": 0, "reduceOnly": True if reduce_only else None, "orderLinkId": link_id},
            change=True)

    def amend_trigger(self, symbol, order_id, trigger):
        return self.call("POST", "/v5/order/amend", {"category": "linear", "symbol": symbol, "orderId": order_id,
                                                     "triggerPrice": num(trigger)}, change=True)

    def cancel_all(self, symbol):
        return self.call("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": symbol}, change=True)

    def closed_pnl(self, start, end=None, symbol=None):
        out, cursor = [], None
        for _ in range(50):
            res = self.call("GET", "/v5/position/closed-pnl", {"category": "linear", "symbol": symbol, "startTime": start,
                                                               "endTime": end, "limit": 100, "cursor": cursor})
            out.extend(res.get("list") or [])
            cursor = res.get("nextPageCursor")
            if not cursor or not res.get("list"):
                break
        return out


def account(venue, key=None, secret=None):
    if venue == "binance":
        return BinanceAccount(key, secret)
    if venue == "bybit":
        return BybitAccount(key, secret)
    keys_for(venue)          # raises the plain explanation
