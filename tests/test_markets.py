"""trader-markets: time zones, calendars, sessions, symbols, expiries, binding."""

import datetime
import json
import os
import unittest

from _load import FIX, ROOT, load

M = load("trader-markets", "markets")
DATA = os.path.join(FIX, "trader-markets", "data")
D = datetime.date
DT = datetime.datetime


class TimeZones(unittest.TestCase):
    def test_every_rule_matches_the_tz_database(self):
        try:
            from zoneinfo import ZoneInfo
        except ImportError:  # Python 3.8: the hand-written rules are all there is
            self.skipTest("zoneinfo not available")
        start = DT(2026, 1, 1)
        for zone in M.ZONES:
            if zone == "UTC":
                continue
            tz = ZoneInfo(zone)
            t = start
            while t < DT(2028, 1, 1):
                ref = datetime.datetime(t.year, t.month, t.day, t.hour, tzinfo=datetime.timezone.utc).astimezone(tz)
                self.assertEqual(M.utc_offset(zone, t), ref.utcoffset().total_seconds() / 3600, (zone, t))
                t += datetime.timedelta(hours=7)

    def test_dst_edges(self):
        ny = "America/New_York"
        self.assertEqual(M.utc_offset(ny, DT(2026, 3, 8, 6, 59)), -5)   # 01:59 EST
        self.assertEqual(M.utc_offset(ny, DT(2026, 3, 8, 7, 0)), -4)    # 03:00 EDT
        self.assertEqual(M.utc_offset("Europe/London", DT(2026, 3, 29, 0, 59)), 0)
        self.assertEqual(M.utc_offset("Europe/London", DT(2026, 3, 29, 1, 0)), 1)
        self.assertEqual(M.utc_offset("Australia/Sydney", DT(2026, 4, 4, 15, 59)), 11)
        self.assertEqual(M.utc_offset("Australia/Sydney", DT(2026, 4, 4, 16, 0)), 10)
        self.assertEqual(M.utc_offset("Asia/Kolkata", DT(2026, 7, 1)), 5.5)

    def test_local_round_trip(self):
        for zone in M.ZONES:
            local = DT(2026, 11, 23, 10, 15)
            self.assertEqual(M.to_local(M.local_to_utc(local, zone), zone), local, zone)


class Calendars(unittest.TestCase):
    def test_nyse_matches_the_original_skills_list(self):
        alerts = load("trader-alerts", "alerts")
        cal = M.calendars()["calendars"]["XNYS"]
        self.assertEqual(sorted(str(d) for d in alerts.NYSE_HOLIDAYS), cal["holidays"])
        self.assertEqual(sorted(str(d) for d in alerts.NYSE_EARLY_CLOSE), sorted(cal["early_closes"]))

    def test_nse_holiday_and_the_expiry_evidence(self):
        self.assertFalse(M.calendar_day("NSE", D(2026, 11, 24))["open"])   # Guru Nanak Jayanti
        self.assertTrue(M.calendar_day("NSE", D(2026, 11, 23))["open"])
        self.assertFalse(M.calendar_day("NSE", D(2027, 1, 26))["open"])    # Republic Day (extra, with evidence)

    def test_unrecorded_days_are_unverified_not_guessed(self):
        day = M.calendar_day("NSE", D(2027, 3, 3))
        self.assertTrue(day["open"])
        self.assertFalse(day["verified"])
        self.assertIn("recorded only through", day["reason"])

    def test_early_close_shortens_the_session(self):
        ss = M.sessions_for("NYSE", D(2026, 11, 27))
        reg = [s for s in ss if s[0] == "regular"]
        self.assertEqual(M.to_local(reg[-1][2], "America/New_York").time(), datetime.time(13))
        hk = M.sessions_for("HKEX", D(2026, 12, 24))
        self.assertEqual([p for p, _, _ in hk], ["pre-open", "regular"])

    def test_weekend(self):
        for code in ("NSE", "NYSE", "LSE", "TSE"):
            self.assertFalse(M.calendar_day(code, D(2026, 11, 21))["open"], code)
        self.assertTrue(M.calendar_day("CRYPTO", D(2026, 11, 21))["open"])


