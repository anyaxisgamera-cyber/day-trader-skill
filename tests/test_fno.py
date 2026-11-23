"""trader-derivatives: option models, implied volatility, chains, basis, payoffs."""

import datetime
import os
import tempfile
import unittest

from _load import FIX, load

F = load("trader-derivatives", "fno")
MD, M = F.MD, F.M


class Models(unittest.TestCase):
    S, K, T, R, V, Q = 25370.0, 25500.0, 6 / 365.0, 0.065, 0.13, 0.012

    def test_matches_py_vollib(self):
        # reference values from py_vollib (Black-Scholes-Merton, analytical greeks; theta per day, vega/rho per 1%)
        ref = {"CE": (120.57137981142395, 0.40274501481307823, 0.0009151365604912372, -15.098301786690598,
                      12.587183943001891, 1.6597922705747457),
               "PE": (228.34335526473586, -0.5970577443674779, 0.0009151365604912372, -11.395973111550235,
                      12.587183943001891, -2.527512054224819)}
        for right, (price, delta, gamma, theta, vega, rho) in ref.items():
            g = F.bs(self.S, self.K, self.T, self.V, self.R, self.Q, right)
            self.assertAlmostEqual(g["price"], price, places=8)
            self.assertAlmostEqual(g["delta"], delta, places=10)
            self.assertAlmostEqual(g["gamma"], gamma, places=12)
            self.assertAlmostEqual(g["theta_day"], theta, places=8)
            self.assertAlmostEqual(g["vega_1pct"], vega, places=8)
            self.assertAlmostEqual(g["rho_1pct"], rho, places=8)

    def test_black76_matches_py_vollib(self):
        self.assertAlmostEqual(F.bs(5600, 5500, 30 / 365.0, 0.3, 0.065, right="CE", model="black76")["price"],
                               243.22959133241207, places=8)
        self.assertAlmostEqual(F.bs(5600, 5500, 30 / 365.0, 0.3, 0.065, right="PE", model="black76")["price"],
                               143.7624133487531, places=8)

    def test_put_call_parity(self):
        c = F.bs(self.S, self.K, self.T, self.V, self.R, self.Q, "CE")["price"]
        p = F.bs(self.S, self.K, self.T, self.V, self.R, self.Q, "PE")["price"]
        import math
        self.assertAlmostEqual(c - p, self.S * math.exp(-self.Q * self.T) - self.K * math.exp(-self.R * self.T), places=8)

    def test_implied_vol_round_trip(self):
        for right in ("CE", "PE"):
            px = F.bs(self.S, self.K, self.T, 0.21, self.R, 0, right)["price"]
            self.assertAlmostEqual(F.implied_vol(px, self.S, self.K, self.T, self.R, 0, right), 0.21, places=5)
        self.assertIsNone(F.implied_vol(0.01, 100, 50, 0.1, right="CE"))  # below intrinsic: no volatility fits

    def test_expired_option_is_intrinsic(self):
        g = F.bs(110, 100, 0, 0.2, right="CE")
        self.assertEqual((g["price"], g["delta"]), (10, 1.0))

    def test_time_to_nse_expiry(self):
        now = datetime.datetime(2026, 12, 29, 4, 0)   # 09:30 IST on expiry day
        t = F.years_to(datetime.date(2026, 12, 29), now, "NSE")
        self.assertAlmostEqual(t * 365 * 24, 6.0, places=6)   # to the 15:30 IST close


