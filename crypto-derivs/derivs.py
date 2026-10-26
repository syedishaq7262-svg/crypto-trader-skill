#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-derivs: who is paying whom. Funding, open interest, positioning and the market's weather.

    python3 derivs.py                 radar across the busiest coins -> crypto/derivs.json
    python3 derivs.py coin FUNDX      one coin in depth
    python3 derivs.py regime          BTC trend, breadth, funding and Fear & Greed -> crypto/regime.json
    python3 derivs.py radar --table   every coin in one table

Funding and open interest describe positioning. They are not signals on their own.
"""

import argparse
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

CROWD_READ = cc.CROWD_READ
FLOW_READ = cc.FLOW_READ
VENUE_NAMES = {"binance": "Binance", "bybit": "Bybit", "hyperliquid": "Hyperliquid"}


def cross_venue():
    """Predicted next funding on Binance, Bybit and Hyperliquid for every coin, from one public request."""
    try:
        return cc.Hyperliquid().predicted_fundings()
    except cc.CryptoError:
        return {}


def spread_row(coin, pf):
    d = pf.get(coin) or {}
    aprs = dict((v, cc.annualize_funding(x["rate"], x["hours"])) for v, x in d.items())
    if len(aprs) < 2:
        return None
    hi = max(aprs.values())
    lo = min(aprs.values())
    return {"coin": coin, "aprs": aprs, "spread": hi - lo}


def radar(a, venue, cfg):
    universe, why = cc.build_universe(venue, cfg, min_volume=a.min_volume, top=a.top or min(venue.default_top, 40),
                                      symbols=a.symbols_list)
    if not universe:
        raise cc.CryptoError("no coins pass the filters")

    def detail(item):
        m, t = item
        oi = cc.oi_change(venue, m["symbol"])
        pos = cc.positioning(venue, m["symbol"]) if venue.has_long_short else {}
        return oi, pos

    workers = 2 if venue.name == "hyperliquid" else 8
    res = cc.parallel(detail, universe, workers=workers)
    rows = []
    failed = []
    for (m, t), r, e in res:
        if e is not None:
            failed.append({"symbol": m["symbol"], "error": str(e)})
            oi, pos = {"oi_usd": t.get("oi_usd"), "ch24": None, "ch7d": None}, {}
        else:
            oi, pos = r
        apr = cc.annualize_funding(t.get("funding"), t.get("funding_hours"))
        ls = (pos or {}).get("long_share")
        row = {"symbol": m["symbol"], "coin": m["coin"], "price": t["last"], "change_24h": t.get("change"),
               "volume_24h": t.get("quote_volume"), "funding": t.get("funding"), "funding_hours": t.get("funding_hours"),
               "funding_apr": apr, "next_funding_ms": t.get("next_funding_ms"),
               "oi_usd": oi.get("oi_usd"), "oi_24h": oi.get("ch24"), "oi_7d": oi.get("ch7d"),
               "long_share": ls, "long_share_24h": (pos or {}).get("long_share_24h"),
               "taker_ratio": (pos or {}).get("taker_ratio")}
        row["crowding"] = cc.crowding(apr, row["oi_24h"], ls, row["change_24h"])
        row["flow"] = None if row["crowding"] in ("crowded longs", "crowded shorts") else \
            cc.flow(row["change_24h"], row["oi_24h"], apr)
        rows.append(row)
    pf = cross_venue()
    spreads = []
    for r in rows:
        sr = spread_row(r["coin"] if venue.name != "hyperliquid" else r["symbol"], pf)
        if sr and sr["spread"] >= a.min_spread / 100.0:
            spreads.append(sr)
    spreads.sort(key=lambda x: -x["spread"])
    aprs = [r["funding_apr"] for r in rows if r["funding_apr"] is not None]
    top10 = rows[:10]
    oi_moves = [r["oi_24h"] for r in top10 if r["oi_24h"] is not None]
    data = {"venue": venue.name, "venue_label": venue.label, "as_of": cc.now_ms(), "as_of_utc": cc.fmt_utc(cc.now_ms()),
            "coins": len(rows), "left_out": why, "failed": failed,
            "avg_funding_apr": cc.mean(aprs), "median_funding_apr": cc.median(aprs),
            "top10_oi_24h": cc.mean(oi_moves), "rows": rows, "spreads": spreads[:10],
            "has_oi_history": venue.has_oi_history, "has_long_short": venue.has_long_short}
    if not a.no_write:
        cc.write_json(cc.wpath("derivs.json"), data)
    if a.json:
        cc.dump(data)
        return 0
    print_radar(data, a)
    if a.stats:
        cc.out("Requests: " + cc.HTTP.summary())
    return 0


def _apr(x):
    return cc.fmt_pct(x, dp=0) if x is not None else "-"


def print_radar(d, a):
    cc.out("Derivatives radar | %s | %s | %d coins" % (d["venue_label"], d["as_of_utc"], d["coins"]))
    avg = d["median_funding_apr"]
    tone = "normal" if avg is None or -0.05 < avg < 0.25 else ("hot" if avg >= 0.25 else "negative")
    line = "Market: median funding %s a year (%s)" % (_apr(avg), tone)
    if d["top10_oi_24h"] is not None:
        line += "; open interest %s in 24h across the 10 busiest coins" % cc.fmt_pct(d["top10_oi_24h"], dp=1)
    cc.out(line + ".")
    if not d["has_oi_history"]:
        cc.out("This venue keeps no open-interest history, so 24h changes and long/short shares are blank.")
    rows = d["rows"]
    crowd = [r for r in rows if r["crowding"]]
    cc.out("")
    cc.out("Crowded trades")
    if not crowd:
        cc.out("  none: funding, open interest and positioning look ordinary")
    order = ["crowded longs", "crowded shorts", "hot funding", "deeply negative funding"]
    for r in sorted(crowd, key=lambda r: (order.index(r["crowding"]), -abs(r["funding_apr"] or 0))):
        bits = ["funding %s a year" % _apr(r["funding_apr"])]
        if r["oi_24h"] is not None:
            bits.append("open interest %s in 24h" % cc.fmt_pct(r["oi_24h"], dp=1))
        if r["long_share"] is not None:
            bits.append("%d%% of accounts long" % round(r["long_share"] * 100))
        bits.append("price %s" % cc.fmt_pct(r["change_24h"], dp=1))
        cc.out("  %-8s %-24s %s" % (r["coin"], r["crowding"], ", ".join(bits)))
        cc.out("  %-8s %s" % ("", CROWD_READ[r["crowding"]]))
    cc.out("")
    cc.out("Price against open interest, 24h")
    any_flow = False
    for f in ("new longs", "shorts adding into a rise", "short covering", "new shorts", "longs adding into a drop", "long unwinding"):
        xs = [r for r in rows if r["flow"] == f]
        if not xs:
            continue
        any_flow = True
        xs.sort(key=lambda r: -abs(r["oi_24h"] or 0))
        cc.out("  %-25s (%s): %s" % (f, FLOW_READ[f], ", ".join("%s %s/%s" % (r["coin"], cc.fmt_pct(r["change_24h"], 1),
                                                                             cc.fmt_pct(r["oi_24h"], 1)) for r in xs[:6])))
    if not any_flow:
        cc.out("  no coin moved more than 1.5% with a 5% change in open interest" if d["has_oi_history"] else "  not available here")
    fr = sorted([r for r in rows if r["funding_apr"] is not None], key=lambda r: -r["funding_apr"])
    if fr:
        cc.out("")
        cc.out("Funding, a year: highest  " + "  ".join("%s %s" % (r["coin"], _apr(r["funding_apr"])) for r in fr[:5]))
        cc.out("                 lowest   " + "  ".join("%s %s" % (r["coin"], _apr(r["funding_apr"])) for r in fr[::-1][:5]))
    if d["spreads"]:
        cc.out("")
        cc.out("Same coin, different venues (next funding, a year)")
        for sp in d["spreads"][:5]:
            parts = ["%s %s" % (VENUE_NAMES.get(v, v), _apr(x)) for v, x in sorted(sp["aprs"].items())]
            cc.out("  %-8s %s   spread %d points" % (sp["coin"], " | ".join(parts), round(sp["spread"] * 100)))
    if a.table:
        cc.out("")
        tab = []
        for r in rows:
            tab.append([r["coin"], cc.fmt_price(r["price"]), cc.fmt_pct(r["change_24h"], 1), _apr(r["funding_apr"]),
                        cc.fmt_usd(r["oi_usd"]), cc.fmt_pct(r["oi_24h"], 1) if r["oi_24h"] is not None else "-",
                        ("%d%%" % round(r["long_share"] * 100)) if r["long_share"] is not None else "-", r["flow"] or ""])
        cc.out(cc.table(tab, ["coin", "price", "24h", "funding/yr", "open int.", "OI 24h", "long", "flow"], "lrrrrrrl"))
    if d["failed"]:
        cc.out("")
        cc.out("Could not read %d coin(s): %s" % (len(d["failed"]), ", ".join(f["symbol"] for f in d["failed"][:6])))
    cc.out("")
    cc.out("Positioning is context, not a signal. Crowded trades can stay crowded for days.")


def coin(a, venue, cfg):
    if not a.target:
        raise cc.CryptoError("which coin? For example: derivs.py coin BTC")
    m = venue.market(a.target)
    sym = m["symbol"]
    t = venue.ticker(sym)
    fs = cc.funding_stats(venue, sym, days=30)
    oi = cc.oi_change(venue, sym)
    pos = cc.positioning(venue, sym)
    pf = cross_venue()
    key = m["coin"] if venue.name != "hyperliquid" else sym
    sr = spread_row(key, pf)
    basis = (t["mark"] / t["index"] - 1.0) if (t.get("mark") and t.get("index")) else None
    crowd = cc.crowding(fs["apr"], oi.get("ch24"), pos.get("long_share"), t.get("change"))
    data = {"venue": venue.name, "symbol": sym, "coin": m["coin"], "as_of": cc.now_ms(), "price": t["last"],
            "mark": t.get("mark"), "index": t.get("index"), "basis": basis, "change_24h": t.get("change"),
            "funding": fs, "open_interest": dict((k, v) for k, v in oi.items() if k != "series"),
            "positioning": pos, "venues": sr, "crowding": crowd,
            "flow": cc.flow(t.get("change"), oi.get("ch24"), fs["apr"])}
    if a.json:
        cc.dump(data)
        return 0
    now = cc.now_ms()
    cc.out("%s derivatives | %s %s | %s" % (m["coin"], venue.label, sym, cc.fmt_utc(now)))
    nxt = fs.get("next_ms")
    line = "Funding    %s now" % cc.fmt_funding(fs["rate"], fs["hours"])
    if nxt:
        line += ". Next payment in %s (%s)" % (cc.fmt_dur(nxt - now), cc.fmt_local(nxt, date=False))
    cc.out(line + ".")
    if fs["n"]:
        rank = fs["rank"]
        where = ("in the top %d%%" % max(1, round((1 - rank) * 100))) if rank is not None and rank >= 0.5 else \
                ("in the bottom %d%%" % max(1, round(rank * 100))) if rank is not None else "unranked"
        cc.out("           30 days: average %s a year; today is %s of %d payments; 7-day average %s; negative %d times."
               % (_apr(fs["mean_apr"]), where, fs["n"], _apr(fs["mean7_apr"]), fs["negative"]))
    oi_line = "Open interest  %s" % cc.fmt_usd(oi.get("oi_usd"))
    if oi.get("ch24") is not None:
        oi_line += ", %s in 24h" % cc.fmt_pct(oi["ch24"], 1)
    if oi.get("ch7d") is not None:
        oi_line += ", %s in 7 days" % cc.fmt_pct(oi["ch7d"], 1)
    if oi.get("series"):
        oi_line += "  " + cc.spark(oi["series"], 24)
    cc.out(oi_line + ".")
    if pos.get("long_share") is not None:
        pl = "Positioning  %d%% of accounts long" % round(pos["long_share"] * 100)
        if pos.get("long_share_24h") is not None:
            pl += " (a day ago %d%%)" % round(pos["long_share_24h"] * 100)
        if pos.get("top_long_share") is not None:
            pl += "; top traders %d%% long" % round(pos["top_long_share"] * 100)
        if pos.get("taker_ratio") is not None:
            pl += "; taker buy/sell %.2f" % pos["taker_ratio"]
        cc.out(pl + ".")
    elif not venue.has_long_short:
        cc.out("Positioning  not published by %s." % venue.label)
    if basis is not None:
        cc.out("Basis  mark %s vs index %s (%s)." % (cc.fmt_price(t["mark"], m.get("tick")), cc.fmt_price(t["index"], m.get("tick")),
                                                   cc.fmt_pct(basis, 3)))
    if sr:
        parts = ["%s %s" % (VENUE_NAMES.get(v, v), _apr(x)) for v, x in sorted(sr["aprs"].items())]
        cc.out("Across venues (next funding, a year): %s." % ", ".join(parts))
    if crowd:
        cc.out("Read: %s. %s" % (crowd, CROWD_READ[crowd]))
    else:
        fl = data["flow"]
        cc.out("Read: %s." % ("%s (%s)" % (fl, FLOW_READ[fl]) if fl else "nothing unusual in funding or positioning"))
    return 0


def regime(a, venue, cfg):
    r = cc.market_regime(venue, cfg)
    r["venue"] = venue.name
    r["as_of"] = cc.now_ms()
    if not a.no_write:
        cc.write_json(cc.wpath("regime.json"), r)
    if a.json:
        cc.dump(r)
        return 0
    print_regime(r, venue)
    return 0


def print_regime(r, venue):
    cc.out("Market regime | %s | %s" % (venue.label, cc.fmt_utc(r["as_of"])))
    tr = {1: "uptrend (above a rising 20-day average)", -1: "downtrend (below a falling 20-day average)", 0: "no clear trend"}[r["trend"]]
    line = "BTC      %s" % cc.fmt_price(r["btc"])
    if r.get("btc_7d") is not None:
        line += ", 7 days %s" % cc.fmt_pct(r["btc_7d"], 1)
    if r.get("btc_30d") is not None:
        line += ", 30 days %s" % cc.fmt_pct(r["btc_30d"], 1)
    cc.out(line + "; " + tr + ".")
    if r.get("vol7") and r.get("vol30"):
        calm = "calmer than" if r["vol7"] < r["vol30"] * 0.85 else "wilder than" if r["vol7"] > r["vol30"] * 1.15 else "about"
        cc.out("Volatility  BTC moved %s a year over 7 days vs %s over 30 (%s usual)." %
               (cc.fmt_pct(r["vol7"], 0, False), cc.fmt_pct(r["vol30"], 0, False), calm))
    if r.get("breadth") is not None:
        cc.out("Breadth  %d%% of the %d busiest coins are above their 20-day average." % (round(r["breadth"] * 100), r["breadth_n"]))
    if r.get("funding_apr") is not None:
        cc.out("Funding  average %s a year across the 20 busiest coins." % _apr(r["funding_apr"]))
    if r.get("fng") is not None:
        fl = "Fear & Greed  %d (%s)" % (r["fng"], r["fng_label"])
        if r.get("fng_7d") is not None:
            fl += ", a week ago %d" % r["fng_7d"]
        cc.out(fl + ".")
    cc.out("Regime: %s. %s" % (r["label"], r["read"]))
    cc.out("This is a rough read of conditions, not a forecast.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="derivs.py", description="Funding, open interest, positioning and market regime.")
    p.add_argument("command", nargs="?", default="radar", choices=["radar", "coin", "regime"])
    p.add_argument("target", nargs="?", default=None, help="the coin for 'coin'")
    p.add_argument("--symbols", default=None)
    p.add_argument("--top", type=int, default=None, help="radar: the N busiest coins (default 40)")
    p.add_argument("--min-volume", type=float, default=None)
    p.add_argument("--min-spread", type=float, default=20.0, help="cross-venue funding spread to list, points a year (default 20)")
    p.add_argument("--table", action="store_true", help="radar: also print every coin")
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    a.symbols_list = [s for s in (a.symbols or "").split(",") if s.strip()] or None
    venue = cc.get_venue(a.venue)
    if getattr(venue, "auto_notes", None):
        cc.out("Using %s (%s)." % (venue.label, "; ".join(venue.auto_notes)))
    if a.command == "coin":
        return coin(a, venue, cfg)
    if a.command == "regime":
        return regime(a, venue, cfg)
    return radar(a, venue, cfg)


if __name__ == "__main__":
    sys.exit(cc.run(main))
