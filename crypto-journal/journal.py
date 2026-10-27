#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-journal: what your trades say, with the honesty a small sample needs.

    python3 journal.py                         the report: last 30 days, paper and live
    python3 journal.py --days 90 --live        live trades only, 90 days
    python3 journal.py trades                  the latest trades, one line each
    python3 journal.py note "chased the entry"  a note on the last closed trade (--trade ID for another)
    python3 journal.py import bybit --days 30  read your closed trades from the exchange (read-only keys are enough)

Win rates come with their 95% range, because 12 trades say very little. Nothing
here predicts the next trade.
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

MIN, HOUR, DAY = 60000, 3600000, 86400000
REVENGE_MS = 30 * MIN                 # a trade opened this soon after a loss
BY_HAND = ("manual", "closed", "by hand", "by hand or another order")   # how the broker records a close it did not plan
EXIT_WORDS = {"manual": "by hand", "closed": "by hand or other", "emergency close: no stop on the exchange": "emergency close"}
SETUP_WORDS = {"orb": "opening range", "breakout": "range breakout", "squeeze": "squeeze break", "trend": "trend pullback",
               "reversal": "RSI reversal", "volume": "volume spike", "rs": "rel. strength", "level": "your level",
               "manual": "own plan"}
FEW = 20                              # under this many trades a win rate is mostly noise
GROUP_GAP_MS = 5 * MIN                # Binance realized-profit rows this close together are one close


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------- trades

def load_trades(a):
    """Closed trades from the ledger and from imports, newest last, each with its open event when there is one."""
    led = cc.read_ledger()
    opens = dict((e["id"], e) for e in led if e.get("type") == "open")
    trades = []
    for e in led:
        if e.get("type") != "close":
            continue
        o = opens.get(e.get("id")) or {}
        t = dict(e)
        t["opened_at"] = e["at"] - (e.get("hold_ms") or 0)
        t["setup"] = e.get("setup") or o.get("setup") or "manual"
        t["source"] = "paper" if e.get("paper", True) else "live"
        t["risk_usd"] = o.get("risk_usd")
        trades.append(t)
    imported = cc.read_jsonl(cc.wpath("imported.jsonl"))
    live = [t for t in trades if t["source"] == "live"]
    for e in imported:
        dupe = any(t["symbol"] == e["symbol"] and abs(t["at"] - e["at"]) <= 2 * MIN and
                   abs((t.get("pnl_usd") or 0) - (e.get("pnl_usd") or 0)) <= max(1.0, 0.05 * abs(e.get("pnl_usd") or 0))
                   for t in live)
        if dupe:
            continue
        t = dict(e)
        t.setdefault("setup", "imported")
        t["source"] = "live"
        t.setdefault("opened_at", e["at"] - (e.get("hold_ms") or 0))
        trades.append(t)
    notes = {}
    for n in cc.read_jsonl(cc.wpath("journal_notes.jsonl")):
        notes.setdefault(n["trade"], []).append(n["text"])
    for t in trades:
        t["notes"] = notes.get(t["id"], [])
    now = cc.now_ms()
    since = cc.parse_when(a.since) if a.since else (now - int(a.days * DAY) if a.days else 0)
    out = [t for t in trades if since <= t["at"] <= now]
    if a.paper:
        out = [t for t in out if t["source"] == "paper"]
    if a.live:
        out = [t for t in out if t["source"] == "live"]
    if a.setup:
        out = [t for t in out if t["setup"] == a.setup]
    if a.coin:
        out = [t for t in out if t.get("coin") == cc.clean_coin(a.coin)]
    out.sort(key=lambda t: t["at"])
    return out, since


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    pnl = [t.get("pnl_usd") or 0.0 for t in trades]
    rs = [t.get("r") for t in trades if t.get("r") is not None]
    wins = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl_usd") or 0) <= 0]
    _, lo, hi = cc.wilson(len(wins), n)
    gw = sum(t["pnl_usd"] for t in wins)
    gl = -sum(t.get("pnl_usd") or 0.0 for t in losses)
    # equity curve and drawdown, in dollars and in R
    eq, peak, dd, dd_at, peak_at = 0.0, 0.0, 0.0, None, trades[0]["at"]
    eqr, peakr, ddr = 0.0, 0.0, 0.0
    for t in trades:
        eq += t.get("pnl_usd") or 0.0
        eqr += t.get("r") or 0.0
        if eq > peak:
            peak, peak_at = eq, t["at"]
        if peak - eq > dd:
            dd, dd_at = peak - eq, (peak_at, t["at"])
        peakr = max(peakr, eqr)
        ddr = max(ddr, peakr - eqr)
    # streaks
    longest_loss, run, last_kind, cur = 0, 0, None, 0
    for t in trades:
        k = (t.get("pnl_usd") or 0) > 0
        run = run + 1 if k == last_kind else 1
        last_kind = k
        if not k:
            longest_loss = max(longest_loss, run)
    cur = run
    fees = sum(t.get("fee") or 0.0 for t in trades)
    funding = sum(t.get("funding") or 0.0 for t in trades)
    gross = sum(pnl) + fees + funding
    return {"n": n, "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / float(n), "win_lo": lo, "win_hi": hi,
            "total_usd": sum(pnl), "avg_usd": sum(pnl) / n, "avg_r": mean(rs), "n_r": len(rs),
            "avg_win_r": mean([t.get("r") for t in wins]), "avg_loss_r": mean([t.get("r") for t in losses]),
            "avg_win_usd": mean([t["pnl_usd"] for t in wins]), "avg_loss_usd": mean([t.get("pnl_usd") or 0.0 for t in losses]),
            "profit_factor": (gw / gl) if gl > 0 else None, "best": max(trades, key=lambda t: t.get("pnl_usd") or 0),
            "worst": min(trades, key=lambda t: t.get("pnl_usd") or 0), "max_dd_usd": dd, "max_dd_r": ddr, "dd_span": dd_at,
            "longest_losing": longest_loss, "streak": cur, "streak_wins": bool(last_kind), "fees": fees, "funding": funding,
            "gross_usd": gross, "cost_share": ((fees + funding) / gross) if gross > 0 else None,
            "hold_win_ms": mean([t.get("hold_ms") for t in wins]), "hold_loss_ms": mean([t.get("hold_ms") for t in losses])}


