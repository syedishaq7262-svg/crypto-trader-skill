#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-budget: turn the plan into an exact order inside your money rules.

    python3 size.py                    crypto/plan.json + crypto/config.json -> crypto/orders.json
    python3 size.py --risk 5           risk $5 on this one (never more than max_loss_per_trade_usd)
    python3 size.py --leverage 2       ask for a leverage (it must still pass the caps)
    python3 size.py --limit            a limit entry at the plan's entry (or --limit PRICE)

The stop decides the size: the further away the stop, the smaller the position.
Leverage only decides how much margin the position locks up, and how far away
liquidation sits. This writes a proposal. crypto-risk checks it; crypto-broker asks you.
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


def per_coin_loss(side, entry, stop, costs, funding_rate, payments):
    """What one coin loses if the stop fills at the stop: the move, fees and slippage both ways,
    and the funding this side would pay over a full hold."""
    loss = abs(entry - stop) + (costs["taker"] + costs["slippage"]) * (entry + stop)
    pay = cc.side_sign(side) * (funding_rate or 0.0) * entry * payments
    return loss + max(0.0, pay)


def per_coin_gain(side, entry, target, costs, funding_rate, payments):
    gain = abs(target - entry) - (costs["taker"] + costs["slippage"]) * entry - costs["maker"] * target
    pay = cc.side_sign(side) * (funding_rate or 0.0) * entry * payments
    return gain - max(0.0, pay)


