#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-pulse: what is moving in crypto today, and what is behind it. Not what to buy.

    python3 pulse.py                 the busiest perpetuals on the best venue from here -> crypto/pulse.json
    python3 pulse.py --top 100       more coins
    python3 pulse.py --n 8           longer lists
    python3 pulse.py --no-news       skip the headlines, the calendar and the exchange notices

One screen: the market's mood (BTC, how many coins are up, funding, Fear & Greed), the next big US releases, the
coins up and down the most in 24 hours with their volume against a usual day, the change in open interest and
what it says about who is buying or selling, funding, the headlines naming them, volume surges, crowded trades,
the coins most in the news, new listings and exchange notices, and the pack's backtest record for chasing moves.
Context, not a call.
"""

import argparse
import importlib.util
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

T, C, QV = cc.T, cc.C, cc.QV
HOUR = 3600000
DAY = 86400000
SURGE = 2.0                    # volume this many times a usual day is a surge
NEW_DAYS = 14                  # a listing this young is new
NOTICE_HOURS = 72
HEADLINE_CHARS = 72
CHASES = (("volume", "long", "buying volume spikes"), ("breakout", "long", "buying range breakouts"),
          ("volume", "short", "shorting volume spikes down"), ("breakout", "short", "shorting breakdowns"))


def load_sibling(folder, name):
    """Use another skill's script when it is installed next to this one; None otherwise."""
    path = os.path.join(HERE, os.pardir, folder, name + ".py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("crypto_pulse_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


# ---------------------------------------------------------------- reading the market

def daily(venue, m, t):
    """Volume against a usual day (the last 24 hours over the average of the 7 whole days before) and the 7-day change."""
    bars = venue.bars(m["symbol"], "1d", 9)
    days = [b[QV] for b in bars[-7:]]
    usual = (sum(days) / len(days)) if len(days) >= 3 else None
    ch7 = None
    if len(bars) >= 8 and bars[-8][C]:                  # the close a week before today's UTC midnight
        ch7 = t["last"] / bars[-8][C] - 1.0
    rvol = (t.get("quote_volume") / usual) if (usual and t.get("quote_volume") is not None) else None
    return {"rvol": rvol, "usual_day": usual, "ch7d": ch7}


def headlines(news, cfg, known, now):
    """{coin: [stories naming it, newest first]} from the last 24 hours, each story once; and how many feeds answered."""
    items, sources = news.read_feeds(news.feeds_from(cfg), retries=0)
    fresh = [it for it in items if it.get("at") and now - DAY <= it["at"] <= now + 10 * 60000]
    for it in fresh:
        it["coins"] = news.coins_in(it["title"] + " " + (it.get("summary") or "")[:200], known)
        it["tags"] = news.tags_of(it["title"] + " " + (it.get("summary") or ""))
    by_coin = {}
    for it in news.cluster(fresh):
        if it.get("dupe_of"):
            continue
        for c in it["coins"]:
            by_coin.setdefault(c, []).append({"title": it["title"], "source": it["source"], "at": it["at"],
                                              "link": it.get("link"), "also": list(it.get("also") or []),
                                              "tags": it.get("tags") or []})
    for c in by_coin:
        by_coin[c].sort(key=lambda h: -h["at"])
    return by_coin, sum(1 for s in sources if s["ok"]), len(sources)


def chase_record():
    """The backtest's record for the setups that chase a move: volume spikes and range breakouts, each side."""
    tr = cc.track_record()
    if not tr:
        return None
    rows = []
    for setup, side, words in CHASES:
        e = cc.track_entry(setup, side)
        if e and e.get("n"):
            rows.append({"setup": setup, "side": side, "words": words, "n": e["n"], "avg_r": e["avg_r"],
                         "lo": e.get("lo"), "hi": e.get("hi")})
    return {"source": cc.track_source(tr), "trades": (tr.get("all") or {}).get("n"), "rows": rows} if rows else None


def gather(venue, cfg, top, n, with_news=True):
    now = cc.now_ms()
    universe, left_out = cc.build_universe(venue, cfg, top=top)
    if not universe:
        raise cc.CryptoError("no coins pass the volume and listing filters on %s" % venue.label)
    workers = 2 if venue.name == "hyperliquid" else 8
    res = cc.parallel(lambda x: daily(venue, x[0], x[1]), universe, workers=workers)
    rows, failed = [], []
    for (m, t), r, e in res:
        if e is not None:
            failed.append({"coin": m["coin"], "error": str(e)})
            r = {"rvol": None, "usual_day": None, "ch7d": None}
        rows.append({"coin": m["coin"], "symbol": m["symbol"], "tick": m.get("tick"), "price": t["last"],
                     "change": t.get("change"), "volume_24h": t.get("quote_volume"), "rvol": r["rvol"],
                     "usual_day": r["usual_day"], "ch7d": r["ch7d"], "funding": t.get("funding"),
                     "funding_hours": t.get("funding_hours"),
                     "funding_apr": cc.annualize_funding(t.get("funding"), t.get("funding_hours")),
                     "oi_usd": t.get("oi_usd"), "oi_24h": None, "read": None})
    by_coin = dict((r["coin"], r) for r in rows)
    moving = [r for r in rows if r["change"] is not None]
    up = sorted([r for r in moving if r["change"] > 0], key=lambda r: -r["change"])[:n]
    down = sorted([r for r in moving if r["change"] < 0], key=lambda r: r["change"])[:n]
    shown = set(r["coin"] for r in up + down)
    surges = sorted([r for r in rows if (r["rvol"] or 0) >= SURGE and r["coin"] not in shown], key=lambda r: -r["rvol"])[:n]
    hot = [r for r in rows if r["funding_apr"] is not None and abs(r["funding_apr"]) >= 0.30]
    short = []
    for r in up + down + surges + hot:
        if r not in short:
            short.append(r)
    if venue.has_oi_history and short:
        got = cc.parallel(lambda r: cc.oi_change(venue, r["symbol"]), short, workers=workers)
        for r, oi, e in got:
            if e is None and oi:
                r["oi_24h"] = oi.get("ch24")
                if oi.get("oi_usd"):
                    r["oi_usd"] = oi["oi_usd"]
    for r in rows:
        r["crowding"] = cc.crowding(r["funding_apr"], r["oi_24h"], None, r["change"])
        r["flow"] = None if r["crowding"] in ("crowded longs", "crowded shorts") else cc.flow(r["change"], r["oi_24h"], r["funding_apr"])
        r["read"] = r["crowding"] or r["flow"]
    crowded = sorted([r for r in rows if r["crowding"]], key=lambda r: -abs(r["funding_apr"] or 0))[:n]

    btc = by_coin.get("BTC")
    if btc is None:
        try:
            bm = venue.market("BTC")
            bt = venue.ticker(bm["symbol"])
            btc = {"coin": "BTC", "price": bt["last"], "tick": bm.get("tick"), "change": bt.get("change")}
            btc.update(daily(venue, bm, bt))
        except cc.CryptoError:
            btc = None
    ups = sum(1 for r in moving if r["change"] > 0)
    busiest = sorted(rows, key=lambda r: -(r["volume_24h"] or 0))[:20]
    aprs = [r["funding_apr"] for r in busiest if r["funding_apr"] is not None]
    mood = {"btc": btc and {"price": btc["price"], "tick": btc.get("tick"), "change": btc.get("change"),
                            "ch7d": btc.get("ch7d")},
            "up": ups, "of": len(moving), "funding_apr": cc.median(aprs), "funding_n": len(aprs)}
    try:
        fg = cc.fear_greed(8)
        if fg:
            mood["fng"], mood["fng_label"] = fg[0][1], fg[0][2]
            mood["fng_7d"] = fg[7][1] if len(fg) > 7 else None
    except cc.CryptoError as e:
        mood["fng_error"] = str(e)

    known = set(m["coin"] for m in venue.markets())
    newest = []
    tk = venue.tickers()
    for m in venue.markets():
        if m.get("listed_ms") and 0 <= now - m["listed_ms"] <= NEW_DAYS * DAY and m["coin"] not in cc.STABLES:
            t = tk.get(m["symbol"])
            if t:
                newest.append({"coin": m["coin"], "symbol": m["symbol"], "listed_ms": m["listed_ms"],
                               "change": t.get("change"), "volume_24h": t.get("quote_volume")})
    newest.sort(key=lambda x: -x["listed_ms"])

    d = {"venue": venue.name, "venue_label": venue.label, "as_of": now, "as_of_utc": cc.fmt_utc(now), "coins": len(rows),
         "left_out": left_out, "failed": failed, "mood": mood, "up": up, "down": down, "surges": surges,
         "crowded": crowded, "new_listings": newest[:n], "chase": chase_record(), "news": None, "macro": None,
         "notices": None, "has_oi_history": venue.has_oi_history}
    news = load_sibling("crypto-news", "news") if with_news else None
    if news is not None:
        jobs = {"news": lambda: headlines(news, cfg, known, now), "macro": lambda: news.macro_soon(48.0),
                "notices": lambda: news.notices(known)}
        got = cc.parallel(lambda k: jobs[k](), list(jobs), workers=3)
        for k, r, e in got:
            if e is not None:
                d[k + "_error"] = str(e)
                continue
            if k == "news":
                by, ok, total = r
                d["news"] = {"feeds_ok": ok, "feeds": total,
                             "most": sorted(([c, len(v)] for c, v in by.items()), key=lambda x: (-x[1], x[0]))[:n]}
                for row in up + down + surges + crowded:
                    hs = by.get(row["coin"]) or []
                    row["headline"] = hs[0] if hs else None
                    row["stories"] = len(hs)
            elif k == "macro":
                d["macro"] = [{"title": e_["title"], "country": e_["country"], "at": e_["at"]} for e_ in r]
            else:
                notes, _ = r
                d["notices"] = [x for x in notes if 0 <= now - x["at"] <= NOTICE_HOURS * HOUR][:n]
    return d


# ---------------------------------------------------------------- printing

def _apr(x):
    return cc.fmt_pct(x, dp=0) if x is not None else "-"


def _ago(ms, now):
    return ("in " + cc.fmt_dur(ms - now)) if ms > now else (cc.fmt_dur(now - ms) + " ago")


def _short(text, n=HEADLINE_CHARS):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def mover_table(rows, oi):
    tab = []
    for r in rows:
        tab.append([r["coin"], cc.fmt_price(r["price"], r.get("tick")), cc.fmt_pct(r["change"], 1),
                    ("%.1fx" % r["rvol"]) if r["rvol"] is not None else "-",
                    cc.fmt_pct(r["oi_24h"], 1) if r["oi_24h"] is not None else "-", _apr(r["funding_apr"]),
                    r["read"] or ""])
    head = ["coin", "price", "24h", "volume vs usual", "open int. 24h" if oi else "open int.", "funding/yr", "what it says"]
    return ["  " + line for line in cc.table(tab, head, "lrrrrrl").splitlines()]


def mood_lines(d):
    m = d["mood"]
    bits = []
    b = m.get("btc")
    if b:
        s = "BTC %s, %s in 24h" % (cc.fmt_price(b["price"], b.get("tick")), cc.fmt_pct(b["change"], 1))
        if b.get("ch7d") is not None:
            s += ", %s in 7 days" % cc.fmt_pct(b["ch7d"], 1)
        bits.append(s)
    bits.append("%d of the %d coins up in 24h" % (m["up"], m["of"]))
    if m.get("funding_apr") is not None:
        who = "longs pay shorts" if m["funding_apr"] > 0 else "shorts pay longs" if m["funding_apr"] < 0 else "nobody pays"
        bits.append("median funding %s a year (%s)" % (_apr(m["funding_apr"]), who))
    if m.get("fng") is not None:
        s = "Fear & Greed %d, %s" % (m["fng"], m["fng_label"])
        if m.get("fng_7d") is not None:
            s += " (%d a week ago)" % m["fng_7d"]
        bits.append(s)
    return ["Mood: %s." % "; ".join(bits)]


def chase_lines(d):
    ch = d.get("chase")
    out = ["What is moving is not a reason to buy it."]
    if not ch:
        return out
    rows = dict(((r["setup"], r["side"]), r) for r in ch["rows"])

    def part(setup, side):
        r = rows.get((setup, side))
        if not r:
            return None
        return "%s %s a trade over %s%s" % (r["words"], cc.fmt_r(r["avg_r"]), "{:,}".format(r["n"]), cc.zero_note(r))
    longs = [p for p in (part("volume", "long"), part("breakout", "long")) if p]
    shorts = [p for p in (part("volume", "short"), part("breakout", "short")) if p]
    s = "In the pack's walk-forward backtest (%s), after costs: %s" % (ch["source"], "; ".join(longs + shorts))
    out[0] += " " + s + "."
    out.append("To test a chase of your own before risking anything: /crypto-idealab, for example change(24) > 10 "
               "on 1h bars (up 10% in a day).")
    return out


def print_pulse(d):
    now = d["as_of"]
    cc.out("Market Pulse | %s | %s | the %d busiest perpetuals" % (d["venue_label"], d["as_of_utc"], d["coins"]))
    for line in mood_lines(d):
        cc.out(line)
    if d.get("macro") is not None:
        ev = [e for e in d["macro"] if e["at"] >= now - 2 * HOUR]
        if ev:
            cc.out("Calendar: %s." % "; ".join("%s %s %s" % (e["country"].replace("USD", "US"), e["title"], _ago(e["at"], now))
                                               for e in ev[:3]))
        else:
            cc.out("Calendar: no high-impact US release in the next 48 hours.")
    elif d.get("macro_error"):
        cc.out("Calendar: not read just now (%s)." % d["macro_error"][:80])
    oi = d["has_oi_history"]
    for title, rows in (("Up most in 24h", d["up"]), ("Down most in 24h", d["down"])):
        cc.out("")
        cc.out(title)
        if rows:
            for line in mover_table(rows, oi):
                cc.out(line)
        else:
            cc.out("  none")
    if not oi:
        cc.out("  This venue keeps no open-interest history, so the 24h change and what it says are blank.")
    if d.get("news") is not None:
        named = [r for r in d["up"] + d["down"] if r.get("headline")]
        if named:
            cc.out("")
            cc.out("Headlines naming them, last 24h")
            for r in named:
                h = r["headline"]
                more = (" +%d more" % (r["stories"] - 1)) if r["stories"] > 1 else ""
                cc.out("  %-6s \"%s\" (%s, %s)%s" % (r["coin"], _short(h["title"]), h["source"], _ago(h["at"], now), more))
            quiet = [r["coin"] for r in d["up"] + d["down"] if not r.get("headline")]
            if quiet:
                cc.out("  No headline names %s: a move with no news is often flows, liquidations or a thin book." %
                       ", ".join(quiet))
    elif d.get("news_error"):
        cc.out("  Headlines not read just now (%s)." % d["news_error"][:80])
    cc.out("")
    if d["surges"]:
        cc.out("Volume far above a usual day (not in the lists above): %s." % ", ".join(
            "%s %.1fx (%s)" % (r["coin"], r["rvol"], cc.fmt_pct(r["change"], 1)) for r in d["surges"]))
    else:
        cc.out("Volume far above a usual day: none beyond the coins above.")
    if d["crowded"]:
        for r in d["crowded"]:
            bits = ["funding %s a year" % _apr(r["funding_apr"])]
            if r["oi_24h"] is not None:
                bits.append("open interest %s in 24h" % cc.fmt_pct(r["oi_24h"], 1))
            bits.append("price %s" % cc.fmt_pct(r["change"], 1))
            cc.out("Crowded: %s, %s (%s). %s" % (r["coin"], r["crowding"], ", ".join(bits), cc.crowd_read(r["crowding"])))
    else:
        cc.out("Crowded: nothing stands out in funding and open interest.")
    nw = d.get("news")
    if nw is not None:
        if nw["most"]:
            cc.out("Most in the news, 24h (%d of %d newsrooms read): %s." % (
                nw["feeds_ok"], nw["feeds"], ", ".join("%s %d stor%s" % (c, k, "y" if k == 1 else "ies") for c, k in nw["most"])))
        else:
            cc.out("Most in the news, 24h: no headline named a listed coin (%d of %d newsrooms read)." % (nw["feeds_ok"], nw["feeds"]))
    if d["new_listings"]:
        cc.out("New on %s: %s." % (d["venue_label"].split(" ")[0], "; ".join(
            "%s, listed %s ago, %s in 24h, %s traded" % (x["coin"], cc.fmt_dur(now - x["listed_ms"]),
                                                          cc.fmt_pct(x["change"], 1), cc.fmt_usd(x["volume_24h"]))
            for x in d["new_listings"])))
    if d.get("notices"):
        cc.out("Exchange notices (Bybit, last %dh): %s." % (NOTICE_HOURS, "; ".join(
            "%s (%s)" % (_short(x["title"], 64), _ago(x["at"], now)) for x in d["notices"])))
    if d["failed"]:
        cc.out("Could not read %d coin%s: %s." % (len(d["failed"]), "" if len(d["failed"]) == 1 else "s",
                                                 ", ".join(f["coin"] for f in d["failed"][:6])))
    cc.out("")
    for line in chase_lines(d):
        cc.out(line)


def main(argv=None):
    p = argparse.ArgumentParser(prog="pulse.py", description="What is moving in crypto today, and what is behind it.")
    p.add_argument("--top", type=int, default=None, help="how many of the busiest perpetuals to read (default 60; 30 on Hyperliquid)")
    p.add_argument("--n", type=int, default=5, help="coins in each list (default 5)")
    p.add_argument("--no-news", action="store_true", help="skip the headlines, the calendar and the exchange notices")
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    venue = cc.get_venue(a.venue)
    top = a.top or (30 if venue.name == "hyperliquid" else 60)
    if not 5 <= top <= 300:
        raise cc.CryptoError("--top must be between 5 and 300")
    if not 1 <= a.n <= 20:
        raise cc.CryptoError("--n must be between 1 and 20")
    d = gather(venue, cfg, top, a.n, with_news=not a.no_news)
    if not a.no_write:
        cc.write_json(cc.wpath("pulse.json"), d)
    if a.json:
        cc.dump(d)
        return 0
    print_pulse(d)
    if a.stats:
        cc.out("Requests: " + cc.HTTP.summary())
    return 0


if __name__ == "__main__":
    sys.exit(cc.run(main))