def breakdown(trades, key):
    groups = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    rows = []
    for k, ts in groups.items():
        rs = [t.get("r") for t in ts if t.get("r") is not None]
        rows.append({"key": k, "n": len(ts), "wins": sum(1 for t in ts if (t.get("pnl_usd") or 0) > 0),
                     "avg_r": mean(rs), "total_usd": sum(t.get("pnl_usd") or 0.0 for t in ts)})
    rows.sort(key=lambda r: (-r["n"], str(r["key"])))
    return rows


def discipline(trades, all_events):
    """Patterns worth a look: trades right after a loss, losers held longer than winners, stops moved, exits by hand."""
    out = []
    after_loss, others = [], []
    last_loss_at = None
    for t in sorted(trades, key=lambda t: t["opened_at"]):
        prev = [p for p in trades if p["at"] <= t["opened_at"]]
        last = max(prev, key=lambda p: p["at"]) if prev else None
        if last is not None and (last.get("pnl_usd") or 0) < 0 and t["opened_at"] - last["at"] <= REVENGE_MS:
            after_loss.append(t)
        else:
            others.append(t)
    if after_loss:
        ra = mean([t.get("r") for t in after_loss])
        ro = mean([t.get("r") for t in others])
        out.append({"flag": "revenge", "n": len(after_loss), "avg_r": ra, "others_r": ro,
                    "words": "%d trade%s opened within 30 minutes of a loss%s." % (
                        len(after_loss), "" if len(after_loss) == 1 else "s",
                        (": %+.2fR on average (the rest: %s)" % (ra, ("%+.2fR" % ro) if ro is not None else "-")) if ra is not None else "")})
    s = stats(trades)
    if s.get("hold_win_ms") and s.get("hold_loss_ms") and s["hold_loss_ms"] > 1.5 * s["hold_win_ms"] and s["losses"] >= 3 and s["wins"] >= 3:
        out.append({"flag": "holding_losers", "words": "Losers were held %.1fx longer than winners (%s against %s)." % (
            s["hold_loss_ms"] / s["hold_win_ms"], cc.fmt_dur(s["hold_loss_ms"]), cc.fmt_dur(s["hold_win_ms"]))})
    ids = set(t["id"] for t in trades)
    moves = [e for e in all_events if e.get("type") == "stop_moved" and e.get("id") in ids]
    if moves:
        out.append({"flag": "stops_moved", "n": len(moves), "words": "Stops moved %d time%s, always toward the price "
                    "(the broker refuses the other way)." % (len(moves), "" if len(moves) == 1 else "s")})
    by_hand = [t for t in trades if t.get("reason") in BY_HAND]
    if by_hand:
        out.append({"flag": "by_hand", "n": len(by_hand), "words": "%d closed by hand before the stop or the target: %s on "
                    "average." % (len(by_hand), ("%+.2fR" % mean([t.get("r") for t in by_hand]))
                                  if mean([t.get("r") for t in by_hand]) is not None else "-")})
    gaps = [t for t in trades if t.get("r") is not None and t["r"] < -1.25]
    if gaps:
        out.append({"flag": "beyond_stop", "n": len(gaps), "words": "%d loss%s bigger than 1.25R: fills past the stop, fees "
                    "and funding. Worst %+.2fR." % (len(gaps), "" if len(gaps) == 1 else "es", min(t["r"] for t in gaps))})
    return out


