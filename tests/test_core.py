"""cryptocore: clock, sessions, names, indicators, statistics, HTTP retries, venue fallback, record and replay."""

import math
import os
import shutil
import tempfile
import unittest

from _load import cc, Session, WED


class Clock(unittest.TestCase):
    def test_parse_when(self):
        self.assertEqual(cc.fmt_utc(cc.parse_when("2026-10-26 13:45")), "2026-10-26 13:45 UTC")
        self.assertEqual(cc.fmt_utc(cc.parse_when("2026-10-26 13:45+11:00")), "2026-10-26 02:45 UTC")
        self.assertEqual(cc.fmt_utc(cc.parse_when("2026-10-26T13:45:00Z")), "2026-10-26 13:45 UTC")
        self.assertEqual(cc.parse_when("1761486300"), 1761486300000)
        self.assertEqual(cc.parse_when(1761486300000), 1761486300000)
        with self.assertRaises(cc.CryptoError):
            cc.parse_when("next tuesday")

    def test_pinned_clock(self):
        cc.set_now("2026-10-21 15:15")
        try:
            self.assertTrue(cc.pinned())
            self.assertEqual(cc.fmt_utc(cc.now_ms()), "2026-10-21 15:15 UTC")
        finally:
            cc.set_now(None)
        self.assertFalse(cc.pinned())

    def test_daylight_saving_by_hand(self):
        # US: second Sunday of March to first Sunday of November (2026: Mar 8 to Nov 1)
        self.assertFalse(cc.us_dst(2026, 3, 7))
        self.assertTrue(cc.us_dst(2026, 3, 8))
        self.assertTrue(cc.us_dst(2026, 10, 31))
        self.assertFalse(cc.us_dst(2026, 11, 1))
        # UK: last Sunday of March to last Sunday of October (2026: Mar 29 to Oct 25)
        self.assertFalse(cc.uk_dst(2026, 3, 28))
        self.assertTrue(cc.uk_dst(2026, 3, 29))
        self.assertFalse(cc.uk_dst(2026, 10, 25))

    def test_session_opens(self):
        self.assertEqual(cc.fmt_utc(cc.session_open_on("ny", 2026, 10, 21)), "2026-10-21 13:30 UTC")
        self.assertEqual(cc.fmt_utc(cc.session_open_on("ny", 2026, 11, 2)), "2026-11-02 14:30 UTC")
        self.assertEqual(cc.fmt_utc(cc.session_open_on("london", 2026, 7, 1)), "2026-07-01 07:00 UTC")
        self.assertEqual(cc.fmt_utc(cc.session_open_on("london", 2026, 10, 26)), "2026-10-26 08:00 UTC")
        self.assertEqual(cc.fmt_utc(cc.session_open_on("tokyo", 2026, 10, 26)), "2026-10-26 00:00 UTC")
        at = cc.parse_when("2026-10-21 13:00")
        self.assertEqual(cc.fmt_utc(cc.last_session_open("ny", at)), "2026-10-20 13:30 UTC")
        with self.assertRaises(cc.CryptoError):
            cc.session_open_on("sydney", 2026, 10, 21)

    def test_durations_and_local_time(self):
        self.assertEqual(cc.fmt_dur(8040000), "2h14m")
        self.assertEqual(cc.fmt_dur(45000), "45s")
        self.assertEqual(cc.fmt_dur(2 * 86400000 + 3 * 3600000), "2d 3h")
        self.assertEqual(cc.fmt_local(cc.parse_when("2026-10-26 10:00"), "+11:00"), "2026-10-26 21:00 (UTC+11)")
        self.assertEqual(cc.fmt_local(cc.parse_when("2026-10-26 10:00"), "+05:30", date=False), "15:30 (UTC+5:30)")


