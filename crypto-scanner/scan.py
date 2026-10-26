#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-scanner: scan every liquid perpetual for setups.

    python3 scan.py universe                  the coins worth scanning -> crypto/universe.json
    python3 scan.py                           all seven setups -> crypto/watchlist.json
    python3 scan.py --setups orb --session ny opening-range breakouts after the US open
    python3 scan.py --symbols BTC,ETH,SOL     only these coins

A scan is a list of charts that match a rule. It is not a list of coins to buy.
"""

import argparse
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, os.pardir, "crypto-setup")):
    if os.path.isfile(os.path.join(_d, "cryptocore.py")):
        sys.path.insert(0, os.path.abspath(_d))
        break
else:
    sys.stderr.write("Problem: cryptocore.py not found. Install the crypto-setup skill next to this one.\n")
    sys.exit(2)
import cryptocore as cc  # noqa: E402

T, O, H, L, C, V, QV = cc.T, cc.O, cc.H, cc.L, cc.C, cc.V, cc.QV
M5 = 300000
ALL_SETUPS = ("orb", "breakout", "squeeze", "trend", "reversal", "volume", "rs")
TITLES = {
    "orb": "Opening-range breakout",
    "breakout": "Range breakout",
    "squeeze": "Squeeze (volatility compression)",
    "trend": "Trend pullback",
    "reversal": "RSI reversal",
    "volume": "Volume spike",
    "rs": "Relative strength vs BTC",
}
SESSION_NAMES = {"utc": "UTC day", "tokyo": "Tokyo", "london": "London", "ny": "New York"}


class Ctx(object):
    """One coin's data for the setups. Pass b15 to skip building it from b5 (the Time Machine does)."""

    def __init__(self, m, t, b5, b1h, b15=None):
        self.m, self.t, self.b5, self.b1h = m, t, b5, b1h
        self.b15 = b15 if b15 is not None else (cc.resample(b5, "15m") if b5 else [])
        a = cc.atr(self.b15, 14) if len(self.b15) > 15 else []
        self.atr15 = a[-1] if a else None
        last = self.b15[-1][C] if self.b15 else None
        self.atr15_pct = (self.atr15 / last) if (self.atr15 and last) else None


def px(m, p):
    """Round a level to the market's tick (or 6 significant figures on Hyperliquid)."""
    if p is None:
        return None
    if m.get("tick"):
        return cc.round_to_tick(p, m["tick"])
    if p == 0:
        return 0.0
    return round(p, max(0, 5 - int(math.floor(math.log10(abs(p))))))


def hit(x, setup, side, trigger, level, stop_ref, at_ms, rvol=None, status="new", metrics=None, note=""):
    m, t = x.m, x.t
    return {
        "symbol": m["symbol"], "coin": m["coin"], "multiplier": m["multiplier"],
        "setup": setup, "side": side, "status": status,
        "trigger": px(m, trigger), "level": px(m, level), "stop_ref": px(m, stop_ref),
        "price": t.get("last"), "at": at_ms, "at_utc": cc.fmt_utc(at_ms),
        "rvol": round(rvol, 2) if rvol else None,
        "atr15_pct": round(x.atr15_pct, 5) if x.atr15_pct else None,
        "volume_24h": t.get("quote_volume"), "change_24h": t.get("change"),
        "funding": t.get("funding"), "funding_hours": t.get("funding_hours"),
        "metrics": metrics or {}, "note": note,
    }


# ---------------------------------------------------------------- the setups

def setup_orb(x, a, sides):
    """First 5-minute close beyond the session's opening range, inside the entry window."""
    now = cc.now_ms()
    s_open = cc.last_session_open(a.session, now)
    or_end = s_open + a.or_minutes * 60000
    win_end = s_open + int(a.entry_hours * 3600000)
    bars = [b for b in x.b5 if b[T] >= s_open]
    or_bars = [b for b in bars if b[T] < or_end]
    if len(or_bars) < a.or_minutes // 5:
        return []
    hi = max(b[H] for b in or_bars)
    lo = min(b[L] for b in or_bars)
    mid = (hi + lo) / 2.0
    after = [b for b in bars if or_end <= b[T] < win_end]
    pre = [b[QV] for b in x.b5 if b[T] < s_open][-48:]
    base = cc.median(pre)
    out = []
    for side in sides:
        trig = None
        for b in after:
            if (side == "long" and b[C] > hi) or (side == "short" and b[C] < lo):
                trig = b
                break
        if trig is None:
            continue
        rvol = trig[QV] / base if base else None
        if rvol is not None and rvol < a.min_rvol:
            continue
        last = x.b5[-1]
        holding = last[C] > hi if side == "long" else last[C] < lo
        out.append(hit(x, "orb", side, trig[C], hi if side == "long" else lo, mid, trig[T] + M5, rvol,
                       "holding" if holding else "back inside",
                       {"session": a.session, "session_open": s_open, "or_minutes": a.or_minutes,
                        "or_high": px(x.m, hi), "or_low": px(x.m, lo), "or_pct": round((hi - lo) / mid, 5)},
                       "first 5m close %s the %d-minute range" % ("above" if side == "long" else "below", a.or_minutes)))
    return out


