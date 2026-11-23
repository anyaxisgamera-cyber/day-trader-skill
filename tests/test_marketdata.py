"""trader-markets: market data with provenance, and the technical-analysis engine."""

import os
import unittest

from _load import FIX, load

MD = load("trader-markets", "marketdata")
I = load("trader-markets", "indicators")
M = MD.M
OFF = os.path.join(FIX, "trader-markets")
REQUIRED = ("provider", "instrument", "kind", "fetched_utc", "freshness", "limitations", "data")


def f():
    return MD.Fetcher(offline=OFF)


class Envelope(unittest.TestCase):
    def check_env(self, env):
        for k in REQUIRED:
            self.assertIn(k, env)
        self.assertNotIn("error", env, env.get("error"))
        for k in ("as_of_utc", "tz", "currency", "exchange", "observed_lag_s", "url"):
            self.assertIn(k, env)

    def test_nse_candles(self):
        env = MD.candles("TCS", "1d", 300, fetcher=f())
        self.check_env(env)
        self.assertEqual((env["provider"], env["instrument"], env["currency"], env["exchange"], env["tz"]),
                         ("yahoo", "TCS.NS", "INR", "NSE", "Asia/Kolkata"))
        self.assertEqual(env["freshness"], "delayed")
        self.assertTrue(any("unofficial" in x for x in env["limitations"]))

    def test_crypto_candles_each_venue(self):
        for sym, venue, inst in (("BTCUSDT", "binance", "BTCUSDT"), ("BTCUSDT", "okx", "BTC-USDT"),
                                 ("ETHUSDT", "kraken", "XETHZUSD"), ("ETHUSDT", "coinbase", "ETH-USD"),
                                 ("ETHUSDT", "kucoin", "ETH-USDT")):
            tf = "4h" if venue == "okx" else "1h"
            env = MD.candles(sym, tf, 50, venue=venue, fetcher=f())
            self.check_env(env)
            self.assertEqual((env["provider"], env["instrument"], env["freshness"]), (venue, inst, "real-time"))
            bars = env["data"]
            self.assertTrue(all(bars[i]["t"] < bars[i + 1]["t"] for i in range(len(bars) - 1)), venue)
            for b in bars:
                self.assertTrue(b["l"] <= min(b["o"], b["c"]) <= max(b["o"], b["c"]) <= b["h"], venue)

    def test_perpetual_falls_back_from_binance_futures(self):
        env = MD.candles("BTC-USDT-SWAP", "1h", 20, fetcher=f())
        self.assertEqual(env["provider"], "okx")
        self.assertTrue(any("451" in x for x in env["limitations"]))

    def test_missing_data_is_reported_not_filled(self):
        env = MD.candles("INFY", "1d", 10, fetcher=f())     # no fixture: like an API failure
        self.assertIn("error", env)
        self.assertIsNone(env["data"])
        self.assertEqual(env["freshness"], "unavailable")
        opt = MD.candles("NIFTY29DEC2626000CE", "1d", 10, fetcher=f())
        self.assertIn("broker feed", opt["error"])
        bad = MD.candles("ZZZZZZZZ", "1d", 10, fetcher=f())
        self.assertIn("unrecognised", bad["error"])

    def test_quote_previous_close_is_yesterdays(self):
        q = MD.quote("RELIANCE", fetcher=f())
        d = q["data"]
        self.assertEqual(d["prev_close"], 1377.5)
        self.assertAlmostEqual(d["change_pct"], (d["regular_price"] / 1377.5 - 1) * 100, places=2)
        self.assertEqual(q["market"]["exchange"], "NSE")

    def test_book(self):
        b = MD.depth("ETHUSDT", "kraken", 10, f())
        d = b["data"]
        self.assertLess(d["bids"][0][0], d["asks"][0][0])
        self.assertAlmostEqual(d["spread"], d["asks"][0][0] - d["bids"][0][0])
        self.assertGreater(d["spread_bps"], 0)
        self.assertTrue(-1 <= d["imbalance"] <= 1)
        self.assertIn("crypto only", MD.depth("TCS", "okx", 5, f())["error"])

    def test_fx(self):
        r = MD.fx_rate("INR", "USD", f())
        self.assertEqual(r["provider"], "yahoo")
        self.assertTrue(0.005 < r["rate"] < 0.02)
        e = MD.fx_rate("USD", "INR", f(), prefer="frankfurter")
        self.assertEqual((e["provider"], e["freshness"]), ("frankfurter", "end-of-day"))
        self.assertTrue(70 < e["rate"] < 120)
        s = MD.fx_rate("USDT", "USD", f())
        self.assertEqual(s["rate"], 1.0)
        self.assertTrue(any("stablecoin" in x for x in s["limitations"]))
        none = MD.fx_rate("JPY", "ZAR", f())
        self.assertIsNone(none["rate"])

    def test_451_is_reported_as_a_region_block(self):
        class Boom(object):
            def __call__(self, req, timeout=0):
                raise MD.HTTPError(req.full_url, 451, "Unavailable", {}, None)
        fe = MD.Fetcher(opener=Boom(), pause=0)
        with self.assertRaises(MD.DataError) as cm:
            fe.get("https://fapi.binance.com/x", "binance", "x")
        self.assertIn("region", str(cm.exception))


