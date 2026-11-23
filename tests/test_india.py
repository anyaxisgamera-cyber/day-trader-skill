"""One system, India first: the original day-trading pipeline on a real NSE session.

TCS on NSE, Monday 23 November 2026, real Yahoo bars (tests/fixtures/india),
Angel One's real scrip master rows (tests/fixtures/trader-markets/data):
scanner symbols -> entry -> exit -> time machine -> budget -> risk -> broker (Angel One
manual ticket) -> alerts. Plus the security rules that keep a person in control."""

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

os.environ["TRADER_MARKETS_DATA"] = os.path.join(FIX, "trader-markets", "data")
INDIA = os.path.join(FIX, "india")

entry = load("trader-entry", "entry")
exit_ = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
size = load("trader-budget", "size")
risk = load("trader-risk", "check")
broker = load("trader-broker", "ticket")
alerts = load("trader-alerts", "alerts")
scan = load("trader-scanner", "scan")

CONFIG = {"budget": 100000, "account_currency": "INR", "cash_buffer_pct": 10, "max_position_pct": 75,
          "max_order_usd": 80000, "min_order_usd": 500, "max_loss_per_trade_usd": 1000,
          "max_daily_loss_usd": 3000, "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0,
          "require_stop": True, "broker": "angelone", "paper": True, "blocked": []}