def size(plan, cfg, venue, a):
    side = plan["side"]
    sg = cc.side_sign(side)
    m = venue.market(plan["symbol"])
    t = venue.ticker(m["symbol"])
    last = t["last"]
    stop, target = plan["stop"], plan["target"]
    if sg * (last - stop) <= 0:
        raise cc.CryptoError("the price %s is already past the stop %s. The setup has failed; make a new plan."
                             % (cc.fmt_price(last, m.get("tick")), cc.fmt_price(stop, m.get("tick"))))
    if sg * (target - last) <= 0:
        raise cc.CryptoError("the price %s is already past the target %s: the move has happened."
                             % (cc.fmt_price(last, m.get("tick")), cc.fmt_price(target, m.get("tick"))))
    notes = []
    if a.limit is not None:
        lim = plan["entry"] if a.limit == "plan" else float(a.limit)
        if sg * (lim - stop) <= 0 or sg * (target - lim) <= 0:
            raise cc.CryptoError("a limit at %s is not between the stop and the target" % cc.fmt_price(lim, m.get("tick")))
        lim = cc.round_to_tick(lim, m.get("tick")) if m.get("tick") else lim
        entry, otype = lim, "limit"
    else:
        # a market order fills near the price now; a price that ran since the plan means more risk, never less
        live = cc.round_to_tick(last, m.get("tick")) if m.get("tick") else last
        worse = sg * (live - plan["entry"]) > (m.get("tick") or plan["entry"] * 1e-6) * 0.5
        entry = live if worse else plan["entry"]
        otype = "market"
        if worse:
            notes.append("Sized from the price now (%s), not the plan's %s: the risk grew by %.0f%%." %
                         (cc.fmt_price(last, m.get("tick")), cc.fmt_price(plan["entry"], m.get("tick")),
                          (abs(entry - stop) / plan["risk"] - 1) * 100))
    risk = abs(entry - stop)
    if risk / plan["risk"] > 1.5:
        raise cc.CryptoError("the price has run %.1fR from the planned entry. Re-plan with /crypto-exit." % ((risk - plan["risk"]) / plan["risk"]))
    costs = cc.plan_costs(plan, venue, cfg)
    pc = plan.get("costs") or {}
    rate = t.get("funding") if t.get("funding") is not None else pc.get("funding_rate")
    hours = t.get("funding_hours") or pc.get("funding_hours") or 8.0
    now = cc.now_ms()
    payments = len(cc.funding_times(max(now, plan["entry_time"]), plan["exit_by"], hours))
    max_loss = float(cfg["max_loss_per_trade_usd"])
    budget = max_loss
    if a.risk is not None:
        if a.risk > max_loss:
            notes.append("You asked to risk $%.2f; your max per trade is $%.2f, so it is $%.2f." % (a.risk, max_loss, max_loss))
        budget = min(a.risk, max_loss)
    if budget <= 0:
        raise cc.CryptoError("the money at risk must be above zero")
    unit_loss = per_coin_loss(side, entry, stop, costs, rate, payments)
    qty_loss = budget / unit_loss
    account = float(cfg["account_usd"])
    margin_cap = account * float(cfg.get("max_margin_pct", 50)) / 100.0
    fee = costs["taker"]
    mmr, ded, mmr_src = cc.mmr_for(venue, m, qty_loss * entry)
    buffer = float(cfg.get("min_liq_buffer") or 0)
    venue_cap = int(m.get("max_leverage") or plan.get("max_leverage_venue") or 125)
    safe = cc.max_safe_leverage(entry, stop, side, mmr, fee, buffer, cap=125) if buffer > 0 else 125
    cfg_cap = int(cfg.get("max_leverage") or 1)
    lev_cap = min(cfg_cap, safe, venue_cap)
    if lev_cap < 1:
        raise cc.CryptoError("even at 1x the liquidation would sit inside %gx the stop distance. The stop is too far." % buffer)
    if a.leverage is not None:
        if a.leverage > lev_cap:
            why = ("your max_leverage of %dx" % cfg_cap if lev_cap == cfg_cap else
                   "liquidation needing %gx the stop distance" % buffer if lev_cap == safe else
                   "the venue's %dx limit" % venue_cap)
            raise cc.CryptoError("%dx is over the %dx this trade allows (%s)." % (a.leverage, lev_cap, why))
        lev_for_cap = a.leverage
    else:
        lev_for_cap = lev_cap
    qty_margin = margin_cap * lev_for_cap / entry
    step = m.get("step")
    qty = cc.floor_step(min(qty_loss, qty_margin), step)
    decided = "max loss" if qty_loss <= qty_margin else "margin cap"
    min_qty = m.get("min_qty") or 0
    min_notional = m.get("min_notional") or 0
    if qty <= 0 or qty < min_qty - 1e-12 or qty * entry < min_notional - 1e-9:
        smallest = max(min_qty or 0, (min_notional / entry) if min_notional else 0)
        if step:
            smallest = math.ceil(smallest / step - 1e-9) * step
        raise cc.CryptoError("too small to trade: the smallest order here (%s, about %s) would lose $%.2f at the stop, over your $%.2f."
                             % (fmt_qty(smallest, m), cc.fmt_money(smallest * entry), smallest * unit_loss, budget))
    notional = qty * entry
    lev = a.leverage if a.leverage is not None else max(1, int(math.ceil(notional / margin_cap - 1e-9)))
    lev = min(lev, lev_cap)
    margin = notional / lev
    mmr, ded, mmr_src = cc.mmr_for(venue, m, notional)
    liq = cc.liq_price(entry, side, lev, mmr, fee, ded, qty)
    loss_usd = qty * unit_loss
    gain_usd = qty * per_coin_gain(side, entry, target, costs, rate, payments)
    tm = cc.read_json(cc.wpath("timemachine.json"), None)
    tm_ref = None
    if tm and (tm.get("plan_ref") or {}).get("plan_id") == (plan.get("plan_id") or cc.plan_id(plan)):
        tm_ref = {"run_at": tm.get("run_at"), "verdict": (tm.get("verdict") or {}).get("final"),
                  "history_avg_r": (tm.get("history") or {}).get("avg_r"), "history_n": (tm.get("history") or {}).get("n")}
    order = {
        "id": "o%d-%s" % (now, m["symbol"]), "created_at": now, "venue": venue.name, "symbol": m["symbol"],
        "coin": m["coin"], "multiplier": m["multiplier"], "tick": m.get("tick"), "step": step,
        "action": "open", "reduce_only": False, "side": side, "type": otype,
        "limit": entry if otype == "limit" else None, "qty": qty, "entry": entry, "plan_entry": plan["entry"],
        "stop": stop, "stop_type": "stop-market, reduce-only, triggered by the mark price",
        "target": target, "target_type": "limit, reduce-only",
        "leverage": lev, "margin_mode": "isolated", "notional": notional, "margin": margin,
        "loss_at_stop_usd": loss_usd, "gain_at_target_usd": gain_usd, "reward_risk": abs(target - entry) / risk,
        "liq_est": liq, "mmr": mmr, "mmr_source": mmr_src, "exit_by": plan["exit_by"], "funding_payments": payments,
        "funding_rate": rate, "funding_hours": hours, "plan_created_at": plan.get("created_at"),
        "costs": {"taker": costs["taker"], "maker": costs["maker"], "slippage": costs["slippage"]},
        "plan_id": plan.get("plan_id") or cc.plan_id(plan),
        "setup": (plan.get("setup") or {}).get("name"), "timemachine": tm_ref, "paper": bool(cfg.get("paper", True)),
        "limits": {"max_loss_usd": budget, "unit_loss": unit_loss, "qty_loss": qty_loss, "margin_cap_usd": margin_cap,
                   "qty_margin": qty_margin, "lev_cap": lev_cap, "lev_for_cap": lev_for_cap, "lev_safe": safe,
                   "lev_cfg": cfg_cap, "lev_venue": venue_cap,
                   "decided_by": decided},
        "notes": notes,
    }
    order["why"] = "%s %s on %s: %s, bound by your %s (%s at the stop)" % (
        (plan.get("setup") or {}).get("name") or "manual plan", side, m["coin"], fmt_qty(qty, m),
        "$%.2f max loss per trade" % budget if decided == "max loss" else "%g%% margin cap" % cfg.get("max_margin_pct", 50),
        "$%.2f" % loss_usd)
    return order


