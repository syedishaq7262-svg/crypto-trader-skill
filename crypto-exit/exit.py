#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-exit: decide how the trade ends before it starts.

    python3 exit.py plan ORBX                          the scanner's hit on ORBX: setup stop, 2R target
    python3 exit.py plan SOL --side long --stop atr:1.5 --target 2R
    python3 exit.py plan BTC --side short --entry 67250 --stop 67900 --target level
    python3 exit.py show                               the current crypto/plan.json

It writes crypto/plan.json for crypto-timemachine, crypto-budget, crypto-risk and
crypto-alerts. A stop is a plan, not a guarantee: a fast market can fill past it.
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

T, O, H, L, C, V, QV = cc.T, cc.O, cc.H, cc.L, cc.C, cc.V, cc.QV
MIN = 60000
FRESH_MS = 10 * MIN
LATE_MS = 60 * 60000                 # a market entry this long after the trigger is a different trade

SETUP_NAMES = {
    "orb": "opening-range breakout", "breakout": "range breakout", "squeeze": "squeeze", "trend": "trend pullback",
    "reversal": "RSI reversal", "volume": "volume spike", "rs": "relative strength", "level": "your level",
}
SETUP_STOP_WORDS = {
    "orb": "the opening-range midpoint",
    "breakout": "one 15-minute ATR back inside the broken level",
    "trend": "the pullback's extreme",
    "reversal": "the 6-hour extreme",
    "volume": "the far end of the spike bar",
    "squeeze": "the middle band (the 20-hour average)",
}


def number(text):
    try:
        v = float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def find_hit(watch, coin, setup=None, side=None):
    """The watchlist hit for a coin: the named setup and side if given, a holding or fresh one first."""
    if not watch:
        return None
    want = cc.clean_coin(coin)
    hits = [h for h in watch.get("hits") or [] if h.get("coin") == want or h.get("symbol") == str(coin).upper()]
    if setup:
        hits = [h for h in hits if h.get("setup") == setup]
    if side:
        hits = [h for h in hits if h.get("side") in (side, "both")]
    if not hits:
        return None
    rank = {"holding": 0, "fresh": 0, "reclaimed": 1, "rejected": 1, "spike": 1, "turning up": 1, "turning down": 1,
            "leader": 2, "laggard": 2, "broke up": 1, "broke down": 1, "coiled": 3, "extended": 4,
            "back inside": 5}
    hits.sort(key=lambda h: (rank.get(h.get("status"), 3), -(h.get("at") or 0)))
    return hits[0]


def market_data(venue, sym):
    jobs = {"b5": lambda: venue.bars(sym, "5m", 400), "b1h": lambda: venue.bars(sym, "1h", 24 * 14 + 30),
            "tk": lambda: venue.tickers()}
    got = {}
    for k, r, e in cc.parallel(lambda k: jobs[k](), list(jobs), workers=3):
        if e is not None:
            raise cc.CryptoError("could not read %s: %s" % (sym, e))
        got[k] = r
    b15 = cc.resample(got["b5"], "15m")
    if len(b15) < 20 or len(got["b1h"]) < 30:
        raise cc.CryptoError("%s has too little history to plan on (%d 15-minute bars)" % (sym, len(b15)))
    a15 = cc.atr(b15, 14)[-1]
    a1h = cc.atr(got["b1h"], 14)[-1]
    return got["tk"][sym], got["b5"], b15, got["b1h"], a15, a1h


def nearest_pivot(b1h, price, kind, beyond):
    """The nearest 1h swing low under (kind 'L') or swing high over (kind 'H') price - beyond / price + beyond."""
    pts = cc.pivots(b1h[-24 * 14:], 3, 3)
    if kind == "L":
        xs = [p for _, p, k in pts if k == "L" and p < price - beyond]
        return max(xs) if xs else None
    xs = [p for _, p, k in pts if k == "H" and p > price + beyond]
    return min(xs) if xs else None


