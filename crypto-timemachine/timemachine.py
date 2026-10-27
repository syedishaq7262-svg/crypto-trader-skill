#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-timemachine: replay a plan 10,000 times before you risk a dollar.

    python3 timemachine.py run              crypto/plan.json, 10,000 replays, seed 19 -> crypto/timemachine.json
    python3 timemachine.py run --days 60    a longer pool and history
    python3 timemachine.py chart            an ASCII fan chart of the last run

Three answers, all after fees, slippage and funding:
  1. Replays: the rest of the trade rebuilt 10,000 times from blocks of the coin's own
     5-minute candles, scaled to today's volatility, with the past trend taken out.
  2. History: the plan's own setup rule re-run on every past day of the pool.
  3. Edge test: the same stop and target from random entries at the same times of day.
     Did the setup beat them, or could luck have done as well?
A simulation built from the recent past, not a prediction.
"""

import argparse
import bisect
import importlib.util
import math
import os
import random
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
STEP = 300000
DAY = 86400000
SLOTS = 288
TARGET, STOP, OPEN = 1, 2, 3
OUTCOME = {TARGET: "target", STOP: "stop", OPEN: "open"}
SETUP_WORDS = {"orb": "opening-range", "breakout": "range-breakout", "trend": "trend-pullback",
               "reversal": "RSI-reversal", "volume": "volume-spike", "squeeze": "squeeze-break"}
HISTORY_SETUPS = ("orb", "breakout", "trend", "reversal", "volume", "squeeze")


def load_sibling(folder, name):
    path = os.path.join(HERE, os.pardir, folder, name + ".py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("crypto_tm_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- the pool of past bars

class Pool(object):
    """Past 5-minute bars, each turned into four ratios to the bar before it.

    Every bar's legs (open, high, low, close as logs over the previous close) can be
    shifted to remove the pool's average drift and scaled so the bar's own day had
    today's volatility (filtered historical simulation). Both keep high >= open, close >= low.
    """

    def __init__(self, bars, vol_scale=True, keep_drift=False, vol_n=SLOTS, clip=(0.5, 2.0), window=2):
        n = len(bars)
        if n < vol_n + SLOTS:
            raise cc.CryptoError("only %d five-minute bars to learn from; the Time Machine needs at least two days" % n)
        self.bars, self.n, self.window = bars, n, window
        self.t = [b[T] for b in bars]
        lo, lh, ll, lc = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
        valid = [False] * n
        for j in range(1, n):
            pc = bars[j - 1][C]
            b = bars[j]
            if self.t[j] - self.t[j - 1] != STEP or pc <= 0 or min(b[O], b[H], b[L], b[C]) <= 0:
                continue
            lo[j], lh[j] = math.log(b[O] / pc), math.log(b[H] / pc)
            ll[j], lc[j] = math.log(b[L] / pc), math.log(b[C] / pc)
            valid[j] = True
        rets = [lc[j] for j in range(n) if valid[j]]
        self.mu = sum(rets) / len(rets) if rets else 0.0
        # rolling standard deviation of the previous vol_n closes
        sig = [None] * n
        s1 = s2 = 0.0
        for j in range(n):
            if j >= vol_n:
                m = vol_n
                var = (s2 - s1 * s1 / m) / (m - 1)
                sig[j] = math.sqrt(var) if var > 0 else 0.0
            s1 += lc[j]
            s2 += lc[j] * lc[j]
            if j >= vol_n:
                s1 -= lc[j - vol_n]
                s2 -= lc[j - vol_n] * lc[j - vol_n]
        last = lc[n - vol_n:]
        mm = sum(last) / len(last)
        self.sigma_now = math.sqrt(sum((x - mm) ** 2 for x in last) / (len(last) - 1))
        typical = cc.median([x for x in sig if x])
        self.sigma_typ = typical or self.sigma_now
        self.vol_ratio = (self.sigma_now / self.sigma_typ) if self.sigma_typ else 1.0
        self.vol_scale, self.keep_drift = vol_scale, keep_drift
        # 1. scale each bar's legs to today's volatility (filtered historical simulation)
        slo, slh, sll, slc = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
        self.scale = [1.0] * n
        clipped = 0
        start = vol_n if vol_scale else 1
        usable = [False] * n
        for j in range(start, n):
            if not valid[j]:
                continue
            k = 1.0
            if vol_scale:
                sj = sig[j]
                if not sj:
                    continue
                k = self.sigma_now / sj
                if k < clip[0] or k > clip[1]:
                    clipped += 1
                    k = min(max(k, clip[0]), clip[1])
            self.scale[j] = k
            slo[j], slh[j], sll[j], slc[j] = lo[j] * k, lh[j] * k, ll[j] * k, lc[j] * k
            usable[j] = True
        # 2. take out the drift. With a month of data, the average move at each time of day is
        #    mostly noise, and every replay that starts at that time would inherit it. So the
        #    mean close move is removed slot by slot (the volatility pattern of the day stays).
        #    --keep-drift keeps the pool's overall trend instead, spread evenly over the day.
        slot_of_j = [(self.t[j] % DAY) // STEP for j in range(n)]
        if keep_drift:
            tot = [slc[j] for j in range(n) if usable[j]]
            shift = [sum(tot) / len(tot) if tot else 0.0] * SLOTS
            per_slot = [0.0] * SLOTS
            cnt = [0] * SLOTS
            for j in range(n):
                if usable[j]:
                    per_slot[slot_of_j[j]] += slc[j]
                    cnt[slot_of_j[j]] += 1
            demean = [(per_slot[x] / cnt[x] if cnt[x] else 0.0) - shift[x] for x in range(SLOTS)]
        else:
            per_slot = [0.0] * SLOTS
            cnt = [0] * SLOTS
            for j in range(n):
                if usable[j]:
                    per_slot[slot_of_j[j]] += slc[j]
                    cnt[slot_of_j[j]] += 1
            demean = [(per_slot[x] / cnt[x]) if cnt[x] else 0.0 for x in range(SLOTS)]
        ro, rh, rl, rc = [1.0] * n, [1.0] * n, [1.0] * n, [1.0] * n
        for j in range(n):
            if usable[j]:
                d = demean[slot_of_j[j]]
                ro[j], rh[j] = math.exp(slo[j] - d), math.exp(slh[j] - d)
                rl[j], rc[j] = math.exp(sll[j] - d), math.exp(slc[j] - d)
        self.ro, self.rh, self.rl, self.rc = ro, rh, rl, rc
        self.usable = usable
        self.clipped = clipped / float(max(1, sum(usable)))
        self.nxt = [usable[j + 1] if j + 1 < n else False for j in range(n)]
        by_slot = [[] for _ in range(SLOTS)]
        for j in range(n):
            if usable[j]:
                by_slot[(self.t[j] % DAY) // STEP].append(j)
        self.cands = []
        for s in range(SLOTS):
            c = []
            for d in range(-window, window + 1):
                c.extend(by_slot[(s + d) % SLOTS])
            if not c:
                raise cc.CryptoError("the pool has no bars near %02d:%02d UTC" % (s * 5 // 60, s * 5 % 60))
            self.cands.append(c)
        self.size = sum(usable)


def slot_of(ms):
    return (ms % DAY) // STEP


# ---------------------------------------------------------------- replays

def run_mc(pool, side, entry, stop, target, n_steps, slot0, block, replays, seed, fund_steps):
    """Replays with exits. Returns [(outcome, exit_price, step, funding_sum)] where funding_sum is
    the sum of rate x price over the payments crossed (positive rates cost a long)."""
    rng = random.Random(seed)
    rnd = rng.random
    p_new = 1.0 / max(1.0, block)
    ro, rh, rl, rc, nxt, cands = pool.ro, pool.rh, pool.rl, pool.rc, pool.nxt, pool.cands
    is_long = side == "long"
    out = []
    for _ in range(replays):
        px = entry
        slot = slot0
        j = -1
        fund = 0.0
        res = None
        for k in range(n_steps):
            if k in fund_steps:
                fund += fund_steps[k] * px
            if j < 0 or not nxt[j] or rnd() < p_new:
                c = cands[slot]
                j = c[int(rnd() * len(c))]
            else:
                j += 1
            o = px * ro[j]
            if is_long:
                if o <= stop:
                    res = (STOP, o, k, fund)
                    break
                if px * rl[j] <= stop:
                    res = (STOP, stop, k, fund)
                    break
                if px * rh[j] >= target:
                    res = (TARGET, target, k, fund)
                    break
            else:
                if o >= stop:
                    res = (STOP, o, k, fund)
                    break
                if px * rh[j] >= stop:
                    res = (STOP, stop, k, fund)
                    break
                if px * rl[j] <= target:
                    res = (TARGET, target, k, fund)
                    break
            px *= rc[j]
            slot += 1
            if slot == SLOTS:
                slot = 0
        out.append(res if res is not None else (OPEN, px, n_steps, fund))
    return out


def run_paths(pool, entry, n_steps, slot0, block, count, seed):
    """Full price paths with no exits, for the fan chart."""
    rng = random.Random(seed)
    rnd = rng.random
    p_new = 1.0 / max(1.0, block)
    rc, nxt, cands = pool.rc, pool.nxt, pool.cands
    paths = []
    for _ in range(count):
        px, slot, j = entry, slot0, -1
        path = [entry]
        for _k in range(n_steps):
            if j < 0 or not nxt[j] or rnd() < p_new:
                c = cands[slot]
                j = c[int(rnd() * len(c))]
            else:
                j += 1
            px *= rc[j]
            path.append(px)
            slot += 1
            if slot == SLOTS:
                slot = 0
        paths.append(path)
    return paths


def summarize_mc(results, plan, costs):
    side, entry, risk, target_r = plan["side"], plan["entry"], plan["risk"], plan["target_r"]
    sg = cc.side_sign(side)
    n = len(results)
    rs, by = [], {TARGET: [], STOP: [], OPEN: []}
    funds = []
    open_px = []
    for code, xp, k, fund in results:
        paid = sg * fund
        r = cc.net_r(side, entry, xp, OUTCOME[code], risk, costs, paid)
        rs.append(r)
        by[code].append(k)
        funds.append(-paid / risk)
        if code == OPEN:
            open_px.append(xp)
    nt, ns, no = len(by[TARGET]), len(by[STOP]), len(by[OPEN])
    decided = nt + ns
    be = cc.breakeven_share(target_r, entry, plan["stop"], plan["target"], risk, costs)
    hist = histogram(rs, 24)
    return {
        "replays": n, "target": nt / float(n), "stop": ns / float(n), "open": no / float(n),
        "green": sum(1 for r in rs if r > 0) / float(n),
        "avg_r": sum(rs) / n, "median_r": cc.median(rs), "p5": cc.percentile(rs, 5), "p25": cc.percentile(rs, 25),
        "p75": cc.percentile(rs, 75), "p95": cc.percentile(rs, 95),
        "t_target": (cc.median(by[TARGET]) + 1) * STEP if nt else None,
        "t_stop": (cc.median(by[STOP]) + 1) * STEP if ns else None,
        "open_median": cc.median(open_px), "open_p5": cc.percentile(open_px, 5), "open_p95": cc.percentile(open_px, 95),
        "share": (nt / float(decided)) if decided else None, "breakeven": be,
        "avg_funding_r": sum(funds) / n, "histogram": hist,
    }


def histogram(xs, bins):
    lo, hi = min(xs), max(xs)
    if hi <= lo:
        return {"lo": lo, "hi": hi, "counts": [len(xs)]}
    w = (hi - lo) / bins
    counts = [0] * bins
    for x in xs:
        counts[min(bins - 1, int((x - lo) / w))] += 1
    return {"lo": lo, "hi": hi, "counts": counts}


def bands_of(paths, cols=60):
    n = len(paths[0])
    idx = sorted(set([0] + [int(round(i * (n - 1) / float(cols - 1))) for i in range(cols)]))
    out = []
    for i in idx:
        xs = sorted(p[i] for p in paths)
        out.append({"step": i, "p5": cc.percentile(xs, 5), "p25": cc.percentile(xs, 25), "p50": cc.percentile(xs, 50),
                    "p75": cc.percentile(xs, 75), "p95": cc.percentile(xs, 95)})
    return out


# ---------------------------------------------------------------- the real record

class Funding(object):
    """Funding actually paid over a span, from the venue's history (or today's rate when there is none)."""

    def __init__(self, history, current_rate, hours):
        hist = sorted(((t // 60000) * 60000, r) for t, r in history or [])
        self.ts = [t for t, _ in hist]
        self.rs = [r for _, r in hist]
        self.current = current_rate or 0.0
        self.hours = hours or 8.0

    def paid(self, side, t0, t1, price_at):
        """Per coin, positive when `side` paid, for payments F with t0 < F <= t1."""
        total = 0.0
        if self.ts:
            i, j = bisect.bisect_right(self.ts, t0), bisect.bisect_right(self.ts, t1)
            for k in range(i, j):
                total += self.rs[k] * price_at(self.ts[k])
        else:
            for f in cc.funding_times(t0, t1, self.hours):
                total += self.current * price_at(f)
        return cc.side_sign(side) * total


def simulate_real(bars, i0, side, entry, stop, target, n_steps):
    """Walk real bars from index i0 (the bar that opens at the entry). (outcome, exit, steps) or None if the data ends."""
    is_long = side == "long"
    for k in range(n_steps):
        i = i0 + k
        if i >= len(bars):
            return None
        b = bars[i]
        if is_long:
            if b[O] <= stop:
                return (STOP, b[O], k)
            if b[L] <= stop:
                return (STOP, stop, k)
            if b[H] >= target:
                return (TARGET, target, k)
        else:
            if b[O] >= stop:
                return (STOP, b[O], k)
            if b[H] >= stop:
                return (STOP, stop, k)
            if b[L] <= target:
                return (TARGET, target, k)
    last = i0 + n_steps - 1
    if last >= len(bars):
        return None
    return (OPEN, bars[last][C], n_steps)


def history_events(scan, setup, m, bars, side):
    """Every past moment the plan's setup rule fired, using the scanner's own code on the bars of the time."""
    name = setup["name"]
    a = scan.args_from(setup.get("params"))
    events = []
    times = [b[T] for b in bars]
    saved_now = cc.now_ms() if cc.pinned() else None
    try:
        if name == "orb":
            if not times:
                return events
            d0 = cc.utc_dt(times[0])
            d1 = cc.utc_dt(times[-1])
            day = cc.datetime(d0.year, d0.month, d0.day, tzinfo=cc.timezone.utc)
            while day <= d1:
                s_open = cc.session_open_on(a.session, day.year, day.month, day.day)
                day += cc.timedelta(days=1)
                if a.session == "ny" and cc.utc_dt(s_open).weekday() >= 5:
                    continue
                win_end = s_open + int(a.entry_hours * cc.HOUR_MS)
                if s_open - 5 * cc.HOUR_MS < times[0] or win_end > times[-1]:
                    continue
                i0 = bisect.bisect_left(times, s_open - 5 * cc.HOUR_MS)
                i1 = bisect.bisect_left(times, win_end)
                x = scan.Ctx(m, {}, bars[i0:i1], [])
                cc.set_now(win_end)
                for h in scan.setup_orb(x, a, [side]):
                    events.append(dict(h))
        elif name in ("breakout", "volume", "trend"):
            b15 = cc.resample(bars, "15m")
            b1h = cc.resample(bars, "1h")
            t1h = [b[T] for b in b1h]
            func = scan.SETUP_FUNCS[name]
            for i in range(120, len(b15)):
                now_t = b15[i][T] + 900000
                k = bisect.bisect_right(t1h, now_t - cc.HOUR_MS)
                x = scan.Ctx(m, {}, [], b1h[max(0, k - 260):k], b15=b15[max(0, i - 199):i + 1])
                for h in func(x, a, [side]):
                    if (name == "breakout" and h.get("status") != "fresh") or h.get("status") == "extended":
                        continue
                    events.append(dict(h))
        elif name == "reversal":
            b1h = cc.resample(bars, "1h")
            for k in range(40, len(b1h)):
                x = scan.Ctx(m, {}, [], b1h[max(0, k - 259):k + 1], b15=[])
                for h in scan.setup_reversal(x, a, [side]):
                    events.append(dict(h))
        elif name == "squeeze" and hasattr(scan, "setup_squeeze_break"):
            b1h = cc.resample(bars, "1h")
            for k in range(250, len(b1h)):
                x = scan.Ctx(m, {}, [], b1h[max(0, k - 749):k + 1], b15=[])
                for h in scan.setup_squeeze_break(x, a, [side]):
                    events.append(dict(h))
    finally:
        cc.set_now(saved_now)
    events.sort(key=lambda e: e["at"])
    return events


def stop_for_event(plan, ev, entry):
    """The plan's stop rule applied to a past event. Rules tied to one moment (a swing, a price)
    carry over in volatility terms: the plan's distance in 15-minute ATRs."""
    sg = cc.side_sign(plan["side"])
    spec = (plan.get("stop_spec") or "").lower()
    atr = ev.get("atr15")
    if spec == "setup":
        return ev["stop_ref_raw"] if ev.get("stop_ref_raw") is not None else ev.get("stop_ref")
    if spec == "range":
        mt = ev.get("metrics") or {}
        return mt.get("or_low") if plan["side"] == "long" else mt.get("or_high")
    if spec.startswith("pct:"):
        return entry * (1.0 - sg * float(spec[4:]) / 100.0)
    if spec.startswith("atr:") and atr:
        return entry - sg * float(spec[4:]) * atr
    if plan.get("stop_atr") and atr:
        return entry - sg * plan["stop_atr"] * atr
    if plan.get("risk_pct"):
        return entry * (1.0 - sg * plan["risk_pct"])
    return None