def setup_breakout(x, a, sides):
    """The last closed 15m bar closes beyond the high or low of the previous N hours, on volume."""
    b = x.b15
    n = int(a.lookback_hours * 4)
    if len(b) < max(n, 96) + 2:
        return []
    last, prev = b[-1], b[-2]
    window = b[-n - 1:-1]
    hi = max(w[H] for w in window)
    lo = min(w[L] for w in window)
    base = cc.median([w[QV] for w in b[-97:-1]])
    rvol = last[QV] / base if base else None
    if rvol is None or rvol < a.min_rvol:
        return []
    atr = x.atr15 or 0.0
    out = []
    if "long" in sides and last[C] > hi:
        fresh = prev[C] <= hi
        out.append(hit(x, "breakout", "long", last[C], hi, hi - atr, last[T] + 900000, rvol,
                       "fresh" if fresh else "extended",
                       {"lookback_hours": a.lookback_hours, "beyond_pct": round(last[C] / hi - 1.0, 5)},
                       "15m close above the %gh high" % a.lookback_hours))
    if "short" in sides and last[C] < lo:
        fresh = prev[C] >= lo
        out.append(hit(x, "breakout", "short", last[C], lo, lo + atr, last[T] + 900000, rvol,
                       "fresh" if fresh else "extended",
                       {"lookback_hours": a.lookback_hours, "beyond_pct": round(1.0 - last[C] / lo, 5)},
                       "15m close below the %gh low" % a.lookback_hours))
    return out


def setup_squeeze(x, a, sides):
    """1h Bollinger band width in the lowest few percent of the last 30 days: a coiled spring, no side yet."""
    c = cc.closes(x.b1h)
    if len(c) < 250:
        return []
    mid, up, lo, wd = cc.bollinger(c, 20, 2.0)
    hist = [w for w in wd[-720:] if w is not None]
    cur = wd[-1]
    if cur is None or len(hist) < 200:
        return []
    rank = cc.pct_rank(cur, hist)
    if rank > a.squeeze_pct / 100.0:
        return []
    return [hit(x, "squeeze", "both", up[-1], mid[-1], None, x.b1h[-1][T] + 3600000, None, "coiled",
                {"width_pct": round(cur, 5), "width_rank": round(rank, 4), "upper": px(x.m, up[-1]), "lower": px(x.m, lo[-1])},
                "band width in the lowest %d%% of 30 days; a break of either band picks the side" % a.squeeze_pct)]