class Names(unittest.TestCase):
    def test_time_zones_without_a_tz_database(self):
        """Python 3.8 and Windows without tzdata have no zone database: the built-in table must match it."""
        import importlib
        real = importlib.import_module
        try:
            def no_zoneinfo(name, *a, **k):
                if name == "zoneinfo":
                    raise ImportError("no zone database here")
                return real(name, *a, **k)
            cc.importlib.import_module = no_zoneinfo
            melb = lambda t: cc.fmt_local(cc.parse_when(t), "Australia/Melbourne")
            self.assertEqual(melb("2026-10-26 10:00"), "2026-10-26 21:00 (UTC+11)")       # daylight time
            self.assertEqual(melb("2026-06-01 10:00"), "2026-06-01 20:00 (UTC+10)")       # standard time
            self.assertEqual(melb("2026-04-04 15:59"), "2026-04-05 02:59 (UTC+11)")       # a minute before it ends
            self.assertEqual(melb("2026-04-04 16:00"), "2026-04-05 02:00 (UTC+10)")       # 03:00 becomes 02:00
            self.assertEqual(cc.fmt_local(cc.parse_when("2026-07-01 12:00"), "America/New_York"), "2026-07-01 08:00 (UTC-4)")
            self.assertEqual(cc.fmt_local(cc.parse_when("2026-12-01 12:00"), "Europe/London"), "2026-12-01 12:00 UTC")
            self.assertEqual(cc.fmt_local(cc.parse_when("2026-12-01 12:00"), "Asia/Kolkata"), "2026-12-01 17:30 (UTC+5:30)")
            self.assertFalse(cc.tz_known("Mars/Olympus"))
            self.assertTrue(cc.tz_known("Australia/Perth"))
        finally:
            cc.importlib.import_module = real

    def test_multipliers(self):
        self.assertEqual(cc.split_multiplier("1000PEPE"), ("PEPE", 1000))
        self.assertEqual(cc.split_multiplier("SHIB1000"), ("SHIB", 1000))
        self.assertEqual(cc.split_multiplier("kPEPE"), ("PEPE", 1000))
        self.assertEqual(cc.split_multiplier("1MBABYDOGE"), ("BABYDOGE", 1000000))
        self.assertEqual(cc.split_multiplier("1000000MOG"), ("MOG", 1000000))
        for plain in ("1INCH", "KAVA", "KNC", "BTC", "A8"):
            self.assertEqual(cc.split_multiplier(plain), (plain, 1))

    def test_what_users_type(self):
        for text in ("btc", "BTC", "BTCUSDT", "btc/usdt", "BTC-PERP", "BTCUSDT.P", "BTC/USDT:USDT"):
            self.assertEqual(cc.clean_coin(text), "BTC", text)
        self.assertEqual(cc.clean_coin("1000pepe"), "PEPE")
        self.assertEqual(cc.clean_coin("kPEPE"), "PEPE")
        self.assertEqual(cc.clean_coin("kava"), "KAVA")


class Indicators(unittest.TestCase):
    def test_sma_ema(self):
        xs = [float(i) for i in range(1, 11)]
        self.assertEqual(cc.sma(xs, 3)[2], 2.0)
        self.assertIsNone(cc.sma(xs, 3)[1])
        e = cc.ema(xs, 3)
        self.assertEqual(e[2], 2.0)                    # seeded with the first average
        self.assertAlmostEqual(e[3], 3.0)               # a straight line: the EMA lags by (n-1)/2
        self.assertAlmostEqual(e[-1], 9.0)

    def test_rsi_extremes_and_known_value(self):
        up = [100.0 + i for i in range(30)]
        self.assertEqual(cc.rsi(up)[-1], 100.0)
        down = [100.0 - i for i in range(30)]
        self.assertEqual(cc.rsi(down)[-1], 0.0)
        # the worked example StockCharts uses for Wilder's RSI(14): 70.53
        closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
                  45.8931, 46.0328, 45.6140, 46.2820, 46.2820]
        self.assertAlmostEqual(cc.rsi(closes, 14)[14], 70.53, places=2)

    def test_atr(self):
        bars = [(i, 10.0, 11.0, 9.0, 10.0, 1.0, 10.0) for i in range(20)]
        self.assertAlmostEqual(cc.atr(bars, 14)[-1], 2.0)

    def test_bollinger_and_vwap(self):
        mid, up, lo, wd = cc.bollinger([10.0] * 25, 20)
        self.assertEqual(wd[-1], 0.0)
        bars = [(0, 1, 2, 0, 1, 10.0, 10.0), (300000, 1, 4, 2, 3, 30.0, 90.0)]
        self.assertAlmostEqual(cc.vwap_from(bars, 0), (1.0 * 10 + 3.0 * 30) / 40.0)

    def test_resample(self):
        bars = [(i * 300000, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 1.0, 2.0) for i in range(7)]
        r = cc.resample(bars, "15m")
        self.assertEqual(len(r), 2)                     # the seventh bar starts a group it cannot finish
        self.assertEqual(r[0], (0, 1.0, 4.0, 0.5, 3.5, 3.0, 6.0))
        self.assertEqual(len(cc.resample(bars, "15m", complete=False)), 3)

    def test_pivots(self):
        hs = [1, 2, 3, 9, 3, 2, 1, 2, 3, 2, 1]
        bars = [(i, h, h, h - 0.5, h, 1, 1) for i, h in enumerate(hs)]
        found = [(i, k) for i, p, k in cc.pivots(bars, 3, 3)]
        self.assertIn((3, "H"), found)


