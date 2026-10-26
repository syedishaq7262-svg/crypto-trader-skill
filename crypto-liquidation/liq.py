#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-liquidation: make sure a normal wick takes out your stop long before it takes out your account.

    python3 liq.py check                                  every order in crypto/orders.json -> PASS or BLOCK
    python3 liq.py at SOL --side long --leverage 10       where 10x liquidates, and how often SOL moved that far
    python3 liq.py at BTC --side short --leverage 5 --stop 68500 --hold 8h

The liquidation price here is an estimate from the exchange's public maintenance
margin; the exchange's own figure is the one that counts.
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

T, O, H, L, C = cc.T, cc.O, cc.H, cc.L, cc.C
LADDER = (2, 3, 5, 10, 20, 25, 50, 75, 100, 125)


def adverse_moves(bars, side, hours):
    """For every hourly close, the worst move against the side within the next `hours` hours (as a fraction)."""
    out = []
    h = int(round(hours))
    for i in range(len(bars) - h):
        c0 = bars[i][C]
        win = bars[i + 1:i + 1 + h]
        if side == "long":
            out.append(1.0 - min(b[L] for b in win) / c0)
        else:
            out.append(max(b[H] for b in win) / c0 - 1.0)
    return out


def share_at_least(moves, dist):
    if not moves:
        return None
    return sum(1 for x in moves if x >= dist - 1e-12) / float(len(moves))


def assess(venue, m, side, entry, leverage, qty=None, stop=None, hold=24.0, days=90, cfg=None, moves=None):
    cfg = cfg or cc.load_config()
    notional = (qty or 0) * entry
    mmr, ded, src = cc.mmr_for(venue, m, notional)
    costs = cc.cost_rates(venue, cfg)
    liq = cc.liq_price(entry, side, leverage, mmr, costs["taker"], ded, qty)
    dist = abs(entry - liq) / entry if liq > 0 else 1.0
    out = {"liq": liq, "dist": dist, "mmr": mmr, "mmr_source": src, "leverage": leverage, "entry": entry}
    if moves is None:
        bars = venue.bars(m["symbol"], "1h", int(days * 24) + int(hold) + 1)
        moves = adverse_moves(bars, side, hold)
        out["days"] = len(bars) / 24.0
    out["p_liq"] = share_at_least(moves, dist)
    out["worst"] = max(moves) if moves else None
    if stop is not None:
        sd = abs(entry - stop) / entry
        out["stop"] = stop
        out["stop_dist"] = sd
        out["buffer"] = dist / sd if sd > 0 else None
        out["p_stop"] = share_at_least(moves, sd)
        need = float(cfg.get("min_liq_buffer") or 0)
        out["need"] = need
        out["max_safe"] = cc.max_safe_leverage(entry, stop, side, mmr, costs["taker"], need) if need else None
    out["moves"] = moves
    return out