def fmt_qty(q, m):
    d = cc.tick_decimals(m.get("step") or 1)
    s = "{:,.{d}f}".format(q, d=d)
    s += " " + (m["base"] if m.get("multiplier", 1) != 1 and m.get("base") else m["coin"])
    if m.get("multiplier", 1) != 1:
        s += " (= {:,.0f} {})".format(q * m["multiplier"], m["coin"])
    return s


def print_order(o, cfg, m):
    tk = o.get("tick")
    P = lambda x: cc.fmt_price(x, tk)
    lim = o["limits"]
    cc.out("Budget | %s %s | %s %s | %s" % (o["coin"], o["side"], o["venue"].capitalize(), o["symbol"], cc.fmt_utc(o["created_at"])))
    cc.out("Size: %s, about %s at %s %s; %dx isolated, margin %s." %
           (fmt_qty(o["qty"], m), cc.fmt_money(o["notional"]), "about" if o["type"] == "market" else "a limit of",
            P(o["entry"]), o["leverage"], cc.fmt_money(o["margin"])))
    rows = [
        ["max loss", "%s / %s a coin (the stop %s + fees, slippage, funding %s)" % (
            cc.fmt_money(lim["max_loss_usd"]), P(lim["unit_loss"]), P(abs(o["entry"] - o["stop"])),
            P(lim["unit_loss"] - abs(o["entry"] - o["stop"]))), cc.fmt_qty(lim["qty_loss"], None)],
        ["margin cap", "%g%% of %s = %s of margin x %dx leverage / %s" % (
            cfg.get("max_margin_pct", 50), cc.fmt_money(float(cfg["account_usd"])), cc.fmt_money(lim["margin_cap_usd"]),
            lim["lev_for_cap"], P(o["entry"])), cc.fmt_qty(lim["qty_margin"], None)],
        ["exchange minimum", "min size %s, min order %s" % (cc.fmt_qty(m.get("min_qty") or 0, m.get("step")),
                                                            cc.fmt_money(m.get("min_notional") or 0)), "ok"],
    ]
    cc.out(cc.table(rows, ["limit", "the math", "qty"], "llr"))
    cc.out("The %s decides it: %s." % (lim["decided_by"], fmt_qty(o["qty"], m)))
    lev_line = "Leverage %dx: the lowest that keeps the margin under %s." % (o["leverage"], cc.fmt_money(lim["margin_cap_usd"]))
    lev_line += " Caps: your max %dx; liquidation at least %gx the stop distance allows up to %dx" % (
        lim["lev_cfg"], float(cfg.get("min_liq_buffer") or 0), lim["lev_safe"])
    if lim["lev_venue"] and lim["lev_venue"] < 125:
        lev_line += "; the venue allows %dx" % lim["lev_venue"]
    cc.out(lev_line + ".")
    cc.out("Stop %s (%s): lose about $%.2f there, fees, slippage and funding included." % (P(o["stop"]), o["stop_type"], o["loss_at_stop_usd"]))
    cc.out("Target %s (%s): make about $%.2f there after fees. Reward:risk %.2f." % (P(o["target"]), o["target_type"],
                                                                                   o["gain_at_target_usd"], o["reward_risk"]))
    sdist = abs(o["entry"] - o["stop"]) / o["entry"]
    cc.out("Estimated liquidation %s; the stop is %s away (%s, maintenance %s)." %
           (cc.fmt_liq(o["liq_est"], o["entry"], o.get("tick")), cc.fmt_pct(sdist, 2, False), o["mmr_source"],
            cc.fmt_pct(o["mmr"], 2, False)))
    cc.out("Out by %s, win or lose." % cc.fmt_local(o["exit_by"]))
    for n in o.get("notes") or []:
        cc.out("Note: " + n)
    if o.get("timemachine") and o["timemachine"].get("verdict"):
        cc.out("Time Machine: " + o["timemachine"]["verdict"].replace("Verdict: ", ""))
    else:
        cc.out("Time Machine: not run for this plan. /crypto-timemachine replays it first.")
    cc.out("%s. A stop is an order, not a promise: in a fast market it can fill past the stop." %
           ("Paper trading" if o["paper"] else "LIVE account"))
    cc.out("Next: /crypto-liquidation to double-check the liquidation, then /crypto-risk.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="size.py", description="Size the plan into an order.")
    p.add_argument("--plan", default=None)
    p.add_argument("--risk", type=float, default=None, help="USD to risk on this trade (capped at max_loss_per_trade_usd)")
    p.add_argument("--leverage", type=int, default=None)
    p.add_argument("--limit", nargs="?", const="plan", default=None, help="a limit entry at the plan's entry, or at PRICE")
    cc.add_common(p, venue=False)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    plan = cc.read_json(a.plan or cc.wpath("plan.json"), None)
    if not plan:
        raise cc.CryptoError("no plan to size. Make one first: /crypto-exit")
    if cc.now_ms() - plan.get("created_at", 0) > 12 * cc.HOUR_MS:
        raise cc.CryptoError("the plan is %s old. Make a fresh one with /crypto-exit." % cc.fmt_dur(cc.now_ms() - plan["created_at"]))
    if plan["side"] == "short" and not cfg.get("allow_short", True):
        raise cc.CryptoError("shorts are off in your config (allow_short is false)")
    blocked = set(str(x).upper() for x in cfg.get("blocked") or [])
    if plan["coin"].upper() in blocked or plan["symbol"].upper() in blocked:
        raise cc.CryptoError("%s is on your blocked list" % plan["coin"])
    venue = cc.get_venue(plan["venue"])
    order = size(plan, cfg, venue, a)
    if not a.no_write:
        cc.write_json(cc.wpath("orders.json"), {"version": 1, "created_at": order["created_at"], "orders": [order]})
    if a.json:
        cc.dump(order)
    else:
        print_order(order, cfg, venue.market(plan["symbol"]))
    return 0


if __name__ == "__main__":
    sys.exit(cc.run(main))
