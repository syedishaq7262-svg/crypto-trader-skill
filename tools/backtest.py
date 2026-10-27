#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Walk-forward backtest: the pack's setups replayed on the past, as if each moment were "now".

    python3 tools/backtest.py                        the 40 busiest coins, the last 90 days, on the best venue from here
    python3 tools/backtest.py --coins 20 --days 60   smaller and faster
    python3 tools/backtest.py --venue bybit
    python3 tools/backtest.py --cache bt-data        keep the candles it downloads, to run again without downloading

How it works. It steps through the past five minutes at a time. At each step the scanner's own rules see only
the candles that had closed by then, never a later one. When a setup fires, the trade is planned the way
/crypto-exit plans it: entry at the trigger, the setup's own stop, a target at 2R, out after 24 hours whatever
happens, one trade at a time per coin and setup. Then it is played forward on the candles that really came
next, paying the exchange's fees, the slippage measured from each coin's order book, and the funding that was
actually charged. Fake money: nothing reads or touches an account.

It also checks every setup against random entries at the same times of day (same stop distance, same target),
replays the Scout's first test as it would have stood at the time (take a setup only when its own last 30 days
on that coin made money after costs), and runs a $1,000 paper account under the pack's default risk limits.

Writes backtest_report.txt (the summary), backtest_trades.csv (every trade) and backtest.json (the numbers).
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
                           "bars": tr["bars"]})
        if hist.get("n", 0) >= 3 and per_trade:
            b = tm.run_baseline(plan, bars, hist, funding, costs, n_steps, None, seed, per_trade=per_trade)
            if b:
                bases[key] = {"n": b["n"], "avg_r": b["avg_r"], "trades": hist["n"]}
    return {"coin": m["coin"], "trades": trades, "baselines": bases, "bars": len(bars)}


# ---------------------------------------------------------------- statistics

def summary(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    p, lo, hi = cc.wilson(wins, n)
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r <= 0)
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
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
            "avg_r": mean, "avg_r_lo": mean - half, "avg_r_hi": mean + half, "total_r": sum(rs),
            "avg_gross_r": sum(t["gross_r"] for t in trades) / n,
            "avg_win_r": (gains / wins) if wins else None, "avg_loss_r": (-losses / (n - wins)) if n > wins else None,
            "profit_factor": (gains / losses) if losses else None, "max_dd_r": dd, "worst_losing_streak": worst_streak}


def evidence_filter(trades, start, days=EVIDENCE_DAYS, min_trades=EVIDENCE_MIN_TRADES):
    """The Scout's first test, as it stood at each trade's time: the same coin, setup and side made money after
    costs over the trades that had closed in the 30 days before. Only trades after the first 30 days are judged."""
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


def paper_account(trades, cfg, rank):
    """A $1,000 paper account taking signals in time order under the pack's default limits: $10 at risk a trade,
    at most 2 open and 4 new a day, never two on one coin, a pause after 2 losses in a row, $30 a day of losses."""
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
    for t in sorted(trades, key=lambda t: (t["at"], rank.get(t["coin"], 999), t["setup"], t["side"])):
        still = []
        for p in sorted(open_pos, key=lambda p: p["exit_at"]):
            if p["exit_at"] <= t["at"]:
                pnl = p["r"] * risk
                balance += pnl
                d = p["exit_at"] // DAY
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
        d = t["at"] // DAY
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


# ---------------------------------------------------------------- output

def pct(x, dp=0):
    return "-" if x is None else ("%." + str(dp) + "f%%") % (100.0 * x)


def rr(x):
    return "-" if x is None else "%+.2fR" % x