def setup_trend(x, a, sides):
    """1h averages stacked in one direction, a 15m pullback to the 20 average, and a close back over it."""
    c1 = cc.closes(x.b1h)
    if len(c1) < 210 or len(x.b15) < 40:
        return []
    e20, e50, e200 = cc.ema(c1, 20)[-1], cc.ema(c1, 50)[-1], cc.ema(c1, 200)[-1]
    b = x.b15
    c15 = cc.closes(b)
    e = cc.ema(c15, 20)
    last = b[-1]
    recent = range(len(b) - 7, len(b) - 1)
    out = []
    if "long" in sides and e20 > e50 > e200 and b[-1][C] > e50:
        touched = any(b[i][L] <= e[i] for i in recent)
        top = max(w[H] for w in b[-24:])
        low6 = min(w[L] for w in b[-7:])
        depth = 1.0 - low6 / top
        if touched and last[C] > e[-1] and last[C] > last[O] and depth >= a.min_pullback / 100.0:
            out.append(hit(x, "trend", "long", last[C], e[-1], low6, last[T] + 900000, None, "reclaimed",
                           {"ema20_1h": px(x.m, e20), "ema50_1h": px(x.m, e50), "ema200_1h": px(x.m, e200),
                            "pullback_pct": round(depth, 5)},
                           "uptrend on 1h; 15m pulled back %.1f%% and closed back over its 20 average" % (depth * 100)))
    if "short" in sides and e20 < e50 < e200 and b[-1][C] < e50:
        touched = any(b[i][H] >= e[i] for i in recent)
        bottom = min(w[L] for w in b[-24:])
        high6 = max(w[H] for w in b[-7:])
        depth = high6 / bottom - 1.0
        if touched and last[C] < e[-1] and last[C] < last[O] and depth >= a.min_pullback / 100.0:
            out.append(hit(x, "trend", "short", last[C], e[-1], high6, last[T] + 900000, None, "rejected",
                           {"ema20_1h": px(x.m, e20), "ema50_1h": px(x.m, e50), "ema200_1h": px(x.m, e200),
                            "pullback_pct": round(depth, 5)},
                           "downtrend on 1h; 15m bounced %.1f%% and closed back under its 20 average" % (depth * 100)))
    return out


def setup_reversal(x, a, sides):
    """1h RSI was stretched in the last four hours and the last hour turned back."""
    c = cc.closes(x.b1h)
    if len(c) < 40:
        return []
    r = cc.rsi(c, 14)
    if any(v is None for v in r[-5:]):
        return []
    prior = r[-5:-1]
    out = []
    move24 = c[-1] / c[-25] - 1.0
    if "long" in sides and min(prior) <= a.rsi_low and r[-1] >= a.rsi_low + 5 and c[-1] > c[-2]:
        stop = min(b[L] for b in x.b1h[-6:])
        out.append(hit(x, "reversal", "long", c[-1], None, stop, x.b1h[-1][T] + 3600000, None, "turning up",
                       {"rsi_min": round(min(prior), 1), "rsi_now": round(r[-1], 1), "move_24h": round(move24, 5)},
                       "1h RSI fell to %.0f and is back to %.0f" % (min(prior), r[-1])))
    if "short" in sides and max(prior) >= a.rsi_high and r[-1] <= a.rsi_high - 5 and c[-1] < c[-2]:
        stop = max(b[H] for b in x.b1h[-6:])
        out.append(hit(x, "reversal", "short", c[-1], None, stop, x.b1h[-1][T] + 3600000, None, "turning down",
                       {"rsi_max": round(max(prior), 1), "rsi_now": round(r[-1], 1), "move_24h": round(move24, 5)},
                       "1h RSI rose to %.0f and is back to %.0f" % (max(prior), r[-1])))
    return out


def setup_volume(x, a, sides):
    """The last closed 15m bar traded several times its usual volume and moved at least one ATR."""
    b = x.b15
    if len(b) < 100:
        return []
    last = b[-1]
    base = cc.median([w[QV] for w in b[-97:-1]])
    if not base:
        return []
    rvol = last[QV] / base
    at = cc.atr(b[:-1], 14)[-1]
    move = last[C] - last[O]
    if rvol < a.spike_rvol or not at or abs(move) < at:
        return []
    side = "long" if move > 0 else "short"
    if side not in sides:
        return []
    return [hit(x, "volume", side, last[C], last[O], last[L] if side == "long" else last[H], last[T] + 900000, rvol,
                "spike", {"move_pct": round(move / last[O], 5), "move_atr": round(abs(move) / at, 2)},
                "%.1fx the usual 15m volume on a %.1f%% move" % (rvol, abs(move) / last[O] * 100))]


