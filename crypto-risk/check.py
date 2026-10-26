#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-risk: the gate between a proposal and the exchange.

    python3 check.py            every order in crypto/orders.json -> PASS or BLOCK, stamped into the file
    python3 check.py --json
    python3 check.py --no-write look without stamping

A BLOCK is final for that order: it is not shown to the user for a yes and it never
reaches crypto-broker. A PASS only means the order may be shown to the user. It is
not a yes, and it is not advice. Getting out of a position is never blocked.
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

PASS_VALID_MS = 15 * 60000
EPS = 1e-9


def is_exit(o):
    return bool(o.get("reduce_only")) or o.get("action") == "close"


def on_grid(x, step):
    if not step or x is None:
        return True
    q = x / step
    return abs(q - round(q)) < 1e-6


def check_order(o, cfg, ctx, venue, m, t, plan, tm):
    """(verdict, reasons, warnings, facts). ctx carries the running batch state."""
    reasons, warns, facts = [], [], {}
    side = o.get("side")
    sym = o.get("symbol")
    # ---- format: always checked, entries and exits alike
    if side not in cc.SIDES:
        reasons.append("format: side must be long or short")
    if not o.get("qty") or o["qty"] <= 0:
        reasons.append("format: the size must be above zero")
    if o.get("type") not in ("market", "limit"):
        reasons.append("format: type must be market or limit")
    if o.get("type") == "limit" and not o.get("limit"):
        reasons.append("format: a limit order needs a limit price")
    if m.get("step") and o.get("qty") and not on_grid(o["qty"], m["step"]):
        reasons.append("format: size %s is not a multiple of the exchange's step %s" % (o["qty"], m["step"]))
    for k in ("limit", "stop", "target"):
        if o.get(k) is not None and m.get("tick") and not on_grid(o[k], m["tick"]):
            reasons.append("format: the %s %s is not on the exchange's price tick %s" % (k, o[k], m["tick"]))
    if reasons:
        return "BLOCK", reasons, warns, facts
    if is_exit(o):
        # getting out must always be possible: everything else is a warning at most
        pos = [p for p in ctx["positions"] if p["symbol"] == sym]
        if not pos:
            warns.append("there is no open %s position in the log to close" % sym)
        return "PASS", reasons, warns, facts
    sg = cc.side_sign(side)
    last = t["last"]
    tick = m.get("tick")
    P = lambda x: cc.fmt_price(x, tick)
    # ---- the entry the risk is measured from: never better than the price now
    if o["type"] == "limit":
        meas = o["limit"]
        if sg * (meas - last) > 0:
            warns.append("the limit %s is through the price %s: it would fill at once, like a market order" % (P(meas), P(last)))
    else:
        live = cc.round_to_tick(last, tick) if tick else last
        meas = live if sg * (live - o["entry"]) > 0 else o["entry"]
    facts["measured_entry"] = meas
    facts["price_now"] = last
    stop, target = o.get("stop"), o.get("target")
    # ---- the rules
    if o["coin"].upper() in ctx["blocked"] or sym.upper() in ctx["blocked"]:
        reasons.append("blocked: %s is on your blocked list" % o["coin"])
    if side == "short" and not cfg.get("allow_short", True):
        reasons.append("short: shorts are off in your config (allow_short is false)")
    if stop is None:
        if cfg.get("require_stop", True):
            reasons.append("stop: every entry needs a stop (require_stop is true)")
    elif sg * (meas - stop) <= 0:
        reasons.append("stop: the price %s is already past the stop %s" % (P(meas), P(stop)))
    if target is not None and sg * (target - meas) <= 0:
        reasons.append("target: the price %s is already past the target %s" % (P(meas), P(target)))
    if stop is not None and sg * (meas - stop) > 0:
        risk = abs(meas - stop)
        plan_risk = abs(o["entry"] - stop) or risk
        drift = abs(meas - o["entry"]) / plan_risk
        facts["drift_r"] = drift
        if drift > ctx["max_drift_r"]:
            reasons.append("moved: the price is %.2fR from the planned entry (limit %.2fR). Re-plan with /crypto-exit."
                           % (drift, ctx["max_drift_r"]))
        if (o.get("costs") or {}).get("slippage") is not None:
            costs = o["costs"]
        else:
            costs = cc.plan_costs(plan if plan and plan.get("plan_id") == o.get("plan_id") else None, venue, cfg)
        rate = t.get("funding")
        unit = risk + (costs["taker"] + costs["slippage"]) * (meas + stop)
        pays = o.get("funding_payments") or 0
        unit += max(0.0, sg * (rate or 0.0) * meas * pays)
        loss = o["qty"] * unit
        facts["loss_usd"] = loss
        if loss > float(cfg["max_loss_per_trade_usd"]) + 0.005:
            reasons.append("loss: %s x %s at the stop (fees, slippage, funding included) = %s, over your %s max loss per trade"
                           % (cc.fmt_qty(o["qty"], m.get("step")), P(unit), cc.fmt_money(loss),
                              cc.fmt_money(float(cfg["max_loss_per_trade_usd"]))))
        if target is not None and sg * (target - meas) > 0:
            rr = abs(target - meas) / risk
            facts["reward_risk"] = rr
            if rr + EPS < float(cfg.get("min_reward_risk") or 0):
                reasons.append("reward:risk: %.2f from the price now, under your %g minimum" % (rr, cfg["min_reward_risk"]))
        # daily loss: today's net loss so far, the risk still open, and this one
        day_loss = max(0.0, -ctx["realized"])
        committed = day_loss + ctx["open_risk"] + loss
        facts["day"] = {"loss_so_far": day_loss, "open_risk": ctx["open_risk"], "after": committed}
        if committed > float(cfg["max_daily_loss_usd"]) + 0.005:
            reasons.append("daily loss: %s lost today + %s at risk in open positions + %s here = %s, over your %s a day"
                           % (cc.fmt_money(day_loss), cc.fmt_money(ctx["open_risk"]), cc.fmt_money(loss),
                              cc.fmt_money(committed), cc.fmt_money(float(cfg["max_daily_loss_usd"]))))
    else:
        loss = None
    # leverage, margin, liquidation
    lev = o.get("leverage") or 1
    if lev > int(cfg.get("max_leverage") or 1):
        reasons.append("leverage: %dx is over your %dx max" % (lev, int(cfg["max_leverage"])))
    if m.get("max_leverage") and lev > m["max_leverage"]:
        reasons.append("leverage: %s allows at most %gx on %s" % (venue.label, m["max_leverage"], sym))
    mode = o.get("margin_mode", "isolated")
    if mode != "isolated" and not cfg.get("allow_cross_margin"):
        reasons.append("margin: cross margin is off in your config (one position could take the whole account)")
    notional = o["qty"] * meas
    margin = notional / lev
    facts["notional"], facts["margin"] = notional, margin
    cap = float(cfg["account_usd"]) * float(cfg.get("max_margin_pct", 50)) / 100.0
    if margin > cap + 0.005:
        reasons.append("margin: %s of margin is over your %g%% cap (%s)" % (cc.fmt_money(margin), cfg.get("max_margin_pct", 50),
                                                                           cc.fmt_money(cap)))
    if ctx["margin_used"] + margin > float(cfg["account_usd"]) + 0.005:
        reasons.append("margin: %s already in use + %s here is more than your %s account"
                       % (cc.fmt_money(ctx["margin_used"]), cc.fmt_money(margin), cc.fmt_money(float(cfg["account_usd"]))))
    if m.get("min_qty") and o["qty"] < m["min_qty"] - EPS:
        reasons.append("size: under the exchange's minimum of %s" % m["min_qty"])
    if m.get("min_notional") and notional < m["min_notional"] - EPS:
        reasons.append("size: %s is under the exchange's minimum order of %s" % (cc.fmt_money(notional), cc.fmt_money(m["min_notional"])))
    if stop is not None:
        mmr, ded, src = cc.mmr_for(venue, m, notional)
        fee = cc.cost_rates(venue, cfg)["taker"]
        liq = cc.liq_price(meas, side, lev, mmr, fee, ded, o["qty"])
        ld = abs(meas - liq) / meas if liq > 0 else 1.0
        sd = abs(meas - stop) / meas
        buf = ld / sd if sd else None
        facts["liq"] = {"price": liq, "dist": ld, "buffer": buf, "mmr_source": src}
        need = float(cfg.get("min_liq_buffer") or 0)
        if need and buf is not None and buf < need:
            safe = cc.max_safe_leverage(meas, stop, side, mmr, fee, need)
            reasons.append("liquidation: estimated at %s, only %.1fx the stop distance away (need %gx). %dx or less passes."
                           % (P(liq), buf, need, safe))
    # positions and counts
    if any(p["symbol"] == sym for p in ctx["positions"]):
        reasons.append("duplicate: a %s position is already open; this pack does not add to positions" % sym)
    if len(ctx["positions"]) >= int(cfg.get("max_open_positions") or 1):
        reasons.append("positions: %d open already, your max is %d" % (len(ctx["positions"]), int(cfg["max_open_positions"])))
    if ctx["entries"] >= int(cfg.get("max_trades_per_day") or 1):
        reasons.append("trades: %d entries today already, your max is %d" % (ctx["entries"], int(cfg["max_trades_per_day"])))
    # cooldown after a losing streak
    need_losses = int(cfg.get("cooldown_after_losses") or 0)
    recent = ctx["last_closes"][-need_losses:] if need_losses else []
    if need_losses and len(recent) == need_losses and all(cc.fnum(e.get("pnl_usd"), 0) < 0 for e in recent):
        until = recent[-1].get("at", 0) + int(float(cfg.get("cooldown_hours") or 0) * cc.HOUR_MS)
        if cc.now_ms() < until:
            reasons.append("cooldown: %d losses in a row; no new entries until %s. A pause beats a revenge trade."
                           % (need_losses, cc.fmt_local(until)))
    # warnings: say them, do not block
    vol = t.get("quote_volume") or 0
    floor = float(cfg.get("min_quote_volume_usd") or 0)
    if floor and vol < floor / 4.0:
        reasons.append("liquidity: %s traded in 24h, a quarter of your %s floor" % (cc.fmt_usd(vol), cc.fmt_usd(floor)))
    elif floor and vol < floor:
        warns.append("thin: %s traded in 24h, under your %s floor" % (cc.fmt_usd(vol), cc.fmt_usd(floor)))
    apr = cc.annualize_funding(t.get("funding"), t.get("funding_hours"))
    if apr is not None and sg * apr >= 0.5:
        per_day = notional * abs(apr) / 365.0
        warns.append("funding: %s a year against this side, about %s a day at this size" % (cc.fmt_pct(apr, 0), cc.fmt_money(per_day)))
    if o.get("paper") is False or not cfg.get("paper", True):
        warns.append("LIVE: this order would use real money")
    if tm is None:
        warns.append("not replayed: /crypto-timemachine has not run on this plan")
    else:
        h = tm.get("history") or {}
        if (h.get("n") or 0) >= 10 and (h.get("avg_r") or 0) < 0:
            warns.append("history: the setup lost %.2fR a trade after costs over its last %d trades here" % (-h["avg_r"], h["n"]))
    if o.get("exit_by") is None:
        warns.append("no time limit: the plan has no exit-by time")
    elif o["exit_by"] <= cc.now_ms():
        reasons.append("time: the plan's exit-by time has passed")
    return ("BLOCK" if reasons else "PASS"), reasons, warns, facts