class Resample(unittest.TestCase):
    def test_three_minute_and_four_hour(self):
        bars = [{"t": 60 * i, "o": i, "h": i + 1, "l": i - 1, "c": i + 0.5, "v": 10} for i in range(9)]
        r = MD.resample(bars, 180)
        self.assertEqual([b["t"] for b in r], [0, 180, 360])
        self.assertEqual((r[0]["o"], r[0]["h"], r[0]["l"], r[0]["c"], r[0]["v"]), (0, 3, -1, 2.5, 30))
        # 4h buckets anchored to IST midnight (gmtoffset 19800): 09:15 IST lands in the 08:00 IST bucket
        t = 3 * 3600 + 45 * 60  # 03:45 UTC = 09:15 IST
        r = MD.resample([{"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], 14400, anchor=-19800)
        self.assertEqual((r[0]["t"] + 19800) % 86400, 8 * 3600)


class Indicators(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(OFF, "yahoo-TCS.NS-1d-2y.json")) as fh:
            _, cls.bars = MD.parse_yahoo_chart(fh.read())
        cls.cl = I.closes(cls.bars)

    def test_matches_the_ta_library(self):
        # reference values computed with the independent `ta` package (v0.11) on this fixture
        ref = {"rsi14": 56.4691290314385, "macd": 14.988621471356055, "macd_signal": 32.115293059265696,
               "bb_upper": 2554.626576246753, "bb_lower": 2351.773423753247, "atr14": 69.9682780366084,
               "adx14": 23.434324814877492, "plus_di": 27.998956164993533, "ema20": 2413.9710052920623,
               "sma50": 2304.008000488281}
        line, sig, _ = I.macd(self.cl)
        _, up, lo, _ = I.bollinger(self.cl)
        a, p, _ = I.adx(self.bars)
        got = {"rsi14": I.rsi(self.cl)[-1], "macd": line[-1], "macd_signal": sig[-1], "bb_upper": up[-1],
               "bb_lower": lo[-1], "atr14": I.atr(self.bars)[-1], "adx14": a[-1], "plus_di": p[-1],
               "ema20": I.ema(self.cl, 20)[-1], "sma50": I.sma(self.cl, 50)[-1]}
        for k, v in ref.items():
            self.assertAlmostEqual(got[k], v, places=6, msg=k)

    def test_not_enough_bars_is_none_not_invented(self):
        self.assertEqual(I.rsi([1, 2, 3], 14), [None, None, None])
        self.assertEqual(I.ema([1, 2], 5), [None, None])
        r = I.analyse(self.bars[:10], "1d")
        self.assertIn("too few", r["error"])

    def test_levels(self):
        p = I.pivots_classic(110, 90, 100)
        self.assertEqual((p["P"], p["R1"], p["S1"]), (100, 110, 90))
        fib = I.fibonacci(200, 100, "up")
        self.assertEqual((fib["0.0%"], fib["50.0%"], fib["61.8%"]), (200, 150, 138.2))
        vp = I.volume_profile(self.bars[-120:])
        self.assertTrue(vp["value_area_low"] <= vp["poc"] <= vp["value_area_high"])
        sup, res = I.support_resistance(self.bars)
        last = self.bars[-1]["c"]
        self.assertTrue(all(s["price"] < last for s in sup) and all(r["price"] > last for r in res))

    def test_vwap_resets_each_session(self):
        bars = [{"t": 0, "h": 11, "l": 9, "c": 10, "v": 100}, {"t": 60, "h": 13, "l": 11, "c": 12, "v": 100},
                {"t": 86400, "h": 21, "l": 19, "c": 20, "v": 5}]
        v = I.vwap(bars, lambda b: b["t"] // 86400)
        self.assertEqual(v, [10, 11, 20])

    def test_candle_patterns(self):
        def bar(o, h, l, c):
            return {"o": o, "h": h, "l": l, "c": c, "v": 1}
        self.assertIn("bullish engulfing", I.candle_patterns([bar(10, 10, 9, 9.5), bar(10, 10.5, 9, 9.2),
                                                               bar(9.1, 10.6, 9, 10.5)]))
        self.assertIn("doji", I.candle_patterns([bar(1, 2, 0, 1), bar(1, 2, 0, 1), bar(10, 11, 9, 10.01)]))
        self.assertIn("inside bar", I.candle_patterns([bar(1, 2, 0, 1), bar(5, 10, 0, 6), bar(5, 8, 2, 6)]))

    def test_structure_and_breakout(self):
        pivots = [0, 10, 5, 15, 10, 20, 15, 25, 20, 30]
        path = []
        for a, b in zip(pivots, pivots[1:]):
            path += [a + (b - a) * k / 4.0 for k in range(4)]
        up = [{"t": i, "o": p, "h": p + 0.1, "l": p - 0.1, "c": p, "v": 1} for i, p in enumerate(path)]
        self.assertTrue(I.market_structure(up)["structure"].startswith("uptrend"))
        b = I.breakout(up, 20)
        self.assertIn(b["type"], ("inside range", "breakout"))
        up.append({"t": 99, "o": 30, "h": 80, "l": 30, "c": 79, "v": 1})
        self.assertEqual(I.breakout(up, 20)["type"], "breakout")

    def test_report_and_confirmation(self):
        r = I.analyse(self.bars, "1d", periods_per_year=252)
        for k in ("rsi14", "macd", "atr14", "adx14", "trend", "support", "resistance", "volume_profile",
                  "structure", "breakout20", "hist_vol_annual"):
            self.assertIn(k, r)
        self.assertIsNone(r["vwap"])   # VWAP is an intraday measure
        self.assertEqual(I.confirm([{"tf": "1h", "trend": "up x"}, {"tf": "1d", "trend": "up y"}]),
                         "aligned up on every timeframe")
        self.assertTrue(I.confirm([{"tf": "1h", "trend": "up"}, {"tf": "1d", "trend": "down"}]).startswith("mixed"))


if __name__ == "__main__":
    unittest.main()
