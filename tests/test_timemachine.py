"""crypto-timemachine. The engine against known answers on synthetic bars, the history and the edge test
against a market built to have (and not have) an edge, and the whole run on the fake exchange."""

import json
import math
import random
import unittest

from _load import cc, load, Session, WED

TM = load("crypto-timemachine", "timemachine")
SCAN = load("crypto-scanner", "scan")
STEP = 300000
T0 = (1760000000000 // 86400000) * 86400000          # a UTC midnight
COSTS0 = {"taker": 0.0, "maker": 0.0, "slippage": 0.0}


def walk(days=20, sigma=0.001, seed=5, drift=0.0, start=100.0, phi=0.0):
    """Synthetic 5-minute bars: a random walk (optionally trending, optionally with momentum phi)."""
    rng = random.Random(seed)
    bars, px, prev = [], start, 0.0
    for i in range(int(288 * days)):
        r = drift + phi * prev + rng.gauss(0, sigma)
        prev = r
        o, c = px, px * math.exp(r)
        h = max(o, c) * math.exp(abs(rng.gauss(0, sigma * 0.3)))
        lo = min(o, c) * math.exp(-abs(rng.gauss(0, sigma * 0.3)))
        bars.append((T0 + i * STEP, o, h, lo, c, 1.0, 100.0))
        px = c
    return bars


def target_share(res):
    nt = sum(1 for r in res if r[0] == TM.TARGET)
    ns = sum(1 for r in res if r[0] == TM.STOP)
    return nt / float(nt + ns)


class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = walk()

    def test_matches_gamblers_ruin_on_a_random_walk(self):
        # a driftless walk: the target (T x the risk away) comes first 1 / (1 + T) of the time
        for vol_scale in (False, True):
            pool = TM.Pool(self.bars, vol_scale=vol_scale)
            for T in (1.0, 2.0):
                res = TM.run_mc(pool, "long", 100.0, 98.5, 100.0 + 1.5 * T, 2880, 0, 12, 4000, 3, {})
                self.assertAlmostEqual(target_share(res), 1.0 / (1.0 + T), delta=0.03, msg="T=%g vol_scale=%s" % (T, vol_scale))
            res = TM.run_mc(pool, "short", 100.0, 101.5, 97.0, 2880, 0, 12, 4000, 3, {})
            self.assertAlmostEqual(target_share(res), 1.0 / 3.0, delta=0.03)

    def test_drift_is_removed_unless_kept(self):
        up = walk(drift=0.0004, seed=9)                       # about +12% a day
        flat = TM.Pool(up)
        kept = TM.Pool(up, keep_drift=True)
        ends = lambda pool: sorted(math.log(p[-1] / 100.0) for p in TM.run_paths(pool, 100.0, 288, 0, 12, 600, 1))
        self.assertLess(abs(ends(flat)[300]), 0.01)
        self.assertGreater(ends(kept)[300], 0.08)
        self.assertGreater(flat.mu * 288, 0.1)                # it still reports the drift it took out

    def test_volatility_is_scaled_to_today(self):
        calm_last_day = walk(days=8, sigma=0.002, seed=2)[:-288] + walk(days=1, sigma=0.0005, seed=3)
        fixed = [(T0 + i * STEP,) + b[1:] for i, b in enumerate(calm_last_day)]
        # stitch prices so the series is continuous
        bars, px = [], 100.0
        for i, b in enumerate(fixed):
            k = px / b[1]
            bars.append((b[0], b[1] * k, b[2] * k, b[3] * k, b[4] * k, b[5], b[6]))
            px = b[4] * k
        pool = TM.Pool(bars)
        self.assertAlmostEqual(pool.sigma_now, 0.0005, delta=0.0001)
        self.assertLess(pool.vol_ratio, 0.5)
        rets = [math.log(pool.rc[j]) for j in range(pool.n) if pool.usable[j] and j < pool.n - 600]
        sd = (sum(r * r for r in rets) / len(rets)) ** 0.5
        self.assertAlmostEqual(sd, 0.001, delta=0.0003)      # the 0.002 days clip at 0.5x: 0.001

    def test_blocks_keep_momentum_that_bar_by_bar_sampling_loses(self):
        trending = walk(days=20, sigma=0.001, seed=4, phi=0.6)
        pool = TM.Pool(trending, vol_scale=False)

        def autocorr(block):
            xs = []
            for p in TM.run_paths(pool, 100.0, 288, 0, block, 200, 5):
                r = [math.log(p[i + 1] / p[i]) for i in range(len(p) - 1)]
                xs.append(cc.corr(r[:-1], r[1:]))
            return sum(xs) / len(xs)
        self.assertGreater(autocorr(12), 0.4)
        self.assertLess(abs(autocorr(1)), 0.1)

    def test_costs_and_funding_on_a_flat_market(self):
        flat = [(T0 + i * STEP, 100.0, 100.0, 100.0, 100.0, 1.0, 100.0) for i in range(288 * 4)]
        pool = TM.Pool(flat, vol_scale=False)
        costs = {"taker": 0.0005, "maker": 0.0002, "slippage": 0.0003}
        res = TM.run_mc(pool, "long", 100.0, 98.0, 104.0, 288, 0, 12, 50, 1, {96: 0.0001, 192: 0.0001})
        self.assertTrue(all(r[0] == TM.OPEN for r in res))
        plan = {"side": "long", "entry": 100.0, "stop": 98.0, "target": 104.0, "risk": 2.0, "target_r": 2.0}
        mc = TM.summarize_mc(res, plan, costs)
        expect = -(0.0008 * 100 / 2.0) - (0.0008 * 100 / 2.0) - (2 * 0.0001 * 100 / 2.0)
        self.assertAlmostEqual(mc["avg_r"], expect, places=9)
        self.assertAlmostEqual(mc["avg_funding_r"], -0.01, places=9)

    def test_a_gap_fills_past_the_stop_and_a_bar_touching_both_is_a_stop(self):
        bars, px = [], 100.0
        for i in range(288 * 3):
            o = px * (0.97 if i % 10 == 9 else 1.0)
            bars.append((T0 + i * STEP, o, o * 1.0001, o * 0.9999, o, 1.0, 100.0))
            px = o
        pool = TM.Pool(bars, vol_scale=False, keep_drift=True)
        res = TM.run_mc(pool, "long", 100.0, 99.0, 103.0, 288, 0, 1, 300, 2, {})
        stops = [r for r in res if r[0] == TM.STOP]
        self.assertTrue(stops)
        self.assertTrue(all(r[1] <= 99.0 for r in stops))
        self.assertTrue(any(r[1] < 98.0 for r in stops))       # filled at the gap's open, not at the stop
        wide = [(T0 + i * STEP, 100.0, 105.0, 95.0, 100.0, 1.0, 100.0) for i in range(288 * 3)]
        res = TM.run_mc(TM.Pool(wide, vol_scale=False), "long", 100.0, 99.0, 101.0, 10, 0, 1, 100, 2, {})
        self.assertTrue(all(r[0] == TM.STOP and r[2] == 0 for r in res))

    def test_same_seed_same_answer(self):
        pool = TM.Pool(self.bars)
        a = TM.run_mc(pool, "long", 100.0, 99.0, 102.0, 288, 10, 12, 500, 19, {})
        b = TM.run_mc(pool, "long", 100.0, 99.0, 102.0, 288, 10, 12, 500, 19, {})
        c = TM.run_mc(pool, "long", 100.0, 99.0, 102.0, 288, 10, 12, 500, 20, {})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_real_bar_walk(self):
        bars = [(T0 + i * STEP, 100.0, 100.5, 99.5, 100.0, 1.0, 1.0) for i in range(10)]
        bars[3] = (bars[3][0], 100.0, 102.5, 99.8, 102.0, 1.0, 1.0)
        self.assertEqual(TM.simulate_real(bars, 1, "long", 100.0, 98.0, 102.0, 8), (TM.TARGET, 102.0, 2))
        self.assertEqual(TM.simulate_real(bars, 1, "short", 100.0, 102.0, 98.0, 8), (TM.STOP, 102.0, 2))
        gap = list(bars)
        gap[2] = (gap[2][0], 97.0, 97.5, 96.5, 97.0, 1.0, 1.0)
        self.assertEqual(TM.simulate_real(gap, 1, "long", 100.0, 98.0, 103.0, 8), (TM.STOP, 97.0, 1))
        self.assertEqual(TM.simulate_real(bars, 4, "long", 100.0, 98.0, 103.0, 3)[0], TM.OPEN)
        self.assertIsNone(TM.simulate_real(bars, 8, "long", 100.0, 98.0, 103.0, 5))

    def test_funding_paid_counts_the_right_payments(self):
        hist = [(T0 + 8 * 3600000 + 4, 0.0001), (T0 + 16 * 3600000 + 2, 0.0002), (T0 + 24 * 3600000, -0.0001)]
        f = TM.Funding(hist, 0.0005, 8)
        one = lambda t: 100.0
        self.assertAlmostEqual(f.paid("long", T0, T0 + 16 * 3600000, one), 0.03)
        self.assertAlmostEqual(f.paid("short", T0 + 8 * 3600000, T0 + 24 * 3600000, one), -0.01)
        none = TM.Funding([], 0.0005, 8)
        self.assertAlmostEqual(none.paid("long", T0, T0 + 24 * 3600000, one), 3 * 0.05)


def orb_market(edge=True, days=28, seed=3):
    """5-minute bars where an opening-range breakout after the New York open (13:30 UTC in summer time)
    always runs to 3R on half the weekdays (edge=True), and fails every time (edge=False). On the other
    weekdays the same hours sell off without a breakout, so a random long at that time of day loses."""
    rng = random.Random(seed)
    start = cc.parse_when("2026-09-14 00:00")               # a Monday, US summer time
    bars, px = [], 100.0
    for d in range(days):
        day0 = start + d * 86400000
        wd = cc.utc_dt(day0).weekday()
        good = (d % 2 == 0)
        for k in range(288):
            t = day0 + k * STEP
            mins = k * 5
            o = px
            vol = 100.0
            if wd < 5 and 13 * 60 + 30 <= mins < 14 * 60:            # the opening range: a zig-zag of about 0.2%
                c = px * (1.0 + 0.002 * (1 if (mins // 5) % 2 else -1))
            elif wd < 5 and 14 * 60 <= mins < 14 * 60 + 15:            # quiet inside the range until the breakout bar
                c = px * 0.9995
            elif wd < 5 and mins == 14 * 60 + 15 and good:           # the breakout bar: closes above the range on 5x volume
                c = px * 1.012
                vol = 500.0
            elif wd < 5 and 14 * 60 + 20 <= mins < 16 * 60 + 30:
                if good:
                    c = px * ((1.0025 if edge else 0.9975))       # the follow-through (or the failure)
                else:
                    c = px * 0.998                               # a sell-off at the same hours, no breakout
            else:
                c = px * math.exp(rng.gauss(0, 0.0004))
            h = max(o, c) * 1.0003
            lo = min(o, c) * 0.9997
            bars.append((t, o, h, lo, c, vol / c, vol))
            px = c
        px = 100.0 + rng.gauss(0, 0.2)                            # each day starts near 100 again
    return bars


class HistoryAndEdge(unittest.TestCase):
    def plan(self, bars):
        return {"side": "long", "venue": "binance", "symbol": "TESTUSDT", "coin": "TEST", "multiplier": 1, "tick": None,
                "stop_spec": "setup", "target_r": 2.0, "risk_pct": 0.01, "stop_atr": 2.0, "_seed": 19,
                "setup": {"name": "orb", "params": {"session": "ny", "or_minutes": 30, "entry_hours": 4.0, "min_rvol": 1.5}}}

    def run_both(self, edge):
        bars = orb_market(edge=edge)
        plan = self.plan(bars)
        funding = TM.Funding([], 0.0, 8)
        hist = TM.run_history(SCAN, plan, TM.plan_market(plan), bars, funding, COSTS0, 288)
        pool = TM.Pool(bars)
        base = TM.run_baseline(plan, bars, hist, funding, COSTS0, 288, pool, 19)
        return hist, base

    def test_a_real_edge_is_found(self):
        hist, base = self.run_both(edge=True)
        self.assertGreaterEqual(hist["n"], 9)
        self.assertEqual(hist["target"], hist["n"])
        self.assertGreater(hist["avg_r"], 1.9)
        self.assertLess(base["luck_p"], 0.01)
        self.assertGreater(base["edge"], 1.0)
        v = TM.final_verdict("Close to break-even", dict(hist, n=max(hist["n"], 10)), base, 0.34)
        self.assertIn("beat random entries", v)

    def test_no_edge_is_called_no_edge(self):
        hist, base = self.run_both(edge=False)
        self.assertGreaterEqual(hist["n"], 9)
        self.assertEqual(hist["stop"], hist["n"])
        self.assertLess(hist["avg_r"], -0.9)
        v = TM.final_verdict("Close to break-even", hist, base, 0.34)
        self.assertIn("no edge", v)

    def test_verdict_rules(self):
        base = {"avg_r": -0.1, "luck_p": 0.4}
        h = {"setup": "orb", "n": 30, "avg_r": 0.2, "ci90": (-0.1, 0.5), "decided": 28, "target": 12, "share_hi": 0.6}
        self.assertIn("no clear edge", TM.final_verdict("Close to break-even", h, base, 0.36))
        self.assertIn("beat random entries", TM.final_verdict("x", h, dict(base, luck_p=0.05), 0.36))
        self.assertIn("too few past trades (6)", TM.final_verdict("Leans toward the stop", dict(h, n=6, decided=4), base, 0.36))
        self.assertIn("no setup to test", TM.final_verdict("Close to break-even", {"setup": None}, None, 0.36))
        self.assertIn("never became a trade", TM.final_verdict("x", {"setup": "orb", "n": 0}, None, 0.36))
        self.assertIn("even the top of the 95% range", TM.final_verdict(
            "x", {"setup": "orb", "n": 7, "decided": 7, "target": 0, "share_hi": 0.35}, None, 0.363))


class FullRun(unittest.TestCase):
    def test_end_to_end_on_the_fake_exchange(self):
        with Session() as s:
            s.run("crypto-scanner", "scan", ["--venue", "binance", "--session", "ny"])
            code, out, err = s.run("crypto-exit", "exit", ["plan", "ORBX"])
            self.assertEqual(code, 0, err)
            code, out, err = s.run("crypto-timemachine", "timemachine", ["run", "--replays", "3000"])
            self.assertEqual(code, 0, err)
            for line in ("3,000 replays", "target first", "stop first", "still open", "Average R after costs",
                         "History: the same opening-range (New York open) rule", "Verdict:", "seed 19",
                         "A simulation, not a prediction"):
                self.assertIn(line, out)
            d = cc.read_json(s.path("timemachine.json"))
            plan = cc.read_json(s.path("plan.json"))
            self.assertEqual(d["plan_ref"]["plan_id"], plan["plan_id"])
            mc = d["mc"]
            self.assertAlmostEqual(mc["target"] + mc["stop"] + mc["open"], 1.0)
            self.assertEqual(len(mc["bands"]), 60)
            self.assertLessEqual(d["pool"]["to"], plan["entry_time"])      # never a candle from after the entry
            code2, out2, err2 = s.run("crypto-timemachine", "timemachine", ["run", "--replays", "3000"])
            self.assertEqual(out, out2)                                     # same seed, same numbers
            code, out, err = s.run("crypto-timemachine", "timemachine", ["chart"])
            self.assertIn("target", out)
            self.assertIn("stop", out)
            self.assertIn("o median", out)

    def test_hyperliquid_pool_is_capped(self):
        with Session() as s:
            s.run("crypto-exit", "exit", ["plan", "SOL", "--side", "long", "--manual", "--venue", "hyperliquid"])
            code, out, err = s.run("crypto-timemachine", "timemachine", ["run", "--replays", "500"])
            self.assertEqual(code, 0, err)
            self.assertIn("about 17 days", out)
            self.assertIn("no setup to test", out)

    def test_refusals(self):
        with Session() as s:
            code, out, err = s.run("crypto-timemachine", "timemachine", ["run"])
            self.assertEqual(code, 1)
            self.assertIn("no plan to replay", err)
            code, out, err = s.run("crypto-timemachine", "timemachine", ["run", "--days", "500"])
            self.assertIn("--days must be", err)


if __name__ == "__main__":
    unittest.main()
