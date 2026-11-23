"""trader-crypto: market overview, listings, token risk, derivatives, scanner, position plans."""

import os
import unittest

from _load import FIX, load

C = load("trader-crypto", "crypto")
MD, M = C.MD, C.M
OFF = os.path.join(FIX, "trader-crypto")


def f():
    return MD.Fetcher(offline=OFF)


class Plan(unittest.TestCase):
    def test_liquidation_formula(self):
        self.assertAlmostEqual(C.liquidation_price(100.0, 10, "long", 0.005), 100 * (1 - 0.1 + 0.005))
        self.assertAlmostEqual(C.liquidation_price(100.0, 10, "short", 0.005), 100 * (1 + 0.1 - 0.005))
        self.assertIsNone(C.liquidation_price(100.0, 1))

    def test_size_from_risk_including_fees(self):
        p = C.plan(100.0, 95.0, 110.0, 1000.0, 1.0, leverage=1, fee_pct=0.1)
        # 1% of 1000 = 10 at risk; per unit 5 + 2 x 0.1% of 100 = 5.2
        self.assertAlmostEqual(p["qty"], 10 / 5.2)
        self.assertAlmostEqual(p["loss_at_stop_incl_fees"], 10.0)
        self.assertAlmostEqual(p["reward_risk"], 2.0)
        self.assertIsNone(p["liquidation_price"])
        self.assertEqual(p["warnings"], [])

    def test_leverage_and_liquidation_warnings(self):
        p = C.plan(1725.0, 1700.0, None, 1000.0, 1.0, leverage=50)
        text = " ".join(p["warnings"])
        self.assertIn("leverage 50x", text)
        self.assertIn("between the stop and liquidation", text)
        q = C.plan(100.0, 80.0, 130.0, 1000.0, 1.0, leverage=10)   # liq 90.5 sits above the 80 stop
        self.assertIn("LIQUIDATION", " ".join(q["warnings"]))

    def test_shorts_and_bad_inputs(self):
        p = C.plan(100.0, 105.0, 90.0, 1000.0, 1.0, leverage=2)
        self.assertEqual(p["side"], "short")
        self.assertGreater(p["liquidation_price"], 105.0)
        with self.assertRaises(C.CryptoError):
            C.plan(100.0, 105.0, 120.0, 1000.0, 1.0, side="short")   # target on the wrong side
        with self.assertRaises(C.CryptoError):
            C.plan(100.0, 105.0, 120.0, 1000.0, 1.0, side="long")    # stop above a long

    def test_funding_cost_sign(self):
        long_ = C.plan(100.0, 95.0, 110.0, 1000.0, 1.0, leverage=2, funding_8h=0.0001, hold_hours=24)
        short = C.plan(100.0, 105.0, 90.0, 1000.0, 1.0, leverage=2, funding_8h=0.0001, hold_hours=24)
        self.assertGreater(long_["funding_cost_estimate"], 0)   # longs pay positive funding
        self.assertLess(short["funding_cost_estimate"], 0)
        self.assertAlmostEqual(long_["funding_cost_estimate"], long_["notional"] * 0.0001 * 3)

    def test_margin_cap(self):
        p = C.plan(100.0, 99.9, 101.0, 100.0, 50.0, leverage=1)
        self.assertLessEqual(p["margin"], 100.0 + 1e-9)
        self.assertTrue(any("capped" in w for w in p["warnings"]))


class Live(unittest.TestCase):
    """Everything below reads recorded real responses (tests/fixtures/trader-crypto)."""

    def test_overview(self):
        o = C.overview(f())
        self.assertEqual(o["errors"], [])
        m = o["market"]
        self.assertTrue(40 < m["btc_dominance_pct"] < 80)
        self.assertIn("as_of_utc", m["source"])
        self.assertEqual(o["fear_greed"]["label"], "Extreme Fear")
        self.assertGreater(o["defi"]["defi_tvl_usd"], 1e9)
        self.assertGreater(o["ethereum_network"]["gas_gwei"], 0)
        self.assertIn("BTC", o["implied_vol"])

    def test_derivs(self):
        d = C.derivs(f(), "BTC")
        self.assertEqual(d["errors"], [])
        fu = d["funding"]
        self.assertAlmostEqual(fu["annualised_pct"], fu["current_rate"] * 3 * 365 * 100)
        self.assertGreater(d["open_interest"]["usd"], 1e8)
        self.assertIn("change_24h_pct", d["open_interest"])
        self.assertGreater(d["long_short"]["accounts_ratio"], 0)
        lq = d["liquidations"]
        self.assertEqual(lq["count"], sum(c["n"] for c in C._clusters(sorted(
            float(x["bkPx"]) for x in __import__("json").loads(f().get("", "okx", "liq-BTC"))["data"][0]["details"]))))
        op = d["options"]
        self.assertAlmostEqual(op["put_call_oi_ratio"], op["puts_oi"] / op["calls_oi"])

    def test_token_risk_flags(self):
        t = C.token(f(), M.resolve("SOL"))
        self.assertEqual(t["coingecko_id"], "solana")
        self.assertAlmostEqual(t["fdv_to_mcap"], t["fdv_usd"] / t["market_cap_usd"])
        self.assertTrue(any("no maximum supply" in x for x in t["risk_flags"]))
        self.assertIn("unlock", t["unlock_note"])

    def test_listing_check(self):
        r = C.check(f(), M.resolve("PEPEUSDT"))
        self.assertTrue(r["verified"])
        self.assertEqual(sorted(r["listed_on"]), ["binance", "coinbase", "kraken", "kucoin", "okx"])
        self.assertTrue(all(v.get("spread_bps") is not None for v in r["venues"]))

    def test_unknown_token_is_not_verified(self):
        class Nothing(MD.Fetcher):
            def get(self, url, provider, key, **kw):
                raise MD.DataError("HTTP 400 from %s" % provider)
        r = C.check(Nothing(offline=None, pause=0), M.resolve("NOTAREALCOINUSDT"))
        self.assertFalse(r["verified"])
        self.assertEqual(r["listed_on"], [])

    def test_scan(self):
        r = C.scan(f(), "1h", 0, ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"], True)
        self.assertEqual(r["scanned"], 6)
        self.assertEqual(r["failed"], [])
        for h in r["hits"]:
            self.assertTrue(h["tags"])
            self.assertTrue(set(t[0] for t in h["tags"]) <= set(k for k, _ in C.SCAN_RULES))
        self.assertIn("not a list of coins to buy", r["note"])


if __name__ == "__main__":
    unittest.main()