def jload(path):
    with open(path) as fh:
        return json.load(fh)


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cwd = os.getcwd()
        cls.d = tempfile.mkdtemp()
        os.chdir(cls.d)
        os.makedirs("trader")
        with open("trader/config.json", "w") as fh:
            json.dump(CONFIG, fh)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.d, ignore_errors=True)

    def step1_entry(self):
        code, out, err = run(entry, ["check", "TCS", "--setup", "level", "--level", "2440", "--offline",
                                     os.path.join(INDIA, "TCS.NS.json"), "--now", "2026-11-23 10:00",
                                     "--write", "trader/entry.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("IST", out)                       # the instrument's own clock
        self.assertIn("₹", out)                     # rupees, not dollars
        self.assertIn("Opening range 9:15-9:30", out)   # NSE opens at 9:15
        self.assertIn("TRIGGERED 9:45 AM at ₹2443.50", out)
        doc = jload(("trader/entry.json"))
        self.assertEqual((doc["status"], doc["price"]), ("triggered", 2443.5))

    def step2_exit(self):
        code, out, err = run(exit_, ["plan", "--stop", "pct:0.7", "--target", "2R", "--write",
                                     "--now", "2026-11-23 10:00"])
        self.assertEqual(code, 0, err)
        plan = jload(("trader/plan.json"))
        self.assertEqual((plan["market"], plan["currency"], plan["tick_size"]), ("NSE", "INR", 0.1))
        self.assertEqual((plan["stop"], plan["target"], plan["flat_by"]), (2426.3, 2477.7, "15:10"))
        self.assertAlmostEqual(plan["stop"] * 10, round(plan["stop"] * 10))   # on the 0.10 tick
        self.assertIn("15:10 IST", out)
        self.assertIn("squared off by the broker", out)

    def step3_timemachine(self):
        code, out, err = run(tm, ["run", "--offline", INDIA, "--now", "2026-11-23 15:40", "--write"])
        self.assertEqual(code, 0, err)
        doc = jload(("trader/timemachine.json"))
        mc = doc["mc"]
        self.assertAlmostEqual(mc["p_target"] + mc["p_stop"] + mc["p_neither"], 1.0, places=3)
        self.assertEqual(mc["bars_left"], 69)            # 9:45 to 15:30 in 5-minute bars
        self.assertIn("3:30 PM", out)                    # NSE's close, not 4:00 PM
        self.assertIn("filled as a flat bar", out)      # Yahoo's NSE gaps are disclosed

    def step4_budget(self):
        code, out, err = run(size, ["--write", "--now", "2026-11-23 10:00"])
        self.assertEqual(code, 0, err)
        o = jload(("trader/orders.json"))["orders"][0]
        self.assertEqual((o["qty"], o["market"], o["currency"]), ("30", "NSE", "INR"))   # 75% cap of 1 lakh
        self.assertIn("₹", out)

    def step5_risk(self):
        code, out, err = run(risk, ["--now", "2026-11-23 10:00"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("PASS", out)
        code, out, _ = run(risk, ["--now", "2026-11-23 15:05", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("no new entries after 15:00", out)
        code, out, _ = run(risk, ["--now", "2026-11-24 10:00", "--no-write"])
        self.assertIn("closed today", out)               # Guru Nanak Jayanti

    def step6_broker(self):
        code, out, err = run(broker, ["show", "--now", "2026-11-23 10:01"])
        self.assertEqual(code, 0, err)
        self.assertIn("Angel One (manual ticket)", out)
        self.assertIn("Nothing has been sent", out)
        self.assertIn('"symboltoken": "11536"', out)
        self.assertIn('"tradingsymbol": "TCS-EQ"', out)
        self.assertIn("STOPLOSS_MARKET", out)
        code, out, err = run(broker, ["approve", "--index", "1", "--reply", "yes", "--now", "2026-11-23 10:01"])
        self.assertEqual(code, 0, err)
        self.assertIn("PAPER: do not place it", out)
        st = jload(("trader/state/broker.json"))
        self.assertEqual(st["orders"][-1]["status"], "approved (manual: not sent)")
        self.assertNotIn("placed", st["orders"][-1]["status"])

    def step7_alerts(self):
        code, out, err = run(alerts, ["add", "--from-plan"])
        self.assertEqual(code, 0, err)
        rules = jload(("trader/alerts.json"))["rules"]
        self.assertEqual([r["type"] for r in rules], ["stop", "target", "time"])
        self.assertEqual(rules[-1]["at"], "15:10")

    def test_pipeline(self):
        for step in ("step1_entry", "step2_exit", "step3_timemachine", "step4_budget", "step5_risk",
                     "step6_broker", "step7_alerts"):
            with self.subTest(step):
                getattr(self, step)()


class Currency(unittest.TestCase):
    PLAN = {"symbol": "TCS", "side": "buy", "entry": 2443.5, "stop": 2426.3, "target": 2477.7, "setup": "level",
            "market": "NSE", "currency": "INR"}

    def test_usd_account_sizes_an_inr_trade_with_the_rate(self):
        s = size.size_trade(self.PLAN, {"budget": 5000, "max_loss_per_trade_usd": 50, "max_order_usd": 4000,
                                        "max_position_pct": 75, "cash_buffer_pct": 10, "account_currency": "USD"},
                            fx=0.01076, lot=1)
        # $50 / (17.20 x 0.01076 = $0.185 a share) = 270 shares; $4000 / $26.29 = 152; 75% of $5000 / $26.29 = 142
        self.assertEqual((s["qty"], s["bound_by"]), (142, "position cap"))
        self.assertAlmostEqual(s["cost"], round(142 * 2443.5 * 0.01076, 2))
        self.assertAlmostEqual(s["max_loss"], round(142 * 17.20 * 0.01076, 2))

    def test_lots(self):
        plan = dict(self.PLAN, symbol="NIFTY29DEC26FUT", entry=25480.0, stop=25400.0, target=25640.0)
        s = size.size_trade(plan, {"budget": 5000000, "max_loss_per_trade_usd": 12000, "max_order_usd": 5000000,
                                   "max_position_pct": 100, "cash_buffer_pct": 0}, lot=65)
        self.assertEqual(s["qty"] % 65, 0)
        self.assertEqual(s["qty"], 130)      # 12000 / 80 = 150 units -> 2 lots of 65
        tiny = size.size_trade(plan, {"budget": 2000000, "max_loss_per_trade_usd": 1000, "max_order_usd": 2000000,
                                      "max_position_pct": 100, "cash_buffer_pct": 0}, lot=65)
        self.assertEqual(tiny["qty"], 0)
        self.assertIn("one lot is 65 units", tiny["refusal"])


class Scanner(unittest.TestCase):
    def test_nse_universe_keeps_the_eq_series_only(self):
        text = "SYMBOL,NAME OF COMPANY, SERIES,DATE OF LISTING\nTCS,Tata Consultancy,EQ,x\nABC,Abc Ltd,BE,x\n"
        kept, counts = scan.nse_universe(text)
        self.assertEqual(kept, [("TCS.NS", "Tata Consultancy")])
        self.assertEqual(counts["not EQ series"], 1)

    def test_one_market_per_scan(self):
        class A(object):
            symbols, min_price, min_volume = "TCS.NS,AAPL", 5.0, 1000000
        with self.assertRaises(scan.ScanError):
            scan._scan_market(A(), ["TCS.NS", "AAPL"])
        a = A()
        a.symbols = "TCS.NS"
        self.assertEqual(scan._scan_market(a, ["TCS.NS", "RELIANCE.NS"]), "NSE")
        self.assertEqual((a.min_price, a.min_volume), (50.0, 100000))   # rupee liquidity floor
        scan.use_market("AAPL")   # leave the module on New York for anything after

    def test_bare_us_universe_stays_us(self):
        class A(object):
            symbols, min_price, min_volume = None, 5.0, 1000000
        self.assertEqual(scan._scan_market(A(), ["CCL", "STX", "AAPL"]), "NYSE")


class Security(unittest.TestCase):
    SECRET_WORDS = re.compile(r"(password|passcode|\bpin\b|totp|otp_secret|api_secret|secret_key)\s*[=:]", re.I)

    def test_no_tool_stores_or_asks_for_broker_credentials(self):
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "tests", "__pycache__")]
            for fn in files:
                if fn.endswith(".py"):
                    with open(os.path.join(base, fn), encoding="utf-8") as fh:
                        src = fh.read()
                    self.assertIsNone(self.SECRET_WORDS.search(src), fn)
                    self.assertNotIn("loginByPassword", src, fn)
                    self.assertNotIn("generateSession", src, fn)

    def test_no_tool_places_orders(self):
        # the only network calls are GETs to public data (and one read-only JSON-RPC gas query)
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "tests", "__pycache__")]
            for fn in files:
                if fn.endswith(".py"):
                    with open(os.path.join(base, fn), encoding="utf-8") as fh:
                        src = fh.read()
                    for bad in ("placeOrder(", "place_order(", "/orders\"", "method=\"POST\"", "method='POST'"):
                        self.assertNotIn(bad, src, "%s: %s" % (fn, bad))

    def test_manual_ticket_never_claims_a_placement(self):
        o = {"symbol": "TCS", "side": "buy", "qty": "30", "type": "market", "time_in_force": "day",
             "stop_loss_stop_price": "2426.30", "take_profit_limit_price": "2477.70", "market": "NSE"}
        text, _ = broker.render_manual(o, 1, 1, {"paper": False}, "READY", {}, True, "angelone")
        self.assertIn("PROPOSED", text)
        self.assertNotRegex(text.lower(), r"\b(was|has been) placed\b|\bconfirmed\b|\bexecuted\b")
        legs, _ = broker.angel_legs(dict(o, symbol="NIFTY29DEC26FUT", market="NFO", lot_size=65))
        self.assertEqual(legs[1]["ordertype"], "STOPLOSS_LIMIT")   # NSE F&O takes no SL-M


if __name__ == "__main__":
    unittest.main()