def setup_rs(ctxs, btc, a, sides):
    """24h return against what BTC's move predicts for the coin (beta from 7 days of hourly returns)."""
    if not btc or len(btc) < 170:
        return []
    bc = cc.closes(btc)
    br = cc.log_returns(bc[-169:])
    b24 = bc[-1] / bc[-25] - 1.0
    rows = []
    for x in ctxs:
        if x.m["coin"] == "BTC" or len(x.b1h) < 170:
            continue
        c = cc.closes(x.b1h)
        bt = cc.beta(cc.log_returns(c[-169:]), br)
        if bt is None:
            continue
        r24 = c[-1] / c[-25] - 1.0
        rows.append((r24 - bt * b24, r24, bt, x))
    if len(rows) < 5:
        return []
    rows.sort(key=lambda r: r[0])
    k = max(1, int(round(len(rows) * a.rs_decile / 100.0)))
    out = []
    picks = []
    if "long" in sides:
        picks += [("long", r) for r in rows[-k:][::-1] if r[0] >= a.rs_min / 100.0]
    if "short" in sides:
        picks += [("short", r) for r in rows[:k] if r[0] <= -a.rs_min / 100.0]
    for side, (ex, r24, bt, x) in picks:
        out.append(hit(x, "rs", side, x.b1h[-1][C], None, None, x.b1h[-1][T] + 3600000, None,
                       "leader" if side == "long" else "laggard",
                       {"excess_24h": round(ex, 5), "move_24h": round(r24, 5), "btc_24h": round(b24, 5), "beta": round(bt, 2)},
                       "%+.1f%% in 24h vs %+.1f%% expected from BTC (beta %.2f)" % (r24 * 100, bt * b24 * 100, bt)))
    return out


PARAM_KEYS = ("session", "or_minutes", "entry_hours", "lookback_hours", "min_rvol", "spike_rvol", "squeeze_pct",
              "min_pullback", "rsi_low", "rsi_high", "rs_decile", "rs_min")


def params_of(a):
    """The thresholds a scan used, so a later re-run of the same rule matches it exactly."""
    return dict((k, getattr(a, k)) for k in PARAM_KEYS if hasattr(a, k))


def args_from(params=None):
    """Scanner arguments with the defaults, overlaid with saved params."""
    a = build_parser().parse_args([])
    a.session = cc.load_config().get("session") or "utc"
    for k, v in (params or {}).items():
        if k in PARAM_KEYS and v is not None:
            setattr(a, k, v)
    return a


SETUP_FUNCS = {"orb": setup_orb, "breakout": setup_breakout, "squeeze": setup_squeeze, "trend": setup_trend,
               "reversal": setup_reversal, "volume": setup_volume}


# ---------------------------------------------------------------- running a scan

def parse_setups(text):
    if not text or text == "all":
        return list(ALL_SETUPS)
    out = []
    for s in text.split(","):
        s = s.strip().lower()
        if s not in ALL_SETUPS:
            raise cc.CryptoError("unknown setup %r. Use: %s" % (s, ", ".join(ALL_SETUPS)))
        out.append(s)
    return out


def do_universe(a, venue, cfg):
    kept, why = cc.build_universe(venue, cfg, min_volume=a.min_volume, top=a.top, symbols=a.symbols_list)
    data = {"venue": venue.name, "as_of": cc.now_ms(), "as_of_utc": cc.fmt_utc(cc.now_ms()),
            "min_volume_usd": a.min_volume if a.min_volume is not None else cfg.get("min_quote_volume_usd"),
            "left_out": why, "markets": len(venue.markets()),
            "coins": [{"symbol": m["symbol"], "coin": m["coin"], "multiplier": m["multiplier"],
                       "volume_24h": t["quote_volume"], "change_24h": t["change"], "tick": m.get("tick"),
                       "max_leverage": m.get("max_leverage"), "funding_hours": m.get("funding_hours")} for m, t in kept]}
    if not a.no_write:
        cc.write_json(cc.wpath("universe.json"), data)
    if a.json:
        cc.dump(data)
        return 0 if kept else 1
    cc.out("Universe . %s . %s" % (venue.label, cc.fmt_utc(cc.now_ms())))
    left = ", ".join("%d %s" % (v, k) for k, v in why.items()) or "nothing"
    cc.out("Kept %d of %d perpetuals (left out: %s)." % (len(kept), len(venue.markets()), left))
    busiest = " . ".join("%s %s" % (m["coin"], cc.fmt_usd(t["quote_volume"])) for m, t in kept[:10])
    if busiest:
        cc.out("Busiest: " + busiest)
    if not a.no_write:
        cc.out("Saved %s" % cc.wpath("universe.json"))
    return 0 if kept else 1