def check(a, cfg):
    path = a.orders or cc.wpath("orders.json")
    doc = cc.read_json(path, None)
    if not doc or not doc.get("orders"):
        raise cc.CryptoError("no orders to check. Size a plan first: /crypto-budget")
    need = float(cfg.get("min_liq_buffer") or 0)
    blocked = 0
    cc.out("Liquidation Guard | %s" % cc.fmt_utc(cc.now_ms()))
    venues = {}
    for i, o in enumerate(doc["orders"], 1):
        if o.get("reduce_only") or o.get("action") == "close":
            continue
        v = venues.get(o["venue"]) or venues.setdefault(o["venue"], cc.get_venue(o["venue"]))
        m = v.market(o["symbol"])
        hold = max(1.0, (o["exit_by"] - cc.now_ms()) / float(cc.HOUR_MS)) if o.get("exit_by") else 24.0
        r = assess(v, m, o["side"], o["entry"], o["leverage"], o["qty"], o.get("stop"), hold, a.days, cfg)
        reasons = []
        if o.get("margin_mode", "isolated") != "isolated" and not cfg.get("allow_cross_margin"):
            reasons.append("cross margin is off in your config: with cross margin one bad trade can take the whole account")
        if r.get("buffer") is not None and need and r["buffer"] < need:
            reasons.append("liquidation is only %.1fx the stop distance away (you need %gx): at %dx a wick that stops you "
                           "could be close to liquidating you. %dx or less passes." % (r["buffer"], need, o["leverage"],
                                                                                    r["max_safe"] or 1))
        if o.get("stop") is None:
            reasons.append("no stop: the liquidation price is your stop, and it costs the whole margin")
        verdict = "BLOCK" if reasons else "PASS"
        blocked += verdict == "BLOCK"
        P = lambda x: cc.fmt_price(x, o.get("tick"))
        cc.out("%-5s #%d %s %s %s @ %s, %dx %s" % (verdict, i, o["coin"], o["side"], cc.fmt_qty(o["qty"], o.get("step")),
                                                 P(o["entry"]), o["leverage"], o.get("margin_mode", "isolated")))
        line = "      Estimated liquidation %s: %s away." % (P(r["liq"]), cc.fmt_pct(r["dist"], 1, False))
        if r.get("stop") is not None:
            line += " The stop %s is %s away: liquidation is %.1fx further (need %gx)." % (
                P(r["stop"]), cc.fmt_pct(r["stop_dist"], 2, False), r["buffer"], need)
        cc.out(line)
        cc.out("      Maintenance margin %s (%s). The exchange's own liquidation price is the one that counts." %
               (cc.fmt_pct(r["mmr"], 2, False), r["mmr_source"]))
        if r.get("p_liq") is not None:
            cc.out("      Last %d days, within %gh of any hourly close: a %s move against a %s happened %s; a %s move "
                   "(the stop) %s." % (round(r.get("days") or a.days), round(hold, 1), cc.fmt_pct(r["dist"], 1, False),
                                       o["side"], pct_words(r["p_liq"]), cc.fmt_pct(r.get("stop_dist") or 0, 2, False),
                                       pct_words(r.get("p_stop"))))
        for why in reasons:
            cc.out("      - " + why)
        o["liq"] = {"verdict": verdict, "checked_at": cc.now_ms(), "price": r["liq"], "dist": r["dist"],
                    "stop_dist": r.get("stop_dist"), "buffer": r.get("buffer"), "need": need, "mmr": r["mmr"],
                    "mmr_source": r["mmr_source"], "p_liq": r.get("p_liq"), "p_stop": r.get("p_stop"),
                    "max_safe_leverage": r.get("max_safe"), "reasons": reasons, "fingerprint": cc.fingerprint(o)}
    if not a.no_write:
        cc.write_json(path, doc)
    cc.out("%d passed, %d blocked. Next: /crypto-risk for the full rules check." % (len(doc["orders"]) - blocked, blocked))
    return 1 if blocked else 0


def pct_words(p, table=False):
    """'never', 'under 0.1% of the time', '7.4% of the time' (the table drops 'of the time')."""
    if p is None:
        return "-"
    if p == 0:
        return "never"
    tail = "" if table else " of the time"
    if p < 0.001:
        return "under 0.1%" + tail
    return ("%.1f%%" % (p * 100) if p < 0.1 else "%d%%" % round(p * 100)) + tail


