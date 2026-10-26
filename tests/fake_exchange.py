# -*- coding: utf-8 -*-
"""A fake exchange for the offline tests.

It answers Binance USD-M, Bybit V5 and Hyperliquid /info requests in their
real response formats (checked against real recorded responses in
tests/test_formats.py), from a SYNTHETIC market: a seeded random walk with
volatility clustering, a busier US session, jumps, and a few coins shaped on
purpose so the scanners have something known to find. Nothing here is real
market data and none of it is shipped to users.

cryptocore loads it when CRYPTO_FAKE_EXCHANGE points at this file.

Failure switches (FAKE_EXCHANGE_MODE, comma separated):
  binance_geoblock  Binance answers HTTP 451 (as it does in the US)
  bybit_geoblock    Bybit answers HTTP 403 from CloudFront
  flaky             the first call to each path fails with HTTP 503
  ratelimit         the first call to each path answers HTTP 429
  timeout           the first call to each path times out
  garbage           one coin's candles come back with a broken row
"""

import json
import math
import os
import random
import socket
import threading
import urllib.parse
from datetime import datetime, timezone

STEP = 300000           # 5-minute bars
DAY = 86400000
HISTORY_DAYS = 35

# coin: (start price, daily vol, beta to BTC, 24h volume USD, listed days ago, note)
COINS = [
    ("BTC", 64000.0, 0.024, 1.00, 9.0e9, 2000, ""),
    ("ETH", 3150.0, 0.032, 1.15, 5.5e9, 2000, ""),
    ("SOL", 152.0, 0.045, 1.35, 1.8e9, 1500, ""),
    ("XRP", 0.62, 0.040, 1.10, 9.0e8, 1500, ""),
    ("DOGE", 0.141, 0.050, 1.40, 7.0e8, 1500, ""),
    ("ADA", 0.38, 0.042, 1.20, 3.0e8, 1500, ""),
    ("AVAX", 27.4, 0.050, 1.35, 2.6e8, 1200, ""),
    ("LINK", 13.6, 0.045, 1.25, 3.2e8, 1200, ""),
    ("SUI", 1.92, 0.060, 1.50, 4.4e8, 600, ""),
    ("BONK", 0.0000215, 0.070, 1.70, 1.6e8, 500, "mult"),
    ("ARB", 0.71, 0.055, 1.40, 1.2e8, 700, ""),
    ("OP", 1.64, 0.055, 1.40, 1.1e8, 700, ""),
    ("NEAR", 4.85, 0.055, 1.35, 1.5e8, 900, ""),
    ("APT", 8.9, 0.055, 1.35, 1.0e8, 700, ""),
    ("INJ", 21.5, 0.060, 1.45, 9.0e7, 800, ""),
    ("TIA", 5.6, 0.065, 1.50, 8.0e7, 500, ""),
    ("WIF", 1.95, 0.080, 1.80, 3.1e8, 450, ""),
    ("ORBX", 12.40, 0.050, 1.20, 1.4e8, 400, "orb_long"),
    ("BRKX", 3.10, 0.050, 1.20, 1.2e8, 400, "breakout_long"),
    ("BRKD", 7.75, 0.050, 1.20, 1.1e8, 400, "breakdown_short"),
    ("SQZX", 2.20, 0.045, 1.10, 9.0e7, 400, "squeeze"),
    ("RSIX", 0.95, 0.055, 1.30, 8.5e7, 400, "reversal_long"),
    ("TRDX", 45.0, 0.030, 0.80, 1.3e8, 400, "trend_long"),
    ("VOLX", 0.48, 0.060, 1.40, 7.0e7, 400, "volume_spike"),
    ("FUNDX", 18.2, 0.050, 1.20, 2.2e8, 400, "crowded_long"),
    ("SHRTX", 0.074, 0.060, 1.30, 1.9e8, 400, "crowded_short"),
    ("NEWX", 0.33, 0.080, 1.50, 3.0e8, 3, "new_listing"),
    ("THINX", 1.05, 0.060, 1.20, 2.5e6, 900, "thin"),
]
FOUR_HOUR_FUNDING = {"WIF", "BONK"}

_LOCK = threading.Lock()
_CACHE = {}
_SEEN = set()


def _modes():
    return set(x.strip() for x in os.environ.get("FAKE_EXCHANGE_MODE", "").split(",") if x.strip())


