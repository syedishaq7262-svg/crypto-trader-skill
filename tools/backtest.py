#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Walk-forward backtest: the pack's setups replayed on the past, as if each moment were "now".

    python3 tools/backtest.py                        the 40 busiest coins, the last 90 days, on the best venue from here
    python3 tools/backtest.py --coins 20 --days 60   smaller and faster
    python3 tools/backtest.py --end 2026-07-29       a window in the past: the 90 days up to 29 July
    python3 tools/backtest.py --venue bybit
    python3 tools/backtest.py --cache bt-data        keep the candles it downloads, to run again without downloading
    python3 tools/backtest.py --trades backtest/2026-10-27-binance-90d-trades.csv.gz --venue binance --end "2026-10-27 06:00"
                                                     re-read an earlier run's trades: the checks, the paper account
                                                     and the track record again, nothing downloaded

How it works. It steps through the past one candle at a time. At each step the scanner's own rules see only the
candles that had closed by then, never a later one. When a setup fires, the trade is planned the way /crypto-exit
plans it: entry at the trigger, the setup's own stop, a target at 2R, out after 24 hours whatever happens, one
trade at a time per coin, setup and side. Then it is played forward on the 5-minute candles that really came next,
paying the exchange's fees, the slippage measured from each coin's order book, and the funding that was actually
charged. Fake money: nothing reads or touches an account.

It also checks every setup against random entries at the same times of day (same stop distance, same target),
replays the pack's two checks as they would have stood at the time (the cost rule: a stop-out may cost at most
max_cost_r, 0.2R; the 30-day check: the same coin, setup and side made money after costs over its last 30 days),
and runs a $1,000 paper account under the pack's default risk limits 100 times, taking signals that fire at the
same moment in a random order each time.