class Sessions(unittest.TestCase):
    def st(self, code, when, zone="UTC"):
        return M.market_status(code, M.local_to_utc(DT.strptime(when, "%Y-%m-%d %H:%M"), zone))

    def test_nse_phases(self):
        ist = "Asia/Kolkata"
        self.assertEqual(self.st("NSE", "2026-11-23 09:05", ist)["phase"], "pre-open")
        self.assertEqual(self.st("NSE", "2026-11-23 09:10", ist)["phase"], "closed")
        self.assertEqual(self.st("NSE", "2026-11-23 09:15", ist)["phase"], "regular")
        self.assertEqual(self.st("NSE", "2026-11-23 15:45", ist)["phase"], "closing")
        st = self.st("NSE", "2026-11-24 10:00", ist)
        self.assertFalse(st["is_open"])
        self.assertEqual(st["next_open_local"], "2026-11-25 09:15")

    def test_lunch_breaks(self):
        self.assertEqual(self.st("TSE", "2026-11-25 12:00", "Asia/Tokyo")["phase"], "lunch")
        self.assertEqual(self.st("HKEX", "2026-11-25 12:30", "Asia/Hong_Kong")["phase"], "lunch")
        self.assertEqual(self.st("SSE", "2026-11-25 11:45", "Asia/Shanghai")["phase"], "lunch")

    def test_mcx_closes_later_in_us_winter(self):
        winter = [s for s in M.sessions_for("MCX", D(2026, 11, 25)) if s[0] == "regular"][0]
        summer = [s for s in M.sessions_for("MCX", D(2026, 7, 15)) if s[0] == "regular"][0]
        self.assertEqual(M.to_local(winter[2], "Asia/Kolkata").time(), datetime.time(23, 55))
        self.assertEqual(M.to_local(summer[2], "Asia/Kolkata").time(), datetime.time(23, 30))

    def test_round_the_clock_markets(self):
        self.assertTrue(self.st("CRYPTO", "2026-11-21 03:00")["is_open"])
        self.assertFalse(self.st("FX", "2026-11-21 12:00")["is_open"])          # Saturday
        self.assertTrue(self.st("FX", "2026-11-22 23:00")["is_open"])           # Sunday 18:00 New York
        self.assertEqual(self.st("CME", "2026-11-23 16:30", "America/Chicago")["phase"], "break")

    def test_never_applies_one_calendar_to_another(self):
        # Nov 23 2026: Tokyo closed (Labour Thanksgiving), NSE and NYSE open
        self.assertFalse(M.calendar_day("TSE", D(2026, 11, 23))["open"])
        self.assertTrue(M.calendar_day("NSE", D(2026, 11, 23))["open"])
        self.assertTrue(M.calendar_day("NYSE", D(2026, 11, 23))["open"])
        # Nov 26 2026: US Thanksgiving, India open
        self.assertFalse(M.calendar_day("NYSE", D(2026, 11, 26))["open"])
        self.assertTrue(M.calendar_day("NSE", D(2026, 11, 26))["open"])


class Resolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lk = M.Lookups(DATA)

    def r(self, s, lk=True):
        return M.resolve(s, self.lk if lk else M.Lookups("/nonexistent-folder"))

    def test_india_first(self):
        r = self.r("RELIANCE")
        self.assertEqual((r["exchange"], r["currency"], r["yahoo"], r["verified"]), ("NSE", "INR", "RELIANCE.NS", True))
        self.assertEqual((r["token"], r["tick_size"]), ("2885", 0.1))
        infy = self.r("INFY")
        self.assertEqual(infy["exchange"], "NSE")
        self.assertEqual(infy["alternatives"][0]["exchange"], "NYSE")
        self.assertEqual(self.r("NYSE:INFY")["exchange"], "NYSE")

    def test_suffixes_and_us(self):
        self.assertEqual(self.r("TCS.NS")["exchange"], "NSE")
        self.assertEqual(self.r("RELIANCE.BO")["exchange"], "BSE")
        a = self.r("AAPL")
        self.assertEqual((a["exchange"], a["currency"], a["verified"]), ("NASDAQ", "USD", True))
        self.assertEqual(self.r("BRK.B")["yahoo"], "BRK-B")
        for sym, ex, ccy in (("7203.T", "TSE", "JPY"), ("0700.HK", "HKEX", "HKD"), ("VOD.L", "LSE", "GBP"),
                             ("SHOP.TO", "TSX", "CAD"), ("BHP.AX", "ASX", "AUD"), ("SAP.DE", "XETRA", "EUR"),
                             ("600519.SS", "SSE", "CNY"), ("000001.SZ", "SZSE", "CNY")):
            r = self.r(sym)
            self.assertEqual((r["exchange"], r["currency"]), (ex, ccy), sym)
        self.assertIn("pence", self.r("VOD.L")["price_unit_note"])

    def test_indices(self):
        for sym, y in (("NIFTY", "^NSEI"), ("BANKNIFTY", "^NSEBANK"), ("FINNIFTY", "NIFTY_FIN_SERVICE.NS"),
                       ("MIDCPNIFTY", "NIFTY_MID_SELECT.NS"), ("SENSEX", "^BSESN"), ("SPX", "^GSPC"), ("N225", "^N225")):
            r = self.r(sym)
            self.assertEqual((r["asset_class"], r["yahoo"]), ("index", y), sym)

    def test_indian_derivatives_are_checked_against_the_scrip_master(self):
        o = self.r("NIFTY29DEC2626000CE")
        self.assertEqual((o["asset_class"], o["exchange"], o["lot_size"], o["tick_size"], o["verified"]),
                         ("option", "NFO", 65, 0.05, True))
        self.assertEqual(o["expiry"], "2026-12-29")
        bad = self.r("NIFTY30DEC2626000CE")   # Wednesday 30 Dec: not a listed expiry
        self.assertFalse(bad["verified"])
        self.assertTrue(any("listed expiries" in n for n in bad["notes"]))
        f = self.r("NIFTY26DECFUT")
        self.assertEqual((f["symbol"], f["expiry"]), ("NIFTY29DEC26FUT", "2026-12-29"))
        s = self.r("SENSEX26NOV2684000CE")
        self.assertEqual(s["exchange"], "BFO")
        loose = self.r("NIFTY 26000 CE")
        self.assertEqual((loose["right"], loose["strike"]), ("CE", 26000.0))

    def test_unsynced_derivative_is_not_verified(self):
        o = self.r("NIFTY29DEC2626000CE", lk=False)
        self.assertFalse(o["verified"])
        self.assertTrue(any("markets.py sync" in n for n in o["notes"]))

    def test_other_derivatives(self):
        occ = self.r("AAPL261218C00200000")
        self.assertEqual((occ["strike"], occ["right"], occ["multiplier"]), (200.0, "CE", 100))
        d = self.r("BTC-26DEC26-100000-C")
        self.assertEqual((d["underlying"], d["venue"], d["option_style"]), ("BTC", "deribit", "european"))
        p = self.r("BTC-USDT-SWAP")
        self.assertEqual((p["asset_class"], p["instrument"]), ("crypto", "perpetual"))
        self.assertEqual(p["venues"]["okx"], "BTC-USDT-SWAP")

    def test_crypto(self):
        for s, base, quote in (("BTCUSDT", "BTC", "USDT"), ("ETH", "ETH", "USDT"), ("SOL-USD", "SOL", "USD"),
                               ("BTC/USDT", "BTC", "USDT"), ("XBTUSD", "BTC", "USD"), ("PEPEUSDT", "PEPE", "USDT")):
            r = self.r(s)
            self.assertEqual((r["asset_class"], r["base"], r["quote"]), ("crypto", base, quote), s)
            self.assertFalse(r["verified"])  # only an exchange listing verifies a pair (crypto.py check)
        self.assertEqual(self.r("BTCUSDT")["venues"]["kraken"], "XBTUSD")
        self.assertIn("unclassified", self.r("NEWTOKENUSDT")["sector"])
        self.assertTrue(any("not on the built-in list" in n for n in self.r("NEWTOKENUSDT")["notes"]))

    def test_stock_tickers_beat_lesser_coin_names(self):
        stx = self.r("STX")
        self.assertEqual((stx["asset_class"], stx["exchange"]), ("equity", "NASDAQ"))
        self.assertEqual(stx["alternatives"][0]["symbol"], "STXUSDT")
        self.assertEqual(self.r("BTC")["asset_class"], "crypto")

    def test_forex_and_commodities(self):
        u = self.r("USDINR")
        self.assertEqual((u["asset_class"], u["yahoo"], u["fx_class"], u["exchange_traded"]),
                         ("forex", "USDINR=X", "exotic", "CDS"))
        self.assertEqual(self.r("EUR/USD")["fx_class"], "major")
        self.assertEqual(self.r("EURGBP")["fx_class"], "minor")
        g = self.r("GOLD")
        self.assertEqual((g["exchange"], g["yahoo_proxy"], g["near_contract"]), ("MCX", "GC=F", "GOLD04DEC26FUT"))
        self.assertTrue(any("proxy" in n for n in g["notes"]))
        self.assertEqual(self.r("GC=F")["underlying"], "GOLD")
        self.assertEqual(self.r("BRENT")["exchange"], "CME")

    def test_invalid_symbols(self):
        for bad in ("", "ZZZZZZZ", "!!!", "XYZ.QQ"):
            r = self.r(bad)
            if bad == "XYZ.QQ":
                self.assertIsNone(r["exchange"])
                continue
            self.assertIn(r["confidence"], ("unknown",), bad)
            self.assertIsNone(r["exchange"], bad)

    def test_snapshot_without_sync(self):
        r = self.r("SBIN", lk=False)
        self.assertEqual((r["exchange"], r["verified"]), ("NSE", True))
        t = self.r("TSLA", lk=False)
        self.assertEqual((t["exchange"], t["verified"]), ("NYSE", False))