class Stats(unittest.TestCase):
    def test_wilson(self):
        p, lo, hi = cc.wilson(13, 28)
        self.assertAlmostEqual(p, 0.4643, places=3)
        self.assertAlmostEqual(lo, 0.2953, places=3)
        self.assertAlmostEqual(hi, 0.6419, places=3)
        self.assertEqual(cc.wilson(0, 0), (None, 0.0, 1.0))

    def test_percentile_rank_beta(self):
        self.assertEqual(cc.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(cc.pct_rank(3, [1, 2, 3, 4]), 0.75)
        b = [0.01, -0.02, 0.015, -0.005, 0.02]
        a = [2 * x for x in b]
        self.assertAlmostEqual(cc.beta(a, b), 2.0)
        self.assertAlmostEqual(cc.corr(a, b), 1.0)


class Formatting(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(cc.fmt_price(67234.5, 0.1), "67,234.5")
        self.assertEqual(cc.fmt_price(3.5, 0.5), "3.5")
        self.assertEqual(cc.fmt_price(0.00001234), "0.00001234")
        self.assertEqual(cc.round_to_tick(1.23456, 0.001), 1.235)
        self.assertEqual(cc.round_to_tick(1.23456, 0.001, "down"), 1.234)
        self.assertEqual(cc.round_to_tick(1.2341, 0.001, "up"), 1.235)

    def test_money_and_funding(self):
        self.assertEqual(cc.fmt_usd(8.21e9), "$8.21B")
        self.assertEqual(cc.fmt_usd(45.6e6), "$45.6M")
        self.assertEqual(cc.fmt_usd(3594.6), "$3.6K")
        self.assertEqual(cc.fmt_funding(0.0001, 8), "+0.0100%/8h (+11.0% a year)")
        self.assertAlmostEqual(cc.annualize_funding(0.0000125, 1), cc.annualize_funding(0.0001, 8))
        self.assertEqual(cc.fmt_small_pct(0.0000003), "<0.001%")

    def test_table(self):
        t = cc.table([["BTC", "1"], ["ETH", "22"]], ["coin", "n"])
        self.assertEqual(t.splitlines()[0], "coin   n")
        self.assertEqual(t.splitlines()[2], "ETH   22")


class Network(unittest.TestCase):
    def test_auto_falls_back_when_an_exchange_blocks_this_location(self):
        with Session(mode="binance_geoblock"):
            v = cc.get_venue("auto")
            self.assertEqual(v.name, "bybit")
            self.assertIn("blocked", v.auto_notes[0])
        with Session(mode="binance_geoblock,bybit_geoblock"):
            self.assertEqual(cc.get_venue("auto").name, "hyperliquid")

    def test_auto_remembers_the_answer(self):
        with Session() as s:
            self.assertEqual(cc.get_venue("auto").name, "binance")
            self.assertEqual(cc.read_json(s.path("cache", "venue.json"))["venue"], "binance")

    def test_retries_recover(self):
        for mode in ("flaky", "ratelimit", "timeout"):
            with Session(mode=mode):
                v = cc.get_venue("bybit")
                self.assertGreater(len(v.markets()), 10, mode)
                self.assertGreaterEqual(cc.HTTP.stats["retries"], 1, mode)

    def test_a_broken_coin_does_not_sink_the_rest(self):
        with Session(mode="garbage"):
            v = cc.get_venue("binance")
            res = cc.parallel(lambda s: len(v.bars(s, "15m", 50)), ["BTCUSDT", "ADAUSDT", "ETHUSDT"])
            self.assertEqual([r for _, r, _ in res], [50, None, 50])
            self.assertIn("malformed", str(res[1][2]))

    def test_unknown_coin_suggests_close_names(self):
        with Session():
            v = cc.get_venue("binance")
            with self.assertRaises(cc.CryptoError) as e:
                v.market("doge2")
            self.assertIn("DOGE", str(e.exception))
            self.assertEqual(v.market("bonk")["symbol"], "1000BONKUSDT")
            self.assertEqual(cc.get_venue("hyperliquid").market("BONK")["symbol"], "kBONK")

    def test_closed_bars_only(self):
        with Session():
            v = cc.get_venue("binance")
            bars = v.bars("BTCUSDT", "15m", 10)
            last_open = (cc.now_ms() // 900000) * 900000 - 900000
            self.assertEqual(bars[-1][0], last_open)
            self.assertEqual(len(bars), 10)

    def test_record_then_replay_offline_gives_the_same_answer(self):
        rec = tempfile.mkdtemp(prefix="crypto-rec-")
        try:
            with Session() as s:
                os.environ["CRYPTO_RECORD"] = rec
                cc.reset_http()
                v = cc.get_venue("bybit")
                first = (len(v.markets()), v.bars("SOLUSDT", "1h", 48)[-1], v.funding_history("SOLUSDT", cc.now_ms() - 86400000 * 3, cc.now_ms()))
            with Session(fake=False) as s:
                os.environ["CRYPTO_OFFLINE"] = rec
                cc.reset_http()
                v = cc.get_venue("bybit")
                again = (len(v.markets()), v.bars("SOLUSDT", "1h", 48)[-1], v.funding_history("SOLUSDT", cc.now_ms() - 86400000 * 3, cc.now_ms()))
                self.assertEqual(first, again)
                with self.assertRaises(cc.OfflineMiss):
                    v.bars("ETHUSDT", "1h", 10)
        finally:
            shutil.rmtree(rec, ignore_errors=True)

    def test_universe_filters(self):
        with Session():
            v = cc.get_venue("binance")
            kept, why = cc.build_universe(v)
            coins = [m["coin"] for m, t in kept]
            self.assertNotIn("USDC", coins)
            self.assertNotIn("NEWX", coins)
            self.assertNotIn("THINX", coins)
            self.assertEqual(why, {"stablecoin": 1, "listed recently": 1, "low volume": 1})
            vols = [t["quote_volume"] for m, t in kept]
            self.assertEqual(vols, sorted(vols, reverse=True))


if __name__ == "__main__":
    unittest.main()
