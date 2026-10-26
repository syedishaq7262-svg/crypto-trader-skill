#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-whales: what the best Hyperliquid traders hold, read from the public chain.

    python3 whales.py top                         the leaderboard's best this month (accounts over $100K)
    python3 whales.py top --window week --n 30
    python3 whales.py coin BTC                    who among the top 50 holds BTC, long or short, and how big
    python3 whales.py wallet 0xABC...             one trader's positions and latest fills
    python3 whales.py follow 0xABC... --name ace  keep an eye on a wallet
    python3 whales.py changes                     what the wallets you follow opened, closed, added or cut since last time

Every Hyperliquid account is public. That makes this context, not a signal: these
traders entered earlier, at other prices, with other sizes and time frames, and
some hedge elsewhere. The leaderboard only shows the ones who are still winning.
"""

import argparse
import os
import re
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

MIN, HOUR, DAY = 60000, 3600000, 86400000
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
BOARD_CACHE_MS = 6 * HOUR
STATE_CACHE_MS = 2 * MIN
KEEP_ROWS = 3000
WINDOWS = ("day", "week", "month", "allTime")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CHANGE_SHARE = 0.2                   # a position grew or shrank by at least a fifth to count as added or cut


def short(addr):
    return addr[:6] + ".." + addr[-4:]


def who(row):
    return (row.get("name") or short(row["address"]))[:24]


def num(x, default=None):
    return cc.fnum(x, default)


# ---------------------------------------------------------------- the leaderboard

def parse_board(data):
    """Rows the leaderboard sends, kept only when every number reads. Odd rows are skipped, not fatal."""
    rows = data.get("leaderboardRows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise cc.CryptoError("the Hyperliquid leaderboard answered something unexpected")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        addr = str(r.get("ethAddress") or "")
        acct = num(r.get("accountValue"))
        if not ADDRESS.match(addr) or acct is None:
            continue
        perf = {}
        for w in r.get("windowPerformances") or []:
            if isinstance(w, (list, tuple)) and len(w) == 2 and isinstance(w[1], dict) and w[0] in WINDOWS:
                perf[w[0]] = {"pnl": num(w[1].get("pnl"), 0.0), "roi": num(w[1].get("roi"), 0.0), "vlm": num(w[1].get("vlm"), 0.0)}
        if "month" not in perf or "allTime" not in perf:
            continue
        name = r.get("displayName")
        out.append({"address": addr.lower(), "name": str(name)[:40] if name else None, "account": acct, "perf": perf})
    return out


def board():
    path = cc.wpath("cache", "hl_leaderboard.json")
    saved = cc.read_json(path, None)
    now = cc.now_ms()
    if saved and 0 <= now - saved.get("at", 0) <= BOARD_CACHE_MS:
        return saved["rows"], saved["at"], None
    try:
        rows = parse_board(cc.HTTP.request("GET", LEADERBOARD_URL, weight=20))
    except cc.CryptoError as e:
        if saved:
            return saved["rows"], saved["at"], "%s (showing the leaderboard from %s)" % (e, cc.fmt_local(saved["at"]))
        raise
    keep = {}
    for key in ("month", "allTime", "week"):
        for r in sorted(rows, key=lambda r: -r["perf"].get(key, {}).get("pnl", 0.0))[:KEEP_ROWS]:
            keep[r["address"]] = r
    rows = list(keep.values())
    cc.write_json(path, {"at": now, "rows": rows, "total": len(rows)})
    return rows, now, None


def ranked(rows, window, min_account):
    out = [r for r in rows if r["account"] >= min_account and r["perf"].get(window, {}).get("pnl", 0.0) > 0]
    out.sort(key=lambda r: -r["perf"][window]["pnl"])
    for r in out:
        r["steady"] = all(r["perf"].get(w, {}).get("pnl", 0.0) > 0 for w in ("week", "month", "allTime"))
    return out


# ---------------------------------------------------------------- wallets

def info(body, weight=2):
    return cc.HTTP.request("POST", INFO_URL, body=body, weight=weight)


def coin_of(name, venue):
    """'kBONK' -> BONK, 'BTC' -> BTC; HIP-3 names ('dex:XYZ') and spot ('@107') are skipped."""
    if ":" in name or name.startswith("@"):
        return None
    try:
        return venue.market(name)["coin"] if venue else cc.clean_coin(name)
    except cc.CryptoError:
        return cc.clean_coin(name)


def wallet_state(addr, venue=None, fresh=False):
    path = cc.wpath("cache", "hl_state", addr + ".json")
    saved = cc.read_json(path, None)
    now = cc.now_ms()
    if saved and not fresh and 0 <= now - saved.get("at", 0) <= STATE_CACHE_MS:
        return saved["state"]
    raw = info({"type": "clearinghouseState", "user": addr})
    if not isinstance(raw, dict):
        raise cc.CryptoError("Hyperliquid answered something unexpected for %s" % short(addr))
    positions = []
    for ap in raw.get("assetPositions") or []:
        p = (ap or {}).get("position") or {}
        szi = num(p.get("szi"), 0.0)
        name = str(p.get("coin") or "")
        coin = coin_of(name, venue)
        if not szi or not coin:
            continue
        lev = p.get("leverage") or {}
        positions.append({"coin": coin, "symbol": name, "side": "long" if szi > 0 else "short", "size": abs(szi),
                          "notional": abs(num(p.get("positionValue"), 0.0)), "entry": num(p.get("entryPx")),
                          "liq": num(p.get("liquidationPx")), "upnl": num(p.get("unrealizedPnl"), 0.0),
                          "roe": num(p.get("returnOnEquity"), 0.0), "leverage": num(lev.get("value")),
                          "margin_mode": lev.get("type"), "funding_since_open": num((p.get("cumFunding") or {}).get("sinceOpen"), 0.0)})
    ms = raw.get("marginSummary") or {}
    state = {"address": addr, "at": now, "account": num(ms.get("accountValue"), 0.0),
             "margin_used": num(ms.get("totalMarginUsed"), 0.0), "notional": num(ms.get("totalNtlPos"), 0.0),
             "positions": sorted(positions, key=lambda p: -p["notional"])}
    cc.write_json(path, {"at": now, "state": state})
    return state


def wallet_fills(addr, since):
    raw = info({"type": "userFillsByTime", "user": addr, "startTime": int(since), "aggregateByTime": True}, weight=20)
    out = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        t = f.get("time")
        try:
            t = int(t)
        except (TypeError, ValueError):
            continue
        out.append({"at": t, "coin": str(f.get("coin") or ""), "dir": str(f.get("dir") or f.get("side") or ""),
                    "px": num(f.get("px")), "sz": num(f.get("sz")), "closed_pnl": num(f.get("closedPnl"), 0.0),
                    "fee": num(f.get("fee"), 0.0)})
    out.sort(key=lambda f: -f["at"])
    return out


def hl_venue():
    try:
        return cc.get_venue("hyperliquid")
    except cc.CryptoError:
        return None


# ---------------------------------------------------------------- commands

def cmd_top(a):
    rows, at, note = board()
    top = ranked(rows, a.window, a.min_account)[:a.n]
    if a.json:
        cc.dump({"as_of": at, "window": a.window, "min_account": a.min_account, "rows": top, "note": note})
        return 0 if top else 1
    cc.out("Hyperliquid leaderboard | best %s | accounts over %s | read %s" % (
        {"allTime": "all time"}.get(a.window, "this " + a.window if a.window != "day" else "today"), cc.fmt_usd(a.min_account),
        cc.fmt_local(at)))
    tab = []
    for i, r in enumerate(top, 1):
        p = r["perf"]
        tab.append([str(i), who(r), cc.fmt_usd(r["account"]), cc.fmt_usd(p[a.window]["pnl"]), cc.fmt_pct(p[a.window]["roi"], 1),
                    cc.fmt_usd(p["allTime"]["pnl"]), "yes" if r["steady"] else ""])
    if tab:
        cc.out(cc.table(tab, ["#", "trader", "account", "pnl", "roi", "all-time pnl", "steady"], "rlrrrrl"))
    else:
        cc.out("  nobody with a profit in that window and an account that size")
    if note:
        cc.out(note)
    cc.out("steady: up over the week, the month and all time. Past profit on a public leaderboard is not skill proven:")
    cc.out("the losers drop off it, and big accounts include market makers and hedged books. Context, not a signal.")
    return 0 if top else 1


def holders(a, coin):
    rows, at, note = board()
    top = ranked(rows, a.window, a.min_account)[:a.n]
    venue = hl_venue()
    res = cc.parallel(lambda r: wallet_state(r["address"], venue), top, workers=4)
    held, failed = [], 0
    for r, st, err in res:
        if err is not None:
            failed += 1
            continue
        for p in st["positions"]:
            if p["coin"] == coin:
                held.append(dict(p, trader=who(r), address=r["address"], account=st["account"], rank=top.index(r) + 1))
    return top, held, failed, at, note, venue


def cmd_coin(a):
    coin = cc.clean_coin(a.target or "")
    if not coin:
        raise cc.CryptoError("which coin? For example: whales.py coin BTC")
    top, held, failed, at, note, venue = holders(a, coin)
    longs = [h for h in held if h["side"] == "long"]
    shorts = [h for h in held if h["side"] == "short"]
    nl, ns = sum(h["notional"] for h in longs), sum(h["notional"] for h in shorts)
    mark = None
    if venue is not None:
        try:
            t = venue.ticker(venue.market(coin)["symbol"])
            mark = t.get("mark") or t["last"]
            fund = cc.annualize_funding(t.get("funding"), t.get("funding_hours") or 1.0)
        except cc.CryptoError:
            fund = None
    else:
        fund = None
    data = {"coin": coin, "as_of": cc.now_ms(), "board_at": at, "top": len(top), "failed": failed, "holders": held,
            "long_usd": nl, "short_usd": ns, "long_share": (nl / (nl + ns)) if (nl + ns) else None, "mark": mark,
            "funding_apr": fund}
    if not a.no_write:
        cc.write_json(cc.wpath("whales.json"), data)
    if a.json:
        cc.dump(data)
        return 0
    cc.out("Whales on %s | the top %d traders by %s profit (accounts over %s) | %s" % (
        coin, len(top), a.window if a.window != "allTime" else "all-time", cc.fmt_usd(a.min_account), cc.fmt_local(cc.now_ms())))
    if not held:
        cc.out("None of them holds %s right now." % coin)
    else:
        lev = cc.median([h["leverage"] for h in held if h["leverage"]]) if any(h["leverage"] for h in held) else None
        cc.out("%d of %d hold it: %d long (%s), %d short (%s). By size, %d%% long.%s" % (
            len(held), len(top), len(longs), cc.fmt_usd(nl), len(shorts), cc.fmt_usd(ns), round(100 * nl / (nl + ns)),
            (" Median leverage %gx." % lev) if lev else ""))
        if mark:
            win = sum(1 for h in held if h["entry"] and cc.side_sign(h["side"]) * (mark - h["entry"]) > 0)
            cc.out("Mark %s: %d of %d are in profit on it." % (cc.fmt_price(mark), win, len(held)))
        tab = [[str(h["rank"]), h["trader"], h["side"], cc.fmt_usd(h["notional"]), cc.fmt_price(h["entry"]),
                cc.fmt_usd(h["upnl"]), ("%gx %s" % (h["leverage"], h["margin_mode"] or "")) if h["leverage"] else "-",
                cc.fmt_price(h["liq"]) if h["liq"] else "-"] for h in sorted(held, key=lambda h: -h["notional"])[:12]]
        cc.out(cc.table(tab, ["rank", "trader", "side", "size", "entry", "unrealized", "leverage", "liquidation"], "rllrrrll"))
    if fund is not None:
        cc.out("Funding on Hyperliquid %s a year: %s pay." % (cc.fmt_pct(fund, 1), "longs" if fund > 0 else "shorts"))
    if failed:
        cc.out("%d wallet%s could not be read." % (failed, "" if failed == 1 else "s"))
    if note:
        cc.out(note)
    cc.out("Context, not a signal: they entered at other prices, with other sizes and time frames, and some hedge elsewhere.")
    return 0


def cmd_wallet(a):
    addr = (a.target or "").lower()
    if not ADDRESS.match(addr):
        raise cc.CryptoError("a wallet is 0x followed by 40 hex characters")
    venue = hl_venue()
    st = wallet_state(addr, venue, fresh=True)
    fills = []
    try:
        fills = wallet_fills(addr, cc.now_ms() - a.days * DAY)
    except cc.CryptoError as e:
        st["fills_error"] = str(e)
    if a.json:
        cc.dump(dict(st, fills=fills[:200]))
        return 0
    names = dict((f["address"], f.get("name")) for f in following())
    cc.out("Wallet %s%s | account %s, %s in positions, %s margin used | %s" % (
        addr, (" (%s)" % names[addr]) if names.get(addr) else "", cc.fmt_usd(st["account"]), cc.fmt_usd(st["notional"]),
        cc.fmt_usd(st["margin_used"]), cc.fmt_local(st["at"])))
    if st["positions"]:
        tab = [[p["coin"], p["side"], cc.fmt_usd(p["notional"]), cc.fmt_price(p["entry"]), cc.fmt_usd(p["upnl"]),
                cc.fmt_pct(p["roe"], 1), ("%gx %s" % (p["leverage"], p["margin_mode"] or "")) if p["leverage"] else "-",
                cc.fmt_price(p["liq"]) if p["liq"] else "-"] for p in st["positions"]]
        cc.out(cc.table(tab, ["coin", "side", "size", "entry", "unrealized", "on margin", "leverage", "liquidation"], "llrrrrll"))
    else:
        cc.out("No open positions.")
    if fills:
        cc.out("")
        cc.out("Latest fills (last %g days):" % a.days)
        tab = [[cc.fmt_local(f["at"]), f["coin"], f["dir"], cc.fmt_price(f["px"]), "%g" % (f["sz"] or 0),
                cc.fmt_usd(f["closed_pnl"]) if f["closed_pnl"] else ""] for f in fills[:12]]
        cc.out(cc.table(tab, ["time", "coin", "what", "price", "size", "closed pnl"], "lllrrr"))
        realized = sum(f["closed_pnl"] - (f["fee"] or 0) for f in fills)
        cc.out("Closed P&L over those %d fills, after fees: %s." % (len(fills), cc.fmt_usd(realized)))
    if st.get("fills_error"):
        cc.out("Fills not read: %s" % st["fills_error"])
    return 0


def following():
    return (cc.read_json(cc.wpath("whales_follow.json"), None) or {}).get("following") or []


def cmd_follow(a, on):
    addr = (a.target or "").lower()
    if not ADDRESS.match(addr):
        raise cc.CryptoError("a wallet is 0x followed by 40 hex characters")
    lst = following()
    if on:
        if any(f["address"] == addr for f in lst):
            cc.out("Already following %s." % short(addr))
            return 0
        lst.append({"address": addr, "name": a.name, "added_at": cc.now_ms()})
        cc.out("Following %s%s. whales.py changes shows what it opens and closes." % (short(addr),
                                                                                    (" (%s)" % a.name) if a.name else ""))
    else:
        if not any(f["address"] == addr for f in lst):
            raise cc.CryptoError("not following %s" % short(addr))
        lst = [f for f in lst if f["address"] != addr]
        cc.out("Stopped following %s." % short(addr))
    if not a.no_write:
        cc.write_json(cc.wpath("whales_follow.json"), {"following": lst})
    return 0


def diff(old, new):
    """What changed between two snapshots of one wallet: opened, closed, added, cut, flipped."""
    o = dict((p["coin"], p) for p in old)
    n = dict((p["coin"], p) for p in new)
    out = []
    for c, p in n.items():
        q = o.get(c)
        if q is None:
            out.append(("opened", p, None))
        elif q["side"] != p["side"]:
            out.append(("flipped", p, q))
        elif p["size"] >= q["size"] * (1 + CHANGE_SHARE):
            out.append(("added to", p, q))
        elif p["size"] <= q["size"] * (1 - CHANGE_SHARE):
            out.append(("cut", p, q))
    for c, q in o.items():
        if c not in n:
            out.append(("closed", None, q))
    return out


def cmd_changes(a):
    lst = following()
    if not lst:
        raise cc.CryptoError("not following anyone yet. whales.py top lists traders; whales.py follow 0x... adds one")
    snaps_path = cc.wpath("state", "whale_snaps.json")
    snaps = cc.read_json(snaps_path, None) or {}
    venue = hl_venue()
    res = cc.parallel(lambda f: wallet_state(f["address"], venue, fresh=True), lst, workers=4)
    changes, first, failed = [], [], []
    for f, st, err in res:
        if err is not None:
            failed.append("%s (%s)" % (f.get("name") or short(f["address"]), str(err)[:60]))
            continue
        prev = snaps.get(f["address"])
        snaps[f["address"]] = {"at": st["at"], "positions": st["positions"]}
        if prev is None:
            first.append(f)
            continue
        for what, p, q in diff(prev["positions"], st["positions"]):
            changes.append({"trader": f.get("name") or short(f["address"]), "address": f["address"], "what": what,
                            "coin": (p or q)["coin"], "side": (p or q)["side"], "notional": (p or q)["notional"],
                            "entry": (p or q)["entry"], "since": prev["at"], "was": q, "now": p})
    if not a.no_write:
        cc.write_json(snaps_path, snaps)
    if a.json:
        cc.dump({"as_of": cc.now_ms(), "changes": changes, "first_look": [f["address"] for f in first], "failed": failed})
        return 0 if changes else 1
    cc.out("Followed wallets | %s | %d wallet%s" % (cc.fmt_local(cc.now_ms()), len(lst), "" if len(lst) == 1 else "s"))
    for c in changes:
        p, q = c["now"], c["was"]
        if c["what"] == "opened":
            line = "opened a %s %s, %s at %s" % (c["coin"], p["side"], cc.fmt_usd(p["notional"]), cc.fmt_price(p["entry"]))
        elif c["what"] == "closed":
            line = "closed its %s %s (was %s, %s unrealized at the last look)" % (c["coin"], q["side"], cc.fmt_usd(q["notional"]),
                                                                                 cc.fmt_usd(q["upnl"]))
        elif c["what"] == "flipped":
            line = "flipped %s from %s to %s, now %s" % (c["coin"], q["side"], p["side"], cc.fmt_usd(p["notional"]))
        else:
            line = "%s its %s %s: %s, was %s" % (c["what"], c["coin"], p["side"], cc.fmt_usd(p["notional"]), cc.fmt_usd(q["notional"]))
        cc.out("  %s %s (since %s)" % (c["trader"], line, cc.fmt_local(c["since"], date=False)))
    if first:
        cc.out("  First look at %s: positions saved; the next run shows what changes." % ", ".join(
            f.get("name") or short(f["address"]) for f in first))
    if not changes and not first:
        cc.out("  No changes since the last look.")
    if failed:
        cc.out("  Not read: " + "; ".join(failed))
    cc.out("Context, not a signal. A wallet closing a position can be taking profit, cutting a loss or hedging elsewhere.")
    return 0 if changes else 1


def build_parser():
    p = argparse.ArgumentParser(prog="whales.py", description="What the best Hyperliquid traders hold.")
    p.add_argument("command", choices=["top", "coin", "wallet", "follow", "unfollow", "following", "changes"])
    p.add_argument("target", nargs="?", default=None, help="coin: the coin; wallet / follow / unfollow: the address")
    p.add_argument("--window", default="month", choices=list(WINDOWS), help="rank by this window's profit (default month)")
    p.add_argument("--min-account", type=float, default=100000.0, help="smallest account counted, USD (default 100K)")
    p.add_argument("--n", type=int, default=None, help="how many traders (top: 20, coin: 50)")
    p.add_argument("--days", type=float, default=7.0, help="wallet: fills from this many days (default 7)")
    p.add_argument("--name", default=None, help="follow: a name for the wallet")
    cc.add_common(p, venue=False)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cc.apply_common(a)
    if a.n is not None and not 1 <= a.n <= 200:
        raise cc.CryptoError("--n must be between 1 and 200")
    a.n = a.n or (50 if a.command == "coin" else 20)
    if a.command == "top":
        return cmd_top(a)
    if a.command == "coin":
        return cmd_coin(a)
    if a.command == "wallet":
        return cmd_wallet(a)
    if a.command in ("follow", "unfollow"):
        return cmd_follow(a, a.command == "follow")
    if a.command == "following":
        lst = following()
        if a.json:
            cc.dump({"following": lst})
            return 0
        if not lst:
            cc.out("Not following anyone.")
        for f in lst:
            cc.out("  %s%s, since %s" % (f["address"], (" (%s)" % f["name"]) if f.get("name") else "", cc.fmt_local(f["added_at"])))
        return 0
    return cmd_changes(a)


if __name__ == "__main__":
    sys.exit(cc.run(main))