class Expiries(unittest.TestCase):
    def test_rule_matches_the_exchange_listing(self):
        self.assertEqual(M.monthly_expiry("NFO", 2026, 11), D(2026, 11, 23))   # Tue 24 is a holiday
        self.assertEqual(M.monthly_expiry("NFO", 2026, 12), D(2026, 12, 29))
        self.assertEqual(M.monthly_expiry("NFO", 2027, 1), D(2027, 1, 25))    # Tue 26 Republic Day
        self.assertEqual(M.weekly_expiries("NFO", 2026, 12),
                         [D(2026, 12, 1), D(2026, 12, 8), D(2026, 12, 15), D(2026, 12, 22), D(2026, 12, 29)])
        self.assertEqual(M.monthly_expiry("BFO", 2026, 12), D(2026, 12, 31))
        self.assertEqual(M.rule_expiries("BANKNIFTY", "NFO", 2026, 12), [D(2026, 12, 29)])  # monthly only

    def test_listed_expiries_agree(self):
        lk = M.Lookups(DATA)
        listed = sorted({M._iso_exp(r["expiry"]) for r in lk.contracts("NIFTY", "NFO") if r["instrumenttype"] == "OPTIDX"})
        dec = [d for d in listed if d.startswith("2026-12")]
        self.assertEqual(dec, [str(d) for d in M.weekly_expiries("NFO", 2026, 12)])


class Binding(unittest.TestCase):
    def test_config_for(self):
        cfg = {"flat_by": "15:55", "markets": {"NSE": {"flat_by": "15:05"}}}
        self.assertEqual(M.config_for(cfg, "NSE")["flat_by"], "15:05")
        self.assertEqual(M.config_for(cfg, "NSE")["no_entries_after"], "15:00")
        self.assertEqual(M.config_for(cfg, "NYSE")["flat_by"], "15:55")
        self.assertEqual(M.day_trade_defaults("LSE"), {"no_entries_before": "08:15", "no_entries_after": "16:00",
                                                       "flat_by": "16:25"})

    def test_bind_and_restore(self):
        entry = load("trader-entry", "entry")
        orig_open, orig_hol = entry.OPEN, dict(entry.NYSE_HOLIDAYS)
        M.bind(entry, "NSE")
        self.assertEqual(entry.OPEN, datetime.time(9, 15))
        self.assertEqual(entry.CLOSE, datetime.time(15, 30))
        self.assertEqual(entry.eastern_offset(DT(2026, 11, 23)), 5.5)
        self.assertIn("2026-11-24", entry.NYSE_HOLIDAYS)
        self.assertEqual(entry.eastern_to_utc(DT(2026, 11, 23, 9, 15)), DT(2026, 11, 23, 3, 45))
        M.bind(entry, "NYSE")
        self.assertEqual(entry.OPEN, orig_open)
        self.assertEqual(entry.NYSE_HOLIDAYS, orig_hol)
        self.assertEqual(entry.eastern_offset(DT(2026, 7, 1)), -4)

    def test_round_the_clock_markets_are_refused_by_session_tools(self):
        exit_ = load("trader-exit", "exit")
        with self.assertRaises(exit_.PlanError):
            exit_.use_market("BTCUSDT")
        tm = load("trader-timemachine", "timemachine")
        with self.assertRaises(tm.TMError):
            tm.use_market("7203.T", continuous=True)   # Tokyo has a lunch break

    def test_lookups_are_shared(self):
        self.assertIs(M.Lookups(DATA), M.Lookups(DATA))

    def test_cli(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = M.main(["status", "NSE", "--now", "2026-11-23 10:00 IST", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())[0]["phase"], "regular")


if __name__ == "__main__":
    unittest.main()
