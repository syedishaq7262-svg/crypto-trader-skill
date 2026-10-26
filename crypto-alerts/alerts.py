#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-alerts: rings when a price, a stop, a time limit or a macro release is reached.

    python3 alerts.py add --from-plan                 the plan's stop, target and time limit
    python3 alerts.py add --from-positions            stop, target, time limit, past-the-stop and near-liquidation
    python3 alerts.py add BTC --above 70000           a level (--below for the other way)
    python3 alerts.py add SOL --pct 3                 a 3% move either way from the price now
    python3 alerts.py add ETH --pct 2 --within 1h     a 2% move inside any hour
    python3 alerts.py add BTC --funding-above 50      funding above 50% a year
    python3 alerts.py add --macro 15                  15 minutes before each high-impact US release
    python3 alerts.py add --at "2026-10-21 18:00" --label "check the trade"
    python3 alerts.py check                           exit 0 when something rang
    python3 alerts.py watch --for 8h                  check every minute until something rings
    python3 alerts.py list | remove ID | reset ID | clear
    python3 alerts.py quote BTC,SOL                   last, mark, 24h change and funding

A level counts as reached when any 1-minute candle since the last check touched it, not
only when the price sits there at the moment of the check. An alert is information,
never an order.
"""

import argparse
import importlib.util
import os
import sys
import time

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
MIN, HOUR, DAY = 60000, 3600000, 86400000
MAX_LOOKBACK = 6 * HOUR             # candles read for touches between checks
REARM_GAP = 0.0025                  # a re-arming level rule needs the price 0.25% back on the other side
NEAR_LIQ = 0.25                     # near liquidation: inside the last quarter of the way from entry to liquidation
PAST_STOP_R = 0.25                  # past the stop by a quarter of the risk with the position still open


def path():
    return cc.wpath("alerts.json")


def load():
    d = cc.read_json(path(), None) or {}
    d.setdefault("rules", [])
    d.setdefault("macro_rung", {})
    return d


def save(d):
    cc.write_json(path(), d)


def new_id(d, base):
    base = base.lower().replace(" ", "-")
    ids = set(r["id"] for r in d["rules"])
    if base not in ids:
        return base
    k = 2
    while "%s-%d" % (base, k) in ids:
        k += 1
    return "%s-%d" % (base, k)


def describe(r):
    P = lambda x: cc.fmt_price(x, r.get("tick"))
    k = r["kind"]
    if k in ("above", "below"):
        return "%s %s %s%s" % (r["coin"], k, P(r["price"]), (" (%s)" % r["label"]) if r.get("label") else "")
    if k == "pct":
        if r.get("within_ms"):
            return "%s moves %g%%%s inside %s" % (r["coin"], r["pct"], {"up": " up", "down": " down"}.get(r.get("direction"), ""),
                                                 cc.fmt_dur(r["within_ms"]))
        return "%s moves %g%%%s from %s" % (r["coin"], r["pct"], {"up": " up", "down": " down"}.get(r.get("direction"), ""),
                                           P(r["ref"]))
    if k == "funding":
        return "%s funding %s %g%% a year" % (r["coin"], r["dir"], r["apr"])
    if k == "time":
        return "at %s%s" % (cc.fmt_local(r["at"]), (": %s" % r["label"]) if r.get("label") else "")
    if k == "macro":
        return "%d minutes before each %s %s-impact release" % (r["lead_min"], r["countries"], r["impact"])
    if k == "past_stop":
        return "%s still open %.2fR past its stop %s" % (r["coin"], PAST_STOP_R, P(r["stop"]))
    if k == "near_liq":
        return "%s mark with under %d%% of the distance to liquidation %s left" % (r["coin"], round(NEAR_LIQ * 100), P(r["liq"]))
    return k


# ---------------------------------------------------------------- adding rules

def rule_base(coin, venue, m, kind, source, expires):
    return {"coin": m["coin"] if m else coin, "symbol": m["symbol"] if m else None, "venue": venue, "kind": kind,
            "tick": m.get("tick") if m else None, "source": source, "created_at": cc.now_ms(), "expires_at": expires,
            "rung_at": None, "rearm": False}


def add_from_plan(d, a):
    p = cc.read_json(cc.wpath("plan.json"), None)
    if not p:
        raise cc.CryptoError("no plan yet. /crypto-exit makes one.")
    if p.get("exit_by") and p["exit_by"] <= cc.now_ms():
        raise cc.CryptoError("the plan's time limit has passed (%s). Make a new plan." % cc.fmt_local(p["exit_by"]))
    m = {"coin": p["coin"], "symbol": p["symbol"], "tick": p.get("tick")}
    long_ = p["side"] == "long"
    out = []
    tag = "%s-plan-%s" % (p["coin"], p.get("plan_id", "")[:4])
    for kind, price, label in ((("below" if long_ else "above"), p["stop"], "the plan's stop"),
                               (("above" if long_ else "below"), p["target"], "the plan's target")):
        r = rule_base(p["coin"], p["venue"], m, kind, "plan", p.get("exit_by"))
        r.update(id=new_id(d, "%s-%s" % (tag, "stop" if "stop" in label else "target")), price=price, label=label,
                 plan_id=p.get("plan_id"), entry=p["entry"], side=p["side"])
        d["rules"].append(r)
        out.append(r)
    if p.get("exit_by"):
        r = rule_base(p["coin"], p["venue"], m, "time", "plan", p["exit_by"] + HOUR)
        r.update(id=new_id(d, "%s-time" % tag), at=p["exit_by"], label="the plan's time limit: out, win or lose",
                 plan_id=p.get("plan_id"))
        d["rules"].append(r)
        out.append(r)
    return out


def add_from_positions(d, a):
    pos = cc.read_positions()
    if not pos:
        raise cc.CryptoError("no open positions. /crypto-broker shows them once something is placed.")
    out = []
    for p in pos:
        if any(r.get("position_id") == p["id"] for r in d["rules"]):
            continue
        m = {"coin": p["coin"], "symbol": p["symbol"], "tick": p.get("tick")}
        long_ = p["side"] == "long"
        tag = "%s-pos" % p["coin"]
        specs = [(("below" if long_ else "above"), {"price": p["stop"], "label": "your stop"}, "stop"),
                 (("above" if long_ else "below"), {"price": p["target"], "label": "your target"}, "target"),
                 ("past_stop", {"stop": p["stop"], "entry": p["entry"], "side": p["side"]}, "past-stop")]
        if p.get("liq") and p["liq"] > 0:
            specs.append(("near_liq", {"liq": p["liq"], "entry": p["entry"], "side": p["side"]}, "near-liq"))
        for kind, extra, name in specs:
            r = rule_base(p["coin"], p["venue"], m, kind, "position", p.get("exit_by"))
            r.update(id=new_id(d, "%s-%s" % (tag, name)), position_id=p["id"], **extra)
            d["rules"].append(r)
            out.append(r)
        if p.get("exit_by"):
            r = rule_base(p["coin"], p["venue"], m, "time", "position", p["exit_by"] + HOUR)
            r.update(id=new_id(d, "%s-time" % tag), at=p["exit_by"], position_id=p["id"],
                     label="%s %s time limit: close it, win or lose (/crypto-broker close %s --yes %s)" % (
                         p["coin"], p["side"], p["coin"], p["coin"]))
            d["rules"].append(r)
            out.append(r)
    return out


def add_manual(d, a, venue):
    expires = cc.now_ms() + int(cc.parse_hold(a.expires, 168.0) * HOUR)
    if a.at or a.in_:
        at = cc.parse_when(a.at) if a.at else cc.now_ms() + int(cc.parse_hold(a.in_) * HOUR)
        if at <= cc.now_ms():
            raise cc.CryptoError("that time has passed")
        r = rule_base(None, None, None, "time", "manual", at + HOUR)
        r.update(id=new_id(d, "time-%s" % cc.fmt_utc(at).replace(" ", "-").replace(":", "")), at=at, label=a.label or "")
        r["coin"] = None
        d["rules"].append(r)
        return [r]
    if a.macro is not None:
        if a.macro < 1 or a.macro > 24 * 60:
            raise cc.CryptoError("--macro takes minutes before the release, 1 to 1440")
        r = rule_base(None, None, None, "macro", "manual", expires)
        r.update(id=new_id(d, "macro-%d" % a.macro), lead_min=a.macro, countries=a.countries.upper(), impact=a.impact)
        r["coin"] = None
        d["rules"].append(r)
        return [r]
    if not a.coin:
        raise cc.CryptoError("which coin? For example: alerts.py add BTC --above 70000")
    m = venue.market(a.coin)
    t = venue.ticker(m["symbol"])
    last = t["last"]
    out = []
    if a.above is not None or a.below is not None:
        for kind, price in (("above", a.above), ("below", a.below)):
            if price is None:
                continue
            if price <= 0:
                raise cc.CryptoError("a level must be a positive price")
            if (kind == "above" and last >= price) or (kind == "below" and last <= price):
                raise cc.CryptoError("%s is already %s %s (now %s)" % (m["coin"], kind, cc.fmt_price(price, m.get("tick")),
                                                                    cc.fmt_price(last, m.get("tick"))))
            r = rule_base(None, venue.name, m, kind, "manual", expires)
            r.update(id=new_id(d, "%s-%s-%s" % (m["coin"], kind, ("%.10g" % price))),
                     price=price, label=a.label or "", rearm=a.rearm)
            d["rules"].append(r)
            out.append(r)
    if a.pct is not None:
        if a.pct <= 0 or a.pct > 90:
            raise cc.CryptoError("--pct takes a percent between 0 and 90")
        r = rule_base(None, venue.name, m, "pct", "manual", expires)
        r.update(id=new_id(d, "%s-move-%g" % (m["coin"], a.pct)), pct=a.pct, ref=last, direction=a.direction,
                 within_ms=int(cc.parse_hold(a.within) * HOUR) if a.within else None, label=a.label or "",
                 rearm=bool(a.within) or a.rearm)
        d["rules"].append(r)
        out.append(r)
    for dirn, apr in (("above", a.funding_above), ("below", a.funding_below)):
        if apr is None:
            continue
        r = rule_base(None, venue.name, m, "funding", "manual", expires)
        r.update(id=new_id(d, "%s-funding-%s" % (m["coin"], dirn)), dir=dirn, apr=apr, label=a.label or "", rearm=a.rearm)
        d["rules"].append(r)
        out.append(r)
    if not out:
        raise cc.CryptoError("what should ring? Add --above, --below, --pct, --funding-above or --funding-below")
    return out


# ---------------------------------------------------------------- checking

def candles_since(venue, symbol, since):
    since = max(since, cc.now_ms() - MAX_LOOKBACK)
    try:
        return venue.bars_between(symbol, "1m", since, cc.now_ms())
    except cc.CryptoError:
        return []


def macro_events(r):
    path_ = os.path.join(HERE, os.pardir, "crypto-news", "news.py")
    if not os.path.isfile(path_):
        raise cc.CryptoError("macro alerts need crypto-news installed next to this skill")
    spec = importlib.util.spec_from_file_location("crypto_alerts_news", path_)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    events, _ = mod.macro_calendar()
    levels = ("High",) if r["impact"] == "high" else ("High", "Medium")
    cs = set(x.strip() for x in r["countries"].split(",") if x.strip())
    return [e for e in events if e["impact"] in levels and (not cs or e["country"] in cs)]


def market_state(rules, venues, last_check):
    """One ticker read per venue and 1-minute candles per symbol since the earliest moment any rule needs."""
    tickers, bars, since = {}, {}, {}
    for r in rules:
        if r.get("symbol") and not r.get("retired"):
            key = (r["venue"], r["symbol"])
            need = max(last_check or r["created_at"], r["created_at"])
            since[key] = min(since.get(key, need), need)
    for (vn, sym), t0 in since.items():
        v = venues.get(vn) or venues.setdefault(vn, cc.get_venue(vn))
        tickers[(vn, sym)] = v.tickers().get(sym)
        bars[(vn, sym)] = candles_since(v, sym, t0)
    return tickers, bars


def condition(r, d, t, bs, venues, now):
    """The message when the rule's condition holds now (or held on a candle since the last check), else None."""
    P = lambda x: cc.fmt_price(x, r.get("tick"))
    k = r["kind"]
    if k == "time":
        if now < r["at"]:
            return None
        late = (" (rang %s late: no check ran sooner)" % cc.fmt_dur(now - r["at"])) if now - r["at"] > 5 * MIN else ""
        return "%s%s%s." % (cc.fmt_local(r["at"]), (": %s" % r["label"]) if r.get("label") else "", late)
    last, mark = t["last"], t.get("mark") or t["last"]
    if k in ("above", "below"):
        up = k == "above"
        touch = next((b for b in bs if (up and b[H] >= r["price"]) or (not up and b[L] <= r["price"])), None)
        if touch is None and ((up and last >= r["price"]) or (not up and last <= r["price"])):
            touch = [now - MIN, last, last, last, last]
        if touch is None:
            return None
        msg = "%s %s %s%s: touched %s at %s; now %s." % (r["coin"], "reached" if up else "fell to", P(r["price"]),
                                                       (" (%s)" % r["label"]) if r.get("label") else "",
                                                       P(touch[H] if up else touch[L]), cc.fmt_local(touch[T] + MIN, date=False),
                                                       P(last))
        if r["source"] in ("plan", "position"):
            what = "stop" if "stop" in (r.get("label") or "") else "take-profit"
            msg += " If the %s order is on the exchange it has probably filled: /crypto-broker sync." % what
        return msg
    if k == "pct":
        if r.get("within_ms"):
            hist = venues[r["venue"]].bars(r["symbol"], "5m", int(r["within_ms"] // 300000) + 1)
            if len(hist) < 2:
                return None
            ch = last / hist[0][O] - 1.0
        else:
            ch = last / r["ref"] - 1.0
            for b in bs:
                for px in (b[H], b[L]):
                    if abs(px / r["ref"] - 1.0) > abs(ch):
                        ch = px / r["ref"] - 1.0
        want = r["pct"] / 100.0
        ok = (ch >= want if r.get("direction") == "up" else ch <= -want if r.get("direction") == "down" else abs(ch) >= want)
        if not ok:
            return None
        return "%s moved %s %s; now %s." % (r["coin"], cc.fmt_pct(ch, 2), ("inside %s" % cc.fmt_dur(r["within_ms"]))
                                           if r.get("within_ms") else "from %s" % P(r["ref"]), P(last))
    if k == "funding":
        apr = cc.annualize_funding(t.get("funding"), t.get("funding_hours") or 8.0)
        if apr is None or not ((r["dir"] == "above" and apr * 100 >= r["apr"]) or (r["dir"] == "below" and apr * 100 <= r["apr"])):
            return None
        return "%s funding is %s, %s your %g%% a year: %s pay." % (
            r["coin"], cc.fmt_funding(t.get("funding"), t.get("funding_hours") or 8.0), r["dir"], r["apr"],
            "longs" if apr > 0 else "shorts")
    if k == "past_stop":
        beyond = cc.side_sign(r["side"]) * (r["stop"] - mark) / (abs(r["entry"] - r["stop"]) or 1e-12)
        if beyond < PAST_STOP_R:
            return None
        return ("%s mark %s is %.2fR past the stop %s and the position is still open. The stop may not be on the exchange: "
                "check it now (/crypto-broker sync), and close it if it is missing." % (r["coin"], P(mark), beyond, P(r["stop"])))
    if k == "near_liq":
        left = abs(mark - r["liq"]) / (abs(r["entry"] - r["liq"]) or 1e-12)
        on_side = (r["side"] == "long" and mark > r["liq"]) or (r["side"] == "short" and mark < r["liq"])
        if left > NEAR_LIQ or not on_side:
            return None
        return ("%s mark %s has only %d%% of the distance to liquidation %s left (entry %s). Isolated margin: this "
                "position's margin is what is at stake." % (r["coin"], P(mark), round(left * 100), P(r["liq"]), P(r["entry"])))
    return None


def rearm_ready(r, t):
    """A rung rule that asked to re-arm comes back once its condition has clearly let go."""
    if r["kind"] in ("above", "below"):
        gap = REARM_GAP * t["last"]
        return t["last"] < r["price"] - gap if r["kind"] == "above" else t["last"] > r["price"] + gap
    return True


def evaluate(d, rules, venues, now, last_check):
    """[(rule, message)] for every rule that rings now. Marks them rung; re-arms rules that ask for it."""
    rung = []
    open_ids = set(p["id"] for p in cc.read_positions())
    tickers, bars = market_state(rules, venues, last_check)
    for r in rules:
        if r.get("position_id") and r["position_id"] not in open_ids:
            r["retired"] = "the position closed"
            continue
        if r["kind"] == "macro":
            try:
                evs = macro_events(r)
            except cc.CryptoError as e:
                r["last_error"] = str(e)
                continue
            for e in evs:
                key = "%s|%s" % (e["at"], e["title"])
                if e["at"] - r["lead_min"] * MIN <= now < e["at"] + 5 * MIN and key not in d["macro_rung"]:
                    d["macro_rung"][key] = now
                    rung.append((r, "%s %s at %s, in %s: a big candle can come with it." % (
                        e["country"], e["title"], cc.fmt_local(e["at"]), cc.fmt_dur(max(0, e["at"] - now)))))
            continue
        t = None
        if r.get("symbol"):
            t = tickers.get((r["venue"], r["symbol"]))
            if not t:
                r["last_error"] = "no price for %s" % r["symbol"]
                continue
        bs = [b for b in bars.get((r.get("venue"), r.get("symbol"))) or []
              if b[T] + MIN > max(last_check or 0, r["created_at"])]
        msg = condition(r, d, t, bs, venues, now)
        if r.get("rung_at"):
            if r.get("rearm") and msg is None and rearm_ready(r, t):
                r["rung_at"] = None
            continue
        if msg:
            rung.append((r, msg))
    for r, _ in rung:
        r["rings"] = r.get("rings", 0) + 1
        if r["kind"] == "macro":                 # it rings once per release, and keeps watching for the next
            r["last_rang"] = now
            continue
        r["rung_at"] = now
        if r["source"] == "plan" and r["kind"] in ("above", "below"):
            for o in rules:                  # the plan's trade is over at its stop or its target: the rest go quiet
                if o is not r and o.get("plan_id") == r.get("plan_id") and o["source"] == "plan" and not o.get("rung_at"):
                    o["retired"] = "the plan's %s rang" % ("stop" if "stop" in (r.get("label") or "") else "target")
    return rung


def check(a, d, quiet=False):
    now = cc.now_ms()
    last_check = d.get("last_check")
    active = [r for r in d["rules"] if not r.get("retired") and (not r.get("expires_at") or now < r["expires_at"])]
    for r in d["rules"]:
        if not r.get("retired") and r.get("expires_at") and now >= r["expires_at"]:
            r["retired"] = "expired"
    venues = {}
    rung = evaluate(d, active, venues, now, last_check)
    d["last_check"] = now
    d["rules"] = [r for r in d["rules"] if not r.get("retired") or now - r.get("created_at", now) < 7 * DAY]
    if not a.no_write:
        save(d)
        for r, msg in rung:
            cc.append_jsonl(cc.wpath("alerts_log.jsonl"), {"at": now, "id": r["id"], "message": msg})
    if a.json:
        cc.dump({"as_of": now, "rang": [{"id": r["id"], "message": m} for r, m in rung],
                 "watching": [r["id"] for r in active if not r.get("rung_at") and not r.get("retired")]})
        return 0 if rung else 1
    for r, msg in rung:
        cc.out("ALERT  %s" % msg)
    if not rung and not quiet:
        watching = [r for r in active if not r.get("rung_at") and not r.get("retired")]
        if watching:
            cc.out("Nothing rang at %s. Watching %d rule%s%s." % (
                cc.fmt_local(now), len(watching), "" if len(watching) == 1 else "s", status_line(watching, venues)))
        else:
            cc.out("Nothing rang at %s, and no rule is left to watch: every alert has rung, expired or retired "
                   "(alerts.py list)." % cc.fmt_local(now))
    for r in active:
        if r.get("last_error") and not quiet:
            cc.out("  %s: %s" % (r["id"], r["last_error"]))
    return 0 if rung else 1


def status_line(rules, venues):
    bits = []
    for r in rules:
        if r["kind"] not in ("above", "below") or not r.get("symbol"):
            continue
        v = venues.get(r["venue"])
        t = v.tickers().get(r["symbol"]) if v else None
        if not t:
            continue
        bits.append("%s %s %s (%s away)" % (r["coin"], r["kind"], cc.fmt_price(r["price"], r.get("tick")),
                                            cc.fmt_pct(abs(r["price"] / t["last"] - 1.0), 2, False)))
    return (": " + "; ".join(bits[:6])) if bits else ""


# ---------------------------------------------------------------- commands

def build_parser():
    p = argparse.ArgumentParser(prog="alerts.py", description="Price, time and position alerts.")
    p.add_argument("command", choices=["add", "check", "watch", "list", "remove", "reset", "clear", "quote"])
    p.add_argument("coin", nargs="?", default=None, help="add / quote: the coin(s); remove / reset: the rule id")
    p.add_argument("--from-plan", action="store_true", help="the plan's stop, target and time limit")
    p.add_argument("--from-positions", action="store_true", help="every open position's stop, target, time, liquidation")
    p.add_argument("--above", type=float, default=None)
    p.add_argument("--below", type=float, default=None)
    p.add_argument("--pct", type=float, default=None, help="a move of this many percent")
    p.add_argument("--within", default=None, help="with --pct: inside this window (1h, 15m), not from the price now")
    p.add_argument("--direction", default=None, choices=["up", "down"], help="with --pct: one way only")
    p.add_argument("--funding-above", type=float, default=None, help="funding above this percent a year")
    p.add_argument("--funding-below", type=float, default=None, help="funding below this percent a year (negative)")
    p.add_argument("--at", default=None, help='a time, "2026-10-21 18:00" (UTC unless an offset is given)')
    p.add_argument("--in", dest="in_", default=None, help="a time from now: 90m, 2h")
    p.add_argument("--macro", type=int, default=None, help="minutes before each high-impact release")
    p.add_argument("--countries", default="USD", help="--macro: currencies (default USD)")
    p.add_argument("--impact", default="high", choices=["high", "medium"])
    p.add_argument("--label", default=None)
    p.add_argument("--rearm", action="store_true", help="ring again after the price moves back")
    p.add_argument("--expires", default="7d", help="manual rules stop after this long (default 7d)")
    p.add_argument("--for", dest="duration", default="8h", help="watch: give up after this long (default 8h)")
    p.add_argument("--every", type=float, default=60.0, help="watch: seconds between checks (default 60)")
    p.add_argument("--keep", action="store_true", help="watch: keep going after something rings")
    p.add_argument("--max-checks", type=int, default=None)
    p.add_argument("--all", action="store_true", help="clear: every rule, not only rung and retired ones")
    cc.add_common(p)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cc.apply_common(a)
    d = load()
    if a.command == "add":
        venue = None
        if a.from_plan:
            added = add_from_plan(d, a)
        elif a.from_positions:
            added = add_from_positions(d, a)
            if not added:
                cc.out("Every open position already has its alerts.")
                return 0
        else:
            if a.coin:
                venue = cc.get_venue(a.venue)
            added = add_manual(d, a, venue)
        if not a.no_write:
            save(d)
        for r in added:
            cc.out("Added %s: %s%s." % (r["id"], describe(r), (", until %s" % cc.fmt_local(r["expires_at"]))
                                       if r.get("expires_at") and r["kind"] != "time" else ""))
        cc.out("alerts.py check rings them; alerts.py watch keeps checking. Alerts never place or close anything.")
        return 0
    if a.command == "check":
        if not d["rules"]:
            raise cc.CryptoError("no alerts set. Try: alerts.py add --from-plan")
        return check(a, d)
    if a.command == "watch":
        if not d["rules"]:
            raise cc.CryptoError("no alerts set. Try: alerts.py add --from-plan")
        limit = cc.parse_hold(a.duration, 8.0) * HOUR
        start, n, rang_any = cc.now_ms(), 0, False
        cc.out("Watching %d rule%s until %s, every %gs." % (len(d["rules"]), "" if len(d["rules"]) == 1 else "s",
                                                          cc.fmt_local(start + int(limit)), a.every))
        while True:
            n += 1
            code = check(a, d, quiet=True)
            rang_any = rang_any or code == 0
            if code == 0 and not a.keep:
                return 0
            live = [r for r in d["rules"] if not r.get("retired") and not (r.get("rung_at") and not r.get("rearm"))]
            if not live:
                cc.out("Every alert has rung or retired.")
                return 0 if rang_any else 2
            if (a.max_checks and n >= a.max_checks) or cc.now_ms() - start + a.every * 1000 > limit:
                cc.out("Stopped after %d check%s; nothing %s." % (n, "" if n == 1 else "s", "more rang" if rang_any else "rang"))
                return 0 if rang_any else 3
            if cc.pinned():
                cc.set_now(cc.now_ms() + int(a.every * 1000))
            else:
                time.sleep(max(5.0, a.every))
    if a.command == "list":
        now = cc.now_ms()
        if a.json:
            cc.dump(d)
            return 0
        if not d["rules"]:
            cc.out("No alerts.")
            return 0
        rows = []
        for r in d["rules"]:
            state = ("rang %s" % cc.fmt_local(r["rung_at"], date=False) if r.get("rung_at") else r["retired"] if r.get("retired")
                     else "expired" if r.get("expires_at") and now >= r["expires_at"] else "watching")
            if r.get("last_rang") and state == "watching":
                state += " (last rang %s)" % cc.fmt_local(r["last_rang"], date=False)
            rows.append([r["id"], describe(r), r["source"], state])
        cc.out(cc.table(rows, ["id", "rule", "from", "state"], "llll"))
        return 0
    if a.command in ("remove", "reset"):
        if not a.coin:
            raise cc.CryptoError("which rule? Give its id (alerts.py list shows them)")
        hits = [r for r in d["rules"] if r["id"] == a.coin.lower()]
        if not hits:
            raise cc.CryptoError("no rule %r. alerts.py list shows the ids" % a.coin)
        if a.command == "remove":
            d["rules"] = [r for r in d["rules"] if r["id"] != hits[0]["id"]]
            cc.out("Removed %s." % hits[0]["id"])
        else:
            hits[0]["rung_at"] = None
            hits[0].pop("retired", None)
            cc.out("Re-armed %s: %s." % (hits[0]["id"], describe(hits[0])))
        if not a.no_write:
            save(d)
        return 0
    if a.command == "clear":
        before = len(d["rules"])
        d["rules"] = [] if a.all else [r for r in d["rules"] if not r.get("retired") and not r.get("rung_at")]
        d["macro_rung"] = {} if a.all else d["macro_rung"]
        if not a.no_write:
            save(d)
        cc.out("Cleared %d rule%s." % (before - len(d["rules"]), "" if before - len(d["rules"]) == 1 else "s"))
        return 0
    if a.command == "quote":
        if not a.coin:
            raise cc.CryptoError("which coins? For example: alerts.py quote BTC,SOL")
        venue = cc.get_venue(a.venue)
        rows, data = [], []
        for c in a.coin.split(","):
            m = venue.market(c.strip())
            t = venue.ticker(m["symbol"])
            P = lambda x: cc.fmt_price(x, m.get("tick"))
            apr = cc.annualize_funding(t.get("funding"), t.get("funding_hours") or 8.0)
            data.append(dict(t, coin=m["coin"], funding_apr=apr))
            rows.append([m["coin"], P(t["last"]), P(t.get("mark") or t["last"]), cc.fmt_pct(t.get("change") or 0, 2),
                         cc.fmt_funding(t.get("funding"), t.get("funding_hours") or 8.0) if t.get("funding") is not None else "-",
                         cc.fmt_usd(t.get("quote_volume") or 0)])
        if a.json:
            cc.dump({"as_of": cc.now_ms(), "venue": venue.name, "quotes": data})
            return 0
        cc.out("Quotes | %s | %s" % (venue.label, cc.fmt_local(cc.now_ms())))
        cc.out(cc.table(rows, ["coin", "last", "mark", "24h", "funding", "24h volume"], "lrrrrr"))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(cc.run(main))