def make_stop(spec, side, entry, hit, a15, b1h, m):
    """(raw stop, words) for a stop rule."""
    sg = cc.side_sign(side)
    spec = (spec or "").strip().lower()
    if spec == "setup":
        if not hit or hit.get("stop_ref") is None:
            raise cc.CryptoError("this plan has no setup stop to lean on. Use --stop atr:1.5, pct:2, swing or a price.")
        ref = hit["stop_ref_raw"] if hit.get("stop_ref_raw") is not None else hit["stop_ref"]
        words = "setup: " + SETUP_STOP_WORDS.get(hit["setup"], "the setup's invalidation level")
        if hit["setup"] == "orb":
            mt = hit.get("metrics") or {}
            words += " (%s + %s) / 2" % (cc.fmt_price(mt.get("or_high"), m.get("tick")), cc.fmt_price(mt.get("or_low"), m.get("tick")))
        return ref, words
    if spec == "range":
        if not hit or hit.get("setup") != "orb":
            raise cc.CryptoError("--stop range needs an opening-range hit")
        mt = hit["metrics"]
        return (mt["or_low"] if side == "long" else mt["or_high"]), "the far side of the opening range"
    if spec.startswith("atr:"):
        k = number(spec[4:])
        if not k:
            raise cc.CryptoError("--stop atr:K needs a positive number, like atr:1.5")
        return entry - sg * k * a15, "%g x the 15-minute ATR (%s)" % (k, cc.fmt_price(a15, m.get("tick")))
    if spec.startswith("pct:"):
        k = number(spec[4:])
        if not k or k >= 50:
            raise cc.CryptoError("--stop pct:X needs a percent between 0 and 50")
        return entry * (1.0 - sg * k / 100.0), "%g%% from the entry" % k
    if spec == "swing":
        piv = nearest_pivot(b1h, entry, "L" if side == "long" else "H", 0.5 * a15)
        if piv is None:
            raise cc.CryptoError("no 1-hour swing %s %s the entry in 14 days. Use atr:1.5 or a price."
                                 % ("low" if side == "long" else "high", "under" if side == "long" else "over"))
        return piv - sg * 0.1 * a15, "beyond the 1-hour swing %s at %s" % ("low" if side == "long" else "high",
                                                                         cc.fmt_price(piv, m.get("tick")))
    v = number(spec)
    if v:
        return v, "your price"
    raise cc.CryptoError("unknown stop %r. Use setup, range, atr:K, pct:X, swing or a price." % spec)


def make_target(spec, side, entry, risk, a15, b1h, m):
    sg = cc.side_sign(side)
    spec = (spec or "").strip().lower()
    if spec.endswith("r") and number(spec[:-1]):
        k = number(spec[:-1])
        return entry + sg * k * risk, "%gR (%g x the risk)" % (k, k)
    if spec == "level":
        piv = nearest_pivot(b1h, entry, "H" if side == "long" else "L", risk)
        if piv is None:
            raise cc.CryptoError("no 1-hour swing %s at least 1R %s the entry in 14 days. Use an R target."
                                 % ("high" if side == "long" else "low", "over" if side == "long" else "under"))
        return piv - sg * 0.1 * a15, "just before the 1-hour swing %s at %s" % (
            "high" if side == "long" else "low", cc.fmt_price(piv, m.get("tick")))
    if spec.startswith("atr:"):
        k = number(spec[4:])
        if not k:
            raise cc.CryptoError("--target atr:K needs a positive number")
        return entry + sg * k * a15, "%g x the 15-minute ATR" % k
    v = number(spec)
    if v:
        return v, "your price"
    raise cc.CryptoError("unknown target %r. Use 2R, 1.5R, level, atr:K or a price." % spec)


def tick_round(p, m, mode="nearest"):
    if m.get("tick"):
        return cc.round_to_tick(p, m["tick"], mode)
    return float("%.6g" % p)


def entry_hit(coin):
    """A fresh trigger from crypto-entry (crypto/entry.json) for this coin, or None."""
    e = cc.read_json(cc.wpath("entry.json"), None)
    if not e or not e.get("hit"):
        return None
    h = e["hit"]
    if h.get("coin") != cc.clean_coin(coin) and h.get("symbol") != str(coin).upper():
        return None
    if cc.now_ms() - (h.get("at") or 0) > 60 * MIN:
        return None
    return e