def _now():
    v = os.environ.get("FAKE_NOW") or os.environ.get("CRYPTO_NOW")
    if not v:
        return int(datetime(2026, 10, 21, 15, 15, tzinfo=timezone.utc).timestamp() * 1000)
    if v.isdigit():
        return int(v)
    v = v.replace("T", " ").replace("Z", "")
    fmt = "%Y-%m-%d %H:%M" if len(v) <= 16 else "%Y-%m-%d %H:%M:%S"
    return int(datetime.strptime(v, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _season(t):
    """Busier around the UTC day roll, London and the US open; quieter at weekends."""
    dt = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
    h = dt.hour + dt.minute / 60.0
    m = 0.75
    m += 0.45 * math.exp(-((h - 14.0) ** 2) / 2.0)
    m += 0.25 * math.exp(-((h - 0.3) ** 2) / 0.6)
    m += 0.20 * math.exp(-((h - 8.0) ** 2) / 1.2)
    if dt.weekday() >= 5:
        m *= 0.7
    return m


def _ny_open(t):
    """The last US stock open (13:30 UTC in summer time, 14:30 in winter) at or before t."""
    dt = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
    for back in range(0, 5):
        d = datetime.fromtimestamp((t - back * DAY) / 1000.0, tz=timezone.utc)
        y = d.year
        # second Sunday of March to first Sunday of November
        mar = datetime(y, 3, 1)
        dst_start = 1 + (6 - mar.weekday()) % 7 + 7
        nov = datetime(y, 11, 1)
        dst_end = 1 + (6 - nov.weekday()) % 7
        day = datetime(y, d.month, d.day)
        summer = datetime(y, 3, dst_start) <= day < datetime(y, 11, dst_end)
        hh = 13 if summer else 14
        o = int(datetime(y, d.month, d.day, hh, 30, tzinfo=timezone.utc).timestamp() * 1000)
        if o <= t and d.weekday() < 5:
            return o
    return None


class Market(object):
    """The synthetic market for one pinned 'now'."""

    def __init__(self, now):
        self.now = now
        self.cur_open = (now // STEP) * STEP          # the bar still forming
        self.start = self.cur_open - HISTORY_DAYS * DAY
        self.times = list(range(self.start, self.cur_open + STEP, STEP))
        self.seasons = [_season(t) for t in self.times]
        self.paths = {}
        self.extra = {}
        self._btc_r = None

    # -- price paths
    def _returns(self, rng, daily_vol, season_scale=1.0):
        n = len(self.times)
        sig = daily_vol / math.sqrt(288.0)
        lg = 0.0
        out = []
        regime = 0.0
        for i in range(n):
            if i % 288 == 0:
                regime = rng.choice((-1.0, 0.0, 0.0, 1.0)) * 0.25 * daily_vol / 288.0
            lg = 0.995 * lg + 0.06 * rng.gauss(0.0, 1.0)
            g = math.exp(max(-1.5, min(1.5, lg)))
            r = regime + sig * self.seasons[i] * season_scale * g * rng.gauss(0.0, 1.0)
            if rng.random() < 1.0 / 900.0:
                r += rng.choice((-1.0, 1.0)) * sig * rng.uniform(4.0, 9.0)
            out.append(r)
        return out

    def _btc(self):
        if self._btc_r is None:
            self._btc_r = self._returns(random.Random(7), 0.024)
        return self._btc_r

    def path(self, coin):
        with _LOCK:
            if coin in self.paths:
                return self.paths[coin]
        spec = [c for c in COINS if c[0] == coin][0]
        _, p0, dvol, beta, vol24, listed, note = spec
        rng = random.Random(sum(ord(ch) * (i + 1) for i, ch in enumerate(coin)) + 11)
        btc = self._btc()
        if coin == "BTC":
            rs = list(btc)
        else:
            idio = self._returns(rng, max(0.01, math.sqrt(max(dvol ** 2 - (beta * 0.024) ** 2, 1e-6))))
            rs = [beta * b + i for b, i in zip(btc, idio)]
        if note == "trend_long":
            rs = [0.35 * r + 0.012 / 288.0 for r in rs]
        if note == "squeeze":
            cut = len(rs) - 3 * 288
            rs = [r if i < cut else 0.15 * r for i, r in enumerate(rs)]
        bars = self._bars(rng, p0, rs, dvol, vol24)
        listed_ms = self.now - listed * DAY
        bars = [b for b in bars if b[0] >= (listed_ms // STEP) * STEP]
        bars = self._shape(coin, note, bars, dvol, vol24, rng)
        with _LOCK:
            self.paths[coin] = bars
        return bars

    def _bars(self, rng, p0, rs, dvol, vol24):
        sig = dvol / math.sqrt(288.0)
        base_vol = vol24 / 288.0
        p = p0
        out = []
        for i, t in enumerate(self.times):
            o = p
            c = o * math.exp(rs[i])
            wick = sig * self.seasons[i] * 0.5
            h = max(o, c) * math.exp(abs(rng.gauss(0.0, wick)))
            lo = min(o, c) * math.exp(-abs(rng.gauss(0.0, wick)))
            qv = base_vol * self.seasons[i] * (0.55 + 2.2 * abs(rs[i]) / max(sig, 1e-9) / 2.0) * math.exp(rng.gauss(0.0, 0.25))
            v = qv / ((o + c) / 2.0)
            out.append([t, o, h, lo, c, v, qv])
            p = c
        return out

    # -- shaped coins, built relative to 'now' so the scanners have known answers
    def _set(self, bars, i, o, c, vol_mult=1.0, wick=0.0015, base_qv=None):
        b = bars[i]
        h = max(o, c) * (1.0 + wick)
        lo = min(o, c) * (1.0 - wick)
        qv = (base_qv if base_qv is not None else b[6]) * vol_mult
        bars[i] = [b[0], o, h, lo, c, qv / ((o + c) / 2.0), qv]

    def _shape(self, coin, note, bars, dvol, vol24, rng):
        if not bars:
            return bars
        idx = dict((b[0], i) for i, b in enumerate(bars))
        last = len(bars) - 2              # the newest closed bar (the last one is still forming)
        base_qv = vol24 / 288.0
        if note == "orb_long":
            o = _ny_open(self.now - 50 * 60000)
            if o is not None and o in idx:
                i0 = idx[o]
                p = bars[i0 - 1][4]
                lo_, hi_ = p * 0.994, p * 1.006
                zig = [1.003, 0.996, 1.004, 0.995, 1.002, 0.999]
                prev = p
                for k in range(6):
                    c = p * zig[k]
                    self._set(bars, i0 + k, prev, c, 1.6, 0.0, base_qv)
                    bars[i0 + k][2] = max(bars[i0 + k][2], hi_ if k == 2 else prev)
                    bars[i0 + k][3] = min(bars[i0 + k][3], lo_ if k == 3 else prev)
                    prev = c
                for k in range(6, 9):
                    c = p * (1.0 + 0.001 * (k - 6))
                    self._set(bars, i0 + k, prev, c, 1.1, 0.001, base_qv)
                    prev = c
                # bar 9 (open + 45m) closes above the range high on four times the volume
                c = hi_ * 1.007
                self._set(bars, i0 + 9, prev, c, 4.0, 0.001, base_qv)
                prev = c
                for k in range(10, last - i0 + 2):
                    c = prev * (1.0 + 0.0006 * (1 if k % 3 else -1))
                    self._set(bars, i0 + k, prev, c, 1.2, 0.0008, base_qv)
                    prev = c
        elif note in ("breakout_long", "breakdown_short"):
            up = note == "breakout_long"
            n24 = 288
            start = last - n24 - 4
            p = bars[start][4]
            level = p * (1.02 if up else 0.98)
            prev = p
            for k in range(start + 1, last - 2):
                wob = 0.006 * math.sin(k / 9.0) + 0.003 * math.sin(k / 3.1)
                c = level * ((0.985 + wob) if up else (1.015 + wob))
                self._set(bars, k, prev, c, 1.0, 0.0, base_qv)
                if up:
                    bars[k][2] = min(max(prev, c) * 1.001, level)
                    bars[k][3] = min(prev, c) * 0.999
                else:
                    bars[k][3] = max(min(prev, c) * 0.999, level)
                    bars[k][2] = max(prev, c) * 1.001
                prev = c
            # the last closed 15-minute bar (three 5-minute bars) breaks the level on heavy volume
            tgt = [1.002, 1.005, 1.009] if up else [0.998, 0.995, 0.991]
            for j, k in enumerate(range(last - 2, last + 1)):
                c = level * tgt[j]
                self._set(bars, k, prev, c, 3.5, 0.0008, base_qv)
                prev = c
            self._set(bars, last + 1, prev, prev * (1.0005 if up else 0.9995), 1.0, 0.0005, base_qv)
        elif note == "reversal_long":
            # a 14-hour slide, then three strong hours (the hourly RSI turns up from deep oversold)
            first_up = idx.get(((self.now // 3600000) - 3) * 3600000)
            if first_up is not None:
                start = first_up - 14 * 12
                prev = bars[start][4]
                for k in range(start + 1, first_up):
                    c = prev * (1.0 - 0.0007)
                    self._set(bars, k, prev, c, 1.3, 0.0004, base_qv)
                    prev = c
                for k in range(first_up, last + 1):
                    c = prev * 1.0021
                    self._set(bars, k, prev, c, 2.2, 0.0006, base_qv)
                    prev = c
                self._set(bars, last + 1, prev, prev, 1.0, 0.0003, base_qv)
        elif note == "trend_long":
            # pull back to the 15m 20-period average for two hours, then reclaim it
            prev = bars[last - 27][4]
            for k in range(last - 26, last - 2):
                c = prev * 0.99935
                self._set(bars, k, prev, c, 0.9, 0.0004, base_qv)
                prev = c
            for k in range(last - 2, last + 1):
                c = prev * 1.0028
                self._set(bars, k, prev, c, 1.8, 0.0004, base_qv)
                prev = c
        elif note == "volume_spike":
            prev = bars[last - 3][4]
            for j, k in enumerate(range(last - 2, last + 1)):
                c = prev * (1.0 + (0.004, 0.012, 0.010)[j])
                self._set(bars, k, prev, c, 7.0, 0.002, base_qv)
                prev = c
        elif note in ("crowded_long", "crowded_short"):
            # price goes nowhere (long) or grinds up (short) while open interest piles in
            prev = bars[last - 288][4]
            for k in range(last - 287, last + 2):
                drift = 0.0 if note == "crowded_long" else 0.0001
                c = prev * (1.0 + drift + 0.0012 * math.sin(k / 5.0))
                self._set(bars, k, prev, c, 1.0, 0.0006, base_qv)
                prev = c
        return bars

    # -- derived series
    def funding_rate(self, coin, t):
        """The 8-hour funding rate settled at time t."""
        note = [c for c in COINS if c[0] == coin][0][6]
        if note == "crowded_long" and t >= self.now - DAY:
            return 0.00095
        if note == "crowded_short" and t >= self.now - DAY:
            return -0.00062
        bars = self.path(coin)
        i = min(len(bars) - 1, max(0, (t - bars[0][0]) // STEP))
        j = max(0, i - 288)
        ret = bars[i][4] / bars[j][4] - 1.0
        rng = random.Random(t // 1000 + len(coin))
        r = 0.0001 + 0.004 * ret + rng.gauss(0.0, 0.00004)
        return max(-0.0025, min(0.0025, round(r, 8)))

    def oi_usd(self, coin, t):
        note = [c for c in COINS if c[0] == coin][0][6]
        spec = [c for c in COINS if c[0] == coin][0]
        base = spec[4] * 0.45
        bars = self.path(coin)
        i = min(len(bars) - 1, max(0, (t - bars[0][0]) // STEP))
        j = max(0, i - 288 * 3)
        trend = bars[i][4] / bars[j][4] - 1.0
        wave = 0.06 * math.sin(t / (DAY * 2.3)) + 0.03 * math.sin(t / (DAY * 0.7))
        v = base * (1.0 + 0.8 * trend + wave)
        if note in ("crowded_long", "crowded_short"):
            age = (self.now - t) / float(DAY)
            if age < 1.0:
                v *= 1.0 + (0.32 if note == "crowded_long" else 0.24) * (1.0 - age)
        return max(v, base * 0.2)

    def long_share(self, coin, t):
        note = [c for c in COINS if c[0] == coin][0][6]
        if note == "crowded_long" and t >= self.now - DAY:
            return 0.74
        if note == "crowded_short" and t >= self.now - DAY:
            return 0.36
        rng = random.Random(t // 3600000 + len(coin) * 31)
        return max(0.3, min(0.75, 0.55 + 0.05 * math.sin(t / (DAY * 1.7)) + rng.gauss(0.0, 0.015)))


def market():
    now = _now()
    with _LOCK:
        m = _CACHE.get(now)
        if m is None:
            m = Market(now)
            _CACHE.clear()
            _CACHE[now] = m
    return m


# ---------------------------------------------------------------- symbols per venue

def binance_symbol(coin):
    return ("1000%sUSDT" % coin) if coin == "BONK" else coin + "USDT"


def bybit_symbol(coin):
    return ("1000%sUSDT" % coin) if coin == "BONK" else coin + "USDT"


def hl_symbol(coin):
    return ("k%s" % coin) if coin == "BONK" else coin


def coin_of(symbol, venue):
    for spec in COINS:
        c = spec[0]
        if venue == "binance" and binance_symbol(c) == symbol:
            return c
        if venue == "bybit" and bybit_symbol(c) == symbol:
            return c
        if venue == "hyperliquid" and hl_symbol(c) == symbol:
            return c
    return None


def mult(coin):
    return 1000.0 if coin == "BONK" else 1.0


def tick_for(p):
    if p >= 10000:
        return 0.1
    if p >= 1000:
        return 0.01
    if p >= 10:
        return 0.001
    if p >= 1:
        return 0.0001
    if p >= 0.01:
        return 0.00001
    return 0.0000001


def _fmt(x):
    return ("%.10f" % x).rstrip("0").rstrip(".") if x == x else "0"


# ---------------------------------------------------------------- shared bar helpers

def bars_for(coin, interval_ms, start, end):
    """Aggregate the 5-minute path into interval bars with open time in [start, end]."""
    m = market()
    raw = m.path(coin)
    if not raw:
        return []
    out = []
    first = (start // interval_ms) * interval_ms
    if first < start:
        first += interval_ms
    t = first
    i_base = raw[0][0]
    while t <= end and t <= m.cur_open:
        i0 = (t - i_base) // STEP
        i1 = (t + interval_ms - i_base) // STEP
        chunk = raw[max(0, i0):max(0, min(len(raw), i1))]
        if chunk and chunk[0][0] >= t:
            o = chunk[0][1]
            c = chunk[-1][4]
            h = max(b[2] for b in chunk)
            lo = min(b[3] for b in chunk)
            v = sum(b[5] for b in chunk)
            qv = sum(b[6] for b in chunk)
            out.append([t, o, h, lo, c, v, qv])
        t += interval_ms
    return out


def last_price(coin):
    return market().path(coin)[-1][4]


def day_stats(coin):
    m = market()
    raw = m.path(coin)
    cut = m.now - DAY
    rows = [b for b in raw if b[0] >= cut]
    if not rows:
        rows = raw[-1:]
    o = rows[0][1]
    c = rows[-1][4]
    return {"open": o, "close": c, "high": max(b[2] for b in rows), "low": min(b[3] for b in rows),
            "vol": sum(b[5] for b in rows), "qv": sum(b[6] for b in rows)}


INTERVALS = {"1m": 60000, "5m": 300000, "15m": 900000, "30m": 1800000, "1h": 3600000, "2h": 7200000,
             "4h": 14400000, "6h": 21600000, "12h": 43200000, "1d": 86400000}
BYBIT_CODES = {"5": "5m", "15": "15m", "30": "30m", "60": "1h", "120": "2h", "240": "4h", "360": "6h",
               "720": "12h", "D": "1d"}
BYBIT_PERIODS = {"5min": "5m", "15min": "15m", "30min": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}


def funding_times(coin, start, end, hours):
    step = int(hours * 3600000)
    t = ((start + step - 1) // step) * step
    out = []
    while t <= min(end, market().now):
        out.append(t)
        t += step
    return out


# ---------------------------------------------------------------- Binance

def binance(path, q):
    m = market()
    now = m.now
    if path == "/fapi/v1/time":
        return {"serverTime": now}
    if path == "/fapi/v1/exchangeInfo":
        syms = []
        for spec in COINS:
            coin = spec[0]
            p = last_price(coin)
            tk = tick_for(p * mult(coin))
            syms.append({
                "symbol": binance_symbol(coin), "pair": binance_symbol(coin), "contractType": "PERPETUAL",
                "deliveryDate": 4133404800000, "onboardDate": now - spec[5] * DAY, "status": "TRADING",
                "baseAsset": ("1000" + coin) if coin == "BONK" else coin, "quoteAsset": "USDT", "marginAsset": "USDT",
                "underlyingType": "COIN", "underlyingSubType": ["Meme"] if coin in ("DOGE", "BONK", "WIF") else [],
                "pricePrecision": 7, "quantityPrecision": 0,
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": _fmt(tk), "minPrice": _fmt(tk), "maxPrice": "1000000"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001" if p > 100 else "1", "minQty": "0.001" if p > 100 else "1", "maxQty": "1000000"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"}],
            })
        syms.append({"symbol": "BTCUSDT_261225", "contractType": "CURRENT_QUARTER", "status": "TRADING", "baseAsset": "BTC",
                     "quoteAsset": "USDT", "marginAsset": "USDT", "underlyingType": "COIN", "filters": []})
        syms.append({"symbol": "BTCDOMUSDT", "contractType": "PERPETUAL", "status": "TRADING", "baseAsset": "BTCDOM",
                     "quoteAsset": "USDT", "marginAsset": "USDT", "underlyingType": "INDEX", "filters": []})
        syms.append({"symbol": "USDCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "baseAsset": "USDC",
                     "quoteAsset": "USDT", "marginAsset": "USDT", "underlyingType": "COIN", "onboardDate": now - 900 * DAY,
                     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"}]})
        return {"timezone": "UTC", "serverTime": now, "symbols": syms}
    if path == "/fapi/v1/fundingInfo":
        return [{"symbol": binance_symbol(c), "adjustedFundingRateCap": "0.02", "adjustedFundingRateFloor": "-0.02",
                 "fundingIntervalHours": 4, "disclaimer": False} for c in sorted(FOUR_HOUR_FUNDING)]
    if path == "/fapi/v1/ticker/24hr":
        out = []
        for spec in COINS:
            c = spec[0]
            d = day_stats(c)
            k = mult(c)
            out.append({"symbol": binance_symbol(c), "priceChange": _fmt((d["close"] - d["open"]) * k),
                        "priceChangePercent": "%.3f" % ((d["close"] / d["open"] - 1) * 100),
                        "lastPrice": _fmt(d["close"] * k), "openPrice": _fmt(d["open"] * k),
                        "highPrice": _fmt(d["high"] * k), "lowPrice": _fmt(d["low"] * k),
                        "volume": _fmt(d["vol"] / k), "quoteVolume": _fmt(d["qv"]), "openTime": now - DAY, "closeTime": now, "count": 100000})
        out.append({"symbol": "USDCUSDT", "lastPrice": "1.0001", "priceChangePercent": "0.010", "openPrice": "1.0000",
                    "highPrice": "1.0003", "lowPrice": "0.9998", "quoteVolume": "450000000", "closeTime": now})
        return out
    if path == "/fapi/v1/premiumIndex":
        out = []
        for spec in COINS:
            c = spec[0]
            hours = 4 if c in FOUR_HOUR_FUNDING else 8
            step = hours * 3600000
            p = last_price(c) * mult(c)
            out.append({"symbol": binance_symbol(c), "markPrice": _fmt(p), "indexPrice": _fmt(p * 0.9998),
                        "estimatedSettlePrice": _fmt(p), "lastFundingRate": "%.8f" % m.funding_rate(c, now),
                        "interestRate": "0.00010000", "nextFundingTime": ((now // step) + 1) * step, "time": now})
        return out
    if path == "/fapi/v1/ticker/bookTicker":
        out = []
        for spec in COINS:
            c = spec[0]
            p = last_price(c) * mult(c)
            tk = tick_for(p)
            out.append({"symbol": binance_symbol(c), "bidPrice": _fmt(p - tk), "bidQty": "10", "askPrice": _fmt(p + tk), "askQty": "12", "time": now})
        return out
    if path == "/fapi/v1/klines":
        coin = coin_of(q.get("symbol"), "binance")
        if coin is None:
            return 400, {"code": -1121, "msg": "Invalid symbol."}
        iv = INTERVALS.get(q.get("interval"))
        if not iv:
            return 400, {"code": -1120, "msg": "Invalid interval."}
        lim = int(q.get("limit", 500))
        if lim > 1500:
            return 400, {"code": -1130, "msg": "Data sent for parameter 'limit' is not valid."}
        start = int(q.get("startTime", m.cur_open - iv * (lim - 1)))
        end = int(q.get("endTime", now))
        rows = bars_for(coin, iv, start, end)[:lim]
        k = mult(coin)
        out = []
        for b in rows:
            r = [b[0], _fmt(b[1] * k), _fmt(b[2] * k), _fmt(b[3] * k), _fmt(b[4] * k), _fmt(b[5] / k), b[0] + iv - 1,
                 _fmt(b[6]), 1000, _fmt(b[5] / k / 2), _fmt(b[6] / 2), "0"]
            out.append(r)
        if "garbage" in _modes() and coin == "ADA" and out:
            out[len(out) // 2] = [out[len(out) // 2][0], "oops"]
        return out
    if path == "/fapi/v1/fundingRate":
        coin = coin_of(q.get("symbol"), "binance")
        hours = 4 if coin in FOUR_HOUR_FUNDING else 8
        start = int(q.get("startTime", now - 30 * DAY))
        end = int(q.get("endTime", now))
        lim = int(q.get("limit", 100))
        ts = funding_times(coin, start, end, hours)[:lim]
        return [{"symbol": q.get("symbol"), "fundingTime": t, "fundingRate": "%.8f" % m.funding_rate(coin, t),
                 "markPrice": _fmt(last_price(coin))} for t in ts]
    if path == "/fapi/v1/openInterest":
        coin = coin_of(q.get("symbol"), "binance")
        p = last_price(coin) * mult(coin)
        return {"symbol": q.get("symbol"), "openInterest": "%.3f" % (m.oi_usd(coin, now) / p), "time": now}
    if path in ("/futures/data/openInterestHist", "/futures/data/globalLongShortAccountRatio",
                "/futures/data/topLongShortPositionRatio", "/futures/data/takerlongshortRatio"):
        coin = coin_of(q.get("symbol"), "binance")
        iv = INTERVALS[q.get("period")]
        lim = int(q.get("limit", 30))
        end = int(q.get("endTime", now))
        start = int(q.get("startTime", end - iv * (lim - 1)))
        if now - start > 30 * DAY:
            return 400, {"code": -1130, "msg": "parameter 'startTime' is invalid."}
        ts = []
        t = ((start + iv - 1) // iv) * iv
        while t <= min(end, now) and len(ts) < lim:
            ts.append(t)
            t += iv
        out = []
        for t in ts:
            if path.endswith("openInterestHist"):
                p = bars_for(coin, STEP, t - STEP, t)
                px = (p[-1][4] if p else last_price(coin)) * mult(coin)
                usd = m.oi_usd(coin, t)
                out.append({"symbol": q.get("symbol"), "sumOpenInterest": "%.3f" % (usd / px),
                            "sumOpenInterestValue": "%.2f" % usd, "timestamp": t})
            elif path.endswith("takerlongshortRatio"):
                ls = m.long_share(coin, t)
                out.append({"buySellRatio": "%.4f" % (ls / (1 - ls) * 0.8), "buyVol": "100", "sellVol": "100", "timestamp": t})
            else:
                ls = m.long_share(coin, t)
                out.append({"symbol": q.get("symbol"), "longShortRatio": "%.4f" % (ls / (1 - ls)),
                            "longAccount": "%.4f" % ls, "shortAccount": "%.4f" % (1 - ls), "timestamp": t})
        return out
    if path == "/fapi/v1/depth":
        coin = coin_of(q.get("symbol"), "binance")
        p = last_price(coin) * mult(coin)
        lim = int(q.get("limit", 100))
        return {"lastUpdateId": 1, "E": now, "T": now, "bids": book(p, lim, -1), "asks": book(p, lim, 1)}
    return 404, {"code": -5000, "msg": "Path %s, Method GET is invalid" % path}


def book(p, n, side):
    tk = tick_for(p)
    out = []
    for i in range(n):
        px = p + side * tk * (1 + i * 3)
        out.append([_fmt(px), _fmt(round(1500.0 / p * (1 + i * 0.4), 6))])
    return out


# ---------------------------------------------------------------- Bybit

def bybit_ok(result):
    return {"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {}, "time": market().now}


def bybit(path, q):
    m = market()
    now = m.now
    if path == "/v5/market/time":
        return bybit_ok({"timeSecond": str(now // 1000), "timeNano": str(now * 1000000)})
    if path == "/v5/market/instruments-info":
        rows = []
        for spec in COINS:
            c = spec[0]
            p = last_price(c) * mult(c)
            rows.append({"symbol": bybit_symbol(c), "contractType": "LinearPerpetual", "status": "Trading",
                         "baseCoin": ("1000" + c) if c == "BONK" else c, "quoteCoin": "USDT", "settleCoin": "USDT",
                         "launchTime": str(now - spec[5] * DAY), "priceScale": "6",
                         "leverageFilter": {"minLeverage": "1", "maxLeverage": "100.00" if c == "BTC" else "50.00", "leverageStep": "0.01"},
                         "priceFilter": {"minPrice": _fmt(tick_for(p)), "maxPrice": "1999999", "tickSize": _fmt(tick_for(p))},
                         "lotSizeFilter": {"maxOrderQty": "1000000", "minOrderQty": "0.001" if p > 100 else "1",
                                           "qtyStep": "0.001" if p > 100 else "1", "minNotionalValue": "5"},
                         "fundingInterval": 240 if c in FOUR_HOUR_FUNDING else 480, "isPreListing": False})
        rows.append({"symbol": "BTCPERP", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "BTC",
                     "quoteCoin": "USDC", "settleCoin": "USDC"})
        return bybit_ok({"category": "linear", "list": rows, "nextPageCursor": ""})
    if path == "/v5/market/tickers":
        rows = []
        for spec in COINS:
            c = spec[0]
            d = day_stats(c)
            k = mult(c)
            hours = 4 if c in FOUR_HOUR_FUNDING else 8
            step = hours * 3600000
            tk = tick_for(d["close"] * k)
            rows.append({"symbol": bybit_symbol(c), "lastPrice": _fmt(d["close"] * k), "indexPrice": _fmt(d["close"] * k * 0.9998),
                         "markPrice": _fmt(d["close"] * k), "prevPrice24h": _fmt(d["open"] * k),
                         "price24hPcnt": "%.6f" % (d["close"] / d["open"] - 1), "highPrice24h": _fmt(d["high"] * k),
                         "lowPrice24h": _fmt(d["low"] * k), "turnover24h": _fmt(d["qv"]), "volume24h": _fmt(d["vol"] / k),
                         "fundingRate": "%.8f" % m.funding_rate(c, now), "nextFundingTime": str(((now // step) + 1) * step),
                         "openInterest": "%.3f" % (m.oi_usd(c, now) / (d["close"] * k)),
                         "openInterestValue": "%.2f" % m.oi_usd(c, now),
                         "bid1Price": _fmt(d["close"] * k - tk), "bid1Size": "10", "ask1Price": _fmt(d["close"] * k + tk), "ask1Size": "10"})
        return bybit_ok({"category": "linear", "list": rows})
    if path == "/v5/market/kline":
        coin = coin_of(q.get("symbol"), "bybit")
        if coin is None:
            return 200, {"retCode": 10001, "retMsg": "Not supported symbols", "result": {}, "retExtInfo": {}, "time": now}
        iv = INTERVALS[BYBIT_CODES[q.get("interval")]]
        lim = int(q.get("limit", 200))
        if lim > 1000:
            return 200, {"retCode": 10001, "retMsg": "params error: limit", "result": {}, "time": now}
        end = int(q.get("end", now))
        start = int(q.get("start", end - iv * (lim - 1)))
        rows = bars_for(coin, iv, start, end)
        rows = rows[-lim:]
        k = mult(coin)
        lst = [[str(b[0]), _fmt(b[1] * k), _fmt(b[2] * k), _fmt(b[3] * k), _fmt(b[4] * k), _fmt(b[5] / k), _fmt(b[6])] for b in rows]
        lst.reverse()
        return bybit_ok({"symbol": q.get("symbol"), "category": "linear", "list": lst})
    if path == "/v5/market/funding/history":
        coin = coin_of(q.get("symbol"), "bybit")
        hours = 4 if coin in FOUR_HOUR_FUNDING else 8
        end = int(q.get("endTime", now))
        start = int(q.get("startTime", end - 30 * DAY))
        lim = int(q.get("limit", 200))
        ts = funding_times(coin, start, end, hours)[-lim:]
        lst = [{"symbol": q.get("symbol"), "fundingRate": "%.8f" % m.funding_rate(coin, t), "fundingRateTimestamp": str(t)} for t in ts]
        lst.reverse()
        return bybit_ok({"category": "linear", "list": lst})
    if path == "/v5/market/open-interest":
        coin = coin_of(q.get("symbol"), "bybit")
        iv = INTERVALS[BYBIT_PERIODS[q.get("intervalTime")]]
        lim = int(q.get("limit", 50))
        end = int(q.get("endTime", now))
        start = int(q.get("startTime", end - iv * (lim - 1)))
        lst = []
        t = ((start + iv - 1) // iv) * iv
        px = last_price(coin) * mult(coin)
        while t <= min(end, now):
            lst.append({"openInterest": "%.3f" % (m.oi_usd(coin, t) / px), "timestamp": str(t)})
            t += iv
        lst = lst[-lim:]
        lst.reverse()
        return bybit_ok({"symbol": q.get("symbol"), "category": "linear", "list": lst, "nextPageCursor": ""})
    if path == "/v5/market/account-ratio":
        coin = coin_of(q.get("symbol"), "bybit")
        iv = INTERVALS[BYBIT_PERIODS[q.get("period")]]
        lim = int(q.get("limit", 50))
        end = int(q.get("endTime", now))
        start = int(q.get("startTime", end - iv * (lim - 1)))
        lst = []
        t = ((start + iv - 1) // iv) * iv
        while t <= min(end, now):
            ls = m.long_share(coin, t)
            lst.append({"symbol": q.get("symbol"), "buyRatio": "%.4f" % ls, "sellRatio": "%.4f" % (1 - ls), "timestamp": str(t)})
            t += iv
        lst = lst[-lim:]
        lst.reverse()
        return bybit_ok({"list": lst, "nextPageCursor": ""})
    if path == "/v5/market/orderbook":
        coin = coin_of(q.get("symbol"), "bybit")
        p = last_price(coin) * mult(coin)
        lim = int(q.get("limit", 25))
        return bybit_ok({"s": q.get("symbol"), "b": book(p, lim, -1), "a": book(p, lim, 1), "ts": now, "u": 1})
    if path == "/v5/market/risk-limit":
        sym = q.get("symbol")
        tiers = [("200000", "0.005", "0.01", "100.00", "0"), ("1000000", "0.01", "0.02", "50.00", "1000"),
                 ("5000000", "0.02", "0.04", "25.00", "11000")]
        lst = [{"id": i + 1, "symbol": sym, "riskLimitValue": a, "maintenanceMargin": b, "initialMargin": c,
                "isLowestRisk": 1 if i == 0 else 0, "maxLeverage": d, "mmDeduction": e} for i, (a, b, c, d, e) in enumerate(tiers)]
        return bybit_ok({"category": "linear", "list": lst})
    return 404, {"retCode": 10404, "retMsg": "not found"}


# ---------------------------------------------------------------- Hyperliquid

def hyperliquid(body):
    m = market()
    now = m.now
    typ = body.get("type")
    if typ in ("meta", "metaAndAssetCtxs"):
        uni = []
        ctx = []
        for spec in COINS:
            c = spec[0]
            if spec[6] in ("thin",):
                continue
            d = day_stats(c)
            k = mult(c)
            uni.append({"szDecimals": 1 if d["close"] * k < 10 else 3, "name": hl_symbol(c),
                        "maxLeverage": 40 if c == "BTC" else 20 if spec[3] < 5e8 else 25, "marginTableId": 56 if c == "BTC" else 20})
            ctx.append({"funding": "%.10f" % (m.funding_rate(c, now) / 8.0), "openInterest": "%.4f" % (m.oi_usd(c, now) * 0.3 / (d["close"] * k)),
                        "prevDayPx": _fmt(d["open"] * k), "dayNtlVlm": _fmt(d["qv"] * 0.5), "premium": "0.0001",
                        "oraclePx": _fmt(d["close"] * k * 0.9998), "markPx": _fmt(d["close"] * k), "midPx": _fmt(d["close"] * k),
                        "impactPxs": [_fmt(d["close"] * k * 0.9999), _fmt(d["close"] * k * 1.0001)], "dayBaseVlm": _fmt(d["vol"] * 0.5 / k)})
        uni.append({"szDecimals": 2, "name": "OLDX", "maxLeverage": 3, "isDelisted": True, "marginTableId": 3})
        ctx.append({"funding": "0", "openInterest": "0", "prevDayPx": "1", "dayNtlVlm": "0", "premium": None, "oraclePx": "1",
                    "markPx": "1", "midPx": None, "impactPxs": None, "dayBaseVlm": "0"})
        meta = {"universe": uni, "marginTables": [[56, {"description": "tiered 40x", "marginTiers": [
            {"lowerBound": "0.0", "maxLeverage": 40}, {"lowerBound": "150000000.0", "maxLeverage": 20}]}]]}
        return meta if typ == "meta" else [meta, ctx]
    if typ == "candleSnapshot":
        req = body.get("req") or {}
        coin = coin_of(req.get("coin"), "hyperliquid")
        if coin is None:
            return 500, "null"
        iv = INTERVALS[req.get("interval")]
        rows = bars_for(coin, iv, int(req.get("startTime")), int(req.get("endTime", now)))[-5000:]
        k = mult(coin)
        return [{"t": b[0], "T": b[0] + iv - 1, "s": req.get("coin"), "i": req.get("interval"), "o": _fmt(b[1] * k),
                 "c": _fmt(b[4] * k), "h": _fmt(b[2] * k), "l": _fmt(b[3] * k), "v": _fmt(b[5] * 0.5 / k), "n": 100} for b in rows]
    if typ == "fundingHistory":
        coin = coin_of(body.get("coin"), "hyperliquid")
        ts = funding_times(coin, int(body.get("startTime")), int(body.get("endTime", now)), 1)[:500]
        return [{"coin": body.get("coin"), "fundingRate": "%.10f" % (m.funding_rate(coin, t) / 8.0), "premium": "0.0001", "time": t} for t in ts]
    if typ == "l2Book":
        coin = coin_of(body.get("coin"), "hyperliquid")
        p = last_price(coin) * mult(coin)
        bids = [{"px": a, "sz": b, "n": 3} for a, b in book(p, 20, -1)]
        asks = [{"px": a, "sz": b, "n": 3} for a, b in book(p, 20, 1)]
        return {"coin": body.get("coin"), "time": now, "levels": [bids, asks]}
    if typ == "predictedFundings":
        out = []
        for spec in COINS:
            c = spec[0]
            r8 = m.funding_rate(c, now)
            nxt_h = ((now // 3600000) + 1) * 3600000
            nxt_8 = ((now // 28800000) + 1) * 28800000
            venues = [["BinPerp", {"fundingRate": "%.8f" % (r8 * 1.05), "nextFundingTime": nxt_8, "fundingIntervalHours": 8}],
                      ["HlPerp", {"fundingRate": "%.10f" % (r8 / 8.0), "nextFundingTime": nxt_h, "fundingIntervalHours": 1}],
                      ["BybitPerp", {"fundingRate": "%.8f" % (r8 * 0.95), "nextFundingTime": nxt_8, "fundingIntervalHours": 8}]]
            if c == "FUNDX":
                venues[1][1]["fundingRate"] = "0.0000300000"   # cheaper to hold the long here: a spread to spot
            out.append([hl_symbol(c), venues])
        return out
    return 422, {"error": "unknown type %s" % typ}


# ---------------------------------------------------------------- Fear & Greed

def fear_greed(q):
    now = market().now
    day0 = (now // DAY) * DAY
    vals = [64, 61, 58, 55, 52, 50, 49, 52, 47, 44, 40, 38, 41, 45, 50, 55, 57, 60, 58, 56, 54, 52, 50, 48, 46, 48, 51, 53, 55, 57, 59]
    labels = lambda v: "Extreme Fear" if v <= 25 else "Fear" if v < 46 else "Neutral" if v <= 54 else "Greed" if v < 75 else "Extreme Greed"
    n = int(q.get("limit", 1))
    data = [{"value": str(vals[i % len(vals)]), "value_classification": labels(vals[i % len(vals)]),
             "timestamp": str((day0 - i * DAY) // 1000)} for i in range(n)]
    data[0]["time_until_update"] = "3600"
    return {"name": "Fear and Greed Index", "data": data, "metadata": {"error": None}}


# ---------------------------------------------------------------- the transport cryptocore calls

def handle(method, url, data, headers):
    modes = _modes()
    u = urllib.parse.urlparse(url)
    host, path = u.netloc, u.path
    q = dict(urllib.parse.parse_qsl(u.query))
    key = (os.environ.get("FAKE_SESSION", ""), host, path, (data or b"")[:40])
    first = key not in _SEEN
    _SEEN.add(key)
    if "binance" in host and "binance_geoblock" in modes:
        return 451, {}, json.dumps({"code": 0, "msg": "Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms."})
    if "bybit" in host and "bybit_geoblock" in modes:
        return 403, {}, "<HTML><BODY>The Amazon CloudFront distribution is configured to block access from your country.</BODY></HTML>"
    if first and "flaky" in modes:
        return 503, {}, "Service Unavailable"
    if first and "ratelimit" in modes:
        return 429, {"Retry-After": "0"}, json.dumps({"code": -1003, "msg": "Too many requests"})
    if first and "timeout" in modes:
        raise socket.timeout("timed out")
    if host == "fapi.binance.com":
        res = binance(path, q)
    elif host == "api.bybit.com":
        res = bybit(path, q)
    elif host == "api.alternative.me":
        res = fear_greed(q)
    elif host == "api.hyperliquid.xyz" and path == "/info":
        res = hyperliquid(json.loads((data or b"{}").decode("utf-8")))
    else:
        return 404, {}, "not found"
    if isinstance(res, tuple):
        status, payload = res
        return status, {}, payload if isinstance(payload, str) else json.dumps(payload)
    return 200, {"Content-Type": "application/json"}, json.dumps(res)
