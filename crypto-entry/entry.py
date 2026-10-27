#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-entry: watch a setup bar by bar and say the moment to get in.

    python3 entry.py check ORBX --setup orb --session ny     forming / waiting / triggered / stale / expired
    python3 entry.py check BTC,ETH,SOL --setup orb           several coins, one block each
    python3 entry.py check SQZX --setup squeeze              the first 1-hour close outside the squeezed bands
    python3 entry.py check BTC --above 70000                 the first 5-minute close above your level
    python3 entry.py check ETH --below 3000 --tf 15m --rvol 1.5
    python3 entry.py watch ORBX --setup orb --for 4h         wakes at each bar close; stops on the trigger

Only closed bars count, and the rules are the scanner's own. On a trigger it writes
crypto/entry.json, and /crypto-exit plans from it. It never places an order.
"""

import argparse
import importlib.util
import json
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

T, O, H, L, C, V, QV = cc.T, cc.O, cc.H, cc.L, cc.C, cc.V, cc.QV
MIN = 60000
M5, M15, HOUR = 300000, 900000, 3600000
SETUPS = ("orb", "breakout", "squeeze", "trend", "reversal", "volume", "level")
BAR = {"orb": M5, "breakout": M15, "trend": M15, "volume": M15, "reversal": HOUR, "squeeze": HOUR}
TF = {"5m": M5, "15m": M15, "1h": HOUR}
TF_WORDS = {M5: "5-minute", M15: "15-minute", HOUR: "1-hour"}
LOOKBACK = {"breakout": 4, "trend": 4, "volume": 4, "reversal": 3, "squeeze": 3}   # bars searched for a recent trigger
SESSION_NAMES = cc.SESSION_NAMES
WAKE_DELAY_S = 3.0          # after a bar closes, give the exchange a moment to publish it

_SCAN = []


def scanner():
    """The scanner's module: the entry rules are its setup functions, so a trigger here is a hit there."""
    if not _SCAN:
        path = os.path.join(HERE, os.pardir, "crypto-scanner", "scan.py")
        if not os.path.isfile(path):
            raise cc.CryptoError("crypto-entry needs crypto-scanner installed next to it")
        spec = importlib.util.spec_from_file_location("crypto_entry_scan", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SCAN.append(mod)
    return _SCAN[0]


def pct_away(frm, to):
    return abs(to / frm - 1.0) * 100.0 if frm else 0.0


def hhmm(ms):
    return cc.fmt_local(ms, date=False)


def cap(text):
    return text[0].upper() + text[1:] if text else text


def setup_of(a):
    return "level" if (a.above is not None or a.below is not None) else a.setup


def told_trigger(setup, h, step, P):
    """What the trigger bar did, as a clause: 'the 15-minute bar closed at 2.96, above the 24h high 2.95, on 3.5x volume'."""
    mt = h.get("metrics") or {}
    up = h["side"] == "long"
    bar = "the %s bar" % TF_WORDS[step]
    rv = (", on %.1fx volume" % h["rvol"]) if h.get("rvol") else ""
    if setup == "orb":
        return "%s closed at %s, %s the range %s %s%s" % (bar, P(h["trigger"]), "above" if up else "below",
                                                         "high" if up else "low", P(h["level"]), rv)
    if setup == "breakout":
        return "%s closed at %s, %s the %gh %s %s%s" % (bar, P(h["trigger"]), "above" if up else "below",
                                                       (h["metrics"] or {}).get("lookback_hours", 24),
                                                       "high" if up else "low", P(h["level"]), rv)
    if setup == "trend":
        return "%s closed at %s, back %s its 20 average (%s) after a %.1f%% %s, in a 1-hour %s" % (
            bar, P(h["trigger"]), "over" if up else "under", P(h["level"]), (mt.get("pullback_pct") or 0) * 100,
            "pullback" if up else "bounce", "uptrend" if up else "downtrend")
    if setup == "volume":
        return "%s traded %.1fx its usual volume on a %.1f%% move %s, closing at %s" % (
            bar, h.get("rvol") or 0, abs(mt.get("move_pct") or 0) * 100, "up" if up else "down", P(h["trigger"]))
    if setup == "reversal":
        return "%s closed at %s as the 1-hour RSI turned %s from %.0f to %.0f" % (
            bar, P(h["trigger"]), "up" if up else "down", mt.get("rsi_min" if up else "rsi_max") or 0, mt.get("rsi_now") or 0)
    if setup == "squeeze":
        return "%s closed at %s, %s the %s band %s: the first close outside the bands after a squeeze" % (
            bar, P(h["trigger"]), "above" if up else "below", "upper" if up else "lower", P(h["level"]))
    return "%s closed at %s, %s your %s%s" % (bar, P(h["trigger"]), "above" if up else "below", P(h["level"]), rv)


def put_trigger(res, setup, h, new, step, now, last, P, after=""):
    """Fill status and words for a hit: triggered (new) or stale (an earlier one)."""
    res.update(hit=h, side=h["side"], status="triggered" if new else "stale")
    what = told_trigger(setup, h, step, P)
    if new:
        res["words"] = "At %s %s." % (hhmm(h["at"]), what)
    else:
        res["words"] = "Triggered %s ago: at %s %s. The last close is %s (%+.2f%%)%s." % (
            cc.fmt_dur(now - h["at"]), hhmm(h["at"]), what, P(last), (last / h["trigger"] - 1) * 100, after)


# ---------------------------------------------------------------- one look at one coin

def look(a, venue, m, sa, since=None):
    """One look at one coin. since: when a watch started (a trigger after it counts as new).
    Returns a dict: status, words, notes, final (nothing more can trigger), hit, last_close."""
    now = cc.now_ms()
    setup = setup_of(a)
    step = TF[a.tf] if setup == "level" else BAR[setup]
    fresh_after = (min(since, now) if since is not None else now) - step
    sides = [a.side] if a.side != "both" else ["long", "short"]
    t = {} if cc.pinned() else dict(venue.ticker(m["symbol"]))
    P = lambda x: cc.fmt_price(x, m.get("tick"))
    res = {"coin": m["coin"], "symbol": m["symbol"], "venue": venue.name, "setup": setup, "as_of": now,
           "step_ms": step, "notes": [], "final": False, "hit": None}
    if setup == "orb":
        look_orb(res, venue, m, sa, sides, now, fresh_after, t, P)
    elif setup == "level":
        look_level(res, a, venue, m, sides, now, fresh_after, t, P)
    elif setup in ("breakout", "trend", "volume"):
        look_15m(res, setup, venue, m, sa, sides, now, fresh_after, t, P)
    else:
        look_1h(res, setup, venue, m, sa, sides, now, fresh_after, t, P)
    if t.get("last") is not None:
        res["price"] = t["last"]
    h = res.get("hit")
    if h and res["status"] == "triggered" and now - h["at"] > step + MIN:
        res["notes"].append("Caught %s after the bar closed: the price may have moved since." % cc.fmt_dur(now - h["at"]))
    return res


def with_close(res, bars, step):
    if bars:
        res["last_close"] = bars[-1][C]
        res["last_close_at"] = bars[-1][T] + step


def trading_day(session, open_ms):
    return not (session == "ny" and cc.utc_dt(open_ms).weekday() >= 5)


def next_open(session, now):
    """The next open after now (New York skips weekends)."""
    day = cc.utc_dt(now)
    for k in range(0, 8):
        d = day + cc.timedelta(days=k)
        o = cc.session_open_on(session, d.year, d.month, d.day)
        if o > now and trading_day(session, o):
            return o
    raise cc.CryptoError("no %s open in the next week" % session)


def look_orb(res, venue, m, sa, sides, now, fresh_after, t, P):
    """The first 5-minute close beyond the opening range, on volume. With both sides, the first side to break wins.
    From an hour after a session's entry window closes until the next open, it waits for that open."""
    scan = scanner()
    s_open = cc.last_session_open(sa.session, now)
    sess = SESSION_NAMES.get(sa.session, sa.session)
    hours = lambda o: o + int(sa.entry_hours * HOUR)
    if not trading_day(sa.session, s_open) or now >= hours(s_open) + HOUR:
        nxt = next_open(sa.session, now)
        res.update(session=sa.session, session_open=nxt, range_end=nxt + sa.or_minutes * MIN, window_end=hours(nxt),
                   label="%s open %s, %d-minute range" % (sess, hhmm(nxt), sa.or_minutes))
        when = hhmm(nxt) if nxt - now < 18 * HOUR else cc.fmt_local(nxt)
        res.update(status="forming", words="Not open yet: the next %s open is %s, in %s. The %d-minute range forms after it%s." % (
            sess, when, cc.fmt_dur(nxt - now), sa.or_minutes,
            "; New York has no session at weekends" if sa.session == "ny" and not trading_day("ny", s_open) else ""))
        return
    or_end = s_open + sa.or_minutes * MIN
    win_end = hours(s_open)
    res.update(session=sa.session, session_open=s_open, range_end=or_end, window_end=win_end,
               label="%s open %s, %d-minute range" % (sess, hhmm(s_open), sa.or_minutes))
    b5 = venue.bars(m["symbol"], "5m", min(1000, int((now - s_open) // M5) + 60))
    with_close(res, b5, M5)
    rng = [b for b in b5 if s_open <= b[T] < or_end]
    if now < or_end:
        so_far = (" So far %s to %s." % (P(min(b[L] for b in rng)), P(max(b[H] for b in rng)))) if rng else ""
        res.update(status="forming", words="The opening range is forming until %s.%s" % (hhmm(or_end), so_far))
        return
    if len(rng) < sa.or_minutes // 5:
        res.update(status="none", final=True, words="The exchange is missing %d of the %d bars in the opening range, so there "
                   "is no range to trade." % (sa.or_minutes // 5 - len(rng), sa.or_minutes // 5))
        return
    hi, lo = max(b[H] for b in rng), min(b[L] for b in rng)
    res.update(or_high=hi, or_low=lo)
    hits = dict((h["side"], h) for h in scan.setup_orb(scan.Ctx(m, t, b5, []), sa, sides))
    base = cc.median([b[QV] for b in b5 if b[T] < s_open][-48:])
    after = [b for b in b5 if or_end <= b[T] < win_end]
    last = b5[-1][C]
    edge = {"long": ("above", "high", hi), "short": ("below", "low", lo)}
    breaks = []                                   # (close time, side, bar) of each side's first close beyond
    for side in sides:
        word, _, lvl = edge[side]
        first = next((b for b in after if (b[C] > lvl if side == "long" else b[C] < lvl)), None)
        if first is not None:
            breaks.append((first[T] + M5, side, first))
    breaks.sort(key=lambda x: x[0])
    trig = None
    for at, side, b in breaks:
        word, name, lvl = edge[side]
        if trig is not None:
            res["notes"].append("Then at %s the 5-minute bar closed %s the range %s %s%s." % (
                hhmm(at), word, name, P(lvl), ": the breakout failed" if side != trig["side"] else ""))
        elif side in hits:
            trig = hits[side]
        else:
            res["notes"].append("At %s the 5-minute bar closed %s the range %s %s on %.1fx volume, under the %gx the rule "
                                "needs on the first close: no %s trade this session." % (
                                    hhmm(at), word, name, P(lvl), (b[QV] / base) if base else 0.0, sa.min_rvol, side))
    if trig is not None:
        inside = trig["status"] != "holding"
        put_trigger(res, "orb", trig, trig["at"] > fresh_after, M5, now, last, P,
                    ", back inside the range" if inside else ", still beyond the range")
        res["final"] = res["status"] == "stale"
        return
    broke = set(s for _, s, _ in breaks)
    open_sides = [s for s in sides if s not in broke]
    if open_sides and now < win_end:
        want = " or ".join("%s %s" % (edge[s][0], P(edge[s][2])) for s in open_sides)
        dist = ", ".join("%.2f%% %s the %s" % (pct_away(last, edge[s][2]), "under" if s == "long" else "over", edge[s][1])
                         for s in open_sides)
        res.update(status="waiting", words="Waiting for a 5-minute close %s on %gx volume, until %s. Last close %s at %s: %s." % (
            want, sa.min_rvol, hhmm(win_end), P(last), hhmm(res["last_close_at"]), dist))
        return
    if open_sides:
        res["notes"].insert(0, "No 5-minute close %s by %s." % (
            " or ".join("%s %s" % (edge[s][0], P(edge[s][2])) for s in open_sides), hhmm(win_end)))
    res.update(status="expired", final=True, words="The %s window is over with no opening-range trade." % sess)


def look_level(res, a, venue, m, sides, now, fresh_after, t, P):
    step = TF[a.tf]
    bars = venue.bars(m["symbol"], a.tf, 120)
    with_close(res, bars, step)
    checks = []
    if a.above is not None and "long" in sides:
        checks.append(("long", a.above, "above", lambda b, v=a.above: b[C] > v))
    if a.below is not None and "short" in sides:
        checks.append(("short", a.below, "below", lambda b, v=a.below: b[C] < v))
    if not checks:
        raise cc.CryptoError("--above is a long trigger and --below a short one; --side %s leaves nothing to watch" % a.side)
    res.update(label="your level, %s closes" % TF_WORDS[step], levels=[[s, v] for s, v, _, _ in checks])
    if len(bars) < 3:
        res.update(status="none", final=True, words="Too few %s bars to watch yet." % TF_WORDS[step])
        return
    last = bars[-1][C]
    stale, never = [], []
    for side, lvl, word, beyond in checks:
        i = len(bars) - 1
        if not beyond(bars[i]):
            continue
        while i > 0 and beyond(bars[i - 1]):
            i -= 1
        back = "under" if side == "long" else "over"
        if i == 0:
            never.append("%s is already %s %s: every %s close on record (%d bars) is %s it. Pick a level %s the price, "
                         "or use %s" % (m["coin"], word, P(lvl), TF_WORDS[step], len(bars), word,
                                        "over" if side == "long" else "under", "--below" if side == "long" else "--above"))
            continue
        b = bars[i]
        at = b[T] + step
        base = cc.median([x[QV] for x in bars[max(0, i - 48):i]])
        rvol = (b[QV] / base) if base else None
        if a.rvol and (rvol or 0) < a.rvol:
            stale.append("At %s the %s bar closed %s %s on %.1fx volume, short of your %gx, so that cross does not count. "
                         "A fresh one needs a close back %s it first" % (hhmm(at), TF_WORDS[step], word, P(lvl), rvol or 0,
                                                                         a.rvol, back))
            continue
        h = scanner().hit(scanner().Ctx(m, t, [], [], b15=[]), "level", side, b[C], lvl, None, at, rvol, "fresh",
                          {"tf": a.tf, "rvol_needed": a.rvol or None}, "%s close %s your level" % (a.tf, word))
        if at > fresh_after:
            put_trigger(res, "level", h, True, step, now, last, P)
            return
        stale.append("It closed %s %s at %s, %s ago, and has stayed %s. A fresh trigger needs a close back %s it first"
                     % (word, P(lvl), hhmm(at), cc.fmt_dur(now - at), word, back))
    if len(never) == len(checks):
        res.update(status="none", final=True, words=". ".join(never) + ". Last close %s." % P(last))
        return
    if len(stale) + len(never) == len(checks):
        res.update(status="stale", words=". ".join(stale + never) + ". Last close %s." % P(last))
        return
    want = " or ".join("%s %s (%.2f%% away)" % (w, P(v), pct_away(last, v)) for s, v, w, bz in checks if not bz(bars[-1]))
    res.update(status="waiting", words="Waiting for a %s close %s%s. Last close %s at %s." % (
        TF_WORDS[step], want, (" on %gx volume" % a.rvol) if a.rvol else "", P(last), hhmm(res["last_close_at"])))
    res["notes"] += [s + "." for s in stale + never]


def recent(func, ctx_at, n_back, sides, sa, fresh_after, keep=None):
    """The newest hit within the last n_back closed bars, and whether it is new: (hit, new) or (None, None).
    ctx_at(back) builds the scanner's Ctx as it stood `back` bars ago."""
    for back in range(n_back):
        x = ctx_at(back)
        if x is None:
            break
        hits = [h for h in func(x, sa, sides) if keep is None or keep(h)]
        if hits:
            return hits[0], hits[0]["at"] > fresh_after
    return None, None


def look_15m(res, setup, venue, m, sa, sides, now, fresh_after, t, P):
    scan = scanner()
    b5 = venue.bars(m["symbol"], "5m", 900)
    b15 = cc.resample(b5, "15m")
    b1h = venue.bars(m["symbol"], "1h", 264) if setup == "trend" else []
    with_close(res, b15, M15)
    res["label"] = {"breakout": "%gh range breakout, 15-minute closes" % sa.lookback_hours,
                    "trend": "trend pullback, 15-minute closes", "volume": "volume spike, 15-minute bars"}[setup]
    if len(b15) < 100:
        res.update(status="none", final=True, words="Too little history (%d 15-minute bars) for this setup." % len(b15))
        return

    def ctx_at(back):
        end = len(b15) - back
        if end < 100:
            return None
        close_t = b15[end - 1][T] + M15
        return scan.Ctx(m, t, [], [b for b in b1h if b[T] + HOUR <= close_t], b15=b15[:end])

    keep = (lambda h: h["status"] == "fresh") if setup == "breakout" else None
    h, new = recent(scan.SETUP_FUNCS[setup], ctx_at, LOOKBACK[setup], sides, sa, fresh_after, keep)
    last = b15[-1][C]
    if h is not None:
        put_trigger(res, setup, h, new, M15, now, last, P)
        return
    if setup == "breakout":
        n = int(sa.lookback_hours * 4)
        win = b15[-n - 1:-1]
        hi, lo = max(b[H] for b in win), min(b[L] for b in win)
        base = cc.median([b[QV] for b in b15[-97:-1]])
        rv = b15[-1][QV] / base if base else 0.0
        up = "long" in sides and last > hi
        if up or ("short" in sides and last < lo):
            res.update(status="stale", words="The last 15-minute close %s is already %s the %gh %s %s, %s." % (
                P(last), "above" if up else "below", sa.lookback_hours, "high" if up else "low", P(hi if up else lo),
                ("on %.1fx volume, under the %gx the rule needs" % (rv, sa.min_rvol)) if rv < sa.min_rvol else
                "with no fresh close beyond it in the last hour"))
            return
        want = []
        if "long" in sides:
            want.append("above %s (%.2f%% away)" % (P(hi), pct_away(last, hi)))
        if "short" in sides:
            want.append("below %s (%.2f%% away)" % (P(lo), pct_away(last, lo)))
        res.update(status="waiting", words="Waiting for a 15-minute close %s, outside the %gh range, on %gx volume. "
                   "Last close %s." % (" or ".join(want), sa.lookback_hours, sa.min_rvol, P(last)))
        return
    if setup == "trend":
        c1 = cc.closes(b1h)
        if len(c1) < 210:
            res.update(status="none", final=True, words="Too little 1-hour history (%d bars) for the trend rule." % len(c1))
            return
        e20, e50, e200 = cc.ema(c1, 20)[-1], cc.ema(c1, 50)[-1], cc.ema(c1, 200)[-1]
        e15 = cc.ema(cc.closes(b15), 20)[-1]
        up = "long" in sides and e20 > e50 > e200
        down = "short" in sides and e20 < e50 < e200
        avgs = "1-hour averages 20: %s, 50: %s, 200: %s" % (P(e20), P(e50), P(e200))
        if not (up or down):
            res.update(status="none", final=True, words="No trend to pull back in: the %s are not stacked%s." % (
                avgs, "" if len(sides) == 2 else " for a %s" % sides[0]))
            return
        res.update(status="waiting", words="A 1-hour %s (%s). Waiting for a 15-minute %s to the 20 average (%s) and a "
                   "close back %s it. Last close %s." % ("uptrend" if up else "downtrend", avgs, "pullback" if up else "bounce",
                                                        P(e15), "over" if up else "under", P(last)))
        return
    base = cc.median([b[QV] for b in b15[-97:-1]])
    atr = cc.atr(b15, 14)[-1]
    res.update(status="waiting", words="Waiting for a 15-minute bar with %gx its usual volume (usual: %s a bar) that moves "
               "at least one ATR (%s). The last bar had %.1fx." % (sa.spike_rvol, cc.fmt_usd(base or 0), P(atr or 0),
                                                                   (b15[-1][QV] / base) if base else 0.0))


def look_1h(res, setup, venue, m, sa, sides, now, fresh_after, t, P):
    scan = scanner()
    b1h = venue.bars(m["symbol"], "1h", 760 if setup == "squeeze" else 264)
    with_close(res, b1h, HOUR)
    res["label"] = "squeeze break, 1-hour closes" if setup == "squeeze" else "RSI reversal, 1-hour closes"
    need = 250 if setup == "squeeze" else 40
    if len(b1h) < need + LOOKBACK[setup]:
        res.update(status="none", final=True, words="Too little 1-hour history (%d bars; the rule needs %d)." % (len(b1h), need))
        return
    func = scan.setup_squeeze_break if setup == "squeeze" else scan.setup_reversal

    def ctx_at(back):
        end = len(b1h) - back
        return scan.Ctx(m, t, [], b1h[:end], b15=[]) if end >= need else None

    h, new = recent(func, ctx_at, LOOKBACK[setup], sides, sa, fresh_after)
    last = b1h[-1][C]
    if h is not None:
        put_trigger(res, setup, h, new, HOUR, now, last, P)
        return
    c = cc.closes(b1h)
    if setup == "reversal":
        r = cc.rsi(c, 14)[-1]
        want = []
        if "long" in sides:
            want.append("%g or under" % sa.rsi_low)
        if "short" in sides:
            want.append("%g or over" % sa.rsi_high)
        res.update(status="waiting", words="1-hour RSI is %s. The rule needs it at %s within 4 hours, then a turn of 5 points "
                   "on a 1-hour close." % (("%.0f" % r) if r is not None else "-", " or ".join(want)))
        return
    mid, up, lo, wd = cc.bollinger(c, 20, 2.0)
    hist = [w for w in wd[-720:] if w is not None]
    rank = min(cc.pct_rank(w, hist) for w in wd[-12:] if w is not None)
    if rank > sa.squeeze_pct / 100.0:
        res.update(status="none", final=True, words="No squeeze: the 1-hour band width has not been in the lowest %d%% of 30 days "
                   "in the last 12 hours (its lowest rank: %d%%). Nothing to time." % (sa.squeeze_pct, round(rank * 100)))
        return
    want = []
    if "long" in sides:
        want.append("above %s (%.2f%% away)" % (P(up[-1]), pct_away(last, up[-1])))
    if "short" in sides:
        want.append("below %s (%.2f%% away)" % (P(lo[-1]), pct_away(last, lo[-1])))
    res.update(status="waiting", words="Coiled: the band width ranks in the lowest %d%% of 30 days. Waiting for a 1-hour close "
               "%s. The bands move every hour. Last close %s." % (max(1, round(rank * 100)), " or ".join(want), P(last)))


# ---------------------------------------------------------------- output

def save(res, sa, a):
    if res["status"] != "triggered" or not res.get("hit") or a.no_write:
        return False
    params = scanner().params_of(sa)
    if res["setup"] == "level":
        params.update(tf=a.tf, rvol=a.rvol or None)
    cc.write_json(cc.wpath("entry.json"), {"version": 1, "as_of": cc.now_ms(), "venue": res["venue"], "params": params,
                                           "status": res["status"], "words": res["words"], "hit": res["hit"]})
    return True


def show(res, m, saved=False):
    cc.out("%s | %s | %s | %s" % (res["status"].upper(), m["coin"], res.get("label", res["setup"]), cc.fmt_local(res["as_of"])))
    cc.out("  " + res["words"])
    for n in res["notes"]:
        cc.out("  " + n)
    if res.get("price") is not None and res.get("last_close_at") and res["status"] in ("waiting", "forming"):
        cc.out("  Price now %s. The %s bar after %s is still forming; it counts only when it closes." % (
            cc.fmt_price(res["price"], m.get("tick")), TF_WORDS[res["step_ms"]], hhmm(res["last_close_at"])))
    if res["status"] == "triggered":
        cc.out("  This is the moment to get in, if you take the trade. Nothing was placed.")
        if saved:
            cc.out("  Saved crypto/entry.json. Next: /crypto-exit %s sets the stop and target BEFORE anything is bought." % m["coin"])
    elif res["status"] == "stale":
        cc.out("  A late entry is a different trade: a worse price for the same stop.")


def line(res, m):
    """One compact line per bar for a watch."""
    return "%s  %-9s %s: %s" % (hhmm(res["as_of"]), res["status"].upper(), m["coin"], res["words"])


# ---------------------------------------------------------------- commands

def build_parser():
    p = argparse.ArgumentParser(prog="entry.py", description="Watch a setup and say the moment to get in.")
    p.add_argument("command", choices=["check", "watch", "level"])
    p.add_argument("coins", help="one coin, or several: BTC,ETH,SOL")
    p.add_argument("--setup", default="orb", choices=SETUPS[:-1], help="the scanner rule to time (default orb)")
    p.add_argument("--side", default="both", choices=["both", "long", "short"])
    p.add_argument("--session", default=None, choices=sorted(cc.SESSIONS), help="opening-range session (default: config)")
    p.add_argument("--or-minutes", type=int, default=None, help="opening range length (default 30)")
    p.add_argument("--above", type=float, default=None, help="your level: trigger on a close above it (a long)")
    p.add_argument("--below", type=float, default=None, help="your level: trigger on a close below it (a short)")
    p.add_argument("--tf", default="5m", choices=sorted(TF), help="bars for --above/--below (default 5m)")
    p.add_argument("--rvol", type=float, default=0.0, help="volume the level close needs, x the median bar (default none)")
    p.add_argument("--for", dest="duration", default="4h", help="watch: give up after this long (default 4h)")
    p.add_argument("--max-checks", type=int, default=None, help="watch: stop after this many bars")
    cc.add_common(p)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cc.apply_common(a)
    if a.command == "level" and a.above is None and a.below is None:
        raise cc.CryptoError("give a level: --above PRICE (a long) or --below PRICE (a short)")
    for v in (a.above, a.below):
        if v is not None and v <= 0:
            raise cc.CryptoError("a level must be a positive price")
    cfg = cc.load_config()
    if not cfg.get("allow_short", True):
        if a.side == "short" or (a.below is not None and a.above is None):
            raise cc.CryptoError("shorts are off in your config (allow_short is false)")
        a.side = "long"
    scan = scanner()
    sa = scan.args_from({"session": a.session or cfg.get("session") or "utc"})
    if a.or_minutes:
        if a.or_minutes % 5 or a.or_minutes < 5:
            raise cc.CryptoError("--or-minutes must be a multiple of 5")
        sa.or_minutes = a.or_minutes
    names = [c.strip() for c in a.coins.split(",") if c.strip()]
    if not names or len(names) > 12:
        raise cc.CryptoError("give one to twelve coins, like BTC or BTC,ETH,SOL")
    venue = cc.get_venue(a.venue)
    if getattr(venue, "auto_notes", None) and not a.json:
        cc.out("Using %s (%s)." % (venue.label, "; ".join(venue.auto_notes)))
    markets = [venue.market(c) for c in names]
    if a.command in ("check", "level"):
        return check(a, venue, markets, sa)
    return watch(a, venue, markets, sa)


def check(a, venue, markets, sa):
    results = [look(a, venue, m, sa) for m in markets]
    saved = None
    for r in results:
        if saved is None and save(r, sa, a):
            saved = r
    if a.json:
        cc.dump(results[0] if len(results) == 1 else results)
    else:
        for i, (r, m) in enumerate(zip(results, markets)):
            if i:
                cc.out("")
            show(r, m, saved is r)
        if len(results) > 1 and saved:
            cc.out("")
            cc.out("crypto/entry.json holds %s (one trigger at a time)." % saved["coin"])
    return 0 if any(r["status"] == "triggered" for r in results) else 1


def watch(a, venue, markets, sa):
    """Wake at each bar close and look. With the clock pinned (--now), replay bar by bar without waiting."""
    limit_ms = int(cc.parse_hold(a.duration, 4.0) * HOUR)
    start = cc.now_ms()
    replay = cc.pinned()
    setup = setup_of(a)
    step = TF[a.tf] if setup == "level" else BAR[setup]
    if not a.json:
        cc.out("Watching %s (%s) on %s from %s, for up to %s. %s" % (
            ", ".join(m["coin"] for m in markets), "your level" if setup == "level" else setup, venue.label,
            cc.fmt_local(start), cc.fmt_dur(limit_ms),
            "Replaying closed bars on the pinned clock." if replay else "One line per closed bar."))
    active = list(markets)
    looks = 0
    last_res = {}
    while True:
        looks += 1
        if not replay:
            venue.tickers(fresh=True)
        for m in list(active):
            r = look(a, venue, m, sa, since=start)
            last_res[m["symbol"]] = r
            if a.json:
                cc.out(json.dumps(r, default=str))
            else:
                cc.out(line(r, m))
            if r["status"] == "triggered":
                saved = save(r, sa, a)
                if not a.json:
                    show(r, m, saved)
                return 0
            if r["final"] or r["status"] in ("expired", "none"):
                active.remove(m)
        state = lambda ms: ", ".join("%s %s" % (m["coin"], last_res[m["symbol"]]["status"]) for m in ms)
        if not active:
            if not a.json:
                cc.out("Nothing left to wait for (%s)." % state(markets))
            return 2
        now = cc.now_ms()
        nxt = (now // step + 1) * step
        if (a.max_checks and looks >= a.max_checks) or nxt - start > limit_ms:
            if not a.json:
                cc.out("Stopped watching after %d look%s; still %s." % (looks, "" if looks == 1 else "s", state(active)))
            return 3
        if replay:
            if nxt > time.time() * 1000:
                if not a.json:
                    cc.out("The replay has reached the present; run watch without --now to keep going.")
                return 3
            cc.set_now(nxt)
        else:
            time.sleep(max(0.0, (nxt - now) / 1000.0) + WAKE_DELAY_S)


if __name__ == "__main__":
    sys.exit(cc.run(main))