def atr5_series(bars, n=14):
    return cc.atr(bars, n)


def run_history(scan, plan, m, bars, funding, costs, n_steps):
    setup = plan.get("setup") or {}
    name = setup.get("name")
    res = {"setup": name, "events": 0, "trades": [], "skipped": 0, "unfinished": 0, "overlap": 0}
    if name == "squeeze" and setup.get("status") == "coiled":
        res["note"] = ("this plan enters before the squeeze breaks, and the squeeze rule only trades the break "
                       "(time it with /crypto-entry %s --setup squeeze)" % plan.get("coin", ""))
        return res
    if name not in HISTORY_SETUPS or scan is None:
        res["note"] = ("no setup to re-run" if not name else
                       "a hand-picked level has no past to re-run" if name == "level" else
                       "the %s setup has no fixed entry rule to re-run" % name if name == "rs" else
                       "crypto-scanner is not installed next to this skill" if scan is None else
                       "the %s setup has no rule to re-run" % name)
        return res
    side = plan["side"]
    sg = cc.side_sign(side)
    events = history_events(scan, setup, plan_market(plan), bars, side)
    res["events"] = len(events)
    b15 = cc.resample(bars, "15m")
    a15 = cc.atr(b15, 14)
    t15 = [b[T] for b in b15]
    for ev in events:
        i = bisect.bisect_right(t15, ev["at"] - 900000) - 1
        ev["atr15"] = a15[i] if 0 <= i < len(a15) else None
    idx = dict((b[T], i) for i, b in enumerate(bars))
    busy_until = -1
    a5 = atr5_series(bars)
    for ev in events:
        at = ev["at"]
        if at < busy_until:
            res["overlap"] += 1
            continue
        i0 = idx.get(at)
        if i0 is None:
            res["unfinished"] += 1
            continue
        entry = ev["trigger"]
        stop = stop_for_event(plan, ev, entry)
        if stop is None or sg * (entry - stop) <= entry * 0.0005:
            res["skipped"] += 1
            continue
        risk = abs(entry - stop)
        target = entry + sg * plan["target_r"] * risk
        sim = simulate_real(bars, i0, side, entry, stop, target, n_steps)
        if sim is None:
            res["unfinished"] += 1
            continue
        code, xp, k = sim
        t_exit = at + k * STEP
        paid = funding.paid(side, at, t_exit, lambda f: price_at(bars, idx, f, entry))
        r = cc.net_r(side, entry, xp, OUTCOME[code], risk, costs, paid)
        atr_at = a5[i0 - 1] if i0 >= 1 else None
        res["trades"].append({"at": at, "at_utc": cc.fmt_utc(at), "entry": entry, "stop": stop, "target": target,
                              "outcome": OUTCOME[code], "exit": xp, "bars": k + (0 if code == OPEN else 1), "r": r,
                              "slot": slot_of(at), "k_atr5": (risk / atr_at) if atr_at else None})
        busy_until = at + (k + 1) * STEP
    tr = res["trades"]
    n = len(tr)
    res["n"] = n
    if n:
        rs = [x["r"] for x in tr]
        res["target"] = sum(1 for x in tr if x["outcome"] == "target")
        res["stop"] = sum(1 for x in tr if x["outcome"] == "stop")
        res["open"] = sum(1 for x in tr if x["outcome"] == "open")
        res["avg_r"] = sum(rs) / n
        res["wins"] = sum(1 for r in rs if r > 0)
        p, lo, hi = cc.wilson(res["wins"], n)
        res["win_rate"], res["win_lo"], res["win_hi"] = p, lo, hi
        decided = res["target"] + res["stop"]
        res["decided"] = decided
        if decided:
            p, lo, hi = cc.wilson(res["target"], decided)
            res["share"], res["share_lo"], res["share_hi"] = p, lo, hi
        rng = random.Random(plan.get("_seed", 19) + 101)
        boots = sorted(sum(rs[int(rng.random() * n)] for _ in range(n)) / n for _ in range(2000))
        res["ci90"] = (boots[100], boots[1899])
    return res