def main(argv=None):
    p = argparse.ArgumentParser(prog="check.py", description="Check every proposed order against your rules.")
    p.add_argument("--orders", default=None, help="the orders file (default crypto/orders.json)")
    p.add_argument("--max-drift-r", type=float, default=0.5, help="block when the price moved more than this many R from the plan")
    cc.add_common(p, venue=False)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    path = a.orders or cc.wpath("orders.json")
    doc = cc.read_json(path, None)
    if not doc or not doc.get("orders"):
        raise cc.CryptoError("no orders to check. Size a plan first: /crypto-budget")
    stats = cc.today_stats(cfg)
    ctx = {"blocked": set(str(x).upper() for x in cfg.get("blocked") or []), "positions": list(stats["open"]),
           "entries": stats["entries"], "realized": stats["realized"], "open_risk": stats["open_risk"],
           "margin_used": stats["open_margin"], "last_closes": stats["last_closes"], "max_drift_r": a.max_drift_r}
    plan = cc.read_json(cc.wpath("plan.json"), None)
    tm_doc = cc.read_json(cc.wpath("timemachine.json"), None)
    venues = {}
    results = []
    now = cc.now_ms()
    for i, o in enumerate(doc["orders"], 1):
        v = venues.get(o.get("venue"))
        if v is None:
            v = venues[o.get("venue")] = cc.get_venue(o.get("venue"))
        try:
            m = v.market(o["symbol"])
            t = v.ticker(m["symbol"])
        except cc.CryptoError as e:
            o["risk"] = {"verdict": "BLOCK", "checked_at": now, "reasons": ["market: %s" % e], "warnings": [],
                         "fingerprint": cc.fingerprint(o)}
            results.append((i, o, "BLOCK", ["market: %s" % e], [], {}))
            continue
        tm = None
        if tm_doc and (tm_doc.get("plan_ref") or {}).get("plan_id") and tm_doc["plan_ref"]["plan_id"] == o.get("plan_id"):
            tm = tm_doc
        verdict, reasons, warns, facts = check_order(o, cfg, ctx, v, m, t, plan, tm)
        o["risk"] = {"verdict": verdict, "checked_at": now, "valid_until": now + PASS_VALID_MS, "reasons": reasons,
                     "warnings": warns, "facts": facts, "fingerprint": cc.fingerprint(o)}
        results.append((i, o, verdict, reasons, warns, facts))
        if verdict == "PASS" and not is_exit(o):
            # later orders in the same batch see this one as filled
            ctx["positions"].append({"symbol": o["symbol"], "margin": facts.get("margin", 0)})
            ctx["entries"] += 1
            ctx["open_risk"] += facts.get("loss_usd") or 0.0
            ctx["margin_used"] += facts.get("margin") or 0.0
    if not a.no_write:
        cc.write_json(path, doc)
        for i, o, verdict, reasons, warns, facts in results:
            cc.append_jsonl(cc.wpath("risk_log.jsonl"), {"at": now, "order": o.get("id"), "symbol": o.get("symbol"),
                                                         "verdict": verdict, "reasons": reasons, "fingerprint": o["risk"]["fingerprint"]})
    if a.json:
        cc.dump([{"order": i, "id": o.get("id"), "verdict": v_, "reasons": r, "warnings": w, "facts": f}
                 for i, o, v_, r, w, f in results])
    else:
        print_results(results, cfg, stats)
    return 1 if any(r[2] == "BLOCK" for r in results) else 0