def report_lines(d):
    s = d["all"]
    out = []
    out.append("Backtest | %s | %d coins | %s to %s | crypto-trader-skill %s" % (
        d["venue_label"], d["coins"], cc.fmt_utc(d["from"]), cc.fmt_utc(d["to"]), d["code"]))
    out.append("Walk-forward: the scanner's own rules saw only the candles closed at each moment; each trade was")
    out.append("planned like /crypto-exit (the setup's stop, %gR target, out after %gh) and played on the candles that came"
               % (d["target_r"], d["hold_hours"]))
    out.append("next, after fees, slippage and the funding actually charged. Fake money only.")
    out.append("")
    if not s["n"]:
        out.append("No trades: no setup fired in the window.")
        return out
    out.append("%s trades: target first %s, stopped %s, out at the time limit %s." % (
        "{:,}".format(s["n"]), "{:,}".format(s["target"]), "{:,}".format(s["stop"]), "{:,}".format(s["time"])))
    out.append("Won (above zero after costs) %s (95%% range %s to %s)." % (pct(s["win_rate"], 1), pct(s["win_lo"], 1),
                                                                        pct(s["win_hi"], 1)))
    out.append("Average %s a trade after costs (90%% range %s to %s); %s before costs, so costs and funding took %.2fR."
               % (rr(s["avg_r"]), rr(s["avg_r_lo"]), rr(s["avg_r_hi"]), rr(s["avg_gross_r"]), s["avg_gross_r"] - s["avg_r"]))
    out.append("Total %s. Profit factor %s. Average win %s, average loss %s." % (
        "%+.1fR" % s["total_r"], ("%.2f" % s["profit_factor"]) if s["profit_factor"] else "-", rr(s["avg_win_r"]),
        rr(s["avg_loss_r"])))
    out.append("Deepest drawdown %.1fR; longest losing streak %d." % (s["max_dd_r"], s["worst_losing_streak"]))
    b = d["baseline"]
    if b:
        out.append("Random entries at the same times of day, same stops and targets: %s a trade over %s; the setups' edge "
                   "over them %s." % (rr(b["avg_r"]), "{:,}".format(b["n"]), rr(b["edge"])))
    out.append("")
    out.append("By setup (each side tested separately):")
    rows = [["setup", "side", "trades", "won", "avg R", "total R", "random", "edge"]]
    for row in d["by_setup"]:
        rows.append([SETUP_WORDS.get(row["setup"], row["setup"]), row["side"], "{:,}".format(row["n"]), pct(row["win_rate"]),
                     rr(row["avg_r"]), "%+.0fR" % row["total_r"], rr(row.get("random_r")), rr(row.get("edge"))])
    out.extend(cc.table(rows[1:], rows[0], "llrrrrrr").splitlines())
    e = d["evidence"]
    if e["judged"]["n"]:
        out.append("")
        out.append("The Scout's first test, as it stood at the time (its own last %d days on that coin positive after costs,"
                   % EVIDENCE_DAYS)
        out.append("at least %d trades), judged on the %s trades after the first %d days:" % (
            EVIDENCE_MIN_TRADES, "{:,}".format(e["judged"]["n"]), EVIDENCE_DAYS))
        for label, x in (("every signal", e["judged"]), ("passed the test", e["taken"])):
            if x["n"]:
                out.append("  %-16s %6s trades, won %s, average %s a trade, total %s" % (
                    label, "{:,}".format(x["n"]), pct(x["win_rate"]), rr(x["avg_r"]), "%+.0fR" % x["total_r"]))
            else:
                out.append("  %-16s none" % label)
    out.append("")
    out.append("A $1,000 paper account under the pack's default limits ($10 at risk a trade, 2 open, 4 a day, a pause after")
    out.append("2 losses in a row, $30 of losses a day):")
    for label, pa in (("every signal", d["paper_all"]), ("Scout's first test", d["paper_evidence"])):
        if pa["trades"]:
            out.append("  %-18s %4d trades taken, $1,000 became %s, deepest drawdown %s" % (
                label, pa["trades"], cc.fmt_money(pa["end"]), cc.fmt_money(pa["max_dd_usd"])))
    out.append("")
    out.append("Survivorship: the coins are today's busiest, so coins that faded or were delisted are missing. Costs are")
    out.append("today's fees and slippage. Past results say nothing certain about the future.")
    return out