def plan_cmd(a, cfg):
    hit, params, venue_name, scanned_at = None, None, None, None
    watch = None
    if not a.manual:
        e = entry_hit(a.coin)
        if e and (a.venue is None or e.get("venue") == a.venue) and (not a.setup or e["hit"].get("setup") == a.setup):
            hit, params, venue_name, scanned_at = e["hit"], e.get("params"), e.get("venue"), e.get("as_of")
        if hit is None:
            watch = cc.read_json(cc.wpath("watchlist.json"), None)
            if watch and (a.venue is None or watch.get("venue") == a.venue):
                hit = find_hit(watch, a.coin, a.setup, a.side)
                if hit is not None:
                    params = watch.get("params") or {"session": watch.get("session"), "or_minutes": watch.get("or_minutes")}
                    venue_name, scanned_at = watch.get("venue"), watch.get("as_of")
        if hit is None and a.setup:
            raise cc.CryptoError("the watchlist has no %s hit on %s. Run /crypto-scanner or plan it by hand with --manual."
                                 % (a.setup, cc.clean_coin(a.coin)))
    venue = cc.VENUES[venue_name]() if hit else cc.get_venue(a.venue)
    m = venue.market(hit["symbol"] if hit else a.coin)
    side = a.side or (hit["side"] if hit and hit.get("side") in cc.SIDES else None)
    if side is None:
        if hit and hit.get("setup") == "squeeze":
            why = " (a squeeze has no side until a band breaks)"
        elif a.manual:
            why = ""
        elif not watch:
            why = ", or run /crypto-scanner first to plan from a setup"
        else:
            why = " (the last scan has no setup on %s; the stop then comes from the ATR)" % m["coin"]
        raise cc.CryptoError("which side? Add --side long or --side short%s." % why)
    plan, entry_words = build_plan(venue, m, side, cfg, hit=hit, params=params, scanned_at=scanned_at,
                                   entry=a.entry, stop=a.stop, target=a.target, hold=a.hold)
    if not a.no_write:
        cc.write_json(cc.wpath("plan.json"), plan)
    if a.json:
        cc.dump(plan)
    else:
        print_plan(plan, entry_words, cfg)
    return 0