def scan(a, venue, cfg):
    setups = parse_setups(a.setups)
    sides = ["long", "short"] if a.side == "both" else [a.side]
    if not cfg.get("allow_short", True) and "short" in sides and a.side == "both":
        sides = ["long"]
    now = cc.now_ms()
    notes = []
    if "orb" in setups:
        s_open = cc.last_session_open(a.session, now)
        if a.session == "ny" and cc.utc_dt(s_open).weekday() >= 5:
            setups.remove("orb")
            notes.append("No New York opening range today: US stocks do not open at weekends.")
        elif now < s_open + (a.or_minutes + 5) * 60000:
            setups.remove("orb")
            notes.append("The %d-minute opening range after %s is still forming." % (a.or_minutes, cc.fmt_utc(s_open)))
        elif now > s_open + int(a.entry_hours * 3600000) + 3600000:
            notes.append("The %s session opened %s ago; opening-range hits may be stale." % (a.session, cc.fmt_dur(now - s_open)))
    universe, why = cc.build_universe(venue, cfg, min_volume=a.min_volume, top=a.top, symbols=a.symbols_list)
    if not universe:
        raise cc.CryptoError("no coins pass the filters (min 24h volume %s). Lower --min-volume or pass --symbols."
                             % cc.fmt_usd(a.min_volume if a.min_volume is not None else cfg.get("min_quote_volume_usd")))
    need5 = any(s in setups for s in ("orb", "breakout", "trend", "volume"))
    need1h = any(s in setups for s in ("squeeze", "trend", "reversal", "rs"))
    n1h = 750 if "squeeze" in setups else 260

    def fetch(item):
        m, t = item
        b5 = venue.bars(m["symbol"], "5m", 900) if need5 else []
        b1h = venue.bars(m["symbol"], "1h", n1h) if need1h else []
        return Ctx(m, t, b5, b1h)

    workers = a.workers or (2 if venue.name == "hyperliquid" else 8)
    results = cc.parallel(fetch, universe, workers=workers)
    ctxs = [r for _, r, e in results if e is None and r is not None]
    failed = [{"symbol": item[0]["symbol"], "error": str(e)} for item, r, e in results if e is not None]
    hits = []
    for x in ctxs:
        for s in setups:
            f = SETUP_FUNCS.get(s)
            if f:
                try:
                    hits.extend(f(x, a, sides))
                except (ValueError, ZeroDivisionError, IndexError) as e:
                    failed.append({"symbol": x.m["symbol"], "error": "%s: %s" % (s, e)})
    if "rs" in setups:
        btc = None
        for x in ctxs:
            if x.m["coin"] == "BTC":
                btc = x.b1h
        if btc is None:
            try:
                bm = venue.market("BTC")
                btc = venue.bars(bm["symbol"], "1h", n1h)
            except cc.CryptoError as e:
                notes.append("Relative strength skipped: %s" % e)
        hits.extend(setup_rs(ctxs, btc, a, sides))
    order = dict((s, i) for i, s in enumerate(ALL_SETUPS))
    hits.sort(key=lambda h: (order[h["setup"]], h["status"] == "back inside", -(h["rvol"] or 0), -(h["volume_24h"] or 0)))
    data = {
        "mode": "scan", "venue": venue.name, "venue_label": venue.label,
        "as_of": now, "as_of_utc": cc.fmt_utc(now), "session": a.session, "or_minutes": a.or_minutes,
        "params": params_of(a),
        "setups": setups, "sides": sides, "universe": len(universe), "scanned": len(ctxs),
        "left_out": why, "failed": failed, "notes": notes, "hits": hits,
    }
    if not a.no_write:
        cc.write_json(cc.wpath("watchlist.json"), data)
    if a.json:
        cc.dump(data)
    else:
        print_scan(data, venue, a)
    if a.stats:
        cc.out("Requests: " + cc.HTTP.summary())
    return 0 if hits else 1


def _lvl(h, key):
    v = h.get(key)
    return cc.fmt_price(v) if v is not None else "-"