def write_csv(path, trades):
    cols = ["coin", "symbol", "setup", "side", "signal_utc", "entry", "stop", "target", "exit_utc", "exit", "outcome",
            "r_after_costs", "r_before_costs", "stop_distance_pct", "minutes_held"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t in sorted(trades, key=lambda t: (t["at"], t["coin"], t["setup"], t["side"])):
            num = lambda x: "%.10g" % x
            w.writerow([t["coin"], t["symbol"], t["setup"], t["side"], cc.fmt_utc(t["at"]), num(t["entry"]), num(t["stop"]),
                        num(t["target"]), cc.fmt_utc(t["exit_at"]), num(t["exit"]),
                        {"open": "time limit"}.get(t["outcome"], t["outcome"]), "%.4f" % t["r"], "%.4f" % t["gross_r"],
                        "%.4f" % (100 * t["risk_pct"]), t["bars"] * 5])


def code_id():
    lc = load("tools", "live_check")
    return lc.code_id()


# ---------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(prog="backtest.py", description="The pack's setups replayed on the past, walk-forward.")
    p.add_argument("--coins", type=int, default=40, help="the N busiest coins (default 40)")
    p.add_argument("--days", type=float, default=90, help="days of past to replay (default 90)")
    p.add_argument("--symbols", default=None, help="these coins instead of the busiest")
    p.add_argument("--target-r", type=float, default=2.0)
    p.add_argument("--hold", default="24h", help="time limit per trade (default 24h)")
    p.add_argument("--workers", type=int, default=0, help="processes for the replay (default: your CPU count, up to 8)")
    p.add_argument("--random-per-trade", type=int, default=10, help="random entries per trade for the luck test")
    p.add_argument("--cache", default=None, metavar="DIR", help="keep downloaded candles in DIR and reuse them")
    p.add_argument("--out", default=".", metavar="DIR", help="where to write the report, the trades and the numbers")
    p.add_argument("--seed", type=int, default=19)
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    venue = cc.get_venue(a.venue)
    if venue.name == "hyperliquid":
        raise cc.CryptoError("Hyperliquid keeps only about 17 days of 5-minute candles: too few to backtest. "
                             "Use --venue binance or --venue bybit.")
    if not 7 <= a.days <= 365:
        raise cc.CryptoError("--days must be between 7 and 365")
    hold_hours = cc.parse_hold(a.hold, 24.0)
    n_steps = int(round(hold_hours * 12))
    end = (cc.now_ms() // cc.HOUR_MS) * cc.HOUR_MS      # whole hours, so a cache made this hour is reused
    window_start = end - int(a.days * DAY)
    start = window_start - WARMUP_DAYS * DAY
    symbols = [s for s in (a.symbols or "").split(",") if s.strip()] or None
    universe, why = cc.build_universe(venue, cfg, top=a.coins, symbols=symbols)
    if not universe:
        raise cc.CryptoError("no coins pass the filters")
    rank = dict((m["coin"], i) for i, (m, t) in enumerate(universe))
    t0 = time.time()
    cc.out("Backtest | %s | the %d busiest coins | the last %g days, after %d days of warm-up" % (
        venue.label, len(universe), a.days, WARMUP_DAYS))
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
    jobs = [(d, window_start, a.target_r, n_steps, a.seed, a.random_per_trade) for d in datas]
    workers = a.workers or min(8, os.cpu_count() or 1)
    results = []
    t1 = time.time()
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
    # the luck test, pooled: random entries' average weighted by each tested coin-setup's trade count, against
    # the setups' own average over those same coins and setups
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
    by_setup = []
    for key, name, params in SETUPS:
        for side in ("long", "short"):
            ts = by_key.get((key, side)) or []
            if not ts:
                continue
            s = summary(ts)
            row = {"setup": key, "side": side, "n": s["n"], "win_rate": s["win_rate"], "avg_r": s["avg_r"],
                   "total_r": s["total_r"]}
            agg = rand_by.get((key, side))
            matched = [t["r"] for t in ts if based(t)]
            if agg and agg[1] and matched:
                row["random_r"] = agg[0] / agg[1]
                row["edge"] = sum(matched) / len(matched) - row["random_r"]
            by_setup.append(row)
    tot_w = sum(agg[1] for agg in rand_by.values())
    baseline = None
    if tot_w:
        rand_avg = sum(agg[0] for agg in rand_by.values()) / tot_w
        covered = [t for t in trades if based(t)]
        baseline = {"avg_r": rand_avg, "n": sum(r["baselines"][k]["n"] for r in results for k in r["baselines"]),
                    "edge": (sum(t["r"] for t in covered) / len(covered) - rand_avg) if covered else None}
    judged, taken = evidence_filter(trades, window_start)
    d = {"version": 1, "venue": venue.name, "venue_label": venue.label, "coins": len(datas), "from": window_start, "to": end,
         "days": a.days, "target_r": a.target_r, "hold_hours": hold_hours, "code": code_id(),
         "all": summary(trades), "by_setup": by_setup, "baseline": baseline,
         "evidence": {"judged": summary(judged), "taken": summary(taken)},
         "paper_all": paper_account(trades, cfg, rank), "paper_evidence": paper_account(taken, cfg, rank),
         "left_out": skipped, "seconds": round(time.time() - t0)}
    for k in ("paper_all", "paper_evidence"):
        d[k].pop("summary", None)
    lines = report_lines(d)
    os.makedirs(a.out, exist_ok=True)
    rep = os.path.join(a.out, "backtest_report.txt")
    with open(rep, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    write_csv(os.path.join(a.out, "backtest_trades.csv"), trades)
    if not a.no_write:
        cc.write_json(os.path.join(a.out, "backtest.json"), d)
    cc.out("")
    for line in lines:
        cc.out(line)
    cc.out("")
    cc.out("Done in %s. Wrote %s, backtest_trades.csv and backtest.json in %s." % (
        cc.fmt_dur((time.time() - t0) * 1000), os.path.basename(rep), os.path.abspath(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(cc.run(main))