def print_results(results, cfg, stats):
    for i, o, verdict, reasons, warns, facts in results:
        tick = o.get("tick")
        P = lambda x: cc.fmt_price(x, tick)
        kind = "exit" if is_exit(o) else "entry"
        cc.out("%-5s #%d %s %s %s, %s, %dx %s" % (verdict, i, o.get("side"), o.get("coin"), cc.fmt_qty(o.get("qty") or 0, o.get("step")),
                                               kind, o.get("leverage") or 1, o.get("margin_mode", "isolated")))
        if kind == "entry" and facts.get("measured_entry") is not None:
            src = "the plan's entry" if facts["measured_entry"] == o["entry"] else "the price now"
            line = "          entry %s (%s), stop %s, target %s" % (P(facts["measured_entry"]), src, P(o.get("stop")), P(o.get("target")))
            cc.out(line)
            bits = []
            if facts.get("loss_usd") is not None:
                bits.append("max loss %s (limit %s)" % (cc.fmt_money(facts["loss_usd"]), cc.fmt_money(float(cfg["max_loss_per_trade_usd"]))))
            if facts.get("reward_risk") is not None:
                bits.append("reward:risk %.2f (min %g)" % (facts["reward_risk"], float(cfg.get("min_reward_risk") or 0)))
            if facts.get("liq"):
                bits.append("liquidation %s, %.1fx the stop distance" % (P(facts["liq"]["price"]), facts["liq"]["buffer"] or 0))
            if bits:
                cc.out("          " + ", ".join(bits))
            d = facts.get("day")
            if d:
                cc.out("          today before this: %d of %d trades, %s lost, %s at risk open; daily limit %s" %
                       (stats["entries"], int(cfg["max_trades_per_day"]), cc.fmt_money(d["loss_so_far"]),
                        cc.fmt_money(d["open_risk"]), cc.fmt_money(float(cfg["max_daily_loss_usd"]))))
        for r in reasons:
            cc.out("        - " + r)
        for w in warns:
            cc.out("        ! " + w)
    n_pass = sum(1 for r in results if r[2] == "PASS")
    n_block = len(results) - n_pass
    cc.out("")
    cc.out("%d passed, %d blocked at %s. %s" % (n_pass, n_block, cc.fmt_local(cc.now_ms()),
                                             "A PASS lasts 15 minutes and only means the order may be shown to you; it is not a yes."
                                             if n_pass else "Nothing can be sent."))
    if n_pass:
        cc.out("Next: /crypto-broker shows the ticket and asks for your yes.")


if __name__ == "__main__":
    sys.exit(cc.run(main))