class Chains(unittest.TestCase):
    ROWS = [{"strike": 100, "right": "CE", "oi": 10, "volume": 5, "iv": 20},
            {"strike": 110, "right": "CE", "oi": 30, "volume": 1, "iv": 18},
            {"strike": 90, "right": "PE", "oi": 40, "volume": 2, "iv": 25},
            {"strike": 100, "right": "PE", "oi": 20, "volume": 3, "iv": 21}]

    def test_max_pain_by_hand(self):
        # pain at 90: calls 0; puts (100-90)*20=200 -> 200. at 100: calls 0, puts 0 -> 0. at 110: calls 100 -> 100
        self.assertEqual(F.max_pain(self.ROWS), 100)

    def test_stats(self):
        st = F.chain_stats(self.ROWS, spot=101)
        self.assertEqual((st["call_oi"], st["put_oi"]), (40, 60))
        self.assertAlmostEqual(st["pcr_oi"], 1.5)
        self.assertAlmostEqual(st["pcr_volume"], 5 / 6.0)
        self.assertEqual(st["atm_strike"], 100)
        self.assertEqual(st["top_put_oi"][0]["strike"], 90)

    def test_csv_long_and_wide(self):
        d = tempfile.mkdtemp()
        p1 = os.path.join(d, "long.csv")
        with open(p1, "w") as fh:
            fh.write("Strike,Type,OI,OI_Change,Volume,IV,LTP\n25500,CE,\"1,200\",50,300,12.5,82\n25500,PE,900,-10,200,13.1,120\n")
        rows = F.read_chain_csv(p1)
        self.assertEqual(rows[0], {"strike": 25500.0, "right": "CE", "oi": 1200.0, "oi_change": 50.0, "volume": 300.0,
                                   "iv": 12.5, "ltp": 82.0})
        p2 = os.path.join(d, "wide.csv")
        with open(p2, "w") as fh:
            fh.write("CE_OI,CE_IV,CE_LTP,Strike,PE_LTP,PE_IV,PE_OI\n100,12,80,25500,110,13,90\n")
        rows = F.read_chain_csv(p2)
        self.assertEqual([r["right"] for r in rows], ["CE", "PE"])
        self.assertEqual(rows[1]["oi"], 90.0)
        p3 = os.path.join(d, "bad.csv")
        with open(p3, "w") as fh:
            fh.write("a,b\n1,2\n")
        with self.assertRaises(F.FnoError):
            F.read_chain_csv(p3)

    def test_deribit_chain(self):
        f = MD.Fetcher(offline=os.path.join(FIX, "trader-derivatives"))
        rows, spot, exp, codes, url = F.deribit_chain(f, "BTC")
        self.assertGreater(len(rows), 10)
        self.assertTrue(all(r["right"] in ("CE", "PE") for r in rows))
        self.assertEqual(exp, min(M._date_from(c[:-5], c[-5:-2], c[-2:]) for c in codes))
        st = F.chain_stats(rows, spot)
        self.assertIsNotNone(st["max_pain"])
        with self.assertRaises(F.FnoError):
            F.deribit_chain(f, "BTC", "1JAN20")


class FuturesAndPayoffs(unittest.TestCase):
    def test_basis(self):
        b = F.basis(25370, 25480, 36, rate=0.065)
        self.assertAlmostEqual(b["basis"], 110)
        self.assertAlmostEqual(b["annualised_pct"], (25480 / 25370.0 - 1) * 365 / 36 * 100)
        self.assertIn("contango", b["state"])
        self.assertIn("backwardation", F.basis(100, 99, 10)["state"])

    def test_buildup(self):
        self.assertTrue(F.buildup(1, 1).startswith("long build-up"))
        self.assertTrue(F.buildup(-1, 1).startswith("short build-up"))
        self.assertTrue(F.buildup(1, -1).startswith("short covering"))
        self.assertTrue(F.buildup(-1, -1).startswith("long unwinding"))

    def test_bull_call_spread(self):
        legs = [F.parse_leg("buy CE 25500 82"), F.parse_leg("sell CE 25700 31")]
        t = F.payoff_table(legs, 25370, lot=65)
        self.assertAlmostEqual(t["net_premium"], -51 * 65)
        self.assertAlmostEqual(t["max_loss"], -51 * 65)
        self.assertAlmostEqual(t["max_profit"], (200 - 51) * 65)
        self.assertEqual(t["breakevens"], [25551.0])

    def test_naked_short_call_is_unlimited(self):
        t = F.payoff_table([F.parse_leg("sell CE 100 5")], 100)
        self.assertEqual(t["max_loss"], "unlimited")
        self.assertEqual(F.payoff_table([F.parse_leg("buy CE 100 5")], 100)["max_profit"], "unlimited")
        with self.assertRaises(F.FnoError):
            F.parse_leg("hold XX 1")


if __name__ == "__main__":
    unittest.main()