Writes backtest_report.txt (the summary), backtest_trades.csv (every trade), backtest.json (the numbers) and
track_record.json (each setup's record). The Scout, the Scanner, the Coin Report and the Exit Planner show the
record that ships with the pack; copy your own track_record.json into your crypto/ folder to show yours instead.
Exit code 0 when the backtest ran.
"""

import argparse
import concurrent.futures
import csv
import gzip
import importlib.util
import json
import math
import multiprocessing
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "crypto-setup"))
import cryptocore as cc  # noqa: E402

T, O, H, L, C = cc.T, cc.O, cc.H, cc.L, cc.C
STEP = 300000
DAY = 86400000
WARMUP_DAYS = 12               # the setups' averages and bands need this much history before the first signal
SETUPS = [("orb-utc", "orb", {"session": "utc"}), ("orb-ny", "orb", {"session": "ny"}), ("breakout", "breakout", {}),
          ("squeeze", "squeeze", {}), ("trend", "trend", {}), ("reversal", "reversal", {}), ("volume", "volume", {})]
SETUP_WORDS = {"orb-utc": "opening range, UTC day", "orb-ny": "opening range, New York", "breakout": "range breakout",
               "squeeze": "squeeze break", "trend": "trend pullback", "reversal": "RSI reversal", "volume": "volume spike"}
EVIDENCE_DAYS = 30
EVIDENCE_MIN_TRADES = 5
CHECKS = (("every", "every signal"), ("cost", "cost rule"), ("record", "30-day check"), ("both", "both checks"))
CSV_COLS = ["coin", "symbol", "setup", "side", "signal_utc", "entry", "stop", "target", "exit_utc", "exit", "outcome",
            "r_after_costs", "r_before_costs", "stop_distance_pct", "minutes_held", "stop_cost_r"]
_MODS = {}


def load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("crypto_bt_" + name, os.path.join(ROOT, folder, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[key] = mod
    return _MODS[key]


# ---------------------------------------------------------------- data

def fetch_coin(venue, m, start, end, cache_dir):
    """5-minute bars from `start` to `end`, the funding charged over them, and today's costs for the coin."""
    path = None
    if cache_dir:
        path = os.path.join(cache_dir, "%s-%s-%d-%d.json.gz" % (venue.name, m["symbol"], start, end))
        if os.path.exists(path):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
    bars = venue.bars_between(m["symbol"], "5m", start, end - STEP)
    funding = venue.funding_history(m["symbol"], start, end)
    t = venue.ticker(m["symbol"])
    slip, _ = cc.measure_slippage(venue, m["symbol"])
    data = {"market": {k: m.get(k) for k in ("symbol", "coin", "multiplier", "tick", "funding_hours")},
            "bars": [list(b) for b in bars], "funding": [list(f) for f in funding],
            "costs": {"taker": venue.taker_fee, "maker": venue.maker_fee, "slippage": slip},
            "funding_rate": t.get("funding") or 0.0}
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
    return data


def read_trades(path):
    """An earlier run's backtest_trades.csv (or .csv.gz) as trades. (trades, costs_estimated)."""
    try:
        if path.endswith(".gz"):
            fh = gzip.open(path, "rt", encoding="utf-8", newline="")
        else:
            fh = open(path, encoding="utf-8", newline="")
        with fh:
            rows = list(csv.DictReader(fh))
    except (IOError, OSError) as e:
        raise cc.CryptoError("could not read %s: %s" % (path, e))
    need = set(CSV_COLS) - {"stop_cost_r"}
    if not rows or need - set(rows[0]):
        raise cc.CryptoError("%s is not a backtest_trades.csv (missing: %s)" % (path, ", ".join(sorted(need - set(rows[0] if rows else {})))))
    trades = []
    for x in rows:
        t = {"coin": x["coin"], "symbol": x["symbol"], "setup": x["setup"], "side": x["side"],
             "at": cc.parse_when(x["signal_utc"]), "exit_at": cc.parse_when(x["exit_utc"]), "entry": float(x["entry"]),
             "stop": float(x["stop"]), "target": float(x["target"]), "exit": float(x["exit"]),
             "outcome": {"time limit": "open"}.get(x["outcome"], x["outcome"]), "r": float(x["r_after_costs"]),
             "gross_r": float(x["r_before_costs"]), "risk_pct": float(x["stop_distance_pct"]) / 100.0,
             "bars": int(x["minutes_held"]) // 5}
        if (x.get("stop_cost_r") or "").strip():
            t["stop_cost_r"] = float(x["stop_cost_r"])
        trades.append(t)
    estimated = any("stop_cost_r" not in t for t in trades)
    if estimated:
        estimate_stop_costs(trades)
    return trades, estimated


def estimate_stop_costs(trades):
    """For files from before the stop_cost_r column: each coin's fees and slippage a side, rebuilt from its own
    stop-outs (what they cost beyond the stop, which also carries a little funding), then each trade's stop-out
    cost in R from that."""
    per = {}
    for t in trades:
        if t["outcome"] == "stop":
            risk = abs(t["entry"] - t["stop"])
            per.setdefault(t["coin"], []).append((t["gross_r"] - t["r"]) * risk / (t["entry"] + t["exit"]))
    rate = dict((c, cc.median(v)) for c, v in per.items())
    fallback = cc.median(list(rate.values())) if rate else cc.DEFAULT_SLIPPAGE + 0.0005
    for t in trades:
        k = max(0.0, rate.get(t["coin"], fallback))
        t["stop_cost_r"] = cc.stop_cost_r(t["entry"], t["stop"], {"taker": k, "slippage": 0.0})


# ---------------------------------------------------------------- one coin, every setup (runs in a worker process)

def test_coin(job):
    data, window_start, target_r, n_steps, seed, per_trade = job
    scan = load("crypto-scanner", "scan")
    tm = load("crypto-timemachine", "timemachine")
    m = data["market"]
    bars = [tuple(b) for b in data["bars"]]
    funding = tm.Funding([tuple(f) for f in data["funding"]], data.get("funding_rate"), m.get("funding_hours") or 8.0)
    costs = data["costs"]
    specs = [("%s|%s" % (key, side), {"name": name, "params": params}, side)
             for key, name, params in SETUPS for side in ("long", "short")]
    found = tm.history_events_many(scan, specs, m, bars)
    trades, bases = [], {}
    for key, setup, side in specs:
        events = [e for e in found[key] if e["at"] >= window_start]
        plan = {"setup": setup, "side": side, "stop_spec": "setup", "target_r": target_r, "coin": m["coin"],
                "symbol": m["symbol"], "_seed": seed}
        hist = tm.run_history(scan, plan, m, bars, funding, costs, n_steps, events=events)
        name = key.split("|")[0]
        for tr in hist.get("trades") or []:
            sg = 1.0 if side == "long" else -1.0
            risk = abs(tr["entry"] - tr["stop"])
            trades.append({"coin": m["coin"], "symbol": m["symbol"], "setup": name, "side": side, "at": tr["at"],
                           "exit_at": tr["at"] + tr["bars"] * STEP, "entry": tr["entry"], "stop": tr["stop"],
                           "target": tr["target"], "exit": tr["exit"], "outcome": tr["outcome"], "r": tr["r"],
                           "gross_r": sg * (tr["exit"] - tr["entry"]) / risk, "risk_pct": risk / tr["entry"],
                           "bars": tr["bars"], "stop_cost_r": cc.stop_cost_r(tr["entry"], tr["stop"], costs)})
        if hist.get("n", 0) >= 3 and per_trade:
            b = tm.run_baseline(plan, bars, hist, funding, costs, n_steps, None, seed, per_trade=per_trade)
            if b:
                bases[key] = {"n": b["n"], "avg_r": b["avg_r"], "trades": hist["n"]}
    return {"coin": m["coin"], "trades": trades, "baselines": bases, "bars": len(bars)}


# ---------------------------------------------------------------- statistics

def mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def summary(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    p, lo, hi = cc.wilson(wins, n)
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r <= 0)
    avg = sum(rs) / n
    sd = math.sqrt(sum((r - avg) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    half = 1.645 * sd / math.sqrt(n)            # 90% range of the average
    eq, peak, dd, streak, worst_streak = 0.0, 0.0, 0.0, 0, 0
    for t in sorted(trades, key=lambda t: (t["exit_at"], t["at"])):
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if t["r"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
    return {"n": n, "wins": wins, "win_rate": p, "win_lo": lo, "win_hi": hi,
            "target": sum(1 for t in trades if t["outcome"] == "target"),
            "stop": sum(1 for t in trades if t["outcome"] == "stop"),
            "time": sum(1 for t in trades if t["outcome"] == "open"),
            "avg_r": avg, "avg_r_lo": avg - half, "avg_r_hi": avg + half, "total_r": sum(rs),
            "avg_gross_r": sum(t["gross_r"] for t in trades) / n,
            "avg_win_r": (gains / wins) if wins else None, "avg_loss_r": (-losses / (n - wins)) if n > wins else None,
            "profit_factor": (gains / losses) if losses else None, "max_dd_r": dd, "worst_losing_streak": worst_streak}


def evidence_filter(trades, start, days=EVIDENCE_DAYS, min_trades=EVIDENCE_MIN_TRADES):
    """The 30-day check (the Scout's first test), as it stood at each trade's time: the same coin, setup and side
    made money after costs over the trades that had closed in the 30 days before. Only trades after the first 30
    days are judged."""
    by_key = {}
    for t in trades:
        by_key.setdefault((t["coin"], t["setup"], t["side"]), []).append(t)
    judged, taken = [], []
    for ts in by_key.values():
        ts.sort(key=lambda t: t["at"])
        for t in ts:
            if t["at"] < start + days * DAY:
                continue
            judged.append(t)
            past = [p["r"] for p in ts if t["at"] - days * DAY <= p["at"] and p["exit_at"] <= t["at"]]
            if len(past) >= min_trades and sum(past) / len(past) > 0:
                taken.append(t)
    return judged, taken


def check_groups(trades, start, limit):
    """The trades judged by the pack's two checks: every signal after the first 30 days, those the cost rule lets
    through (a stop-out costs at most `limit` R, judged to two decimals as crypto-risk does), those with a positive
    30-day record, and those passing both."""
    judged, taken = evidence_filter(trades, start)
    ok = lambda t: not cc.over_cost_limit(t["stop_cost_r"], limit)
    order = lambda ts: sorted(ts, key=lambda t: (t["at"], t["coin"], t["setup"], t["side"]))
    return {"every": order(judged), "cost": order([t for t in judged if ok(t)]), "record": order(taken),
            "both": order([t for t in taken if ok(t)])}


def check_table(groups, start, end):
    """Each group's summary, and its average in each half of the judged stretch: a rule that helps only in one half
    is the market's mood, not a rule."""
    mid = start + EVIDENCE_DAYS * DAY + (end - start - EVIDENCE_DAYS * DAY) // 2
    out = {"mid": mid}
    for key, ts in groups.items():
        s = summary(ts)
        for half, part in (("first_half", [t["r"] for t in ts if t["at"] < mid]),
                           ("second_half", [t["r"] for t in ts if t["at"] >= mid])):
            s[half] = {"n": len(part), "avg_r": mean(part)}
        out[key] = s
    return out


def cost_split(trades, limit):
    """Over the whole window: the trades the cost rule would have refused against the rest."""
    over = [t for t in trades if cc.over_cost_limit(t["stop_cost_r"], limit)]
    within = [t for t in trades if not cc.over_cost_limit(t["stop_cost_r"], limit)]
    return {"limit": limit, "over": summary(over), "within": summary(within)}


def paper_account(trades, cfg, rank=None, tie=None, day_start_ms=0):
    """A $1,000 paper account taking signals in time order under the pack's default limits: $10 at risk a trade,
    at most 2 open and 4 new a day, never two on one coin, a pause after 2 losses in a row, $30 a day of losses.
    Signals at the same moment go by `tie` (a number per trade's id) when given, else by `rank` (busiest first).
    The trading day starts `day_start_ms` after midnight UTC, as it does at midnight in the user's time zone."""
    rank = rank or {}
    risk = float(cfg.get("max_loss_per_trade_usd") or 10)
    max_open = int(cfg.get("max_open_positions") or 2)
    per_day = int(cfg.get("max_trades_per_day") or 4)
    daily = float(cfg.get("max_daily_loss_usd") or 30)
    pause_after = int(cfg.get("cooldown_after_losses") or 2)
    pause_ms = int(float(cfg.get("cooldown_hours") or 12) * cc.HOUR_MS)
    balance = start = float(cfg.get("account_usd") or 1000)
    open_pos, taken = [], []
    day_count, day_loss = {}, {}
    run_losses, paused_until, peak, dd = 0, 0, start, 0.0
    if tie is not None:
        key = lambda t: (t["at"], tie[id(t)])
    else:
        key = lambda t: (t["at"], rank.get(t["coin"], 999), t["setup"], t["side"])
    for t in sorted(trades, key=key):
        still = []
        for p in sorted(open_pos, key=lambda p: p["exit_at"]):
            if p["exit_at"] <= t["at"]:
                pnl = p["r"] * risk
                balance += pnl
                d = (p["exit_at"] - day_start_ms) // DAY
                if pnl < 0:
                    day_loss[d] = day_loss.get(d, 0.0) - pnl
                run_losses = run_losses + 1 if pnl <= 0 else 0
                if run_losses >= pause_after:
                    paused_until, run_losses = p["exit_at"] + pause_ms, 0
                peak = max(peak, balance)
                dd = max(dd, peak - balance)
            else:
                still.append(p)
        open_pos = still
        d = (t["at"] - day_start_ms) // DAY
        if (t["at"] < paused_until or len(open_pos) >= max_open or day_count.get(d, 0) >= per_day
                or any(p["coin"] == t["coin"] for p in open_pos)
                or day_loss.get(d, 0.0) + risk * (len(open_pos) + 1) > daily):
            continue
        open_pos.append(t)
        taken.append(t)
        day_count[d] = day_count.get(d, 0) + 1
    for p in sorted(open_pos, key=lambda p: p["exit_at"]):
        balance += p["r"] * risk
        peak = max(peak, balance)
        dd = max(dd, peak - balance)
    return {"start": start, "end": balance, "trades": len(taken), "max_dd_usd": dd, "risk_usd": risk,
            "summary": summary(taken)}


def paper_runs(trades, cfg, runs=100, seed=19):
    """The paper account `runs` times. Each run starts its trading day at a random hour, as a trader's day starts
    at midnight wherever they live, and takes signals that fire at the same moment in a new random order. One
    run is one path: the 4 a day go to whatever fires first after the day starts, so a single run says more about
    the hour after its midnight than about the rules."""
    rng = random.Random(seed)
    ends, counts, dds = [], [], []
    start = float(cfg.get("account_usd") or 1000)
    for _ in range(runs):
        day_start = rng.randrange(24) * cc.HOUR_MS
        tie = dict((id(t), rng.random()) for t in trades)
        pa = paper_account(trades, cfg, tie=tie, day_start_ms=day_start)
        start = pa["start"]
        ends.append(pa["end"])
        counts.append(pa["trades"])
        dds.append(pa["max_dd_usd"])
    return {"runs": runs, "start": start, "median": cc.median(ends), "lo": cc.percentile(ends, 5),
            "hi": cc.percentile(ends, 95), "min": min(ends) if ends else None, "max": max(ends) if ends else None,
            "trades": cc.median(counts), "max_dd_usd": cc.median(dds),
            "losing_runs": sum(1 for e in ends if e < start)}


def setup_rows(trades, results):
    """The by-setup table and the pooled luck test: random entries' average weighted by each tested coin-setup's
    trade count, against the setups' own average over those same coins and setups."""
    by_key = {}
    for t in trades:
        by_key.setdefault((t["setup"], t["side"]), []).append(t)
    rand_by, has_base = {}, set()
    for r in results:
        for key, b in r["baselines"].items():
            name, side = key.split("|")
            agg = rand_by.setdefault((name, side), [0.0, 0])
            agg[0] += b["avg_r"] * b["trades"]
            agg[1] += b["trades"]
            has_base.add((r["coin"], name, side))
    based = lambda t: (t["coin"], t["setup"], t["side"]) in has_base
    rows = []
    for key, name, params in SETUPS:
        for side in ("long", "short"):
            ts = by_key.get((key, side)) or []
            if not ts:
                continue
            s = summary(ts)
            row = {"setup": key, "side": side, "n": s["n"], "win_rate": s["win_rate"], "avg_r": s["avg_r"],
                   "avg_gross_r": s["avg_gross_r"], "total_r": s["total_r"]}
            agg = rand_by.get((key, side))
            matched = [t["r"] for t in ts if based(t)]
            if agg and agg[1] and matched:
                row["random_r"] = agg[0] / agg[1]
                row["edge"] = sum(matched) / len(matched) - row["random_r"]
            rows.append(row)
    baseline = None
    tot_w = sum(agg[1] for agg in rand_by.values())
    if tot_w:
        rand_avg = sum(agg[0] for agg in rand_by.values()) / tot_w
        covered = [t for t in trades if based(t)]
        baseline = {"avg_r": rand_avg, "n": sum(r["baselines"][k]["n"] for r in results for k in r["baselines"]),
                    "edge": (sum(t["r"] for t in covered) / len(covered) - rand_avg) if covered else None}
    return rows, baseline


def z_for(groups, alpha=0.10):
    """The z for a two-sided range at `alpha`, widened for `groups` comparisons made at once (Bonferroni): with 14
    setup-and-side groups, two would clear an ordinary 90% range now and then by luck alone."""
    from statistics import NormalDist
    return NormalDist().inv_cdf(1.0 - alpha / (2.0 * max(1, groups)))


def track_record(d, groups):
    """Each setup and side's record, raw and with both checks: what the Scout, the Scanner, the Coin Report and
    the Exit Planner show next to a live setup. lo and hi are the average's range widened for testing every
    setup and side at once, so "can't be told from zero" means what it says."""
    by, both, judged = {}, {}, {}
    for t in d["trades"]:
        by.setdefault((t["setup"], t["side"]), []).append(t)
    for t in groups["both"]:
        both.setdefault((t["setup"], t["side"]), []).append(t)
    for t in groups["every"]:
        judged.setdefault((t["setup"], t["side"]), []).append(t)
    r4 = lambda x: None if x is None else round(x, 4)
    z = z_for(len(d["by_setup"]))

    def ranged(s):
        if not s.get("n"):
            return {"n": 0, "avg_r": None, "lo": None, "hi": None}
        se = (s["avg_r_hi"] - s["avg_r"]) / 1.645
        return {"n": s["n"], "avg_r": r4(s["avg_r"]), "lo": r4(s["avg_r"] - z * se), "hi": r4(s["avg_r"] + z * se)}
    setups = {}
    for row in d["by_setup"]:
        k = (row["setup"], row["side"])
        e = ranged(summary(by.get(k) or []))
        e.update({"avg_gross_r": r4(row["avg_gross_r"]), "win_rate": r4(row["win_rate"])})
        if row.get("random_r") is not None:
            e["random_r"] = r4(row["random_r"])
        e["checked"] = ranged(summary(both.get(k) or []))
        e["checked"]["of"] = len(judged.get(k) or [])
        setups["%s|%s" % k] = e
    ch = d["checks"]
    return {"version": 1, "made_by": "tools/backtest.py", "source": d.get("source"), "venue": d["venue"],
            "venue_label": d["venue_label"], "coins": d["coins"], "from": d["from"], "to": d["to"],
            "from_utc": cc.fmt_utc(d["from"]), "to_utc": cc.fmt_utc(d["to"]), "days": d["days"],
            "target_r": d["target_r"], "hold_hours": d["hold_hours"], "code": d["code"],
            "cost_limit_r": d["cost_limit_r"], "costs_estimated": d["costs_estimated"],
            "evidence_days": EVIDENCE_DAYS, "evidence_min_trades": EVIDENCE_MIN_TRADES,
            "range_z": round(z, 3), "range_note": "lo and hi: the average's 90%% range widened for %d groups tested at once"
                                                  % len(d["by_setup"]),
            "all": {"n": d["all"]["n"], "avg_r": round(d["all"]["avg_r"], 4), "avg_gross_r": round(d["all"]["avg_gross_r"], 4)},
            "checks": dict((k, {"n": ch[k]["n"], "avg_r": round(ch[k]["avg_r"], 4) if ch[k]["n"] else None,
                                "first_half": round(ch[k]["first_half"]["avg_r"], 4) if ch[k]["first_half"]["n"] else None,
                                "second_half": round(ch[k]["second_half"]["avg_r"], 4) if ch[k]["second_half"]["n"] else None})
                           for k, _ in CHECKS),
            "setups": setups}


# ---------------------------------------------------------------- output

def pct(x, dp=0):
    return "-" if x is None else ("%." + str(dp) + "f%%") % (100.0 * x)


def rr(x):
    return "-" if x is None else "%+.2fR" % x


def usd(x):
    return "-" if x is None else "$" + "{:,.0f}".format(x)


def count(n):
    return "{:,}".format(n)


def report_lines(d):
    s = d["all"]
    out = []
    out.append("Backtest | %s | %d coins | %s to %s | %s" % (
        d["venue_label"], d["coins"], cc.fmt_utc(d["from"]), cc.fmt_utc(d["to"]),
        ("crypto-trader-skill " + d["code"]) if not d.get("source") else "trades re-read from " + d["source"]))
    out.append("Walk-forward: the scanner's own rules saw only the candles closed at each moment; each trade was")
    out.append("planned like /crypto-exit (the setup's stop, %gR target, out after %gh) and played on the candles that came"
               % (d["target_r"], d["hold_hours"]))
    out.append("next, after fees, slippage and the funding actually charged. Fake money only.")
    out.append("")
    if not s["n"]:
        out.append("No trades: no setup fired in the window.")
        return out
    out.append("%s trades: target first %s, stopped %s, out at the time limit %s." % (
        count(s["n"]), count(s["target"]), count(s["stop"]), count(s["time"])))
    out.append("Won (above zero after costs) %s (95%% range %s to %s)." % (pct(s["win_rate"], 1), pct(s["win_lo"], 1),
                                                                        pct(s["win_hi"], 1)))
    out.append("Average %s a trade after costs (90%% range %s to %s); %s before costs, so costs and funding took %.2fR."
               % (rr(s["avg_r"]), rr(s["avg_r_lo"]), rr(s["avg_r_hi"]), rr(s["avg_gross_r"]), s["avg_gross_r"] - s["avg_r"]))
    out.append("Total %s. Profit factor %s. Average win %s, average loss %s." % (
        "%+.1fR" % s["total_r"], ("%.2f" % s["profit_factor"]) if s["profit_factor"] else "-", rr(s["avg_win_r"]),
        rr(s["avg_loss_r"])))
    out.append("Deepest drawdown %.1fR; longest losing streak %d." % (s["max_dd_r"], s["worst_losing_streak"]))
    b = d.get("baseline")
    if b:
        out.append("Random entries at the same times of day, same stops and targets: %s a trade over %s; the setups' edge "
                   "over them %s." % (rr(b["avg_r"]), count(b["n"]), rr(b["edge"])))
    elif d.get("source"):
        out.append("Random entries: not in a trades file; the original run's report has them.")
    out.append("")
    out.append("By setup (each side tested separately):")
    rows = [["setup", "side", "trades", "won", "avg R", "before costs", "total R", "random", "edge"]]
    for row in d["by_setup"]:
        rows.append([SETUP_WORDS.get(row["setup"], row["setup"]), row["side"], count(row["n"]), pct(row["win_rate"]),
                     rr(row["avg_r"]), rr(row["avg_gross_r"]), "%+.0fR" % row["total_r"], rr(row.get("random_r")),
                     rr(row.get("edge"))])
    out.extend(cc.table(rows[1:], rows[0], "llrrrrrrr").splitlines())
    ch = d["checks"]
    lim = d["cost_limit_r"]
    if not ch["every"]["n"]:
        out.append("")
        out.append("The pack's two checks and the paper account judge the trades after the first %d days: use --days %d or more."
                   % (EVIDENCE_DAYS, EVIDENCE_DAYS + 10))
    else:
        out.append("")
        out.append("The pack's two checks, on the %s trades after the first %d days, each judged with only what was known"
                   % (count(ch["every"]["n"]), EVIDENCE_DAYS))
        out.append("at the time:")
        out.append("  cost rule      a stop-out costs at most %s (fees and slippage, known before the entry)%s" % (
            ("%gR" % lim) if lim else "-, the rule is off",
            "" if not d["costs_estimated"] else "; rebuilt from each coin's stop-outs, as this file has no cost column"))
        out.append("  30-day check   the same coin, setup and side made money after costs over its last %d days, at least %d trades"
                   % (EVIDENCE_DAYS, EVIDENCE_MIN_TRADES))
        out.append("A rule that helps in only one half of the stretch is the market's mood, not a rule. The halves split at %s."
                   % cc.fmt_utc(ch["mid"]))
        rows = []
        for k, label in CHECKS:
            x = ch[k]
            rows.append(["  " + label, count(x["n"]), rr(x["first_half"]["avg_r"]), rr(x["second_half"]["avg_r"]),
                         rr(x.get("avg_r")), pct(x.get("win_rate"))])
        out.extend(cc.table(rows, ["", "trades", "first half", "second half", "all", "won"], "lrrrrr").splitlines())
    cs = d["cost_split"]
    if lim and cs["over"]["n"] and cs["within"]["n"]:
        out.append("Over the whole window, the %s trades whose stop-out cost more than %gR averaged %s after costs (%s before);"
                   % (count(cs["over"]["n"]), lim, rr(cs["over"]["avg_r"]), rr(cs["over"]["avg_gross_r"])))
        out.append("the other %s averaged %s (%s before)." % (count(cs["within"]["n"]), rr(cs["within"]["avg_r"]),
                                                             rr(cs["within"]["avg_gross_r"])))
    pr = d["paper"]
    if pr:
        first = pr[CHECKS[0][0]]
        out.append("")
        out.append("A %s paper account under the pack's default limits ($%g at risk a trade, %d open, %d a day, a pause after"
                   % (usd(first["start"]), d["limits"]["risk"], d["limits"]["open"], d["limits"]["per_day"]))
        out.append("%d losses in a row, $%g of losses a day), on the same %s trades, run %d times. It takes whatever fires"
                   % (d["limits"]["pause_after"], d["limits"]["daily"], count(ch["every"]["n"]), first["runs"]))
        out.append("first each day, so each run starts its day at a random hour (as a trader's day starts at midnight where")
        out.append("they live) and takes signals that fire together in a random order. The median run, and the middle 90%:")
        rows = []
        for k, label in CHECKS:
            x = pr.get(k)
            if not x or x["median"] is None:
                continue
            rows.append(["  " + label, "%d trades" % round(x["trades"]), usd(x["median"]), "%s to %s" % (usd(x["lo"]), usd(x["hi"])),
                         usd(x["max_dd_usd"]), "%d of %d" % (x["losing_runs"], x["runs"])])
        out.extend(cc.table(rows, ["", "taken", "became", "middle 90%", "drawdown", "runs that lost"], "lrrrrr").splitlines())
    out.append("")
    out.append("Survivorship: the coins are today's busiest, so coins that faded or were delisted are missing. Costs are")
    out.append("today's fees and slippage. Past results say nothing certain about the future.")
    return out


def write_csv(path, trades):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for t in sorted(trades, key=lambda t: (t["at"], t["coin"], t["setup"], t["side"])):
            num = lambda x: "%.10g" % x
            w.writerow([t["coin"], t["symbol"], t["setup"], t["side"], cc.fmt_utc(t["at"]), num(t["entry"]), num(t["stop"]),
                        num(t["target"]), cc.fmt_utc(t["exit_at"]), num(t["exit"]),
                        {"open": "time limit"}.get(t["outcome"], t["outcome"]), "%.4f" % t["r"], "%.4f" % t["gross_r"],
                        "%.4f" % (100 * t["risk_pct"]), t["bars"] * 5, "%.4f" % t["stop_cost_r"]])


def code_id():
    lc = load("tools", "live_check")
    return lc.code_id()


# ---------------------------------------------------------------- the two ways in

def window_end(a, default):
    if not a.end:
        return default
    end = (cc.parse_when(a.end) // STEP) * STEP
    if end > cc.now_ms() and not a.trades:
        raise cc.CryptoError("--end %s is in the future" % a.end)
    return end


def download_and_replay(a, cfg):
    """(trades, setup results, the window and the venue) from the exchange."""
    venue = cc.get_venue(a.venue)
    if venue.name == "hyperliquid":
        raise cc.CryptoError("Hyperliquid keeps only about 17 days of 5-minute candles: too few to backtest. "
                             "Use --venue binance or --venue bybit.")
    hold_hours = cc.parse_hold(a.hold, 24.0)
    n_steps = int(round(hold_hours * 12))
    end = window_end(a, (cc.now_ms() // cc.HOUR_MS) * cc.HOUR_MS)     # whole hours, so a cache made this hour is reused
    window_start = end - int(a.days * DAY)
    start = window_start - WARMUP_DAYS * DAY
    symbols = [s for s in (a.symbols or "").split(",") if s.strip()] or None
    universe, why = cc.build_universe(venue, cfg, top=a.coins, symbols=symbols)
    if not universe:
        raise cc.CryptoError("no coins pass the filters")
    t0 = time.time()
    cc.out("Backtest | %s | the %d busiest coins | %g days to %s, after %d days of warm-up" % (
        venue.label, len(universe), a.days, cc.fmt_utc(end), WARMUP_DAYS))
    cc.out("Downloading 5-minute candles and funding...")
    datas, skipped = [], []

    def get(item):
        m, t = item
        return fetch_coin(venue, m, start, end, a.cache)
    for (m, t), data, err in cc.parallel(get, universe, workers=2 if venue.name == "hyperliquid" else 4):
        expected = (end - start) // STEP
        if err is not None:
            skipped.append("%s (%s)" % (m["coin"], str(err)[:60]))
        elif len(data["bars"]) < 0.9 * expected:
            skipped.append("%s (only %d days of candles)" % (m["coin"], len(data["bars"]) // 288))
        else:
            datas.append(data)
    cc.out("  %d coins ready in %.0fs%s" % (len(datas), time.time() - t0,
                                           ("; left out: " + ", ".join(skipped)) if skipped else ""))
    if not datas:
        raise cc.CryptoError("no coin had enough history to test")
    jobs = [(x, window_start, a.target_r, n_steps, a.seed, a.random_per_trade) for x in datas]
    workers = a.workers or min(8, os.cpu_count() or 1)
    results = []
    cc.out("Replaying %d setups x 2 sides on %d coins with %d worker%s..." % (len(SETUPS), len(datas), workers,
                                                                           "" if workers == 1 else "s"))
    done = 0
    if workers > 1 and len(jobs) > 1:
        try:
            ctx = multiprocessing.get_context("spawn")    # the same on Windows, macOS and Linux, and safe after threads
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
                for res in ex.map(test_coin, jobs):
                    results.append(res)
                    done += 1
                    sys.stderr.write("\r  %d of %d coins" % (done, len(jobs)))
        except Exception as e:                          # no worker processes here: one process is fine
            sys.stderr.write("\n  worker processes unavailable (%s); carrying on in one process\n" % e)
            results, done = [], 0
    if not results:
        for j in jobs:
            results.append(test_coin(j))
            done += 1
            sys.stderr.write("\r  %d of %d coins" % (done, len(jobs)))
    sys.stderr.write("\n")
    trades = [t for r in results for t in r["trades"]]
    meta = {"venue": venue.name, "venue_label": venue.label, "coins": len(datas), "from": window_start, "to": end,
            "hold_hours": hold_hours, "code": code_id(), "source": None, "left_out": skipped, "t0": t0}
    return trades, results, meta


def reread(a, cfg):
    """(trades, no setup results, the window and the venue) from an earlier run's trades file."""
    t0 = time.time()
    trades, estimated = read_trades(a.trades)
    if not trades:
        raise cc.CryptoError("%s has no trades" % a.trades)
    last = max(t["at"] for t in trades)
    end = window_end(a, ((last + cc.HOUR_MS - 1) // cc.HOUR_MS) * cc.HOUR_MS)
    name = a.venue if a.venue and a.venue != "auto" else next(
        (v for v in ("binance", "bybit") if v in os.path.basename(a.trades).lower()), None)
    if not name:
        raise cc.CryptoError("which exchange are these trades from? Pass --venue binance or --venue bybit")
    venue = cc.get_venue(name)
    meta = {"venue": venue.name, "venue_label": venue.label,
            "coins": len(set(t["coin"] for t in trades)), "from": end - int(a.days * DAY), "to": end,
            "hold_hours": cc.parse_hold(a.hold, 24.0), "code": None, "source": os.path.basename(a.trades),
            "left_out": [], "t0": t0, "costs_estimated": estimated}
    return [t for t in trades if meta["from"] <= t["at"] < end], [], meta


def main(argv=None):
    p = argparse.ArgumentParser(prog="backtest.py", description="The pack's setups replayed on the past, walk-forward.")
    p.add_argument("--coins", type=int, default=40, help="the N busiest coins (default 40)")
    p.add_argument("--days", type=float, default=90, help="days of past to replay (default 90)")
    p.add_argument("--end", default=None, help="the window ends here, UTC: 2026-07-29 or \"2026-07-29 06:00\" (default now)")
    p.add_argument("--symbols", default=None, help="these coins instead of the busiest")
    p.add_argument("--target-r", type=float, default=2.0)
    p.add_argument("--hold", default="24h", help="time limit per trade (default 24h)")
    p.add_argument("--max-cost-r", type=float, default=None,
                   help="the cost rule's limit in R (default max_cost_r from your config, 0.2; 0 turns it off)")
    p.add_argument("--paper-runs", type=int, default=100, help="paper account runs, each in a new random order (default 100)")
    p.add_argument("--workers", type=int, default=0, help="processes for the replay (default: your CPU count, up to 8)")
    p.add_argument("--random-per-trade", type=int, default=10, help="random entries per trade for the luck test")
    p.add_argument("--cache", default=None, metavar="DIR", help="keep downloaded candles in DIR and reuse them")
    p.add_argument("--trades", default=None, metavar="FILE",
                   help="re-read an earlier run's backtest_trades.csv (or .csv.gz) instead of downloading")
    p.add_argument("--out", default=".", metavar="DIR", help="where to write the report, the trades and the numbers")
    p.add_argument("--seed", type=int, default=19)
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    if not 7 <= a.days <= 365:
        raise cc.CryptoError("--days must be between 7 and 365")
    if not 1 <= a.paper_runs <= 1000:
        raise cc.CryptoError("--paper-runs must be between 1 and 1000")
    limit = cc.cost_limit({"max_cost_r": a.max_cost_r} if a.max_cost_r is not None else cfg)
    if a.trades:
        trades, results, meta = reread(a, cfg)
    else:
        trades, results, meta = download_and_replay(a, cfg)
    rows, baseline = setup_rows(trades, results)
    groups = check_groups(trades, meta["from"], limit)
    d = {"version": 2, "venue": meta["venue"], "venue_label": meta["venue_label"], "coins": meta["coins"],
         "from": meta["from"], "to": meta["to"], "days": a.days, "target_r": a.target_r, "hold_hours": meta["hold_hours"],
         "code": meta["code"], "source": meta["source"], "cost_limit_r": limit,
         "costs_estimated": bool(meta.get("costs_estimated")),
         "all": summary(trades), "by_setup": rows, "baseline": baseline,
         "checks": check_table(groups, meta["from"], meta["to"]), "cost_split": cost_split(trades, limit),
         "evidence": {"judged": summary(groups["every"]), "taken": summary(groups["record"])},
         "limits": {"risk": float(cfg.get("max_loss_per_trade_usd") or 10), "open": int(cfg.get("max_open_positions") or 2),
                    "per_day": int(cfg.get("max_trades_per_day") or 4), "daily": float(cfg.get("max_daily_loss_usd") or 30),
                    "pause_after": int(cfg.get("cooldown_after_losses") or 2)},
         "paper": dict((k, paper_runs(groups[k], cfg, a.paper_runs, a.seed)) for k, _ in CHECKS if groups[k]),
         "left_out": meta["left_out"]}
    d["seconds"] = round(time.time() - meta["t0"])
    d["trades"] = trades
    lines = report_lines(d)
    os.makedirs(a.out, exist_ok=True)
    rep = os.path.join(a.out, "backtest_report.txt")
    with open(rep, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    wrote = [os.path.basename(rep)]
    if not a.trades:
        write_csv(os.path.join(a.out, "backtest_trades.csv"), trades)
        wrote.append("backtest_trades.csv")
    if d["all"]["n"]:
        cc.write_json(os.path.join(a.out, cc.TRACK_FILE), track_record(d, groups))
        wrote.append(cc.TRACK_FILE)
    d.pop("trades")
    if not a.no_write:
        cc.write_json(os.path.join(a.out, "backtest.json"), d)
        wrote.append("backtest.json")
    cc.out("")
    for line in lines:
        cc.out(line)
    cc.out("")
    cc.out("Done in %s. Wrote %s in %s." % (cc.fmt_dur((time.time() - meta["t0"]) * 1000), ", ".join(wrote),
                                           os.path.abspath(a.out)))
    if d["all"]["n"]:
        cc.out("To show this run's record next to live setups instead of the pack's: copy %s into your crypto/ folder."
               % cc.TRACK_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(cc.run(main))
