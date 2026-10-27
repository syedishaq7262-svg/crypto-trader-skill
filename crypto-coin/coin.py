#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-coin: one clean card for any coin. Price, trend, levels, volatility, liquidity,
funding, open interest, positioning, the market around it and any setup live on it now.

    python3 coin.py BTC
    python3 coin.py pepe --venue bybit
    python3 coin.py SOL --json

Facts and context, not a call. The card never says buy or sell.
"""

import argparse
import importlib.util
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
HOUR = 3600000
DAY = 86400000


def load_sibling(folder, name):
    """Use another skill's script when it is installed next to this one; None otherwise."""
    path = os.path.join(HERE, os.pardir, folder, name + ".py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("crypto_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def trend_of(bars):
    c = cc.closes(bars)
    if len(c) < 55:
        return None
    e20, e50 = cc.ema(c, 20)[-1], cc.ema(c, 50)[-1]
    if c[-1] > e20 > e50:
        return "up"
    if c[-1] < e20 < e50:
        return "down"
    return "mixed"


def nearest_levels(bars, price, side, n=2):
    """Swing highs above (side='R') or swing lows below (side='S') the price, nearest first."""
    pts = cc.pivots(bars, 4, 4)
    if side == "R":
        xs = sorted(set(p for _, p, k in pts if k == "H" and p > price))
    else:
        xs = sorted(set(p for _, p, k in pts if k == "L" and p < price), reverse=True)
    out = []
    for x in xs:
        if all(abs(x / y - 1) > 0.003 for y in out):
            out.append(x)
        if len(out) >= n:
            break
    return out


def build(venue, text, cfg, with_setups=True, with_news=True):
    m = venue.market(text)
    sym = m["symbol"]
    tk = venue.tickers()
    t = tk[sym]
    now = cc.now_ms()
    jobs = {
        "b5": lambda: venue.bars(sym, "5m", 900),
        "b1h": lambda: venue.bars(sym, "1h", 750),
        "b1d": lambda: venue.bars(sym, "1d", 120),
        "funding": lambda: cc.funding_stats(venue, sym, 30),
        "oi": lambda: cc.oi_change(venue, sym),
        "pos": lambda: cc.positioning(venue, sym),
        "book": lambda: venue.orderbook(sym, 500 if venue.name == "binance" else 200),
        "fng": lambda: cc.fear_greed(2),
    }
    if m["coin"] != "BTC":
        bm = venue.market("BTC")
        jobs["btc1h"] = lambda: venue.bars(bm["symbol"], "1h", 170)
    news = load_sibling("crypto-news", "news") if with_news else None
    if news is not None:
        known = set(x["coin"] for x in venue.markets())
        jobs["news"] = lambda: news.coin_headlines(m["coin"], known, cfg)
        jobs["macro"] = lambda: news.macro_soon(24.0)
        jobs["notices"] = lambda: news.notices(known)[0]
    names = list(jobs)
    res = cc.parallel(lambda k: jobs[k](), names, workers=2 if venue.name == "hyperliquid" else 6)
    got = {}
    errors = {}
    for k, r, e in res:
        if e is None:
            got[k] = r
        else:
            errors[k] = str(e)
    if "b1h" not in got:
        raise cc.CryptoError("could not read %s candles: %s" % (sym, errors.get("b1h")))
    b1h = got["b1h"]
    b5 = got.get("b5") or []
    b1d = got.get("b1d") or []
    b15 = cc.resample(b5, "15m") if b5 else []
    b4h = cc.resample(b1h, "4h")
    price = t["last"]
    c1 = cc.closes(b1h)
    last24 = [b for b in b1h if b[T] >= now - DAY]
    last7 = [b for b in b1h if b[T] >= now - 7 * DAY]

    def chg(hours):
        ref = [b for b in b1h if b[T] <= now - hours * HOUR]
        return (price / ref[-1][C] - 1.0) if ref else None

    d = {
        "venue": venue.name, "venue_label": venue.label, "symbol": sym, "coin": m["coin"], "multiplier": m["multiplier"],
        "tick": m.get("tick"), "max_leverage": m.get("max_leverage"), "as_of": now, "as_of_utc": cc.fmt_utc(now),
        "price": price, "mark": t.get("mark"), "index": t.get("index"),
        "change_24h": t.get("change"), "change_7d": chg(168), "change_30d": chg(720),
        "high_24h": t.get("high") or (max(b[H] for b in last24) if last24 else None),
        "low_24h": t.get("low") or (min(b[L] for b in last24) if last24 else None),
        "high_7d": max(b[H] for b in last7) if last7 else None, "low_7d": min(b[L] for b in last7) if last7 else None,
        "volume_24h": t.get("quote_volume"),
        "trend": {"15m": trend_of(b15), "1h": trend_of(b1h), "4h": trend_of(b4h), "1d": trend_of(b1d)},
        "rsi": {},
        "listed_ms": m.get("listed_ms"), "errors": errors,
    }
    for name, bars in (("1h", b1h), ("4h", b4h), ("1d", b1d)):
        r = cc.rsi(cc.closes(bars), 14) if len(bars) > 20 else []
        d["rsi"][name] = round(r[-1], 1) if r and r[-1] is not None else None
    a1 = cc.atr(b1h, 14)
    a1d = cc.atr(b1d, 14) if len(b1d) > 15 else [None]
    d["atr_1h_pct"] = (a1[-1] / price) if a1 and a1[-1] else None
    d["atr_1d_pct"] = (a1d[-1] / price) if a1d and a1d[-1] else None
    d["vol_7d"] = cc.realized_vol(c1[-169:], 8760)
    d["vol_30d"] = cc.realized_vol(c1[-721:], 8760)
    vols = [b[QV] for b in b1h]
    day_vols = [sum(vols[i:i + 24]) for i in range(max(0, len(vols) - 24 * 8), len(vols) - 24, 24)]
    d["rvol_24h"] = (sum(vols[-24:]) / cc.mean(day_vols)) if day_vols and cc.mean(day_vols) else None
    d["resistance"] = nearest_levels(b1h[-24 * 14:], price, "R")
    d["support"] = nearest_levels(b1h[-24 * 14:], price, "S")
    d["vwap_utc_day"] = cc.vwap_from(b5, (now // DAY) * DAY) if b5 else None
    if len(b1d) >= 2:
        d["prev_day_high"], d["prev_day_low"] = b1d[-1][H], b1d[-1][L]
    e20d = cc.ema(cc.closes(b1d), 20)[-1] if len(b1d) >= 20 else None
    d["ema20_1d"] = e20d
    d["spark_7d"] = [b[C] for b in last7]
    if "btc1h" in got and len(got["btc1h"]) > 30:
        bc = cc.closes(got["btc1h"])
        bt = cc.beta(cc.log_returns(c1[-169:]), cc.log_returns(bc[-169:]))
        b24 = bc[-1] / bc[-25] - 1.0 if len(bc) > 25 else None
        d["btc_24h"] = b24
        d["beta"] = bt
        d["corr"] = cc.corr(cc.log_returns(c1[-169:]), cc.log_returns(bc[-169:]))
        if bt is not None and b24 is not None and d["change_24h"] is not None:
            d["vs_btc_24h"] = d["change_24h"] - bt * b24
    if "book" in got:
        bs = cc.book_stats(got["book"][0], got["book"][1], 0.0025)
        d["book"] = bs
        if bs:
            avg, filled = cc.walk_book(got["book"][1], 10000.0)
            d["impact_10k"] = (avg / bs["mid"] - 1.0) if (avg and filled >= 9999) else None
    if "funding" in got:
        d["funding"] = got["funding"]
    if "oi" in got:
        d["open_interest"] = dict((k, v) for k, v in got["oi"].items() if k != "series")
    if "pos" in got:
        d["positioning"] = got["pos"]
    if "fng" in got and got["fng"]:
        d["fng"], d["fng_label"] = got["fng"][0][1], got["fng"][0][2]
    reg = cc.read_json(cc.wpath("regime.json"), None)
    if reg and reg.get("venue") == venue.name and 0 <= now - reg.get("as_of", 0) < 6 * HOUR:
        d["regime"] = reg.get("label")
    fs = d.get("funding") or {}
    oi = d.get("open_interest") or {}
    pos = d.get("positioning") or {}
    d["crowding"] = cc.crowding(fs.get("apr"), oi.get("ch24"), pos.get("long_share"), d["change_24h"])
    d["flow"] = cc.flow(d["change_24h"], oi.get("ch24"), fs.get("apr"))
    d["setups"] = []
    if with_setups and b5:
        scan = load_sibling("crypto-scanner", "scan")
        if scan is not None:
            x = scan.Ctx(m, t, b5, b1h)
            d["setups"] = scan.setups_for(x, cfg.get("session"))
            d["session"] = cfg.get("session") or "utc"
    if "news" in got:
        heads, ok, total = got["news"]
        d["news"] = {"count": len(heads), "feeds_ok": ok, "feeds": total,
                     "items": [{"title": h["title"], "source": h["source"], "at": h["at"], "link": h["link"],
                                "tags": h.get("tags") or [], "important": h.get("important"), "also": h.get("also") or []}
                               for h in heads[:3]]}
    if got.get("macro") is not None:
        d["macro"] = [{"title": e["title"], "country": e["country"], "at": e["at"]} for e in got["macro"]]
    elif "macro" in errors:
        d["macro_error"] = errors.pop("macro")
    if got.get("notices"):
        d["notices"] = [n for n in got["notices"] if m["coin"] in (n.get("coins") or []) and now - n["at"] <= 14 * DAY]
    d["notes"] = notes_for(d, cfg)
    return d


def notes_for(d, cfg):
    out = []
    now = d["as_of"]
    for n in d.get("notices") or []:
        if n.get("kind") == "delistings":
            out.append("! %s announced a delisting %s ago: \"%s\". Positions are closed at the delisting time." % (
                n.get("source", "An exchange"), cc.fmt_dur(now - n["at"]), n["title"]))
        else:
            out.append("%s announced a listing %s ago: \"%s\"." % (n.get("source", "An exchange"), cc.fmt_dur(now - n["at"]),
                                                               n["title"]))
    if d.get("listed_ms") and now - d["listed_ms"] < 30 * DAY:
        out.append("Listed %s ago: little history, so every statistic here is thin." % cc.fmt_dur(now - d["listed_ms"]))
    if (d.get("volume_24h") or 0) < cfg.get("min_quote_volume_usd", 2e7):
        out.append("Thin: 24h volume %s is under the %s floor the scanner uses." %
                   (cc.fmt_usd(d.get("volume_24h")), cc.fmt_usd(cfg.get("min_quote_volume_usd", 2e7))))
    fs = d.get("funding") or {}
    if fs.get("rank") is not None and fs.get("n", 0) >= 20:
        if fs["rank"] >= 0.95:
            out.append("Funding is in the top 5% of the last 30 days: holding a long is unusually expensive.")
        elif fs["rank"] <= 0.05:
            out.append("Funding is in the bottom 5% of the last 30 days: holding a short is unusually expensive.")
    if d.get("high_24h") and d["price"] >= d["high_24h"] * 0.995:
        out.append("Trading at the 24h high.")
    elif d.get("low_24h") and d["price"] <= d["low_24h"] * 1.005:
        out.append("Trading at the 24h low.")
    if d.get("vol_7d") and d.get("vol_30d") and d["vol_7d"] > 1.5 * d["vol_30d"]:
        out.append("This week is far wilder than the month: stops need more room.")
    b = d.get("book")
    if b and b["spread"] > 0.001:
        out.append("Wide spread (%s): market orders lose that much straight away." % cc.fmt_small_pct(b["spread"]))
    if d.get("multiplier", 1) != 1:
        out.append("Priced per %s coins on this venue (%s)." % ("{:,}".format(d["multiplier"]), d["symbol"]))
    return out


TREND_WORD = {"up": "up", "down": "down", "mixed": "mixed", None: "-"}
SETUP_WORDS = {"orb": "opening-range breakout", "breakout": "range breakout", "squeeze": "squeeze", "trend": "trend pullback",
               "reversal": "RSI reversal", "volume": "volume spike", "rs": "relative strength"}


def backtest_lines(setups, session):
    """One line per live setup and side on its record in the published backtest, then where the record is from."""
    seen, out = set(), []
    for h in setups:
        if h["side"] not in cc.SIDES:
            continue
        key = (cc.track_key(h["setup"], session), h["side"])
        if key in seen:
            continue
        seen.add(key)
        w = cc.track_words(h["setup"], h["side"], session)
        if w:
            out.append("%s %ss: %s" % (cc.TRACK_WORDS.get(key[0], h["setup"]), h["side"], w))
    if out:
        out.append("(the walk-forward backtest: %s)" % cc.track_source())
    return out


def print_card(d):
    tick = d.get("tick")
    P = lambda p: cc.fmt_price(p, tick) if p is not None else "-"
    cc.out("%s | %s %s perpetual | %s" % (d["coin"], d["venue_label"].split(" ")[0], d["symbol"], d["as_of_utc"]))
    parts = ["24h %s" % cc.fmt_pct(d["change_24h"])]
    if d.get("change_7d") is not None:
        parts.append("7d %s" % cc.fmt_pct(d["change_7d"]))
    if d.get("change_30d") is not None:
        parts.append("30d %s" % cc.fmt_pct(d["change_30d"]))
    cc.out("Price      %s   %s" % (P(d["price"]), "   ".join(parts)))
    rng = "24h %s to %s" % (P(d.get("low_24h")), P(d.get("high_24h")))
    if d.get("low_7d") is not None:
        rng += "  |  7d %s to %s" % (P(d["low_7d"]), P(d["high_7d"]))
    cc.out("Range      " + rng)
    tr = d["trend"]
    cc.out("Trend      15m %s, 1h %s, 4h %s, 1d %s   (price against its 20 and 50 averages)" %
           tuple(TREND_WORD[tr[k]] for k in ("15m", "1h", "4h", "1d")))
    rs = d["rsi"]
    cc.out("Momentum   RSI 1h %s, 4h %s, 1d %s" % tuple(("%.0f" % rs[k]) if rs.get(k) is not None else "-" for k in ("1h", "4h", "1d")))
    vl = []
    if d.get("atr_1h_pct"):
        vl.append("typical hour %s" % cc.fmt_pct(d["atr_1h_pct"], 2, False))
    if d.get("atr_1d_pct"):
        vl.append("typical day %s" % cc.fmt_pct(d["atr_1d_pct"], 1, False))
    if d.get("vol_7d") and d.get("vol_30d"):
        vl.append("realized %s a year this week vs %s this month" % (cc.fmt_pct(d["vol_7d"], 0, False), cc.fmt_pct(d["vol_30d"], 0, False)))
    if vl:
        cc.out("Volatility " + " | ".join(vl))
    lv = []
    if d.get("resistance"):
        lv.append("resistance " + ", ".join(P(x) for x in d["resistance"]))
    if d.get("support"):
        lv.append("support " + ", ".join(P(x) for x in d["support"]))
    if lv:
        cc.out("Levels     " + "  |  ".join(lv) + "   (1h swing points, 14 days)")
    lv2 = []
    if d.get("vwap_utc_day"):
        lv2.append("VWAP since 00:00 UTC %s" % P(d["vwap_utc_day"]))
    if d.get("prev_day_high"):
        lv2.append("yesterday %s to %s" % (P(d["prev_day_low"]), P(d["prev_day_high"])))
    if d.get("ema20_1d"):
        lv2.append("20-day average %s" % P(d["ema20_1d"]))
    if lv2:
        cc.out("           " + "  |  ".join(lv2))
    vline = "24h %s" % cc.fmt_usd(d.get("volume_24h"))
    if d.get("rvol_24h"):
        vline += " (%.1fx its 7-day average)" % d["rvol_24h"]
    b = d.get("book")
    if b:
        vline += "  |  spread %s, %s%s bids and %s asks within 0.25%%" % (
            cc.fmt_small_pct(b["spread"]), "" if b["reach"] else "at least ", cc.fmt_usd(b["bid_usd"]), cc.fmt_usd(b["ask_usd"]))
    cc.out("Liquidity  " + vline)
    if d.get("impact_10k") is not None:
        if d["impact_10k"] < 0.00001:
            cc.out("           a $10,000 market buy would barely move it (under 0.001% from the mid)")
        else:
            cc.out("           a $10,000 market buy would fill about %s above the mid" % cc.fmt_small_pct(d["impact_10k"]))
    fs = d.get("funding")
    if fs:
        fl = cc.fmt_funding(fs["rate"], fs["hours"])
        if fs.get("next_ms"):
            fl += ", next in %s" % cc.fmt_dur(fs["next_ms"] - d["as_of"])
        if fs.get("mean_apr") is not None:
            fl += "  |  30-day average %s a year" % cc.fmt_pct(fs["mean_apr"], 0)
        cc.out("Funding    " + fl)
    oi = d.get("open_interest") or {}
    pos = d.get("positioning") or {}
    if oi.get("oi_usd") is not None:
        ol = cc.fmt_usd(oi["oi_usd"])
        if oi.get("ch24") is not None:
            ol += ", %s in 24h" % cc.fmt_pct(oi["ch24"], 1)
        if pos.get("long_share") is not None:
            ol += "  |  %d%% of accounts long" % round(pos["long_share"] * 100)
        cc.out("Open int.  " + ol)
    if d.get("crowding"):
        cc.out("           %s: %s" % (d["crowding"], cc.crowd_read(d["crowding"], pos.get("long_share"))))
    elif d.get("flow"):
        cc.out("           %s (%s)" % (d["flow"], cc.FLOW_READ[d["flow"]]))
    mk = []
    if d.get("btc_24h") is not None and d["coin"] != "BTC":
        mk.append("BTC 24h %s" % cc.fmt_pct(d["btc_24h"], 1))
    if d.get("beta") is not None:
        mk.append("moves %.1fx BTC" % d["beta"])
    if d.get("vs_btc_24h") is not None:
        mk.append("%s vs what BTC implied" % cc.fmt_pct(d["vs_btc_24h"], 1))
    if d.get("fng") is not None:
        mk.append("Fear & Greed %d (%s)" % (d["fng"], d["fng_label"]))
    if d.get("regime"):
        mk.append("regime %s" % d["regime"])
    if mk:
        cc.out("Market     " + " | ".join(mk))
    sp = cc.spark(d.get("spark_7d") or [], 28)
    if sp:
        cc.out("7 days     " + sp)
    nw = d.get("news")
    if nw is not None:
        now = d["as_of"]
        if nw["count"]:
            h = nw["items"][0]
            cc.out("News       %s%d headline%s in 24h; latest: \"%s\" (%s, %s ago)%s%s" % (
                "! " if any(i.get("important") for i in nw["items"]) else "", nw["count"], "" if nw["count"] == 1 else "s",
                h["title"], h["source"] + ((" +%d more" % len(h["also"])) if h["also"] else ""), cc.fmt_dur(now - h["at"]),
                (" [%s]" % ", ".join(h["tags"][:3])) if h["tags"] else "", "  /crypto-news has the rest" if nw["count"] > 1 else ""))
        else:
            cc.out("News       no headline naming %s in 24h (%d of %d feeds read)" % (d["coin"], nw["feeds_ok"], nw["feeds"]))
    if d.get("macro") is not None:
        now = d["as_of"]
        ev = ["US %s %s" % (e["title"], ("in %s" % cc.fmt_dur(e["at"] - now)) if e["at"] > now else
                            ("out %s ago" % cc.fmt_dur(now - e["at"]))) for e in d["macro"][:3]]
        cc.out("Macro      " + ("; ".join(ev) if ev else "no high-impact US release in the next 24h"))
    elif d.get("macro_error"):
        cc.out("Macro      the calendar could not be read just now (%s)" % d["macro_error"][:90])
    if d.get("setups"):
        cc.out("Setups now " + "; ".join("%s%s (%s)" % (SETUP_WORDS.get(h["setup"], h["setup"]),
                                                          (" " + h["side"]) if h["side"] in cc.SIDES else "", h["status"])
                                         for h in d["setups"]))
        lines = backtest_lines(d["setups"], d.get("session"))
        for i, line in enumerate(lines):
            cc.out(("Backtest   " if i == 0 else "           ") + line)
    else:
        cc.out("Setups now none of the scanner's six single-coin setups")
    for n in d.get("notes") or []:
        cc.out("Note       " + n)
    if d.get("errors"):
        cc.out("Missing    " + "; ".join("%s (%s)" % (k, v[:60]) for k, v in d["errors"].items()))
    cc.out("Context, not a call. Next: /crypto-exit %s to plan a stop and target." % d["coin"])


def main(argv=None):
    p = argparse.ArgumentParser(prog="coin.py", description="One clean card for any coin.")
    p.add_argument("coin", help="BTC, eth, 1000PEPE, kPEPE, SOLUSDT ...")
    p.add_argument("--no-setups", action="store_true", help="skip the setup check")
    p.add_argument("--no-news", action="store_true", help="skip the headlines and the macro calendar")
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    venue = cc.get_venue(a.venue)
    if getattr(venue, "auto_notes", None):
        cc.out("Using %s (%s)." % (venue.label, "; ".join(venue.auto_notes)))
    d = build(venue, a.coin, cfg, with_setups=not a.no_setups, with_news=not a.no_news)
    if not a.no_write:
        cc.write_json(cc.wpath("coins", "%s.json" % d["symbol"]), d)
    if a.json:
        cc.dump(d)
    else:
        print_card(d)
    if a.stats:
        cc.out("Requests: " + cc.HTTP.summary())
    return 0


if __name__ == "__main__":
    sys.exit(cc.run(main))