def print_scan(d, venue, a):
    cc.out("Scan | %s | %s" % (d["venue_label"], d["as_of_utc"]))
    left = ", ".join("%d %s" % (v, k) for k, v in d["left_out"].items())
    cc.out("%d coins scanned%s. Closed bars only; the newest 5m bar closed at %s."
           % (d["scanned"], (" (left out: %s)" % left) if left else "",
              cc.fmt_utc((d["as_of"] // 300000) * 300000, date=False)))
    for n in d["notes"]:
        cc.out(n)
    by = {}
    for h in d["hits"]:
        by.setdefault(h["setup"], []).append(h)
    for s in d["setups"]:
        rows = by.get(s, [])
        cc.out("")
        failed_orb = []
        if s == "orb":
            so = cc.last_session_open(d["session"], d["as_of"])
            cc.out("%s | %s open at %s | %d-minute range" % (TITLES[s], SESSION_NAMES[d["session"]],
                                                            cc.fmt_utc(so, date=False), d["or_minutes"]))
            failed_orb = [h for h in rows if h["status"] != "holding"]
            rows = [h for h in rows if h["status"] == "holding"]
        else:
            cc.out(TITLES[s])
        if not rows:
            cc.out("  none")
        show = rows[:a.show]
        if not show:
            if failed_orb:
                cc.out("  Broke out, then fell back inside: " + ", ".join("%s %s" % (h["coin"], h["side"]) for h in failed_orb))
            continue
        if s == "orb":
            tab = [[h["coin"], h["side"], "%s to %s" % (cc.fmt_price(h["metrics"]["or_low"]), cc.fmt_price(h["metrics"]["or_high"])),
                    "%s @ %s" % (cc.fmt_price(h["trigger"]), cc.fmt_utc(h["at"], date=False)), cc.fmt_price(h["price"]),
                    "%.1fx" % h["rvol"] if h["rvol"] else "-", h["status"]] for h in show]
            cc.out(cc.table(tab, ["coin", "side", "range", "trigger (5m close)", "now", "rvol", "status"], "lllrrrl"))
        elif s == "breakout":
            tab = [[h["coin"], h["side"], _lvl(h, "level"), cc.fmt_price(h["trigger"]),
                    cc.fmt_pct(h["metrics"]["beyond_pct"]), "%.1fx" % h["rvol"], h["status"]] for h in show]
            cc.out(cc.table(tab, ["coin", "side", "level", "15m close", "beyond", "rvol", "status"], "llrrrrl"))
        elif s == "squeeze":
            tab = [[h["coin"], cc.fmt_pct(h["metrics"]["width_pct"], plus=False), "%d%%" % round(h["metrics"]["width_rank"] * 100),
                    cc.fmt_price(h["metrics"]["lower"]), cc.fmt_price(h["metrics"]["upper"])] for h in show]
            cc.out(cc.table(tab, ["coin", "band width", "rank (30d)", "lower band", "upper band"], "lrrrr"))
        elif s == "trend":
            tab = [[h["coin"], h["side"], cc.fmt_price(h["trigger"]), _lvl(h, "level"), _lvl(h, "stop_ref"),
                    cc.fmt_pct(h["metrics"]["pullback_pct"], plus=False)] for h in show]
            cc.out(cc.table(tab, ["coin", "side", "15m close", "15m EMA20", "pullback extreme", "pullback"], "llrrrr"))
        elif s == "reversal":
            tab = [[h["coin"], h["side"], cc.fmt_price(h["trigger"]),
                    "%.0f -> %.0f" % (h["metrics"].get("rsi_min", h["metrics"].get("rsi_max")), h["metrics"]["rsi_now"]),
                    cc.fmt_pct(h["metrics"]["move_24h"]), _lvl(h, "stop_ref")] for h in show]
            cc.out(cc.table(tab, ["coin", "side", "1h close", "RSI", "24h", "6h extreme"], "llrrrr"))
        elif s == "volume":
            tab = [[h["coin"], h["side"], cc.fmt_price(h["trigger"]), cc.fmt_pct(h["metrics"]["move_pct"]),
                    "%.1fx" % h["rvol"], "%.1f ATR" % h["metrics"]["move_atr"]] for h in show]
            cc.out(cc.table(tab, ["coin", "side", "15m close", "move", "rvol", "size"], "llrrrr"))
        elif s == "rs":
            tab = [[h["coin"], h["status"], cc.fmt_pct(h["metrics"]["move_24h"]), cc.fmt_pct(h["metrics"]["btc_24h"]),
                    "%.2f" % h["metrics"]["beta"], cc.fmt_pct(h["metrics"]["excess_24h"])] for h in show]
            cc.out(cc.table(tab, ["coin", "role", "24h", "BTC 24h", "beta", "vs expected"], "llrrrr"))
        if len(rows) > len(show):
            cc.out("  ... %d more in crypto/watchlist.json" % (len(rows) - len(show)))
        if failed_orb:
            cc.out("  Broke out, then fell back inside: " + ", ".join("%s %s" % (h["coin"], h["side"]) for h in failed_orb))
    if d["failed"]:
        cc.out("")
        cc.out("Could not read %d coin(s): %s" % (len(d["failed"]), ", ".join(f["symbol"] for f in d["failed"][:6])))
    cc.out("")
    n = len(d["hits"])
    coins = len(set(h["coin"] for h in d["hits"]))
    cc.out("%d hit%s across %d coin%s. A scan lists charts that match a rule; it is not a list of coins to buy."
           % (n, "" if n == 1 else "s", coins, "" if coins == 1 else "s"))
    if n:
        cc.out("Next: /crypto-coin <coin> for the full picture, /crypto-exit to set the stop and target.")


def build_parser():
    p = argparse.ArgumentParser(prog="scan.py", description="Scan liquid perpetuals for setups.")
    p.add_argument("command", nargs="?", default="scan", choices=["scan", "universe", "orb"])
    p.add_argument("--setups", default="all", help="comma list: " + ",".join(ALL_SETUPS))
    p.add_argument("--symbols", default=None, help="only these coins, e.g. BTC,ETH,SOL")
    p.add_argument("--side", default="both", choices=["both", "long", "short"])
    p.add_argument("--session", default=None, choices=sorted(cc.SESSIONS), help="opening-range session (default utc)")
    p.add_argument("--or-minutes", type=int, default=30, help="opening range length (default 30)")
    p.add_argument("--entry-hours", type=float, default=4.0, help="breakouts count this long after the open (default 4)")
    p.add_argument("--lookback-hours", type=float, default=24.0, help="range breakout lookback (default 24)")
    p.add_argument("--min-rvol", type=float, default=1.5, help="relative volume a breakout needs (default 1.5)")
    p.add_argument("--spike-rvol", type=float, default=4.0, help="relative volume for a volume spike (default 4)")
    p.add_argument("--squeeze-pct", type=int, default=10, help="band-width rank for a squeeze (default 10)")
    p.add_argument("--min-pullback", type=float, default=0.5, help="trend pullback depth in %% (default 0.5)")
    p.add_argument("--rsi-low", type=float, default=25.0)
    p.add_argument("--rsi-high", type=float, default=75.0)
    p.add_argument("--rs-decile", type=float, default=10.0, help="top and bottom share for relative strength (default 10)")
    p.add_argument("--rs-min", type=float, default=3.0, help="minimum excess move in %% (default 3)")
    p.add_argument("--min-volume", type=float, default=None, help="24h volume floor in USD (default from config, 20M)")
    p.add_argument("--top", type=int, default=None, help="scan only the N busiest coins")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--show", type=int, default=12, help="rows per setup (default 12)")
    cc.add_common(p)
    return p


def setups_for(x, session=None, sides=("long", "short")):
    """Run every single-coin setup on one coin's data (crypto-coin and crypto-scout use this)."""
    a = build_parser().parse_args([])
    a.session = session or cc.load_config().get("session") or "utc"
    out = []
    now = cc.now_ms()
    s_open = cc.last_session_open(a.session, now)
    for s in ("orb", "breakout", "squeeze", "trend", "reversal", "volume"):
        if s == "orb" and (now < s_open + (a.or_minutes + 5) * 60000 or
                           (a.session == "ny" and cc.utc_dt(s_open).weekday() >= 5)):
            continue
        try:
            out.extend(SETUP_FUNCS[s](x, a, list(sides)))
        except (ValueError, ZeroDivisionError, IndexError):
            continue
    return out


def main(argv=None):
    a = build_parser().parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    a.session = a.session or cfg.get("session") or "utc"
    if a.or_minutes % 5 or a.or_minutes < 5:
        raise cc.CryptoError("--or-minutes must be a multiple of 5")
    a.symbols_list = [s for s in (a.symbols or "").split(",") if s.strip()] or None
    if a.command == "orb":
        a.setups = "orb"
    venue = cc.get_venue(a.venue)
    if getattr(venue, "auto_notes", None):
        cc.out("Using %s (%s)." % (venue.label, "; ".join(venue.auto_notes)))
    if a.command == "universe":
        return do_universe(a, venue, cfg)
    return scan(a, venue, cfg)


if __name__ == "__main__":
    sys.exit(cc.run(main))