def price_at(bars, idx, f, fallback):
    i = idx.get(f - STEP)
    return bars[i][C] if i is not None else fallback


def run_baseline(plan, bars, hist, funding, costs, n_steps, pool, seed, per_trade=100):
    """Random entries at each historical trade's time of day, with that trade's stop distance
    (in 5-minute ATRs) and the plan's target. Then: could luck have matched the setup?"""
    trades = [t for t in hist.get("trades") or [] if t.get("k_atr5")]
    if len(trades) < 3:
        return None
    side = plan["side"]
    sg = cc.side_sign(side)
    rng = random.Random(seed + 202)
    a5 = atr5_series(bars)
    idx = dict((b[T], i) for i, b in enumerate(bars))
    n = len(bars)
    times = [b[T] for b in bars]
    by_slot = [[] for _ in range(SLOTS)]
    for e in range(16, n - 1):
        if times[e] - times[e - 1] == STEP and a5[e - 1]:
            by_slot[(times[e] % DAY) // STEP].append(e)
    w = pool.window if pool else 2
    groups = []
    for tr in trades:
        cand = []
        for d in range(-w, w + 1):
            cand.extend(by_slot[(tr["slot"] + d) % SLOTS])
        got = []
        tries = 0
        while len(got) < per_trade and cand and tries < per_trade * 4:
            tries += 1
            e = cand[int(rng.random() * len(cand))]
            entry = bars[e - 1][C]
            risk = tr["k_atr5"] * a5[e - 1]
            if risk <= entry * 0.0005:
                continue
            stop = entry - sg * risk
            target = entry + sg * plan["target_r"] * risk
            sim = simulate_real(bars, e, side, entry, stop, target, n_steps)
            if sim is None:
                continue
            code, xp, k = sim
            paid = funding.paid(side, times[e], times[e] + k * STEP, lambda f: price_at(bars, idx, f, entry))
            got.append(cc.net_r(side, entry, xp, OUTCOME[code], risk, costs, paid))
        if got:
            groups.append(got)
    if len(groups) < 3:
        return None
    flat = [r for g in groups for r in g]
    base = sum(flat) / len(flat)
    obs = hist["avg_r"]
    m = len(groups)
    reps = 2000
    hits = 0
    for _ in range(reps):
        s = 0.0
        for g in groups:
            s += g[int(rng.random() * len(g))]
        if s / m >= obs:
            hits += 1
    rs = [t["r"] for t in hist["trades"]]
    k = len(rs)
    diffs = sorted(sum(rs[int(rng.random() * k)] for _ in range(k)) / k - base for _ in range(2000))
    return {"n": len(flat), "avg_r": base, "luck_p": (hits + 1) / float(reps + 1), "edge": obs - base,
            "edge_ci90": (diffs[100], diffs[1899]), "per_trade": per_trade}


def plan_market(plan):
    return {"symbol": plan["symbol"], "coin": plan["coin"], "multiplier": plan.get("multiplier", 1), "tick": plan.get("tick")}


# ---------------------------------------------------------------- verdicts

def mc_verdict(mc, plan, exit_words):
    share, be = mc["share"], mc["breakeven"]
    lines = []
    if share is None or mc["open"] >= 0.5:
        head = ("Mostly out of time: %.1f%% of the replays reached neither the stop nor the target by %s; "
                "average R after costs %+.2f." % (mc["open"] * 100, exit_words, mc["avg_r"]))
        lines.append("The stop or the target is far for a %gh hold. A nearer target, a closer stop or a longer hold changes this."
                     % plan.get("hold_hours", 24))
        return head, lines
    gap = (share - be) * 100.0
    toward = "the target" if gap > 0 else "the stop"
    if abs(gap) < 5:
        label = "Close to break-even"
    elif abs(gap) < 15:
        label = "Leans toward %s" % toward
    else:
        label = "Strongly toward %s" % toward
    head = ("%s: to break even after costs the target must come first in %.1f%% of the replays that hit a side; it did in %.1f%%."
            % (label, be * 100, share * 100))
    if mc["open"] > 0.40:
        lines.append("%.1f%% of the replays were still open at %s; the price then decides those." % (mc["open"] * 100, exit_words))
    if (gap > 0 and mc["avg_r"] < 0) or (gap < 0 and mc["avg_r"] > 0):
        lines.append("Average R after costs is %+.2f, the other way, because of the replays still open at the end." % mc["avg_r"])
    return head, lines


def final_verdict(mc_label, hist, base, breakeven):
    n = hist.get("n") or 0
    if not hist.get("setup"):
        return "Verdict: no setup to test, only this stop and target. The replays: %s." % mc_label.lower()
    if hist.get("note"):
        return "Verdict: %s, so only the replays speak: %s." % (hist["note"], mc_label.lower())
    if n == 0:
        return "Verdict: the setup never became a trade in the pool, so only the replays speak: %s." % mc_label.lower()
    if hist.get("decided", 0) >= 5 and hist["share_hi"] < breakeven:
        return ("Verdict: no edge so far. The target came first in %d of %d decided past trades; even the top of the "
                "95%% range (%d%%) is under the %.1f%% break-even." % (hist["target"], hist["decided"],
                                                                     round(hist["share_hi"] * 100), breakeven * 100))
    if n < 10:
        return ("Verdict: too few past trades (%d) to judge the setup itself. The replays: %s." % (n, mc_label.lower()))
    avg, (lo, hi) = hist["avg_r"], hist["ci90"]
    if base is None:
        if avg <= 0:
            return "Verdict: no edge. The setup lost %.2fR a trade after costs over %d past trades." % (-avg, n)
        return "Verdict: the setup made %+.2fR a trade over %d past trades, with no random baseline to compare. Small sample." % (avg, n)
    if avg <= 0:
        return ("Verdict: no edge. The setup lost %.2fR a trade after costs over its last %d trades here "
                "(random entries: %+.2fR)." % (-avg, n, base["avg_r"]))
    if base["luck_p"] <= 0.10:
        return ("Verdict: the setup beat random entries here: %+.2fR a trade after costs over %d trades, against %+.2fR "
                "for random entries; luck matches that %d%% of the time. A small sample: a hint, not proof."
                % (avg, n, base["avg_r"], round(base["luck_p"] * 100)))
    return ("Verdict: no clear edge. The setup made %+.2fR a trade after costs, but random entries at the same hours "
            "did as well %d%% of the time. %d trades is a small sample." % (avg, round(base["luck_p"] * 100), n))


# ---------------------------------------------------------------- the run

def fetch_pool(venue, plan, days):
    """30 days (or `days`) of 5-minute bars before the plan's entry, and the funding paid over them."""
    sym = plan["symbol"]
    entry_time = (plan["entry_time"] // STEP) * STEP
    note = None
    if venue.name == "hyperliquid" and days > 17:
        days, note = 17, "Hyperliquid keeps about 17 days of 5-minute candles, so the pool is 17 days."
    start = entry_time - days * DAY
    jobs = {"bars": lambda: venue.bars_between(sym, "5m", start, entry_time - 1),
            "funding": lambda: venue.funding_history(sym, start, entry_time)}
    got = {}
    for k, r, e in cc.parallel(lambda k: jobs[k](), list(jobs), workers=2):
        if e is not None and k == "bars":
            raise cc.CryptoError("could not read %s candles: %s" % (sym, e))
        got[k] = r if e is None else []
    bars = [b for b in got["bars"] if b[T] < entry_time]
    return bars, got.get("funding") or [], days, note


def evaluate(plan, venue, cfg, replays=10000, seed=19, days=30, block=12.0, window=2, vol_scale=True,
             keep_drift=False, baseline=True, pool_data=None, scan=None, paths=400):
    """Everything the Time Machine knows about one plan, as the dict it saves in crypto/timemachine.json.
    pool_data, when given, is fetch_pool's answer (crypto-scout fetches it once and reuses it)."""
    for k in ("venue", "symbol", "side", "entry", "stop", "target", "entry_time", "exit_by", "risk", "target_r"):
        if plan.get(k) is None:
            raise cc.CryptoError("the plan is missing %s; make it again with /crypto-exit" % k)
    plan = dict(plan)
    plan["_seed"] = seed
    plan.setdefault("plan_id", cc.plan_id(plan))
    side = plan["side"]
    entry_time = (plan["entry_time"] // STEP) * STEP
    n_steps = int((plan["exit_by"] - entry_time) // STEP)
    if n_steps < 1:
        raise cc.CryptoError("the plan's time limit is before its entry")
    bars, fund_hist, days, note_days = pool_data if pool_data is not None else fetch_pool(venue, plan, days)
    if len(bars) < 3 * SLOTS:
        raise cc.CryptoError("only %d five-minute bars before the entry; the Time Machine needs at least 3 days" % len(bars))
    costs = cc.plan_costs(plan, venue, cfg)
    pc = plan.get("costs") or {}
    pool = Pool(bars, vol_scale=vol_scale, keep_drift=keep_drift, window=window)
    rate = pc.get("funding_rate") or 0.0
    hours = pc.get("funding_hours") or 8.0
    fund_steps = {}
    for f in cc.funding_times(entry_time, plan["exit_by"], hours):
        k = int((f - entry_time) // STEP)
        if 1 <= k < n_steps:
            fund_steps[k] = rate
    slot0 = slot_of(entry_time)
    results = run_mc(pool, side, plan["entry"], plan["stop"], plan["target"], n_steps, slot0, block, replays, seed, fund_steps)
    mc = summarize_mc(results, plan, costs)
    if paths:
        sample = run_paths(pool, plan["entry"], n_steps, slot0, block, paths, seed + 1)
        mc["bands"] = bands_of(sample)
        mc["sample_paths"] = [[round(x, 10) for x in p[::max(1, n_steps // 60)]] for p in sample[:40]]
    funding = Funding(fund_hist, rate, hours)
    if scan is None:
        scan = load_sibling("crypto-scanner", "scan")
    hist = run_history(scan, plan, plan_market(plan), bars, funding, costs, n_steps)
    base = None
    if hist.get("n", 0) >= 3 and baseline:
        base = run_baseline(plan, bars, hist, funding, costs, n_steps, pool, seed)
    exit_words = cc.fmt_utc(plan["exit_by"])
    head, sub = mc_verdict(mc, plan, exit_words)
    label = head.split(":")[0]
    verdict = final_verdict(label, hist, base, mc["breakeven"])
    return {
        "version": 1, "run_at": cc.now_ms(),
        "plan_ref": dict((k, plan.get(k)) for k in ("plan_id", "created_at", "venue", "symbol", "coin", "side", "entry",
                                                  "stop", "target", "target_r", "entry_time", "exit_by")),
        "params": {"replays": replays, "seed": seed, "block": block, "window": window, "days": days,
                   "vol_scale": vol_scale, "keep_drift": keep_drift, "n_steps": n_steps},
        "pool": {"bars": pool.size, "from": bars[0][T], "to": bars[-1][T] + STEP, "vol_ratio": pool.vol_ratio,
                 "clipped": pool.clipped, "drift_per_day": pool.mu * SLOTS},
        "costs": costs, "funding_rate": rate, "funding_hours": hours,
        "mc": mc, "history": hist, "baseline": base,
        "verdict": {"mc": head, "notes": sub, "final": verdict}, "notes": [x for x in [note_days] if x],
    }


def run(a, cfg):
    plan = cc.read_json(a.plan or cc.wpath("plan.json"), None)
    if not plan:
        raise cc.CryptoError("no plan to replay. Make one first: /crypto-exit (exit.py plan <COIN>)")
    venue = cc.get_venue(plan.get("venue"))
    data = evaluate(plan, venue, cfg, replays=a.replays, seed=a.seed, days=a.days, block=a.block, window=a.window,
                    vol_scale=not a.no_vol_scale, keep_drift=a.keep_drift, baseline=not a.no_baseline)
    if not a.no_write:
        cc.write_json(cc.wpath("timemachine.json"), data)
    if a.json:
        slim = dict(data)
        slim["mc"] = dict((k, v) for k, v in data["mc"].items() if k not in ("sample_paths",))
        cc.dump(slim)
    else:
        print_run(data, plan)
    if a.stats:
        cc.out("Requests: " + cc.HTTP.summary())
    return 0


def print_run(d, plan):
    tk = plan.get("tick")
    P = lambda x: cc.fmt_price(x, tk)
    mc, hist, base, prm, pool = d["mc"], d["history"], d["baseline"], d["params"], d["pool"]
    cc.out("Time Machine | %s %s | %s %s | plan made %s" % (plan["coin"], plan["side"], plan["venue_label"].split(" ")[0],
                                                          plan["symbol"], cc.fmt_utc(plan["created_at"])))
    hours = prm["n_steps"] * STEP / float(cc.HOUR_MS)
    cc.out("Plan: entry %s, stop %s (-1R), target %s (%+gR), out by %s (%d five-minute bars)." %
           (P(plan["entry"]), P(plan["stop"]), P(plan["target"]), round(plan["target_r"], 2), cc.fmt_utc(plan["exit_by"]),
            prm["n_steps"]))
    pl = "Pool: %s five-minute bars, %s to %s." % ("{:,}".format(pool["bars"]), cc.fmt_utc(pool["from"], date=True)[:10],
                                                   cc.fmt_utc(pool["to"])[:10])
    if prm["vol_scale"]:
        pl += " Today is %.2fx as volatile as the pool's typical day; replays are scaled to today." % pool["vol_ratio"]
    pl += " Drift %s." % ("kept" if prm["keep_drift"] else "removed (%+.2f%% a day in the pool)" % (pool["drift_per_day"] * 100))
    cc.out(pl)
    for n in d.get("notes") or []:
        cc.out(n)
    cc.out("")
    cc.out("%s replays of the next %gh in blocks of about %s, seed %d:" %
           ("{:,}".format(prm["replays"]), hours, cc.fmt_dur(prm["block"] * STEP), prm["seed"]))
    cc.out("  target first  %5.1f%%   %s" % (mc["target"] * 100, ("median %s in" % cc.fmt_dur(mc["t_target"])) if mc["t_target"] else ""))
    cc.out("  stop first    %5.1f%%   %s" % (mc["stop"] * 100, ("median %s in" % cc.fmt_dur(mc["t_stop"])) if mc["t_stop"] else ""))
    ol = "  still open    %5.1f%%   out at the time limit" % (mc["open"] * 100)
    if mc.get("open_median"):
        ol += ", price median %s (5%% to 95%%: %s to %s)" % (P(mc["open_median"]), P(mc["open_p5"]), P(mc["open_p95"]))
    cc.out(ol)
    cc.out("  green         %5.1f%%   ended above zero after costs" % (mc["green"] * 100))
    cc.out("  Average R after costs %+.2f (median %+.2f; 90%% of replays between %+.2f and %+.2f)." %
           (mc["avg_r"], mc["median_r"], mc["p5"], mc["p95"]))
    c = plan.get("costs") or {}
    cc.out("  Costs: %.2fR in; out %.2fR at the target, %.2fR at a stop; funding averaged %+.2fR." %
           (c.get("entry_r", 0), c.get("target_exit_r", 0), c.get("stop_exit_r", 0), mc["avg_funding_r"]))
    cc.out(d["verdict"]["mc"])
    for line in d["verdict"]["notes"]:
        cc.out("  " + line)
    cc.out("")
    name = hist.get("setup")
    if hist.get("note"):
        cc.out("History: %s." % hist["note"])
    elif name:
        words = SETUP_WORDS.get(name, name)
        sess = ((plan.get("setup") or {}).get("params") or {}).get("session")
        if name == "orb" and sess:
            words += " (%s open)" % cc.SESSION_NAMES.get(sess, sess)
        n = hist.get("n") or 0
        cc.out("History: the same %s rule on the pool fired %d time%s; %d became %s, one at a time." %
               (words, hist["events"], "" if hist["events"] == 1 else "s", n, "a trade" if n == 1 else "trades"))
        if n:
            avg = "Average R after costs %+.2f" % hist["avg_r"]
            if n >= 3:
                avg += " (90%% range %+.2f to %+.2f)" % (hist["ci90"][0], hist["ci90"][1])
            cc.out("  %d target, %d stop, %d out at the time limit. %s." % (hist["target"], hist["stop"], hist["open"], avg))
            if hist.get("decided"):
                cc.out("  Target first in %d of %d decided: %d%% (95%% range %d%% to %d%%); break-even needs %.1f%%." %
                       (hist["target"], hist["decided"], round(hist["share"] * 100), round(hist["share_lo"] * 100),
                        round(hist["share_hi"] * 100), d["mc"]["breakeven"] * 100))
        extra = []
        if hist.get("overlap"):
            extra.append("%d fired while a trade was open" % hist["overlap"])
        if hist.get("unfinished"):
            extra.append("%d too recent to finish" % hist["unfinished"])
        if hist.get("skipped"):
            extra.append("%d had no valid stop" % hist["skipped"])
        if extra:
            cc.out("  Left out: " + ", ".join(extra) + ".")
        if plan.get("stop_spec") not in ("setup", "range") and not str(plan.get("stop_spec", "")).startswith(("atr:", "pct:")):
            cc.out("  Past trades use the plan's stop in volatility terms: %.1f x the 15-minute ATR." % (plan.get("stop_atr") or 0))
    if base:
        cc.out("Edge test: %s random entries at the same times of day, same stop distance and target, averaged %+.2fR." %
               ("{:,}".format(base["n"]), base["avg_r"]))
        if base["edge"] < 0:
            cc.out("  The setup did worse than random entries: edge %+.2fR a trade (90%% range %+.2f to %+.2f)." %
                   (base["edge"], base["edge_ci90"][0], base["edge_ci90"][1]))
        else:
            cc.out("  Luck alone matched the setup's %+.2fR %d%% of the time: edge %+.2fR a trade (90%% range %+.2f to %+.2f)." %
                   (hist["avg_r"], round(base["luck_p"] * 100), base["edge"], base["edge_ci90"][0], base["edge_ci90"][1]))
    cc.out(d["verdict"]["final"])
    cc.out("Built from the last %d days of this coin's own candles. A simulation, not a prediction." % prm["days"])


# ---------------------------------------------------------------- chart

def chart(a):
    d = cc.read_json(a.source or cc.wpath("timemachine.json"), None)
    if not d:
        raise cc.CryptoError("no Time Machine run yet. Run: timemachine.py run")
    pr = d["plan_ref"]
    bands = d["mc"]["bands"]
    rows = 18
    entry, stop, target = pr["entry"], pr["stop"], pr["target"]
    lo = min(min(b["p5"] for b in bands), stop, target)
    hi = max(max(b["p95"] for b in bands), stop, target)
    pad = (hi - lo) * 0.04
    lo, hi = lo - pad, hi + pad

    def row_of(p):
        return int(round((hi - p) / (hi - lo) * (rows - 1)))
    grid = [[" "] * len(bands) for _ in range(rows)]
    for x, b in enumerate(bands):
        for r in range(row_of(b["p95"]), row_of(b["p5"]) + 1):
            grid[r][x] = "."
        for r in range(row_of(b["p75"]), row_of(b["p25"]) + 1):
            grid[r][x] = ":"
        grid[row_of(b["p50"])][x] = "o"
    for x in range(len(bands)):
        for p, ch in ((target, "="), (stop, "-")):
            r = row_of(p)
            if grid[r][x] == " ":
                grid[r][x] = ch
    labels = {row_of(target): "target %s" % cc.fmt_price(target), row_of(stop): "stop %s" % cc.fmt_price(stop),
              row_of(entry): "entry %s" % cc.fmt_price(entry)}
    width = max(len(cc.fmt_price(hi)), len(cc.fmt_price(lo)))
    cc.out("Time Machine fan chart | %s %s | %s replays' price paths" % (pr["coin"], pr["side"], "400"))
    for r in range(rows):
        price = hi - (hi - lo) * r / (rows - 1)
        cc.out("%s |%s| %s" % (cc.fmt_price(price).rjust(width), "".join(grid[r]), labels.get(r, "")))
    hours = d["params"]["n_steps"] * STEP / float(cc.HOUR_MS)
    cc.out("%s  entry%s+%gh" % (" " * width, " " * max(1, len(bands) - 8), hours))
    cc.out("o median   : middle half   . 90% of paths   = target   - stop. Paths ignore the stop and target.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="timemachine.py", description="Replay a plan before you risk a dollar.")
    p.add_argument("command", nargs="?", default="run", choices=["run", "chart"])
    p.add_argument("--plan", default=None, help="a plan file (default crypto/plan.json)")
    p.add_argument("--replays", type=int, default=10000)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--days", type=int, default=30, help="days of 5-minute bars to learn from (default 30)")
    p.add_argument("--block", type=float, default=12.0, help="average block length in 5-minute bars (default 12 = 1 hour; 1 = bar by bar)")
    p.add_argument("--window", type=int, default=2, help="time-of-day match, +/- bars (default 2)")
    p.add_argument("--no-vol-scale", action="store_true", help="replay past volatility as it was")
    p.add_argument("--keep-drift", action="store_true", help="keep the pool's trend in the replays")
    p.add_argument("--no-baseline", action="store_true", help="skip the random-entry edge test")
    p.add_argument("--from", dest="source", default=None, help="chart: the run to draw (default crypto/timemachine.json)")
    cc.add_common(p, venue=False)
    a = p.parse_args(argv)
    cc.apply_common(a)
    if a.command == "chart":
        return chart(a)
    if a.replays < 100 or a.replays > 200000:
        raise cc.CryptoError("--replays must be between 100 and 200,000")
    if a.days < 3 or a.days > 120:
        raise cc.CryptoError("--days must be between 3 and 120")
    return run(a, cc.load_config())


if __name__ == "__main__":
    sys.exit(cc.run(main))
