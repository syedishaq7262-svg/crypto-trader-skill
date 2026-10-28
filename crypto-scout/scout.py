#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-scout: today's live setups that pass the cost rule, each with its record.

    python3 scout.py                    scan, plan the live setups, test each on its own coin's last 30 days
    python3 scout.py --top 6            test fewer (faster)
    python3 scout.py --symbols SOL,ETH  only these coins
    python3 scout.py pick 1             make board entry 1 the current plan, ready for /crypto-budget

The board leads each setup with its record in the pack's walk-forward backtests and orders them by it, the
least bad first. Each one's last 30 days on its own coin, against random entries at the same hours, comes
next as context: in the backtests a setup's recent record on a coin did not predict its next trade. No
setup has shown an edge after costs, and the board says so. It is a list of what the rules found, not advice.
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

T, O, H, L, C = cc.T, cc.O, cc.H, cc.L, cc.C
TESTABLE = ("orb", "breakout", "trend", "reversal", "volume", "squeeze")
NAMES = {"orb": "opening-range breakout", "breakout": "range breakout", "trend": "trend pullback",
         "reversal": "RSI reversal", "volume": "volume spike", "rs": "relative strength", "squeeze": "squeeze break"}
GROUPS = ("listed", "gone", "untested")
GROUP_TITLES = {"listed": "Within the cost rule, best long-run record first",
                "gone": "Gone since it fired (the plan no longer holds)",
                "untested": "No fixed entry rule to test"}
FRESH_WATCH_MS = 15 * 60000
SPARES = 10                    # extra candidates to try when the cost rule leaves some out
STATUS_RANK = {"fresh": 0, "holding": 0, "spike": 0, "reclaimed": 1, "rejected": 1, "turning up": 1,
               "turning down": 1, "broke up": 1, "broke down": 1, "leader": 2, "laggard": 2}


class TooTight(Exception):
    """The plan's stop-out would cost more than the cost rule allows: crypto-risk would block it."""

    def __init__(self, cost, limit):
        Exception.__init__(self, "a stop-out would cost %.2fR" % cost)
        self.cost, self.limit = cost, limit