def at(a, cfg):
    if not a.coin:
        raise cc.CryptoError("which coin? For example: liq.py at SOL --side long --leverage 10")
    venue = cc.get_venue(a.venue)
    m = venue.market(a.coin)
    t = venue.ticker(m["symbol"])
    entry = a.entry or t["last"]
    hold = cc.parse_hold(a.hold, 24.0)
    bars = venue.bars(m["symbol"], "1h", int(a.days * 24) + int(hold) + 1)
    moves = adverse_moves(bars, a.side, hold)
    lev = a.leverage
    vmax = int(m.get("max_leverage") or 125)
    if lev > vmax:
        raise cc.CryptoError("%s allows at most %dx on %s" % (venue.label, vmax, m["symbol"]))
    mmr0, _d, src0 = cc.mmr_for(venue, m, (a.qty or 0) * entry)
    fee0 = cc.cost_rates(venue, cfg)["taker"]
    if cc.instant_liquidation(lev, mmr0, fee0):
        raise cc.CryptoError("at %dx the margin (%s of the position) would not even cover the maintenance margin of %s (%s) "
                             "plus the fee to close: liquidation on the first tick." %
                             (lev, cc.fmt_pct(1.0 / lev, 2, False), cc.fmt_pct(mmr0, 2, False), src0))
    r = assess(venue, m, a.side, entry, lev, a.qty, a.stop, hold, a.days, cfg, moves=moves)
    P = lambda x: cc.fmt_price(x, m.get("tick"))
    days = len(bars) / 24.0
    data = dict((k, v) for k, v in r.items() if k != "moves")
    data.update({"coin": m["coin"], "symbol": m["symbol"], "venue": venue.name, "side": a.side, "hold_hours": hold, "days": days})
    ladder = []
    data["ladder_note"] = None
    for L_ in LADDER:
        if L_ > vmax:
            break
        if cc.instant_liquidation(L_, r["mmr"], fee0):
            data["ladder_note"] = ("From %dx up the margin would not cover the %s maintenance estimate: liquidation at once."
                                   % (L_, cc.fmt_pct(r["mmr"], 2, False)))
            break
        lp = cc.liq_price(entry, a.side, L_, r["mmr"], fee0)
        d = abs(entry - lp) / entry if lp > 0 else 1.0
        ladder.append({"leverage": L_, "liq": lp, "dist": d, "p": share_at_least(moves, d)})
    data["ladder"] = ladder
    if a.json:
        cc.dump(data)
        return 0
    cc.out("Liquidation | %s %s at %dx isolated | %s %s | %s" % (m["coin"], a.side, lev, venue.label.split(" ")[0], m["symbol"],
                                                               cc.fmt_utc(cc.now_ms())))
    cc.out("Entry %s%s. Estimated liquidation %s: %s %s." % (P(entry), " (now)" if not a.entry else "", P(r["liq"]),
                                                           cc.fmt_pct(r["dist"], 2, False), "below" if a.side == "long" else "above"))
    cc.out("Maintenance margin %s (%s). The exchange's own liquidation price is the one that counts." %
           (cc.fmt_pct(r["mmr"], 2, False), r["mmr_source"]))
    cc.out("Last %d days, within %gh of any hourly close: a %s move against a %s happened %s. The worst was %s." %
           (round(days), hold, cc.fmt_pct(r["dist"], 1, False), a.side, pct_words(r["p_liq"]), cc.fmt_pct(r["worst"] or 0, 1, False)))
    if a.stop is not None:
        cc.out("Your stop %s is %s away: liquidation is %.1fx further (the pack needs %gx). Highest leverage that passes: %dx." %
               (P(a.stop), cc.fmt_pct(r["stop_dist"], 2, False), r["buffer"], r["need"], r["max_safe"] or 0))
    rows = [["%dx" % x["leverage"], P(x["liq"]), cc.fmt_pct(x["dist"], 1, False), pct_words(x["p"], table=True)] for x in ladder]
    cc.out("")
    cc.out(cc.table(rows, ["leverage", "liquidation", "away", "how often a move that big came within %gh" % hold], "rrrr"))
    if data.get("ladder_note"):
        cc.out(data["ladder_note"])
    cc.out("")
    cc.out("Isolated margin: a liquidation costs this position's margin, not the rest of the account.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="liq.py", description="Where liquidation sits, and how often price went that far.")
    p.add_argument("command", nargs="?", default="check", choices=["check", "at"])
    p.add_argument("coin", nargs="?", default=None)
    p.add_argument("--orders", default=None, help="check: the orders file (default crypto/orders.json)")
    p.add_argument("--side", choices=list(cc.SIDES), default="long")
    p.add_argument("--leverage", type=int, default=10)
    p.add_argument("--entry", type=float, default=None)
    p.add_argument("--stop", type=float, default=None)
    p.add_argument("--qty", type=float, default=None, help="size in coins (for tiered maintenance margin)")
    p.add_argument("--hold", default=None, help="how long the position stays open: 8h, 2d (default 24h)")
    p.add_argument("--days", type=int, default=90, help="days of hourly candles to measure moves on (default 90)")
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    if a.leverage < 1 or a.leverage > 125:
        raise cc.CryptoError("--leverage must be between 1 and 125")
    if a.command == "at":
        return at(a, cfg)
    return check(a, cfg)


if __name__ == "__main__":
    sys.exit(cc.run(main))
