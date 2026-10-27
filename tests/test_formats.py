"""The parsers against REAL exchange responses (recorded by the CCXT project), and the fake
exchange against the same shapes, so the offline tests exercise the code the real data hits."""

import json
import os
import unittest

from _load import cc, real, Session, FAKE

import importlib.util
_spec = importlib.util.spec_from_file_location("fx_for_formats", FAKE)
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)


class RealResponses(unittest.TestCase):

    def test_bybit_tradfi_contracts_are_left_out(self):
        rows = [{"symbol": "SOXLUSDT", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "SOXL",
                 "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "ETF", "marketRegion": "US"},
                {"symbol": "XAGUSDT", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "XAG",
                 "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "commodity", "marketRegion": ""},
                {"symbol": "NVDAUSDT", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "NVDA",
                 "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "stock"},
                {"symbol": "PAXGUSDT", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "PAXG",
                 "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "", "marketRegion": ""},
                {"symbol": "SOLUSDT", "contractType": "LinearPerpetual", "status": "Trading", "baseCoin": "SOL",
                 "quoteCoin": "USDT", "settleCoin": "USDT"}]
        self.assertEqual([m["symbol"] for m in cc.Bybit.parse_markets(rows)], ["PAXGUSDT", "SOLUSDT"])
    def test_binance_klines(self):
        bars = cc.Binance.parse_bars(real("binance_swap_klines.json"))
        self.assertEqual(len(bars), 3)
        t, o, h, l, c, v, qv = bars[0]
        self.assertEqual(t, 1712738460000)
        self.assertAlmostEqual(c, 71481.9)
        self.assertAlmostEqual(qv, 142.9638)          # index 7: quote asset volume
        self.assertTrue(all(isinstance(x, float) for x in bars[0][1:]))

    def test_binance_ticker_change_is_a_fraction(self):
        tk = cc.Binance.parse_tickers(real("binance_spot_ticker24h.json"), [], [])
        self.assertAlmostEqual(tk["BTCUSDT"]["change"], -0.00258)
        self.assertAlmostEqual(tk["BTCUSDT"]["quote_volume"], 1723237251.7018472)

    def test_bybit_kline_is_reversed_to_oldest_first(self):
        res = real("bybit_swap_kline.json")["result"]["list"]
        bars = cc.Bybit.parse_bars(res)
        self.assertEqual([b[0] for b in bars], sorted(b[0] for b in bars))
        self.assertEqual(bars[0][0], 1706630400000)
        self.assertAlmostEqual(bars[-1][4], 43352.8)

    def test_bybit_ticker(self):
        rows = real("bybit_swap_ticker.json")["result"]["list"]
        tk = cc.Bybit.parse_tickers(rows)
        t = tk["BTCUSDT"]
        raw = rows[0]
        self.assertAlmostEqual(t["funding"], float(raw["fundingRate"]))
        self.assertAlmostEqual(t["change"], float(raw["price24hPcnt"]))
        self.assertAlmostEqual(t["oi_usd"], float(raw["openInterestValue"]))
        self.assertEqual(t["next_funding_ms"], int(raw["nextFundingTime"]))

    def test_bybit_error_codes_arrive_as_strings_or_numbers(self):
        self.assertEqual(str(real("bybit_swap_kline.json")["retCode"]), "0")
        self.assertEqual(str(real("bybit_open_interest.json")["retCode"]), "0")

    def test_hyperliquid_candles(self):
        rows = sorted(real("hyperliquid_candles.json"), key=lambda r: int(r["t"]))
        bars = cc.Hyperliquid.parse_bars(rows[::-1])
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0][0], int(rows[0]["t"]))
        self.assertAlmostEqual(bars[0][4], float(rows[0]["c"]))
        self.assertGreater(bars[0][6], 0)             # quote volume estimated from coins x price

    def test_hyperliquid_builder_dex_markets_are_left_out(self):
        ms = cc.Hyperliquid.parse_markets(real("hyperliquid_meta_ctx_hip3.json"))
        self.assertEqual(ms, [])                       # xyz:XYZ100 and xyz:TSLA are HIP-3 markets

    def test_orderbook_shapes(self):
        b = real("binance_swap_orderbook.json")
        self.assertIn("bids", b)
        self.assertEqual(len(b["bids"][0]), 2)
        y = real("bybit_swap_orderbook.json")["result"]
        self.assertIn("b", y)
        self.assertIn("a", y)


class FakeMatchesReal(unittest.TestCase):
    """The fake exchange must answer in the real formats, or the offline tests prove nothing."""

    def setUp(self):
        self.s = Session().__enter__()

    def tearDown(self):
        self.s.__exit__(None, None, None)

    def test_binance_kline_rows_have_twelve_fields_like_the_real_ones(self):
        fake = fx.binance("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "5m", "limit": "3"})
        self.assertEqual(len(fake[0]), len(real("binance_swap_klines.json")[0]))
        self.assertIsInstance(fake[0][0], int)
        self.assertIsInstance(fake[0][1], str)

    def test_bybit_kline_and_ticker_keys(self):
        fake = fx.bybit("/v5/market/kline", {"symbol": "BTCUSDT", "interval": "5", "limit": "3"})
        realr = real("bybit_swap_kline.json")
        self.assertEqual(set(fake["result"]), set(realr["result"]))
        self.assertEqual(len(fake["result"]["list"][0]), len(realr["result"]["list"][0]))
        fake_t = fx.bybit("/v5/market/tickers", {"category": "linear"})["result"]["list"][0]
        real_t = real("bybit_swap_ticker.json")["result"]["list"][0]
        used = {"symbol", "lastPrice", "markPrice", "indexPrice", "price24hPcnt", "prevPrice24h", "highPrice24h",
                "lowPrice24h", "turnover24h", "fundingRate", "nextFundingTime", "openInterestValue", "bid1Price", "ask1Price"}
        self.assertTrue(used <= set(real_t), used - set(real_t))
        self.assertTrue(used <= set(fake_t), used - set(fake_t))

    def test_hyperliquid_candle_keys(self):
        fake = fx.hyperliquid({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "5m",
                                                                 "startTime": cc.now_ms() - 900000, "endTime": cc.now_ms()}})
        self.assertEqual(set(fake[0]), set(real("hyperliquid_candles.json")[0]))

    def test_hyperliquid_asset_context_keys(self):
        meta, ctx = fx.hyperliquid({"type": "metaAndAssetCtxs"})
        real_ctx = real("hyperliquid_meta_ctx_hip3.json")[1][0]
        self.assertEqual(set(ctx[0]), set(real_ctx))


class RealAccountShapes(unittest.TestCase):
    """The account parsers (broker, whales) on real recorded account responses."""

    def test_binance_positions(self):
        from _load import load
        ex = load("crypto-broker", "exchanges")
        acct = ex.BinanceAccount("k", "s")
        acct.call = lambda *a, **k: real("binance_swap_positions.json")
        p = acct.positions("BTCUSDT")[0]
        self.assertEqual((p["symbol"], p["side"]), ("BTCUSDT", "long"))
        self.assertAlmostEqual(p["qty"], 0.009)
        self.assertAlmostEqual(p["entry"], 67445.9)
        self.assertAlmostEqual(p["liq"], 64846.78939917)
        self.assertAlmostEqual(p["margin"], 7.60299749)                       # cross: isolatedMargin is "0"

    def test_bybit_positions_with_a_string_ret_code(self):
        from _load import load
        ex = load("crypto-broker", "exchanges")
        acct = ex.BybitAccount("k", "s")
        acct._offset = 0
        raw = real("bybit_swap_positions.json")
        orig = cc.HTTP.request
        try:
            cc.HTTP.request = lambda *a, **k: (200, raw)
            ps = acct.positions()
        finally:
            cc.HTTP.request = orig
        self.assertTrue(ps)
        self.assertEqual(ps[0]["symbol"], "TRIAUSDT")
        self.assertEqual(ps[0]["side"], "short")                                # side "Sell" is a short
        self.assertAlmostEqual(ps[0]["entry"], 0.01961459)

    def test_hyperliquid_wallet_with_string_numbers(self):
        from _load import load
        wh = load("crypto-whales", "whales")
        orig = wh.info
        try:
            wh.info = lambda body, weight=2: real("hyperliquid_clearinghouse.json")
            with Session() as s:
                st = wh.wallet_state("0x" + "1" * 40, None, fresh=True)
        finally:
            wh.info = orig
        p = st["positions"][0]
        self.assertEqual((p["coin"], p["side"], p["margin_mode"]), ("HYPE", "short", "isolated"))
        self.assertEqual(p["leverage"], 10.0)                                  # "10", a string, in the real answer
        self.assertAlmostEqual(p["size"], 4.0)
        self.assertAlmostEqual(st["account"], 76.206517)


if __name__ == "__main__":
    unittest.main()