def sibling(folder, name):
    path = os.path.join(HERE, os.pardir, folder, name + ".py")
    if not os.path.isfile(path):
        raise cc.CryptoError("crypto-scout needs %s installed next to it" % folder)
    spec = importlib.util.spec_from_file_location("crypto_scout_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_watchlist(scan, venue, cfg, a):
    """The scanner's hits: the saved watchlist when it is fresh and fits, otherwise a new scan."""
    w = cc.read_json(cc.wpath("watchlist.json"), None)
    now = cc.now_ms()
    if (w and not a.rescan and w.get("venue") == venue.name and 0 <= now - w.get("as_of", 0) <= FRESH_WATCH_MS
            and not a.symbols):
        return w, True
    sa = scan.build_parser().parse_args([])
    sa.session = a.session or cfg.get("session") or "utc"
    sa.symbols_list = [s for s in (a.symbols or "").split(",") if s.strip()] or None
    sa.side = a.side
    data = scan.compute(sa, venue, cfg)
    if not a.no_write:
        cc.write_json(cc.wpath("watchlist.json"), data)
    return data, False


def get_regime(venue, cfg, a):
    r = cc.read_json(cc.wpath("regime.json"), None)
    if r and r.get("venue") == venue.name and 0 <= cc.now_ms() - r.get("as_of", 0) <= 2 * cc.HOUR_MS:
        return r
    try:
        r = cc.market_regime(venue, cfg)
    except cc.CryptoError:
        return None
    r["venue"], r["as_of"] = venue.name, cc.now_ms()
    if not a.no_write:
        cc.write_json(cc.wpath("regime.json"), r)
    return r


def record_r(setup, side, session):
    """The setup and side's average R after costs in the pack's backtests, over the trades within the cost rule
    (the ones the pack lets you take); None when the backtests have no record of it."""
    e = cc.track_entry(setup, side, session)
    if not e or not e.get("n"):
        return None
    cr = e.get("cost_rule") or {}
    return cr.get("avg_r") if cr.get("n") else e.get("avg_r")


def candidates_of(watch, a):
    hits = watch.get("hits") or []
    session = (watch.get("params") or {}).get("session") or watch.get("session")
    want = set(cc.clean_coin(s) for s in (a.symbols or "").split(",") if s.strip())
    setups = set((a.setups or ",".join(TESTABLE + ("rs",))).split(","))
    out, skipped = [], {"no side yet": [], "failed or ran": [], "past the entry window": []}
    window_ms = float((watch.get("params") or {}).get("entry_hours") or 4) * cc.HOUR_MS
    now = cc.now_ms()
    seen = set()
    broke = set(h["coin"] for h in hits if h["setup"] == "squeeze" and h["side"] in cc.SIDES)
    for h in hits:
        if want and h["coin"] not in want:
            continue
        if h["setup"] not in setups:
            continue
        if h["side"] not in cc.SIDES:
            if h["coin"] not in broke:                 # a squeeze that already broke is listed by its break
                skipped["no side yet"].append(h["coin"])
            continue
        if a.side != "both" and h["side"] != a.side:
            continue
        if h.get("status") in ("back inside", "extended"):
            skipped["failed or ran"].append("%s %s %s" % (h["coin"], h["side"], h["setup"]))
            continue
        opened = (h.get("metrics") or {}).get("session_open")
        if h["setup"] == "orb" and opened and now > opened + window_ms:
            skipped["past the entry window"].append("%s %s" % (h["coin"], h["side"]))   # its record is of on-time entries
            continue
        key = (h["coin"], h["setup"], h["side"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    def record(h):
        r = record_r(h["setup"], h["side"], session)
        return -9.0 if r is None else r
    # what can still be entered first, then the best long-run record, then the freshest and loudest
    out.sort(key=lambda h: (h["setup"] not in TESTABLE, STATUS_RANK.get(h.get("status"), 3), -record(h),
                            (now - (h.get("at") or now)) // (15 * 60000), -(h.get("rvol") or 0), -(h.get("volume_24h") or 0)))
    return out, skipped


def assess(h, venue, cfg, mods, regime, params, scanned_at, a):
    exitm, tm, scan = mods
    m = venue.market(h["symbol"])
    tk = venue.ticker(m["symbol"])
    plan_time = cc.now_ms()
    entry_time_guess = (plan_time // cc.STEP5) * cc.STEP5
    pool_days = 30 if venue.name != "hyperliquid" else 17
    bars_all = venue.bars_between(m["symbol"], "5m", entry_time_guess - pool_days * cc.DAY_MS, plan_time)
    if len(bars_all) < 3 * 288:
        raise cc.CryptoError("too little history (%d five-minute bars)" % len(bars_all))
    b5 = bars_all[-400:]
    b15 = cc.resample(b5, "15m")
    b1h = cc.resample(bars_all, "1h")[-(24 * 14 + 30):]
    a15 = cc.atr(b15, 14)[-1]
    a1h = cc.atr(b1h, 14)[-1]
    plan, entry_words = exitm.build_plan(venue, m, h["side"], cfg, hit=h, params=params, scanned_at=scanned_at,
                                         data=(tk, b5, b15, b1h, a15, a1h))
    lim = cc.cost_limit(cfg)
    cost = plan["costs"]["entry_r"] + plan["costs"]["stop_exit_r"]
    if cc.over_cost_limit(cost, lim):
        raise TooTight(cost, lim)            # crypto-risk would block it: no need to replay it
    entry_time = (plan["entry_time"] // cc.STEP5) * cc.STEP5
    funding = venue.funding_history(m["symbol"], entry_time - pool_days * cc.DAY_MS, entry_time)
    pool = [b for b in bars_all if b[T] < entry_time]
    data = tm.evaluate(plan, venue, cfg, replays=a.replays, seed=a.seed, days=pool_days,
                       pool_data=(pool, funding, pool_days, None), scan=scan, paths=0)
    ctx = {}
    try:
        oi = cc.oi_change(venue, m["symbol"])
        pos = cc.positioning(venue, m["symbol"]) if venue.has_long_short else {}
        ctx["crowding"] = cc.crowding(cc.annualize_funding(tk.get("funding"), tk.get("funding_hours")), oi.get("ch24"),
                                      (pos or {}).get("long_share"), tk.get("change"))
    except cc.CryptoError:
        ctx["crowding"] = None
    flags = flags_for(h, plan, tk, regime, ctx, cfg)
    return {"coin": m["coin"], "symbol": m["symbol"], "side": h["side"], "setup": h["setup"], "status": h.get("status"),
            "at": h.get("at"), "plan": plan, "entry_words": entry_words, "tm": slim_tm(data), "flags": flags,
            "crowding": ctx.get("crowding"), "session": (params or {}).get("session"),
            "backtest": cc.track_entry(h["setup"], h["side"], (params or {}).get("session"))}


FDR = 0.10


def bh_passes(pvals, q=FDR):
    """Benjamini-Hochberg: the indices whose p-values survive testing them all at once at false-discovery rate q."""
    m = len(pvals)
    if not m:
        return set()
    order = sorted(range(m), key=lambda i: pvals[i])
    cut = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / float(m):
            cut = rank
    return set(order[:cut]) if cut > 0 else set()


def slim_tm(d):
    mc = dict((k, v) for k, v in d["mc"].items() if k not in ("bands", "sample_paths", "histogram"))
    hist = dict(d["history"])
    hist["trades"] = hist.get("trades", [])[-60:]
    return {"version": d["version"], "run_at": d["run_at"], "plan_ref": d["plan_ref"], "params": d["params"],
            "pool": d["pool"], "costs": d["costs"], "mc": mc, "history": hist, "baseline": d["baseline"],
            "verdict": d["verdict"], "notes": d.get("notes") or [], "from_scout": True}


def flags_for(h, plan, tk, regime, ctx, cfg):
    """(hard, soft) warnings. Hard ones mean the trade as planned is gone; soft ones are headwinds."""
    hard, soft = [], []
    sg = cc.side_sign(plan["side"])
    last = tk["last"]
    drift = sg * (last - plan["entry"]) / plan["risk"]
    if plan["entry_rule"] == "trigger" and drift > 0.5:
        hard.append("the price has run %.1fR past the trigger" % drift)
    if sg * (last - plan["stop"]) <= 0:
        hard.append("the price is past the stop")
    if regime:
        lab = regime.get("label")
        if plan["side"] == "long" and lab in ("Risk-off", "Fearful"):
            soft.append("a long in a %s market" % lab)
        if plan["side"] == "short" and lab == "Risk-on":
            soft.append("a short in a Risk-on market")
        if plan["side"] == "long" and lab == "Overheated":
            soft.append("a long in an Overheated market")
    crowd = ctx.get("crowding")
    if crowd == "crowded longs" and plan["side"] == "long":
        soft.append("joins crowded longs")
    if crowd == "crowded shorts" and plan["side"] == "short":
        soft.append("joins crowded shorts")
    apr = (plan.get("costs") or {}).get("funding_apr")
    if apr is not None and sg * apr >= 0.30:
        soft.append("funding %s a year against the side" % cc.fmt_pct(apr, 0))
    c = plan.get("costs") or {}
    if c.get("entry_r", 0) + c.get("stop_exit_r", 0) > 0.2:          # only reached when the cost rule is off
        soft.append("costs %.2fR at a stop-out" % (c["entry_r"] + c["stop_exit_r"]))
    if (tk.get("quote_volume") or 0) < float(cfg.get("min_quote_volume_usd") or 0):
        soft.append("thin: %s in 24h" % cc.fmt_usd(tk.get("quote_volume")))
    if plan["target_r"] + 1e-9 < float(cfg.get("min_reward_risk") or 0):
        soft.append("reward:risk under your minimum")
    return {"hard": hard, "soft": soft}


def group_of(c):
    """listed: can be taken as planned; gone: the price has run past the plan since the setup fired; untested: no
    fixed entry rule to re-run (relative strength). A coin's recent record no longer sorts anything: in the
    pack's backtests it did not predict the next trade."""
    hist = c["tm"]["history"]
    if hist.get("note") or not hist.get("setup"):
        return "untested"
    if c["flags"]["hard"]:
        return "gone"
    return "listed"


def board_order(c):
    p = c["plan"]["costs"]
    r = c.get("record_r")
    return (GROUPS.index(c["group"]), 9.0 if r is None else -r, p["entry_r"] + p["stop_exit_r"])   # no record: last


def backtest_line(c):
    """The setup and side's record in the pack's backtests: within the cost rule first, then all of them."""
    e = cc.track_entry(c["setup"], c["side"], c.get("session"))
    if not e or not e.get("n"):
        if c["setup"] == "orb" and c.get("session") not in (None, "utc", "ny"):
            return "backtest: none for the %s opening range (only the UTC day and New York were tested)" % (
                cc.SESSION_NAMES.get(c["session"], c["session"]))
        return None
    words = "%s %ss" % (cc.TRACK_WORDS.get(cc.track_key(c["setup"], c.get("session")), c["setup"]), c["side"])
    n = lambda x: "{:,}".format(x)
    cr = e.get("cost_rule") or {}
    if cr.get("n"):
        return "backtest: %s within the cost rule %s a trade after costs over %s trades%s; all of them %s over %s%s" % (
            words, cc.fmt_r(cr["avg_r"]), n(cr["n"]), cc.zero_note(cr), cc.fmt_r(e["avg_r"]), n(e["n"]), cc.zero_note(e))
    return "backtest: %s %s a trade after costs over %s trades%s" % (words, cc.fmt_r(e["avg_r"]), n(e["n"]), cc.zero_note(e))


def line_for(i, c):
    p, tm = c["plan"], c["tm"]
    h, b = tm["history"], tm["baseline"]
    P = lambda x: cc.fmt_price(x, p.get("tick"))
    head = "%2d. %s %s, %s (%s %s)" % (i, c["coin"], c["side"], NAMES.get(c["setup"], c["setup"]), c["status"],
                                     cc.fmt_utc(c["at"], date=False) if c.get("at") else "")
    out = [head]
    bt = backtest_line(c)
    if bt:
        out.append("     " + bt)
    n = h.get("n") or 0
    if h.get("note"):
        ev = "this coin: no history to re-run (%s)" % h["note"]
    elif n == 0:
        ev = "this coin, last 30 days: never became a trade"
    else:
        ev = "this coin, last 30 days: %+.2fR a trade over %d trade%s" % (h["avg_r"], n, "" if n == 1 else "s")
        if h.get("decided"):
            ev += ", target first %d of %d" % (h["target"], h["decided"])
        if b:
            lp = b["luck_p"]
            ev += "; random entries %+.2fR; luck matches it %s of the time" % (
                b["avg_r"], ("%.1f%%" % (lp * 100)) if lp < 0.1 else "%d%%" % round(lp * 100))
            if c.get("passes_fdr"):
                ev += " (still clear after testing %d setups at once)" % c.get("fdr_tests", 0)
    out.append("     " + ev)
    out.append("     plan: entry %s, stop %s, target %s (%.1fR), out within %gh; a stop-out costs %.2fR; replays: %s" % (
        P(p["entry"]), P(p["stop"]), P(p["target"]), p["target_r"], p["hold_hours"],
        p["costs"]["entry_r"] + p["costs"]["stop_exit_r"], tm["verdict"]["mc"].split(":")[0].lower()))
    fl = c["flags"]["hard"] + c["flags"]["soft"]
    if fl:
        out.append("     note: " + "; ".join(fl))
    return out


def board(a, cfg):
    exitm = sibling("crypto-exit", "exit")
    tm = sibling("crypto-timemachine", "timemachine")
    scan = sibling("crypto-scanner", "scan")
    venue = cc.get_venue(a.venue)
    if getattr(venue, "auto_notes", None):
        cc.out("Using %s (%s)." % (venue.label, "; ".join(venue.auto_notes)))
    watch, reused = get_watchlist(scan, venue, cfg, a)
    regime = get_regime(venue, cfg, a)
    cands, skipped = candidates_of(watch, a)
    params = watch.get("params") or {"session": watch.get("session"), "or_minutes": watch.get("or_minutes")}
    fresher = None
    if skipped["past the entry window"]:
        fresher = scan.fresher_session(params.get("session") or "utc", int(params.get("or_minutes") or 30), cc.now_ms())
    testable = [h for h in cands if h["setup"] in TESTABLE]
    untested = [h for h in cands if h["setup"] not in TESTABLE]
    plan_n = min(a.top, len(testable))
    results, failures, tight = [], [], []
    tries = 0
    for h in testable:
        if len(results) + len(failures) >= plan_n or tries >= a.top + SPARES:
            break
        tries += 1
        sys.stderr.write("Testing %d of %d: %s %s %s\n" % (len(results) + len(failures) + 1, plan_n, h["coin"], h["side"],
                                                         h["setup"]))
        try:
            res = assess(h, venue, cfg, (exitm, tm, scan), regime, params, watch.get("as_of"), a)
        except TooTight as e:
            sys.stderr.write("Left out: %s %s %s, %s\n" % (h["coin"], h["side"], h["setup"], e))
            tight.append({"coin": h["coin"], "side": h["side"], "setup": h["setup"], "cost_r": round(e.cost, 4)})
            continue
        except cc.CryptoError as e:
            failures.append({"coin": h["coin"], "setup": h["setup"], "side": h["side"], "error": str(e)})
            continue
        results.append(res)
    for h in untested[:max(0, a.top - len(results) - len(failures))]:
        try:
            results.append(assess(h, venue, cfg, (exitm, tm, scan), regime, params, watch.get("as_of"), a))
        except TooTight as e:
            tight.append({"coin": h["coin"], "side": h["side"], "setup": h["setup"], "cost_r": round(e.cost, 4)})
        except cc.CryptoError as e:
            failures.append({"coin": h["coin"], "setup": h["setup"], "side": h["side"], "error": str(e)})
    skipped["stops too tight for the costs"] = ["%s %s %s %.2fR" % (t["coin"], t["side"], NAMES.get(t["setup"], t["setup"]),
                                                                    t["cost_r"]) for t in tight]
    # one p-value per tested setup with a positive record; correct for testing them together
    tested = [c for c in results if c["tm"]["baseline"] and (c["tm"]["history"].get("avg_r") or 0) > 0]
    ok = bh_passes([c["tm"]["baseline"]["luck_p"] for c in tested])
    m_tests = len([c for c in results if c["tm"]["baseline"]])
    for i, c in enumerate(tested):
        c["passes_fdr"] = i in ok
    for c in results:
        c["fdr_tests"] = m_tests
        c["group"] = group_of(c)
        c["record_r"] = record_r(c["setup"], c["side"], c.get("session"))
    results.sort(key=board_order)
    for i, c in enumerate(results, 1):
        c["rank"] = i
    data = {"version": 1, "as_of": cc.now_ms(), "venue": venue.name, "venue_label": venue.label,
            "regime": regime.get("label") if regime else None, "regime_read": regime.get("read") if regime else None,
            "watchlist_as_of": watch.get("as_of"), "watchlist_reused": reused, "live_setups": len(watch.get("hits") or []),
            "tested": len(results), "skipped": skipped, "failures": failures, "candidates": results,
            "entry_window_ms": float((watch.get("params") or {}).get("entry_hours") or 4) * cc.HOUR_MS,
            "fresher_session": list(fresher) if fresher else None, "cost_limit_r": cc.cost_limit(cfg),
            "backtest_source": cc.track_source() if cc.track_record() else None,
            "backtest_trades": ((cc.track_record() or {}).get("all") or {}).get("n"),
            "backtest_clear": sorted(k for k, e in ((cc.track_record() or {}).get("setups") or {}).items()
                                     if ((e.get("cost_rule") or e).get("lo") or -1) > 0),
            "params": {"top": a.top, "replays": a.replays, "seed": a.seed}}
    if not a.no_write:
        cc.write_json(cc.wpath("scout.json"), data)
    if a.json:
        cc.dump(data)
        return 0
    print_board(data)
    if a.stats:
        cc.out("Requests: " + cc.HTTP.summary())
    return 0 if any(c["group"] == "listed" for c in results) else 1


def print_board(d):
    cc.out("Scout | %s | %s" % (d["venue_label"], cc.fmt_local(d["as_of"])))
    if d.get("regime"):
        cc.out("Regime: %s. %s" % (d["regime"], d.get("regime_read") or ""))
    sk = d["skipped"]
    line = "%d setups live in the scan; %d tested on each coin's own last 30 days, after fees, slippage and funding." % (
        d["live_setups"], d["tested"])
    if sk.get("no side yet"):
        line += (" %d coiled squeeze%s with no side yet (%s): /crypto-entry COIN --setup squeeze waits for the break."
                 % (len(sk["no side yet"]), "" if len(sk["no side yet"]) == 1 else "s", ", ".join(sk["no side yet"][:6])))
    cc.out(line)
    if sk.get("failed or ran"):
        cc.out("Left out, already failed or run: %s." % ", ".join(sk["failed or ran"][:8]))
    if sk.get("past the entry window"):
        n = len(sk["past the entry window"])
        line = "Left out, opening-range breakouts past their %s entry window (%s%s): a late entry is a different trade." % (
            cc.fmt_dur(d.get("entry_window_ms") or 4 * cc.HOUR_MS), ", ".join(sk["past the entry window"][:6]),
            " and %d more" % (n - 6) if n > 6 else "")
        fr = d.get("fresher_session")
        if fr:
            line += " For a fresh range: scout.py --session %s (the %s open, %s ago)." % (
                fr[0], cc.SESSION_NAMES.get(fr[0], fr[0]), cc.fmt_dur(d["as_of"] - fr[1]))
        cc.out(line)
    if sk.get("stops too tight for the costs"):
        cc.out("Left out, stops too tight for the costs (fees and slippage over %gR at a stop-out, which crypto-risk "
               "blocks): %s." % (d.get("cost_limit_r") or 0, ", ".join(sk["stops too tight for the costs"][:8])))
    by = {}
    for c in d["candidates"]:
        by.setdefault(c["group"], []).append(c)
    for g in GROUPS:
        rows = by.get(g, [])
        if not rows:
            continue
        title = GROUP_TITLES[g] if (g != "listed" or d.get("cost_limit_r")) else "Best long-run record first"
        cc.out("")
        cc.out("%s (%d)" % (title, len(rows)))
        for c in rows:
            for ln in line_for(c["rank"], c):
                cc.out(ln)
    if d["failures"]:
        cc.out("")
        cc.out("Could not test: " + "; ".join("%s %s (%s)" % (f["coin"], f["setup"], f["error"][:60]) for f in d["failures"]))
    cc.out("")
    if not d.get("backtest_source"):
        cc.out("Nothing on this board has shown an edge after costs. Not trading is a position too.")
    elif d.get("backtest_clear"):
        cc.out("In the pack's backtests (%s trades, %s) only %s made money after costs clearly enough to tell from luck, "
               "and a setup's last 30 days on a coin did not predict its next trade." % (
                   "{:,}".format(d.get("backtest_trades") or 0), d["backtest_source"],
                   ", ".join(k.replace("|", " ") + "s" for k in d["backtest_clear"])))
    else:
        cc.out("Nothing on this board has shown an edge: in the pack's backtests (%s trades, %s) no setup made money "
               "after costs clearly enough to tell from luck, and a setup's last 30 days on a coin did not predict its "
               "next trade. Not trading is a position too." % ("{:,}".format(d.get("backtest_trades") or 0),
                                                                d["backtest_source"]))
    cc.out("A list of what the rules found, not advice. To take one further: scout.py pick N, then /crypto-budget.")


def pick(a, cfg):
    d = cc.read_json(cc.wpath("scout.json"), None)
    if not d or not d.get("candidates"):
        raise cc.CryptoError("no scout board yet. Run: scout.py")
    try:
        n = int(a.target)
    except (TypeError, ValueError):
        raise cc.CryptoError("pick which entry? For example: scout.py pick 1")
    hits = [c for c in d["candidates"] if c.get("rank") == n]
    if not hits:
        raise cc.CryptoError("the board has no entry %d" % n)
    c = hits[0]
    age = cc.now_ms() - d["as_of"]
    if age > 30 * 60000:
        raise cc.CryptoError("the board is %s old; prices have moved. Run scout.py again." % cc.fmt_dur(age))
    if c["flags"]["hard"]:
        cc.out("Careful: " + "; ".join(c["flags"]["hard"]) + ".")
    if not a.no_write:
        cc.write_json(cc.wpath("plan.json"), c["plan"])
        cc.write_json(cc.wpath("timemachine.json"), c["tm"])
    p = c["plan"]
    cc.out("Picked %d: %s %s, %s. The plan is now crypto/plan.json (plan %s) with its Time Machine run." %
           (n, c["coin"], c["side"], NAMES.get(c["setup"], c["setup"]), p["plan_id"]))
    cc.out(c["tm"]["verdict"]["final"])
    cc.out("Next: /crypto-budget to size it (or /crypto-exit to change the stop or target first).")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="scout.py", description="Today's live setups within the cost rule, each with its record.")
    p.add_argument("command", nargs="?", default="board", choices=["board", "pick"])
    p.add_argument("target", nargs="?", default=None, help="pick: the board entry number")
    p.add_argument("--top", type=int, default=10, help="test at most this many setups (default 10)")
    p.add_argument("--symbols", default=None)
    p.add_argument("--setups", default=None, help="comma list (default every testable setup and relative strength)")
    p.add_argument("--side", default="both", choices=["both", "long", "short"])
    p.add_argument("--session", default=None, choices=sorted(cc.SESSIONS))
    p.add_argument("--rescan", action="store_true", help="scan again even if the watchlist is fresh")
    p.add_argument("--replays", type=int, default=2000)
    p.add_argument("--seed", type=int, default=19)
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    if a.command == "pick":
        return pick(a, cfg)
    if a.top < 1 or a.top > 40:
        raise cc.CryptoError("--top must be between 1 and 40")
    return board(a, cfg)


if __name__ == "__main__":
    sys.exit(cc.run(main))