def build_plan(venue, m, side, cfg, hit=None, params=None, scanned_at=None, entry=None, stop=None, target=None,
               hold=None, data=None):
    """The plan as a dict, plus a few words on where the entry came from. data, when given, is
    (ticker, 5m bars, 15m bars, 1h bars, 15m ATR, 1h ATR) already fetched (crypto-scout passes it)."""
    sym = m["symbol"]
    if side == "short" and not cfg.get("allow_short", True):
        raise cc.CryptoError("shorts are off in your config (allow_short is false)")
    sg = cc.side_sign(side)
    t, b5, b15, b1h, a15, a1h = data if data is not None else market_data(venue, sym)
    last = t["last"]
    now = cc.now_ms()
    grid = (now // cc.STEP5) * cc.STEP5
    warnings = []

    # entry
    entry_arg = entry
    spec = (entry or "auto").lower()
    if spec == "auto":
        spec = "trigger" if (hit and hit.get("trigger") and now - hit["at"] <= FRESH_MS) else "market"
    if spec == "trigger":
        if not hit or not hit.get("trigger"):
            raise cc.CryptoError("there is no trigger to enter at. Use --entry market or a price.")
        entry, entry_time, entry_rule = hit["trigger"], hit["at"], "trigger"
        entry_words = "the trigger, %s at %s" % (cc.fmt_price(entry, m.get("tick")), cc.fmt_utc(hit["at"], date=False))
        if now - hit["at"] > FRESH_MS:
            warnings.append("The trigger is %s old; the price now is %s." % (cc.fmt_dur(now - hit["at"]), cc.fmt_price(last, m.get("tick"))))
    elif spec == "market":
        entry, entry_time, entry_rule = last, grid, "market"
        entry_words = "now, at the market"
        if hit and hit.get("trigger"):
            entry_words += " (the trigger was %s at %s)" % (cc.fmt_price(hit["trigger"], m.get("tick")),
                                                           cc.fmt_utc(hit["at"], date=False))
            if hit.get("at") and now - hit["at"] > LATE_MS:
                warnings.append("The setup triggered %s ago, so this is a late entry: the setup's record (entries at "
                                "the trigger) says little about it." % cc.fmt_dur(now - hit["at"]))
    else:
        v = number(spec)
        if not v:
            raise cc.CryptoError("unknown entry %r. Use auto, trigger, market or a price." % entry_arg)
        entry, entry_time, entry_rule = v, grid, "price"
        entry_words = "your price (a resting order that may never fill)"
    entry = tick_round(entry, m)

    # stop
    stop_spec = stop or ("setup" if hit and hit.get("stop_ref") is not None else "atr:1.5")
    raw_stop, stop_words = make_stop(stop_spec, side, entry, hit, a15, b1h, m)
    stop = tick_round(raw_stop, m, "down" if side == "long" else "up")
    if sg * (entry - stop) <= 0:
        raise cc.CryptoError("the stop %s is not %s the entry %s" % (cc.fmt_price(stop, m.get("tick")),
                                                                    "under" if side == "long" else "over",
                                                                    cc.fmt_price(entry, m.get("tick"))))
    risk = abs(entry - stop)
    if risk < entry * 0.0005:
        raise cc.CryptoError("the stop is %s from the entry: tighter than the spread and the fees can survive. Widen it."
                             % cc.fmt_pct(risk / entry, 3, False))

    # target
    target_spec = target or ("%gR" % max(2.0, float(cfg.get("min_reward_risk") or 0)))
    raw_target, target_words = make_target(target_spec, side, entry, risk, a15, b1h, m)
    target = tick_round(raw_target, m)
    if sg * (target - entry) <= 0:
        raise cc.CryptoError("the target %s is not %s the entry" % (cc.fmt_price(target, m.get("tick")),
                                                                   "over" if side == "long" else "under"))
    target_r = abs(target - entry) / risk
    if target_r < 0.25:
        raise cc.CryptoError("the target is only %.2fR away" % target_r)

    # hold and time
    hold_h = cc.parse_hold(hold, float(cfg.get("max_hold_hours") or 24))
    exit_by = entry_time + int(hold_h * cc.HOUR_MS)

    # costs and funding
    costs = cc.cost_rates(venue, cfg)
    if costs["slippage"] is None:
        costs["slippage"], costs["slippage_source"] = cc.measure_slippage(venue, sym)
    c_in = (costs["taker"] + costs["slippage"]) * entry / risk
    c_stop = (costs["taker"] + costs["slippage"]) * stop / risk
    c_tgt = costs["maker"] * target / risk
    rate = t.get("funding")
    hours = t.get("funding_hours") or m.get("funding_hours") or 8.0
    pays = cc.funding_times(entry_time, exit_by, hours)
    funding_r = (sg * (rate or 0.0) * entry * len(pays)) / risk     # positive: this side pays
    apr = cc.annualize_funding(rate, hours)
    breakeven = cc.breakeven_share(target_r, entry, stop, target, risk, costs)

    # warnings
    if risk < 0.5 * a15:
        warnings.append("The stop is closer than half a 15-minute ATR (%s): ordinary noise will hit it often."
                        % cc.fmt_price(a15, m.get("tick")))
    if a1h and risk > 3.0 * a1h:
        warnings.append("Wide stop: more than three 1-hour ATRs. The position will be small.")
    lim = cc.cost_limit(cfg)
    if cc.over_cost_limit(c_in + c_stop, lim):
        k = costs["taker"] + costs["slippage"]
        need = 2.0 * entry * k / (lim + sg * k)          # the stop distance where a stop-out costs exactly the limit
        warnings.append("Fees and slippage take %.2fR from a stop-out, over your %gR limit (max_cost_r): crypto-risk will "
                        "block it. A stop at least %s a coin (%s) from the entry would pass, if a level there means "
                        "something; otherwise skip the trade." % (c_in + c_stop, lim, cc.fmt_price(need * 1.01, m.get("tick")),
                                                                  cc.fmt_pct(need * 1.01 / entry, 2, False)))
    elif c_in + c_stop > 0.2:
        warnings.append("Fees and slippage cost %.2fR on a stop-out: the stop is tight for the costs." % (c_in + c_stop))
    if apr is not None and sg * apr >= 0.30:
        warnings.append("Funding is %s a year against this side: a full %gh hold costs about %.2fR."
                        % (cc.fmt_pct(apr, 0), hold_h, funding_r))
    if entry_rule == "trigger" and abs(last - entry) > 0.25 * risk:
        warnings.append("The price (%s) is %.2fR from the trigger already." % (cc.fmt_price(last, m.get("tick")), abs(last - entry) / risk))
    if entry_rule != "price" and sg * (last - stop) <= 0:
        raise cc.CryptoError("the price %s is already past the stop %s" % (cc.fmt_price(last, m.get("tick")), cc.fmt_price(stop, m.get("tick"))))
    week = b1h[-168:]
    if week:
        hi7, lo7 = max(b[H] for b in week), min(b[L] for b in week)
        if (side == "long" and target > hi7) or (side == "short" and target < lo7):
            warnings.append("The target is beyond the 7-day range (%s to %s)." % (cc.fmt_price(lo7, m.get("tick")), cc.fmt_price(hi7, m.get("tick"))))
    piv = nearest_pivot(b1h, entry, "H" if side == "long" else "L", 0.0)
    if piv is not None and sg * (target - piv) > 0 and sg * (piv - entry) > 0 and target_spec != "level":
        warnings.append("A 1-hour swing %s at %s sits between the entry and the target." %
                        ("high" if side == "long" else "low", cc.fmt_price(piv, m.get("tick"))))
    if round(target_r, 2) < float(cfg.get("min_reward_risk") or 0):
        warnings.append("Reward:risk %.2f is under your %g minimum: crypto-risk will block it." % (target_r, cfg["min_reward_risk"]))
    if hit and hit.get("status") in ("back inside", "extended"):
        warnings.append("The setup is %s." % hit["status"])

    setup = None
    if hit:
        setup = {"name": hit["setup"], "status": hit.get("status"), "at": hit.get("at"), "trigger": hit.get("trigger"),
                 "level": hit.get("level"), "stop_ref": hit.get("stop_ref"), "metrics": hit.get("metrics") or {},
                 "params": params or {}, "scanned_at": scanned_at}
    plan = {
        "version": 1, "created_at": now, "venue": venue.name, "venue_label": venue.label,
        "symbol": sym, "coin": m["coin"], "multiplier": m["multiplier"], "tick": m.get("tick"), "step": m.get("step"),
        "min_qty": m.get("min_qty"), "min_notional": m.get("min_notional"), "max_leverage_venue": m.get("max_leverage"),
        "side": side, "setup": setup,
        "entry": entry, "entry_rule": entry_rule, "entry_time": entry_time, "entry_time_utc": cc.fmt_utc(entry_time),
        "price_at_plan": last,
        "stop": stop, "stop_spec": stop_spec, "stop_rule": stop_words,
        "target": target, "target_spec": target_spec, "target_rule": target_words, "target_r": round(target_r, 4),
        "risk": risk, "risk_pct": risk / entry, "atr15": a15, "atr1h": a1h, "stop_atr": risk / a15 if a15 else None,
        "hold_hours": hold_h, "exit_by": exit_by, "exit_by_utc": cc.fmt_utc(exit_by),
        "costs": dict(costs, entry_r=c_in, stop_exit_r=c_stop, target_exit_r=c_tgt, funding_rate=rate,
                      funding_hours=hours, funding_apr=apr, funding_payments=len(pays), funding_r=funding_r),
        "breakeven_share": breakeven, "warnings": warnings,
    }
    plan["plan_id"] = cc.plan_id(plan)
    return plan, entry_words


def print_plan(p, entry_words=None, cfg=None):
    tk = p.get("tick")
    P = lambda x: cc.fmt_price(x, tk)
    sg = 1 if p["side"] == "long" else -1
    head = "Exit plan | %s %s | %s %s | %s" % (p["coin"], p["side"], p["venue_label"].split(" ")[0], p["symbol"],
                                              cc.fmt_utc(p["created_at"]))
    cc.out(head)
    s = p.get("setup")
    if s:
        line = SETUP_NAMES.get(s["name"], s["name"])
        mt = s.get("metrics") or {}
        if s["name"] == "orb":
            sess = (s.get("params") or {}).get("session", "utc")
            sess = cc.SESSION_NAMES.get(sess, sess)
            line += " (%s open, %s to %s)" % (sess, P(mt.get("or_low")), P(mt.get("or_high")))
        line += ", triggered %s, %s" % (cc.fmt_utc(s["at"], date=False), s.get("status"))
        cc.out("  Setup        " + line)
    cc.out("  Entry        %s  %s" % (P(p["entry"]), entry_words or p["entry_rule"]))
    rounding = "" if p["stop_rule"] == "your price" else ", rounded away from the entry"
    cc.out("  Stop         %s  %s%s" % (P(p["stop"]), p["stop_rule"], rounding))
    cc.out("  Target       %s  %s" % (P(p["target"]), p["target_rule"]))
    risk_line = "%s a coin (%s of the entry" % (P(p["risk"]), cc.fmt_pct(p["risk_pct"], 2, False))
    if p.get("stop_atr"):
        risk_line += "; %.1f x the 15-minute ATR" % p["stop_atr"]
    cc.out("  Risk         " + risk_line + ")")
    c = p["costs"]
    cc.out("  Reward:risk  %.2f before costs; after fees and slippage a win nets %+.2fR and a stop-out %+.2fR" %
           (p["target_r"], p["target_r"] - c["entry_r"] - c["target_exit_r"], -(1 + c["entry_r"] + c["stop_exit_r"])))
    cc.out("  Costs        taker %.3f%%, maker %.3f%%, slippage %s" % (c["taker"] * 100, c["maker"] * 100,
                                                                     c.get("slippage_source") or "%.1f bps" % (c["slippage"] * 1e4)))
    if c.get("funding_rate") is not None:
        who = "longs pay" if c["funding_rate"] > 0 else "shorts pay" if c["funding_rate"] < 0 else "nobody pays"
        cc.out("  Funding      %s, %s; a full hold crosses %d payment%s: %+.2fR for this side" %
               (cc.fmt_funding(c["funding_rate"], c["funding_hours"]), who, c["funding_payments"],
                "" if c["funding_payments"] == 1 else "s", -c["funding_r"]))
    cc.out("  Break-even   the target must come first in %.1f%% of trades that hit a side (fees and slippage included)"
           % (p["breakeven_share"] * 100))
    cc.out("  Out by       %s (%gh), win or lose" % (cc.fmt_local(p["exit_by"]), p["hold_hours"]))
    for i, line in enumerate(backtest_lines(p)):
        cc.out(("  Backtest     " if i == 0 else "               ") + line)
    if p.get("multiplier", 1) != 1:
        cc.out("  Note         prices are per %s coins on this venue (%s)" % ("{:,}".format(p["multiplier"]), p["symbol"]))
    for w in p.get("warnings") or []:
        cc.out("  Warning      " + w)
    cc.out("A stop is a plan, not a guarantee: a fast market can fill past it.")
    cc.out("Next: /crypto-timemachine to replay this plan, then /crypto-budget to size it.")


def backtest_lines(p):
    """The published backtest's record for this plan's setup and side, in two lines (none for a plan with no setup)."""
    s = p.get("setup") or {}
    if not s.get("name"):
        return []
    session = (s.get("params") or {}).get("session")
    parts = cc.track_parts(s["name"], p["side"], session)
    if not parts:
        if s["name"] == "orb" and session not in (None, "utc", "ny"):
            return ["the backtest covers the UTC-day and New York opening ranges, not the %s one"
                    % cc.SESSION_NAMES.get(session, session)]
        return []
    words = cc.TRACK_WORDS.get(cc.track_key(s["name"], session), s["name"])
    out = ["%s %ss: %s (%s)" % (words, p["side"], parts[0], cc.track_source())]
    if parts[1]:
        out.append("within the cost rule (a stop-out costs at most %gR): %s"
                   % ((cc.track_record() or {}).get("cost_limit_r") or 0.2, parts[1]))
    return out


def show_cmd(a):
    p = cc.read_json(cc.wpath("plan.json"), None)
    if not p:
        raise cc.CryptoError("no plan yet. Run: exit.py plan <COIN>")
    if a.json:
        cc.dump(p)
    else:
        print_plan(p)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="exit.py", description="Set the stop and the target before the entry.")
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "show"])
    p.add_argument("coin", nargs="?", default=None)
    p.add_argument("--side", choices=list(cc.SIDES), default=None)
    p.add_argument("--setup", default=None, help="use this scanner setup's hit (orb, breakout, trend, ...)")
    p.add_argument("--manual", action="store_true", help="ignore the watchlist")
    p.add_argument("--entry", default=None, help="auto (default), trigger, market or a price")
    p.add_argument("--stop", default=None, help="setup, range, atr:K, pct:X, swing or a price")
    p.add_argument("--target", default=None, help="2R (default), 1.5R, level, atr:K or a price")
    p.add_argument("--hold", default=None, help="out by this long after the entry: 8h, 90m, 2d (default from config, 24h)")
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    if a.command == "show":
        return show_cmd(a)
    if not a.coin:
        raise cc.CryptoError("which coin? For example: exit.py plan SOL --side long")
    return plan_cmd(a, cfg)


if __name__ == "__main__":
    sys.exit(cc.run(main))