# ---------------------------------------------------------------- output

def fr(x):
    return ("%+.2fR" % x) if x is not None else "-"


def money(x):
    """+$29.40 or -$10.10: a result, with its sign."""
    if x is None:
        return "-"
    return ("+" if x > 0 else "") + cc.fmt_money(x)


def plural_streak(s):
    if s["streak_wins"]:
        return "win" if s["streak"] == 1 else "wins"
    return "loss" if s["streak"] == 1 else "losses"


def hour_bucket(t, tz):
    off = cc._tz_offset_minutes(t["opened_at"], tz)
    h = (cc.utc_dt(t["opened_at"]) + cc.timedelta(minutes=off)).hour
    b = (h // 4) * 4
    return "%02d-%02dh" % (b, b + 4)


def print_report(trades, since, a):
    cfg = cc.load_config()
    tz = cfg.get("display_tz", "UTC")
    which = "paper" if a.paper else "live" if a.live else "paper and live"
    span = ("since %s" % cc.fmt_local(since)) if since else "all time"
    cc.out("Journal | %s | %s%s | %s" % (which, span, (" | setup %s" % a.setup) if a.setup else "", cc.fmt_local(cc.now_ms())))
    s = stats(trades)
    if not s["n"]:
        cc.out("No closed trades in that window. /crypto-broker records every trade it places; "
               "journal.py import reads trades made elsewhere.")
        return s, []
    cc.out("%d trade%s: %d won, %d lost. Win rate %d%% (95%% range %d%% to %d%%)." % (
        s["n"], "" if s["n"] == 1 else "s", s["wins"], s["losses"], round(s["win_rate"] * 100), round(s["win_lo"] * 100),
        round(s["win_hi"] * 100)))
    if s["n"] < FEW:
        cc.out("  %s too few to judge a win rate or an edge: the range above is how wide the truth could be." %
               ("1 trade is" if s["n"] == 1 else "%d trades are" % s["n"]))
    elif s["n"] < 50:
        cc.out("  Still a small sample: the range is wide.")
    line = "Average %s a trade" % fr(s["avg_r"]) if s["avg_r"] is not None else "Average"
    cc.out("%s, %s a trade; total %s. Profit factor %s." % (
        line, money(s["avg_usd"]), money(s["total_usd"]),
        ("%.2f" % s["profit_factor"]) if s["profit_factor"] is not None else "- (no losses)"))
    if s["n_r"] < s["n"]:
        cc.out("  %d imported trade%s have no planned stop, so no R." % (s["n"] - s["n_r"], "" if s["n"] - s["n_r"] == 1 else "s"))
    wl = ["Average win %s (%s)" % (fr(s["avg_win_r"]), money(s["avg_win_usd"])) if s["wins"] else "No wins yet",
          ("average loss %s (%s)" % (fr(s["avg_loss_r"]), money(s["avg_loss_usd"]))) if s["losses"] else "no losses yet"]
    line = ", ".join(wl) + "."
    if s["n"] > 1:
        line += " Best %s %s, worst %s %s." % (money(s["best"]["pnl_usd"]), s["best"].get("coin", ""),
                                               money(s["worst"]["pnl_usd"]), s["worst"].get("coin", ""))
    cc.out(line)
    if s["avg_win_r"] is not None and s["avg_loss_r"] is not None and s["avg_win_r"] > 0 > s["avg_loss_r"]:
        be = -s["avg_loss_r"] / (s["avg_win_r"] - s["avg_loss_r"])
        s["breakeven"] = be
        where = ("above it" if s["win_lo"] > be else "under it" if s["win_hi"] < be else
                 "%s it, and the range includes it" % ("above" if s["win_rate"] > be else "under"))
        cc.out("Break-even at your average win and loss: %d%%. Your win rate is %s." % (round(be * 100), where))
    dd = "Max drawdown %s (-%.1fR)" % (cc.fmt_money(-s["max_dd_usd"]), s["max_dd_r"]) if s["max_dd_usd"] > 0 else "No drawdown"
    if s.get("dd_span"):
        d0, d1 = cc.fmt_local(s["dd_span"][0]).split(" ")[0], cc.fmt_local(s["dd_span"][1]).split(" ")[0]
        dd += (", on %s" % d0) if d0 == d1 else (", %s to %s" % (d0, d1))
    cc.out("%s. Longest losing streak %d; now %d %s in a row." % (dd, s["longest_losing"], s["streak"], plural_streak(s)))
    if s["fees"] or s["funding"]:
        bits = ["fees %s" % cc.fmt_money(s["fees"])]
        if s["funding"] > 0:
            bits.append("funding paid %s" % cc.fmt_money(s["funding"]))
        elif s["funding"] < 0:
            bits.append("funding received %s" % cc.fmt_money(-s["funding"]))
        share = ""
        if s["cost_share"] is not None and s["fees"] + s["funding"] > 0:
            share = ": %s%d%% of the profit before costs" % ("net " if s["funding"] < 0 else "", round(s["cost_share"] * 100))
        cc.out("Costs: %s%s." % (", ".join(bits), share))
    cc.out("")
    for title, key in (("By setup", lambda t: SETUP_WORDS.get(t["setup"], t["setup"])), ("By coin", lambda t: t.get("coin") or "?"),
                       ("By side", lambda t: t.get("side") or "?"),
                       ("By exit", lambda t: EXIT_WORDS.get(t.get("reason"), t.get("reason") or "?")),
                       ("By hour opened (%s)" % tz, lambda t: hour_bucket(t, tz)), ("Paper or live", lambda t: t["source"])):
        rows = breakdown(trades, key)
        if len(rows) < 2 and title not in ("By setup",):
            continue
        cc.out("%-22s %s" % (title + ":", " | ".join("%s %d (%d won) %s" % (r["key"], r["n"], r["wins"],
                                                                         fr(r["avg_r"]) if r["avg_r"] is not None else money(r["total_usd"]))
                                                     for r in rows[:6])))
    flags = discipline(trades, cc.read_ledger())
    if flags:
        cc.out("")
        cc.out("Worth a look:")
        for f in flags:
            cc.out("  " + ("! " if f["flag"] in ("revenge", "holding_losers") else "  ") + f["words"])
    cc.out("")
    cc.out("Recent trades:")
    for t in trades[-(a.recent or 8):][::-1]:
        cc.out("  " + trade_line(t))
    cc.out("")
    cc.out("A journal shows what happened, not what happens next. Small samples mislead: read the ranges.")
    return s, flags


def trade_line(t):
    note = (' "%s"' % "; ".join(t["notes"])[:80]) if t.get("notes") else ""
    return ("%s  %-5s %-5s %-14s %7s %10s  %-15s%s" % (
        cc.fmt_local(t["at"]).replace(" UTC", ""), t.get("coin", "?"), t.get("side") or "?",
        SETUP_WORDS.get(t["setup"], t["setup"])[:14], fr(t.get("r")), money(t.get("pnl_usd") or 0),
        EXIT_WORDS.get(t.get("reason"), t.get("reason") or "")[:15], note)).rstrip()


# ---------------------------------------------------------------- import

def exchanges():
    path = os.path.join(HERE, os.pardir, "crypto-broker", "exchanges.py")
    if not os.path.isfile(path):
        raise cc.CryptoError("importing needs crypto-broker installed next to this skill")
    spec = importlib.util.spec_from_file_location("crypto_journal_exchanges", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def import_bybit(acct, start, end):
    rows = []
    s = start
    while s < end:
        e = min(end, s + 7 * DAY - 1)                      # closed P&L answers 7 days a call
        rows.extend(acct.closed_pnl(s, e))
        s = e + 1
    out = []
    for r in rows:
        at = int(cc.fnum(r.get("updatedTime"), 0) or 0)
        if not at:
            continue
        opened = int(cc.fnum(r.get("createdTime"), at) or at)
        coin = cc.clean_coin(r.get("symbol", "").replace("USDT", ""))
        out.append({"type": "close", "id": "bybit:%s" % r.get("orderId"), "ext_id": "bybit:%s" % r.get("orderId"), "at": at,
                    "hold_ms": max(0, at - opened), "venue": "bybit", "symbol": r.get("symbol"), "coin": coin,
                    "side": "long" if r.get("side") == "Sell" else "short", "qty": cc.fnum(r.get("closedSize") or r.get("qty")),
                    "entry": cc.fnum(r.get("avgEntryPrice")), "price": cc.fnum(r.get("avgExitPrice")),
                    "pnl_usd": cc.fnum(r.get("closedPnl"), 0.0), "fee": None, "funding": None, "r": None,
                    "reason": "imported", "paper": False, "setup": "imported", "source_note": "Bybit closed P&L (fees included)"})
    return out


def import_binance(acct, start, end):
    realized = acct.income(start, end, "REALIZED_PNL") or []
    commission = acct.income(start, end, "COMMISSION") or []
    funding = acct.income(start, end, "FUNDING_FEE") or []
    by_sym = {}
    for r in realized:
        by_sym.setdefault(r.get("symbol"), []).append(r)
    out = []
    for sym, rows in by_sym.items():
        rows.sort(key=lambda r: int(r.get("time", 0)))
        groups, cur = [], []
        for r in rows:
            if cur and int(r["time"]) - int(cur[-1]["time"]) > GROUP_GAP_MS:
                groups.append(cur)
                cur = []
            cur.append(r)
        if cur:
            groups.append(cur)
        prev_end = start
        for g in groups:
            t0, t1 = int(g[0]["time"]), int(g[-1]["time"])
            fee = -sum(cc.fnum(c.get("income"), 0.0) for c in commission
                       if c.get("symbol") == sym and prev_end < int(c.get("time", 0)) <= t1)
            fund = -sum(cc.fnum(f.get("income"), 0.0) for f in funding
                        if f.get("symbol") == sym and prev_end < int(f.get("time", 0)) <= t1)
            pnl = sum(cc.fnum(r.get("income"), 0.0) for r in g) - fee - fund
            side = None
            try:
                fills = [f for f in acct.fills(sym, t0 - MIN, t1 + MIN) if cc.fnum(f.get("realizedPnl"), 0.0)]
                if fills:
                    side = "long" if fills[0].get("side") == "SELL" else "short"
            except cc.CryptoError:
                side = None
            out.append({"type": "close", "id": "binance:%s:%s" % (sym, g[0].get("tradeId") or t0),
                        "ext_id": "binance:%s:%s" % (sym, g[0].get("tradeId") or t0), "at": t1, "hold_ms": None,
                        "venue": "binance", "symbol": sym, "coin": cc.clean_coin((sym or "").replace("USDT", "")),
                        "side": side, "pnl_usd": pnl, "fee": fee, "funding": fund, "r": None, "reason": "imported",
                        "paper": False, "setup": "imported", "source_note": "Binance realized profit, fees and funding"})
            prev_end = t1
    return out


def cmd_import(a):
    venue = (a.target or "").lower()
    if venue not in ("binance", "bybit"):
        raise cc.CryptoError("import from binance or bybit, for example: journal.py import bybit --days 30")
    ex = exchanges()
    acct = ex.account(venue)
    end = cc.now_ms()
    start = end - int(min(a.days or 30, 180) * DAY)
    got = import_bybit(acct, start, end) if venue == "bybit" else import_binance(acct, start, end)
    have = set(e.get("ext_id") for e in cc.read_jsonl(cc.wpath("imported.jsonl")))
    new = [e for e in got if e["ext_id"] not in have]
    if not a.no_write:
        for e in sorted(new, key=lambda e: e["at"]):
            cc.append_jsonl(cc.wpath("imported.jsonl"), e)
    cc.out("Read %d closed trade%s from %s over %g days: %d new, %d already imported. Nothing was changed on the exchange." % (
        len(got), "" if len(got) == 1 else "s", venue.capitalize(), (end - start) / float(DAY), len(new), len(got) - len(new)))
    if new:
        cc.out("journal.py --live shows them with the rest. Imported trades have no planned stop, so they count in dollars, not R.")
    return 0


# ---------------------------------------------------------------- commands

def build_parser():
    p = argparse.ArgumentParser(prog="journal.py", description="Your trading record, honestly measured.")
    p.add_argument("command", nargs="?", default="report", choices=["report", "trades", "note", "import"])
    p.add_argument("target", nargs="?", default=None, help="note: the text; import: binance or bybit")
    p.add_argument("--days", type=float, default=30.0, help="the window (default 30; 0 = all time)")
    p.add_argument("--since", default=None, help='start date instead of --days, "2026-10-01"')
    p.add_argument("--paper", action="store_true", help="paper trades only")
    p.add_argument("--live", action="store_true", help="live trades only")
    p.add_argument("--setup", default=None)
    p.add_argument("--coin", default=None)
    p.add_argument("--recent", type=int, default=None, help="trades listed (default 8 in the report, 20 in trades)")
    p.add_argument("--trade", default=None, help="note: the trade id (default the last closed trade)")
    cc.add_common(p, venue=False)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cc.apply_common(a)
    if a.paper and a.live:
        raise cc.CryptoError("pick one of --paper and --live, or neither for both")
    if a.command == "import":
        return cmd_import(a)
    if a.command == "note":
        if not a.target or not a.target.strip():
            raise cc.CryptoError('what is the note? For example: journal.py note "chased the entry"')
        a.days, a.since = 0, None
        trades, _ = load_trades(a)
        if not trades:
            raise cc.CryptoError("no closed trade to note yet")
        t = [x for x in trades if x["id"] == a.trade][0] if a.trade and any(x["id"] == a.trade for x in trades) else None
        if a.trade and t is None:
            raise cc.CryptoError("no trade %r. journal.py trades lists them" % a.trade)
        t = t or trades[-1]
        if not a.no_write:
            cc.append_jsonl(cc.wpath("journal_notes.jsonl"), {"at": cc.now_ms(), "trade": t["id"], "text": a.target.strip()[:500]})
        cc.out("Noted on %s %s %s (%s): %s" % (t.get("coin"), t.get("side") or "", fr(t.get("r")), cc.fmt_local(t["at"]),
                                              a.target.strip()[:500]))
        return 0
    trades, since = load_trades(a)
    if a.command == "trades":
        if a.json:
            cc.dump({"trades": trades})
            return 0
        if not trades:
            cc.out("No closed trades in that window.")
            return 1
        rows = trades[-(a.recent or 20):][::-1]
        wide = min(34, max(len(t["id"]) for t in rows))
        for t in rows:
            cc.out("%-*s  %s" % (wide, t["id"][:wide], trade_line(t)))
        return 0
    if a.json:
        s = stats(trades)
        s.pop("best", None)
        s.pop("worst", None)
        cc.dump({"since": since, "stats": s, "flags": discipline(trades, cc.read_ledger()),
                 "by_setup": breakdown(trades, lambda t: t["setup"]), "by_coin": breakdown(trades, lambda t: t.get("coin")),
                 "trades": len(trades)})
        return 0
    s, flags = print_report(trades, since, a)
    if not a.no_write and s["n"]:
        keep = dict((k, v) for k, v in s.items() if k not in ("best", "worst"))
        cc.write_json(cc.wpath("journal.json"), {"as_of": cc.now_ms(), "since": since, "stats": keep, "flags": flags,
                                                 "by_setup": breakdown(trades, lambda t: t["setup"])})
    return 0 if s["n"] else 1


if __name__ == "__main__":
    sys.exit(cc.run(main))
