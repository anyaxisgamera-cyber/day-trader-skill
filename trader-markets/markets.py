#!/usr/bin/env python3
"""markets.py: the shared market brain for every trader-* skill.

One system, no market switching. Give it any instrument and it works out what
it is, where it trades, in which currency, in which time zone, and whether
that market is open right now, using that exchange's own calendar.

    python3 markets.py resolve RELIANCE NIFTY29DEC2626000CE AAPL BTCUSDT USDINR GOLD
    python3 markets.py status NSE NYSE CRYPTO FX          # open / closed / pre-open, next open
    python3 markets.py status RELIANCE.NS --now "2026-11-24 10:00"
    python3 markets.py calendar NSE --year 2026           # holidays, source, verified through
    python3 markets.py expiry NIFTY --month 2026-12        # NSE rule vs the scrip master
    python3 markets.py sync                                # download symbol lists to trader/markets/

What it knows and where each piece comes from
  * Time zones: rules written out here (India, Japan, Hong Kong, China, Korea,
    Singapore and South Africa have no daylight saving; the US, Canada, the
    UK, the EU and Sydney do). No zoneinfo, so it runs on Python 3.8. The
    tests check every rule against the system tz database where it exists.
  * Holidays and early closes: calendars.json, built from the open-source
    exchange_calendars package (see build_calendars.py). Each calendar says
    how far it is recorded ("through"). A date past that is reported as
    UNVERIFIED, never guessed. NSE/BSE 2026 is recorded; the 2027 NSE list is
    not published yet (NSE publishes it in December).
  * Sessions: the regular hours, pre-open, after-hours and lunch breaks per
    exchange in EXCHANGES below, each with a note.
  * Symbols: NSE's own equity list (EQUITY_L.csv), Angel One's public scrip
    master (every NSE, BSE, NFO, BFO, MCX and CDS instrument with its lot size,
    tick size and expiry) and Nasdaq Trader's US symbol directory, downloaded
    by `sync` to trader/markets/. Without them, a bare ticker can only be
    guessed and the answer says so (verified: false).

India first: a bare ticker that is on the NSE list is NSE; on both NSE and a
US exchange (INFY), NSE wins and the US listing is named as the alternative.
"""

from __future__ import print_function

import argparse
import calendar as _cal
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time

try:
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    Request = urlopen = None

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(HERE, "calendars.json")
NSE_SNAPSHOT = os.path.join(HERE, "nse_symbols.csv")
NSE_SNAPSHOT_DATE = "2026-11-23"
DATA_DIR = os.environ.get("TRADER_MARKETS_DATA", os.path.join("trader", "markets"))
UTC = "UTC"
UA = "Mozilla/5.0 (compatible; day-trader-skill/3.0)"


class MarketError(Exception):
    """A plain one-line reason."""


# --------------------------------------------------------------------------
# Time zones (no zoneinfo: the pack promises Python 3.8)
# --------------------------------------------------------------------------

# zone -> (standard offset in hours, daylight-saving rule or None)
ZONES = {
    "UTC": (0.0, None),
    "Asia/Kolkata": (5.5, None),
    "Asia/Tokyo": (9.0, None),
    "Asia/Seoul": (9.0, None),
    "Asia/Hong_Kong": (8.0, None),
    "Asia/Shanghai": (8.0, None),
    "Asia/Singapore": (8.0, None),
    "Asia/Dubai": (4.0, None),
    "Africa/Johannesburg": (2.0, None),
    "America/New_York": (-5.0, "US"),
    "America/Toronto": (-5.0, "US"),
    "America/Chicago": (-6.0, "US"),
    "Europe/London": (0.0, "EU"),
    "Europe/Berlin": (1.0, "EU"),
    "Europe/Paris": (1.0, "EU"),
    "Europe/Amsterdam": (1.0, "EU"),
    "Europe/Zurich": (1.0, "EU"),
    "Australia/Sydney": (10.0, "AU"),
}
ZONE_LABEL = {"UTC": "UTC", "Asia/Kolkata": "IST", "Asia/Tokyo": "JST", "Asia/Seoul": "KST",
              "Asia/Hong_Kong": "HKT", "Asia/Shanghai": "CST", "Asia/Singapore": "SGT",
              "Asia/Dubai": "GST", "Africa/Johannesburg": "SAST", "America/New_York": "ET",
              "America/Toronto": "ET", "America/Chicago": "CT", "Europe/London": "UK",
              "Europe/Berlin": "CET", "Europe/Paris": "CET", "Europe/Amsterdam": "CET",
              "Europe/Zurich": "CET", "Australia/Sydney": "AET"}


def _nth_weekday(year, month, weekday, n):
    """The n-th `weekday` (Mon=0) of the month; n=-1 is the last one."""
    if n > 0:
        d = datetime.date(year, month, 1)
        d += datetime.timedelta(days=(weekday - d.weekday()) % 7)
        return d + datetime.timedelta(days=7 * (n - 1))
    last = datetime.date(year, month, _cal.monthrange(year, month)[1])
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


def _naive_utc(dt):
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def _dst(rule, std, utc):
    y = utc.year
    if rule == "US":   # 2nd Sunday March 02:00 local to 1st Sunday November 02:00 local
        start = datetime.datetime.combine(_nth_weekday(y, 3, 6, 2), datetime.time(2)) - datetime.timedelta(hours=std)
        end = datetime.datetime.combine(_nth_weekday(y, 11, 6, 1), datetime.time(2)) - datetime.timedelta(hours=std + 1)
        return start <= utc < end
    if rule == "EU":   # last Sunday March 01:00 UTC to last Sunday October 01:00 UTC
        start = datetime.datetime.combine(_nth_weekday(y, 3, 6, -1), datetime.time(1))
        end = datetime.datetime.combine(_nth_weekday(y, 10, 6, -1), datetime.time(1))
        return start <= utc < end
    if rule == "AU":   # 1st Sunday October 02:00 standard to 1st Sunday April 03:00 daylight (southern summer)
        start = datetime.datetime.combine(_nth_weekday(y, 10, 6, 1), datetime.time(2)) - datetime.timedelta(hours=std)
        end = datetime.datetime.combine(_nth_weekday(y, 4, 6, 1), datetime.time(3)) - datetime.timedelta(hours=std + 1)
        return utc >= start or utc < end
    return False


def utc_offset(zone, utc):
    """Hours east of UTC for `zone` at the naive-UTC instant `utc`."""
    if zone not in ZONES:
        raise MarketError("unknown time zone %r" % zone)
    std, rule = ZONES[zone]
    utc = _naive_utc(utc)
    return std + (1.0 if rule and _dst(rule, std, utc) else 0.0)


def to_local(utc, zone):
    utc = _naive_utc(utc)
    return utc + datetime.timedelta(hours=utc_offset(zone, utc))


def local_to_utc(local, zone):
    """Naive local wall time -> naive UTC. In a DST gap or overlap the earlier
    reading of the clock wins, like most exchanges' own notices."""
    std, rule = ZONES[zone]
    for off in ((std + 1, std) if rule else (std,)):
        utc = local - datetime.timedelta(hours=off)
        if utc_offset(zone, utc) == off:
            return utc
    return local - datetime.timedelta(hours=std)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


# --------------------------------------------------------------------------
# Exchanges
# --------------------------------------------------------------------------

def T(h, m=0):
    return datetime.time(h, m)


# sessions: list of (phase, start, end) in local time. "regular" phases are the
# continuous market; "lunch" is a break between two regular phases.
EXCHANGES = {
    # ---- India (IST, no DST). NSE and BSE share the equity holiday list. ----
    "NSE": {"name": "National Stock Exchange of India", "country": "IN", "tz": "Asia/Kolkata",
            "currency": "INR", "calendar": "XBOM", "yahoo_suffix": ".NS",
            "sessions": [("pre-open", T(9), T(9, 8)), ("regular", T(9, 15), T(15, 30)),
                         ("closing", T(15, 40), T(16))],
            "early": None, "note": "pre-open order entry 9:00-9:08, matching to 9:15; closing session 15:40-16:00. "
                                   "Muhurat (Diwali) special sessions are announced yearly and are not modelled."},
    "BSE": {"name": "BSE (Bombay Stock Exchange)", "country": "IN", "tz": "Asia/Kolkata", "currency": "INR",
            "calendar": "XBOM", "yahoo_suffix": ".BO",
            "sessions": [("pre-open", T(9), T(9, 8)), ("regular", T(9, 15), T(15, 30)),
                         ("closing", T(15, 40), T(16))], "early": None, "note": "same hours as NSE"},
    "NFO": {"name": "NSE Futures & Options", "country": "IN", "tz": "Asia/Kolkata", "currency": "INR",
            "calendar": "XBOM", "sessions": [("regular", T(9, 15), T(15, 30))], "early": None,
            "note": "equity and index derivatives on NSE"},
    "BFO": {"name": "BSE Futures & Options", "country": "IN", "tz": "Asia/Kolkata", "currency": "INR",
            "calendar": "XBOM", "sessions": [("regular", T(9, 15), T(15, 30))], "early": None,
            "note": "SENSEX and BANKEX derivatives"},
    "CDS": {"name": "NSE Currency Derivatives", "country": "IN", "tz": "Asia/Kolkata", "currency": "INR",
            "calendar": "XBOM", "sessions": [("regular", T(9), T(17))], "early": None,
            "note": "currency segment; it also closes on some RBI holidays the equity list does not carry"},
    "MCX": {"name": "Multi Commodity Exchange of India", "country": "IN", "tz": "Asia/Kolkata", "currency": "INR",
            "calendar": "XBOM", "sessions": [("regular", T(9), T(23, 30))], "early": None, "mcx": True,
            "note": "non-agri commodities 9:00-23:30 IST while the US is on daylight time and 9:00-23:55 in "
                    "US winter; agri contracts close at 17:00 or 21:00. On some NSE holidays MCX still runs "
                    "its evening session: check MCX's own circular for those days."},
    # ---- United States ----
    "NYSE": {"name": "New York Stock Exchange", "country": "US", "tz": "America/New_York", "currency": "USD",
             "calendar": "XNYS", "yahoo_suffix": "",
             "sessions": [("pre-market", T(4), T(9, 30)), ("regular", T(9, 30), T(16)),
                          ("after-hours", T(16), T(20))], "early": T(13), "note": "early closes at 13:00 ET"},
    "NASDAQ": {"name": "Nasdaq", "country": "US", "tz": "America/New_York", "currency": "USD",
               "calendar": "XNYS", "yahoo_suffix": "",
               "sessions": [("pre-market", T(4), T(9, 30)), ("regular", T(9, 30), T(16)),
                            ("after-hours", T(16), T(20))], "early": T(13), "note": "NYSE holiday list"},
    # ---- Rest of the world ----
    "LSE": {"name": "London Stock Exchange", "country": "GB", "tz": "Europe/London", "currency": "GBP",
            "calendar": "XLON", "yahoo_suffix": ".L", "sessions": [("regular", T(8), T(16, 30))],
            "early": T(12, 30), "note": "Yahoo quotes most LSE shares in pence (GBp), not pounds"},
    "TSE": {"name": "Tokyo Stock Exchange (JPX)", "country": "JP", "tz": "Asia/Tokyo", "currency": "JPY",
            "calendar": "XTKS", "yahoo_suffix": ".T",
            "sessions": [("regular", T(9), T(11, 30)), ("lunch", T(11, 30), T(12, 30)),
                         ("regular", T(12, 30), T(15, 30))], "early": None,
            "note": "afternoon session runs to 15:30 since 5 Nov 2024"},
    "HKEX": {"name": "Hong Kong Exchanges", "country": "HK", "tz": "Asia/Hong_Kong", "currency": "HKD",
             "calendar": "XHKG", "yahoo_suffix": ".HK",
             "sessions": [("pre-open", T(9), T(9, 30)), ("regular", T(9, 30), T(12)), ("lunch", T(12), T(13)),
                          ("regular", T(13), T(16))], "early": T(12), "note": "half days close at 12:00"},
    "SSE": {"name": "Shanghai Stock Exchange", "country": "CN", "tz": "Asia/Shanghai", "currency": "CNY",
            "calendar": "XSHG", "yahoo_suffix": ".SS",
            "sessions": [("pre-open", T(9, 15), T(9, 25)), ("regular", T(9, 30), T(11, 30)),
                         ("lunch", T(11, 30), T(13)), ("regular", T(13), T(15))], "early": None,
            "note": "A-shares; foreign access is through Stock Connect"},
    "SZSE": {"name": "Shenzhen Stock Exchange", "country": "CN", "tz": "Asia/Shanghai", "currency": "CNY",
             "calendar": "XSHG", "yahoo_suffix": ".SZ",
             "sessions": [("pre-open", T(9, 15), T(9, 25)), ("regular", T(9, 30), T(11, 30)),
                          ("lunch", T(11, 30), T(13)), ("regular", T(13), T(15))], "early": None,
             "note": "same mainland holidays as Shanghai"},
    "ASX": {"name": "Australian Securities Exchange", "country": "AU", "tz": "Australia/Sydney", "currency": "AUD",
            "calendar": "XASX", "yahoo_suffix": ".AX", "sessions": [("regular", T(10), T(16))],
            "early": T(14, 10), "note": "opening auction staggers 10:00-10:09"},
    "TSX": {"name": "Toronto Stock Exchange", "country": "CA", "tz": "America/Toronto", "currency": "CAD",
            "calendar": "XTSE", "yahoo_suffix": ".TO", "sessions": [("regular", T(9, 30), T(16))],
            "early": T(13), "note": "TSX Venture uses .V"},
    "XETRA": {"name": "Deutsche Boerse Xetra", "country": "DE", "tz": "Europe/Berlin", "currency": "EUR",
              "calendar": "XETR", "yahoo_suffix": ".DE", "sessions": [("regular", T(9), T(17, 30))],
              "early": T(14), "note": ""},
    "EPA": {"name": "Euronext Paris", "country": "FR", "tz": "Europe/Paris", "currency": "EUR",
            "calendar": "XPAR", "yahoo_suffix": ".PA", "sessions": [("regular", T(9), T(17, 30))],
            "early": T(14, 5), "note": ""},
    "AMS": {"name": "Euronext Amsterdam", "country": "NL", "tz": "Europe/Amsterdam", "currency": "EUR",
            "calendar": "XAMS", "yahoo_suffix": ".AS", "sessions": [("regular", T(9), T(17, 30))],
            "early": T(14, 5), "note": ""},
    "SIX": {"name": "SIX Swiss Exchange", "country": "CH", "tz": "Europe/Zurich", "currency": "CHF",
            "calendar": "XSWX", "yahoo_suffix": ".SW", "sessions": [("regular", T(9), T(17, 30))],
            "early": None, "note": "closing auction 17:20-17:30"},
    "KRX": {"name": "Korea Exchange", "country": "KR", "tz": "Asia/Seoul", "currency": "KRW",
            "calendar": "XKRX", "yahoo_suffix": ".KS", "sessions": [("regular", T(9), T(15, 30))],
            "early": None, "note": "KOSDAQ uses .KQ"},
    "SGX": {"name": "Singapore Exchange", "country": "SG", "tz": "Asia/Singapore", "currency": "SGD",
            "calendar": "XSES", "yahoo_suffix": ".SI",
            "sessions": [("regular", T(9), T(12)), ("lunch", T(12), T(13)), ("regular", T(13), T(17))],
            "early": None, "note": ""},
    "JSE": {"name": "Johannesburg Stock Exchange", "country": "ZA", "tz": "Africa/Johannesburg", "currency": "ZAR",
            "calendar": "XJSE", "yahoo_suffix": ".JO", "sessions": [("regular", T(9), T(17))], "early": None,
            "note": "Yahoo quotes JSE shares in cents (ZAc)"},
    # ---- Round-the-clock markets ----
    "CRYPTO": {"name": "Crypto (all exchanges)", "country": "", "tz": "UTC", "currency": "", "calendar": None,
               "always_open": True, "sessions": [], "early": None,
               "note": "trades 24/7; a single exchange can halt deposits, withdrawals or trading for "
                       "maintenance, so check that exchange's status page"},
    "FX": {"name": "Spot foreign exchange (OTC)", "country": "", "tz": "America/New_York", "currency": "",
           "calendar": None, "fx_week": True, "sessions": [], "early": None,
           "note": "24 hours from Sunday 17:00 to Friday 17:00 New York time; thin around holidays"},
    "CME": {"name": "CME Globex (incl. COMEX, NYMEX, CBOT)", "country": "US", "tz": "America/Chicago",
            "currency": "USD", "calendar": "XNYS", "globex": True, "sessions": [], "early": None,
            "note": "Sunday 17:00 to Friday 16:00 Chicago time with a daily 16:00-17:00 break. Holidays use "
                    "the NYSE list as an approximation: CME often runs a shortened session instead."},
}
YAHOO_SUFFIX = {}
for _code, _ex in EXCHANGES.items():
    if _ex.get("yahoo_suffix"):
        YAHOO_SUFFIX.setdefault(_ex["yahoo_suffix"], _code)
YAHOO_SUFFIX.update({".KQ": "KRX", ".V": "TSX", ".BR": "EPA", ".LS": "EPA", ".MI": "EPA", ".MC": "EPA"})
SUFFIX_NOTE = {".BR": "Euronext Brussels, Euronext Paris calendar used", ".LS": "Euronext Lisbon, Paris calendar used",
               ".MI": "Borsa Italiana, Euronext Paris calendar used as an approximation",
               ".MC": "Bolsa de Madrid, Euronext Paris calendar used as an approximation"}


def exchange(code):
    code = (code or "").upper()
    if code not in EXCHANGES:
        raise MarketError("unknown exchange %r (known: %s)" % (code, ", ".join(sorted(EXCHANGES))))
    return EXCHANGES[code]


# --------------------------------------------------------------------------
# Calendars
# --------------------------------------------------------------------------

_CAL = None


def calendars():
    global _CAL
    if _CAL is None:
        try:
            with open(CAL_FILE) as fh:
                _CAL = json.load(fh)
        except (IOError, OSError, ValueError):
            _CAL = {"source": "missing", "calendars": {}}
    return _CAL


def calendar_day(code, d):
    """What `d` (a local date) is on exchange `code`.

    {"open": bool, "reason": str, "verified": bool, "early_close": time|None, "source": str}
    """
    ex = exchange(code)
    src = calendars().get("source", "")
    if ex.get("always_open"):
        return {"open": True, "reason": "crypto trades every day", "verified": True, "early_close": None,
                "source": "24/7 market"}
    if ex.get("fx_week") or ex.get("globex"):
        # weekly cycle markets: a "day" is open if any of it trades
        wd = d.weekday()
        if wd == 5:
            return {"open": False, "reason": "Saturday", "verified": True, "early_close": None, "source": "weekly cycle"}
        if ex.get("globex") and _holiday(ex, d)[0]:
            return {"open": False, "reason": "US holiday (approximate for CME)", "verified": False,
                    "early_close": None, "source": src}
        return {"open": True, "reason": "", "verified": True, "early_close": None, "source": "weekly cycle"}
    if d.weekday() >= 5:
        return {"open": False, "reason": "weekend", "verified": True, "early_close": None, "source": "weekday rule"}
    hol, verified, why = _holiday(ex, d)
    if hol:
        return {"open": False, "reason": why, "verified": True, "early_close": None, "source": src}
    cal = calendars()["calendars"].get(ex["calendar"] or "", {})
    early = cal.get("early_closes", {}).get(d.isoformat())
    early_t = None
    if early:
        h, m = early.split(":")
        early_t = datetime.time(int(h), int(m))
    return {"open": True, "reason": "" if verified else
            "%s calendar is recorded only through %s, so this day is not verified" % (code, cal.get("through", "?")),
            "verified": verified, "early_close": early_t, "source": src}


def _holiday(ex, d):
    """(is_holiday, verified, reason)."""
    key = ex.get("calendar")
    cal = calendars()["calendars"].get(key or "", {})
    if not cal:
        return False, False, "no holiday calendar for this exchange"
    iso = d.isoformat()
    if iso in cal.get("extra_holidays", {}):
        return True, True, "exchange holiday: " + cal["extra_holidays"][iso]
    if iso <= cal.get("through", ""):
        if iso in set(cal.get("holidays", [])):
            return True, True, "exchange holiday (%s)" % key
        return False, True, ""
    return False, False, ""


def sessions_for(code, d):
    """[(phase, start_utc, end_utc)] for local date d, [] when closed."""
    ex = exchange(code)
    day = calendar_day(code, d)
    if not day["open"] or ex.get("always_open") or ex.get("fx_week") or ex.get("globex"):
        return []
    out = []
    reg = list(ex["sessions"])
    if ex.get("mcx"):
        us_dst = utc_offset("America/New_York", datetime.datetime.combine(d, T(12))) == -4.0
        reg = [("regular", T(9), T(23, 30) if us_dst else T(23, 55))]
    early = day["early_close"]
    for phase, a, b in reg:
        if early is not None:
            if a >= early and phase != "pre-open":
                continue
            if b > early and phase in ("regular", "lunch"):
                b = early
        out.append((phase, local_to_utc(datetime.datetime.combine(d, a), ex["tz"]),
                    local_to_utc(datetime.datetime.combine(d, b), ex["tz"])))
    return out


def regular_hours(code, d):
    """(open_local_time, close_local_time) of the regular market, or None when closed."""
    ex = exchange(code)
    ss = [s for s in sessions_for(code, d) if s[0] == "regular"]
    if not ss:
        return None
    return (to_local(ss[0][1], ex["tz"]).time(), to_local(ss[-1][2], ex["tz"]).time())


def _weekly_status(ex, now_utc):
    """FX and CME Globex: open/closed by the weekly cycle."""
    if ex.get("fx_week"):
        ny = to_local(now_utc, "America/New_York")
        wd, t = ny.weekday(), ny.time()
        closed = wd == 5 or (wd == 6 and t < T(17)) or (wd == 4 and t >= T(17))
        if closed:
            days = (6 - wd) % 7
            nxt = datetime.datetime.combine(ny.date() + datetime.timedelta(days=days), T(17))
            return "closed", local_to_utc(nxt, "America/New_York"), "weekend"
        fri = ny.date() + datetime.timedelta(days=(4 - wd) % 7)
        return "open", local_to_utc(datetime.datetime.combine(fri, T(17)), "America/New_York"), ""
    ch = to_local(now_utc, "America/Chicago")
    wd, t = ch.weekday(), ch.time()
    if wd == 5 or (wd == 6 and t < T(17)) or (wd == 4 and t >= T(16)):
        days = (6 - wd) % 7
        nxt = datetime.datetime.combine(ch.date() + datetime.timedelta(days=days), T(17))
        return "closed", local_to_utc(nxt, "America/Chicago"), "weekend"
    if T(16) <= t < T(17):
        return "break", local_to_utc(datetime.datetime.combine(ch.date(), T(17)), "America/Chicago"), "daily break"
    end_day = ch.date() if t < T(16) else ch.date() + datetime.timedelta(days=1)
    return "open", local_to_utc(datetime.datetime.combine(end_day, T(16)), "America/Chicago"), ""


def market_status(code, now_utc=None):
    """Where exchange `code` is in its trading day at `now_utc`."""
    ex = exchange(code)
    now_utc = _naive_utc(now_utc or utc_now())
    local = to_local(now_utc, ex["tz"])
    res = {"exchange": code, "name": ex["name"], "tz": ex["tz"], "tz_label": ZONE_LABEL.get(ex["tz"], ex["tz"]),
           "local_time": local.strftime("%Y-%m-%d %H:%M"), "now_utc": now_utc.strftime("%Y-%m-%d %H:%M"),
           "note": ex.get("note", "")}
    if ex.get("always_open"):
        res.update(phase="open", is_open=True, verified=True, reason="24/7")
        return res
    if ex.get("fx_week") or ex.get("globex"):
        phase, nxt, why = _weekly_status(ex, now_utc)
        if ex.get("globex") and phase == "open" and not calendar_day(code, local.date())["open"]:
            phase, why = "closed", "US holiday (approximate for CME)"
        res.update(phase=phase, is_open=phase == "open", reason=why, verified=not ex.get("globex"),
                   next_change_utc=nxt.strftime("%Y-%m-%d %H:%M"),
                   next_change_local=to_local(nxt, ex["tz"]).strftime("%Y-%m-%d %H:%M"))
        return res
    day = calendar_day(code, local.date())
    res["verified"] = day["verified"]
    if day["early_close"]:
        res["early_close"] = day["early_close"].strftime("%H:%M")
    phase, ends = "closed", None
    for p, a, b in sessions_for(code, local.date()):
        if a <= now_utc < b:
            phase, ends = p, b
            break
    res["phase"] = phase
    res["is_open"] = phase == "regular"
    res["reason"] = day["reason"]
    if ends:
        res["phase_ends_local"] = to_local(ends, ex["tz"]).strftime("%H:%M")
    nxt = next_open(code, now_utc)
    if nxt and not res["is_open"]:
        res["next_open_local"] = to_local(nxt, ex["tz"]).strftime("%Y-%m-%d %H:%M")
        res["next_open_utc"] = nxt.strftime("%Y-%m-%d %H:%M")
    return res


def next_open(code, now_utc, horizon=15):
    ex = exchange(code)
    d = to_local(now_utc, ex["tz"]).date()
    for i in range(horizon):
        for p, a, _ in sessions_for(code, d + datetime.timedelta(days=i)):
            if p == "regular" and a > now_utc:
                return a
    return None


def trading_days(code, start, end):
    out, d = [], start
    while d <= end:
        if calendar_day(code, d)["open"]:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def prev_trading_day(code, d):
    for _ in range(15):
        if calendar_day(code, d)["open"]:
            return d
        d -= datetime.timedelta(days=1)
    return d


# --------------------------------------------------------------------------
# Indian derivative expiries
# --------------------------------------------------------------------------

# The rule SEBI's 2025 circular set (effective 1 Sep 2025): NSE derivatives expire
# on Tuesdays, BSE derivatives on Thursdays; monthly contracts on the last such day
# of the month; weekly options only on one benchmark per exchange (NIFTY on NSE,
# SENSEX on BSE). A holiday moves the expiry to the previous trading day.
EXPIRY_RULES = {
    "NFO": {"weekday": 1, "weekly": {"NIFTY"}},
    "BFO": {"weekday": 3, "weekly": {"SENSEX"}},
}


def monthly_expiry(seg, year, month):
    rule = EXPIRY_RULES[seg]
    d = _nth_weekday(year, month, rule["weekday"], -1)
    return prev_trading_day("NSE", d)


def weekly_expiries(seg, year, month):
    rule = EXPIRY_RULES[seg]
    d = _nth_weekday(year, month, rule["weekday"], 1)
    out = []
    while d.month == month:
        out.append(prev_trading_day("NSE", d))
        d += datetime.timedelta(days=7)
    return out


def _last_weekly(listed, und, seg):
    """The last date the exchange lists weeklies for: monthlies run months ahead,
    weeklies only a few weeks, so later rule dates are 'not listed yet'."""
    if und.upper() not in EXPIRY_RULES[seg]["weekly"]:
        return "9999-12-31"
    gaps = [d for i, d in enumerate(listed[:-1])
            if (datetime.date(*map(int, listed[i + 1].split("-"))) - datetime.date(*map(int, d.split("-")))).days > 8]
    return gaps[0] if gaps else listed[-1]


def rule_expiries(underlying, seg, year, month):
    if underlying.upper() in EXPIRY_RULES[seg]["weekly"]:
        return weekly_expiries(seg, year, month)
    return [monthly_expiry(seg, year, month)]


# --------------------------------------------------------------------------
# Symbol data (downloaded by `sync`)
# --------------------------------------------------------------------------

NSE_EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/%s"
SCRIP_FIELDS = ("exch_seg", "symbol", "token", "name", "instrumenttype", "expiry", "strike", "lotsize", "tick_size")


def _get(url, timeout=60):
    req = Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    resp = urlopen(req, timeout=timeout)
    data = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _price_divisor(seg):
    return 1e7 if seg == "CDS" else 100.0


def condense_scrip(rows):
    """Angel One's scrip master -> list of dicts with real units.

    The master stores strike and tick size in paise (x100), and in x1e7 units
    for currency derivatives (CDS)."""
    out = []
    for r in rows:
        seg = r.get("exch_seg", "")
        div = _price_divisor(seg)
        try:
            strike = float(r.get("strike") or 0) / div
            tick = float(r.get("tick_size") or 0) / div
        except ValueError:
            strike, tick = 0.0, 0.0
        out.append({"exch_seg": seg, "symbol": r.get("symbol", ""), "token": r.get("token", ""),
                    "name": r.get("name", ""), "instrumenttype": r.get("instrumenttype", ""),
                    "expiry": r.get("expiry", ""), "strike": round(strike, 4) if strike > 0 else 0,
                    "lotsize": int(float(r.get("lotsize") or 1)), "tick_size": round(tick, 6)})
    return out


def write_scrip_csv(rows, path):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SCRIP_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in SCRIP_FIELDS})


_LOOKUPS = {}


class Lookups(object):
    """The symbol lists `sync` downloaded. Every lookup works without them:
    the answer is then marked unverified. Lookups() returns one shared,
    already-loaded instance per folder."""

    def __new__(cls, folder=None):
        folder = folder or os.environ.get("TRADER_MARKETS_DATA") or DATA_DIR
        key = os.path.abspath(folder)
        hit = _LOOKUPS.get(key)
        if hit is not None:
            return hit
        obj = object.__new__(cls)
        obj._init(folder)
        _LOOKUPS[key] = obj
        return obj

    def __init__(self, folder=None):
        pass

    def _init(self, folder=None):
        self.folder = folder or os.environ.get("TRADER_MARKETS_DATA") or DATA_DIR
        self.nse, self.bse, self.us, self.scrip = {}, {}, {}, None
        self.loaded = []
        self.snapshot = False
        self._load()

    def _path(self, name):
        return os.path.join(self.folder, name)

    def _load(self):
        p = self._path("nse_equity.csv")
        if not os.path.exists(p) and os.path.exists(NSE_SNAPSHOT):
            with open(NSE_SNAPSHOT, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("#") or "," not in line:
                        continue
                    sym, series = line.strip().split(",", 1)
                    self.nse[sym.upper()] = {"name": "", "series": series, "isin": ""}
            self.loaded.append("nse_symbols.csv (bundled snapshot, %s)" % NSE_SNAPSHOT_DATE)
            self.snapshot = True
        if os.path.exists(p):
            with open(p, newline="", encoding="utf-8", errors="replace") as fh:
                for row in csv.reader(fh):
                    if len(row) >= 3 and row[0] != "SYMBOL":
                        self.nse[row[0].strip().upper()] = {"name": row[1].strip(), "series": row[2].strip(),
                                                           "isin": row[6].strip() if len(row) > 6 else ""}
            self.loaded.append("nse_equity.csv")
        p = self._path("us_symbols.txt")
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("|")
                    if len(parts) >= 3 and parts[0] and parts[0] != "Symbol":
                        self.us[parts[0].upper()] = {"name": parts[1], "exchange": parts[2]}
            self.loaded.append("us_symbols.txt")
        self._scrip_path = self._path("scrip.csv")
        if os.path.exists(self._scrip_path):
            self.loaded.append("scrip.csv")

    def scrip_rows(self):
        if self.scrip is None:
            self.scrip = []
            self._by_symbol, self._by_name = {}, {}
            if os.path.exists(self._scrip_path):
                with open(self._scrip_path, newline="", encoding="utf-8") as fh:
                    for r in csv.DictReader(fh):
                        r["strike"] = float(r["strike"] or 0)
                        r["lotsize"] = int(r["lotsize"] or 1)
                        r["tick_size"] = float(r["tick_size"] or 0)
                        self.scrip.append(r)
                for r in self.scrip:
                    if r["exch_seg"] == "BSE" and r["instrumenttype"] == "":
                        self.bse.setdefault(r["symbol"].upper(), r)
                    self._by_symbol.setdefault(r["symbol"].upper(), []).append(r)
                    self._by_name.setdefault(r["name"].upper(), []).append(r)
        return self.scrip

    def scrip_symbol(self, symbol, seg=None):
        self.scrip_rows()
        for r in self._by_symbol.get(symbol.upper(), []):
            if seg is None or r["exch_seg"] == seg:
                return r
        return None

    def equity_scrip(self, symbol, seg="NSE"):
        """The cash-market row (token, tick size) for an NSE or BSE share."""
        want = symbol.upper() + ("-EQ" if seg == "NSE" else "")
        hit = self.scrip_symbol(want, seg)
        if hit is None and seg == "NSE":
            for suf in ("-BE", "-BZ", "-SM", "-ST"):
                hit = self.scrip_symbol(symbol.upper() + suf, seg)
                if hit:
                    break
        return hit

    def contracts(self, name, seg=None, kind=None):
        self.scrip_rows()
        return [r for r in self._by_name.get(name.upper(), []) if
                (seg is None or r["exch_seg"] == seg) and (kind is None or r["instrumenttype"] == kind)]


def sync(folder=None, log=print, what=("nse", "scrip", "us")):
    folder = folder or DATA_DIR
    if not os.path.isdir(folder):
        os.makedirs(folder)
    done = {}
    if "nse" in what:
        data = _get(NSE_EQUITY_URL)
        with open(os.path.join(folder, "nse_equity.csv"), "wb") as fh:
            fh.write(data)
        done["nse_equity.csv"] = data.count(b"\n") - 1
        log("NSE equity list: %d rows (%s)" % (done["nse_equity.csv"], NSE_EQUITY_URL))
    if "scrip" in what:
        rows = condense_scrip(json.loads(_get(SCRIP_URL, timeout=180).decode("utf-8")))
        write_scrip_csv(rows, os.path.join(folder, "scrip.csv"))
        done["scrip.csv"] = len(rows)
        log("Angel One scrip master: %d instruments (%s)" % (len(rows), SCRIP_URL))
    if "us" in what:
        lines = ["Symbol|Name|Exchange"]
        codes = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
        for name in ("nasdaqlisted.txt", "otherlisted.txt"):
            text = _get(NASDAQ_URL % name).decode("utf-8", "replace")
            for row in text.splitlines()[1:]:
                p = row.split("|")
                if len(p) < 4 or p[0].startswith("File Creation"):
                    continue
                if name == "nasdaqlisted.txt":
                    if p[3] == "Y":
                        continue
                    lines.append("%s|%s|NASDAQ" % (p[0], p[1]))
                else:
                    if p[6] == "Y":
                        continue
                    lines.append("%s|%s|%s" % (p[0].replace(".", ".").replace("$", "-P"), p[1], codes.get(p[2], p[2])))
            time.sleep(0.3)
        with open(os.path.join(folder, "us_symbols.txt"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        done["us_symbols.txt"] = len(lines) - 1
        log("US symbol directory: %d rows (nasdaqtrader.com)" % (len(lines) - 1))
    with open(os.path.join(folder, "sync.json"), "w") as fh:
        json.dump({"synced_utc": utc_now().strftime("%Y-%m-%d %H:%M"), "files": done}, fh, indent=1)
    return done


# --------------------------------------------------------------------------
# Instrument resolution
# --------------------------------------------------------------------------

FIAT = {"USD", "INR", "EUR", "GBP", "JPY", "HKD", "AUD", "CAD", "CHF", "CNY", "CNH", "NZD", "SGD", "KRW",
        "SEK", "NOK", "DKK", "ZAR", "MXN", "BRL", "TRY", "RUB", "AED", "SAR", "THB", "IDR", "MYR", "PHP",
        "PLN", "HUF", "CZK", "ILS", "TWD"}
MAJORS = {"EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "USD"}
CRYPTO_QUOTES = ("FDUSD", "USDT", "USDC", "BUSD", "TUSD", "DAI", "USD", "INR", "EUR", "GBP", "TRY", "BRL",
                 "JPY", "BTC", "ETH", "BNB")
STABLECOINS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "BUSD", "PYUSD", "USDE", "USDS", "FRAX", "USDD", "GUSD", "USDP"}
# Bases treated as crypto when typed alone. Words that are also common stock tickers
# (COIN, LINK...) are left out on purpose: type LINKUSDT for the token.
COINS = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "BNB": "BNB", "SOL": "Solana", "XRP": "XRP", "DOGE": "Dogecoin",
    "ADA": "Cardano", "TRX": "TRON", "TON": "Toncoin", "AVAX": "Avalanche", "SHIB": "Shiba Inu",
    "DOT": "Polkadot", "BCH": "Bitcoin Cash", "LTC": "Litecoin", "NEAR": "NEAR Protocol", "MATIC": "Polygon (old)",
    "POL": "Polygon", "UNI": "Uniswap", "ICP": "Internet Computer", "APT": "Aptos", "SUI": "Sui",
    "ARB": "Arbitrum", "OP": "Optimism", "PEPE": "Pepe", "WIF": "dogwifhat", "BONK": "Bonk", "FIL": "Filecoin",
    "ATOM": "Cosmos", "XLM": "Stellar", "HBAR": "Hedera", "INJ": "Injective", "RNDR": "Render (old)",
    "RENDER": "Render", "FET": "Artificial Superintelligence Alliance", "TAO": "Bittensor", "SEI": "Sei",
    "TIA": "Celestia", "AAVE": "Aave", "MKR": "Maker", "LDO": "Lido DAO", "CRV": "Curve", "ENA": "Ethena",
    "ONDO": "Ondo", "JUP": "Jupiter", "PYTH": "Pyth", "IMX": "Immutable", "SAND": "The Sandbox",
    "MANA": "Decentraland", "AXS": "Axie Infinity", "GALA": "Gala", "FLOKI": "Floki", "TRUMP": "Official Trump",
    "HYPE": "Hyperliquid", "XMR": "Monero", "ETC": "Ethereum Classic", "KAS": "Kaspa", "STX": "Stacks",
    "WLD": "Worldcoin", "USDT": "Tether", "USDC": "USD Coin", "DAI": "Dai", "XBT": "Bitcoin (Kraken code)",
}
CRYPTO_SECTOR = {
    "BTC": "Layer 1 / store of value", "ETH": "Layer 1 / smart contracts", "SOL": "Layer 1", "ADA": "Layer 1",
    "AVAX": "Layer 1", "DOT": "Layer 0", "NEAR": "Layer 1", "APT": "Layer 1", "SUI": "Layer 1", "SEI": "Layer 1",
    "TON": "Layer 1", "TRX": "Layer 1", "ATOM": "Layer 0", "ICP": "Layer 1", "KAS": "Layer 1",
    "ARB": "Layer 2", "OP": "Layer 2", "POL": "Layer 2 / sidechain", "MATIC": "Layer 2 / sidechain",
    "IMX": "Layer 2 / gaming", "STX": "Bitcoin Layer 2", "TIA": "Modular data availability",
    "UNI": "DeFi", "AAVE": "DeFi", "MKR": "DeFi", "LDO": "DeFi / liquid staking", "CRV": "DeFi", "ENA": "DeFi",
    "JUP": "DeFi", "INJ": "DeFi", "ONDO": "Tokenized real-world assets", "PYTH": "Oracle",
    "FET": "AI", "TAO": "AI", "RENDER": "AI / compute", "RNDR": "AI / compute", "WLD": "AI / identity",
    "SAND": "Gaming / metaverse", "MANA": "Gaming / metaverse", "AXS": "Gaming", "GALA": "Gaming",
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "WIF": "Meme", "BONK": "Meme", "FLOKI": "Meme",
    "TRUMP": "Meme", "USDT": "Stablecoin", "USDC": "Stablecoin", "DAI": "Stablecoin", "FDUSD": "Stablecoin",
    "XMR": "Privacy", "HYPE": "DeFi / perp exchange", "BNB": "Exchange token / Layer 1", "XRP": "Payments",
    "XLM": "Payments", "LTC": "Payments", "BCH": "Payments", "HBAR": "Layer 1", "FIL": "Storage",
    "ETC": "Layer 1", "PAXG": "Tokenized gold", "XAUT": "Tokenized gold",
}

INDEX_ALIASES = {
    # India first
    "NIFTY": ("^NSEI", "NSE", "NIFTY 50"), "NIFTY50": ("^NSEI", "NSE", "NIFTY 50"),
    "BANKNIFTY": ("^NSEBANK", "NSE", "NIFTY Bank"), "NIFTYBANK": ("^NSEBANK", "NSE", "NIFTY Bank"),
    "FINNIFTY": ("NIFTY_FIN_SERVICE.NS", "NSE", "NIFTY Financial Services"),
    "MIDCPNIFTY": ("NIFTY_MID_SELECT.NS", "NSE", "NIFTY Midcap Select"),
    "NIFTYNXT50": ("^NSMIDCP", "NSE", "NIFTY Next 50"), "NIFTYIT": ("^CNXIT", "NSE", "NIFTY IT"),
    "NIFTYMIDCAP100": ("^CRSMID", "NSE", "NIFTY Midcap 100"), "NIFTY500": ("^CRSLDX", "NSE", "NIFTY 500"),
    "INDIAVIX": ("^INDIAVIX", "NSE", "India VIX"), "SENSEX": ("^BSESN", "BSE", "S&P BSE Sensex"),
    "BANKEX": ("BSE-BANK.BO", "BSE", "BSE Bankex"),
    # world
    "SPX": ("^GSPC", "NYSE", "S&P 500"), "SP500": ("^GSPC", "NYSE", "S&P 500"), "NDX": ("^NDX", "NASDAQ", "Nasdaq 100"),
    "NASDAQ100": ("^NDX", "NASDAQ", "Nasdaq 100"), "COMP": ("^IXIC", "NASDAQ", "Nasdaq Composite"),
    "DJI": ("^DJI", "NYSE", "Dow Jones Industrial Average"), "DOW": ("^DJI", "NYSE", "Dow Jones Industrial Average"),
    "RUT": ("^RUT", "NYSE", "Russell 2000"), "VIX": ("^VIX", "NYSE", "Cboe VIX"),
    "FTSE": ("^FTSE", "LSE", "FTSE 100"), "FTSE100": ("^FTSE", "LSE", "FTSE 100"),
    "N225": ("^N225", "TSE", "Nikkei 225"), "NIKKEI": ("^N225", "TSE", "Nikkei 225"),
    "HSI": ("^HSI", "HKEX", "Hang Seng"), "HANGSENG": ("^HSI", "HKEX", "Hang Seng"),
    "SSEC": ("000001.SS", "SSE", "SSE Composite"), "SHCOMP": ("000001.SS", "SSE", "SSE Composite"),
    "CSI300": ("000300.SS", "SSE", "CSI 300"), "ASX200": ("^AXJO", "ASX", "S&P/ASX 200"),
    "TSX": ("^GSPTSE", "TSX", "S&P/TSX Composite"), "DAX": ("^GDAXI", "XETRA", "DAX 40"),
    "CAC": ("^FCHI", "EPA", "CAC 40"), "CAC40": ("^FCHI", "EPA", "CAC 40"),
    "STOXX50": ("^STOXX50E", "XETRA", "Euro Stoxx 50"), "AEX": ("^AEX", "AMS", "AEX"),
    "SMI": ("^SSMI", "SIX", "Swiss Market Index"), "KOSPI": ("^KS11", "KRX", "KOSPI"),
    "STI": ("^STI", "SGX", "Straits Times Index"),
}
# Commodities: India first. A bare name is the MCX contract; the global benchmark
# on Yahoo is the price proxy because there is no free MCX price feed.
COMMODITIES = {
    "GOLD": ("GC=F", "COMEX gold, USD per troy ounce", "GOLD"), "SILVER": ("SI=F", "COMEX silver, USD per troy ounce", "SILVER"),
    "CRUDEOIL": ("CL=F", "NYMEX WTI crude, USD per barrel", "CRUDEOIL"), "CRUDE": ("CL=F", "NYMEX WTI crude, USD per barrel", "CRUDEOIL"),
    "WTI": ("CL=F", "NYMEX WTI crude, USD per barrel", None), "BRENT": ("BZ=F", "ICE Brent crude, USD per barrel", None),
    "NATURALGAS": ("NG=F", "NYMEX Henry Hub natural gas, USD per MMBtu", "NATURALGAS"),
    "NATGAS": ("NG=F", "NYMEX Henry Hub natural gas, USD per MMBtu", "NATURALGAS"),
    "COPPER": ("HG=F", "COMEX copper, USD per pound", "COPPER"),
    "ALUMINIUM": ("ALI=F", "COMEX aluminum, USD per metric ton", "ALUMINIUM"),
    "ALUMINUM": ("ALI=F", "COMEX aluminum, USD per metric ton", "ALUMINIUM"),
    "ZINC": (None, "no free benchmark future on Yahoo", "ZINC"), "LEAD": (None, "no free benchmark future on Yahoo", "LEAD"),
    "NICKEL": (None, "no free benchmark future on Yahoo", "NICKEL"),
    "PLATINUM": ("PL=F", "NYMEX platinum, USD per troy ounce", None), "PALLADIUM": ("PA=F", "NYMEX palladium, USD per troy ounce", None),
    "CORN": ("ZC=F", "CBOT corn, US cents per bushel", None), "WHEAT": ("ZW=F", "CBOT wheat, US cents per bushel", None),
    "SOYBEAN": ("ZS=F", "CBOT soybeans, US cents per bushel", None), "COTTON": ("CT=F", "ICE cotton, US cents per pound", "COTTONCNDY"),
    "SUGAR": ("SB=F", "ICE raw sugar, US cents per pound", None), "COFFEE": ("KC=F", "ICE coffee, US cents per pound", None),
    "COCOA": ("CC=F", "ICE cocoa, USD per metric ton", None),
}
YAHOO_FUTURES = {v[0]: k for k, v in COMMODITIES.items() if v[0]}

MONTHS = {m.upper(): i for i, m in enumerate(_cal.month_abbr) if m}
RE_ANGEL_OPT = re.compile(r"^([A-Z0-9&\-]+?)(\d{2})([A-Z]{3})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
RE_ANGEL_FUT = re.compile(r"^([A-Z0-9&\-]+?)(\d{2})([A-Z]{3})(\d{2})FUT$")
RE_NSE_MONTH_FUT = re.compile(r"^([A-Z0-9&\-]+?)(\d{2})([A-Z]{3})FUT$")  # NIFTY26DECFUT (Zerodha style)
RE_OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
RE_DERIBIT = re.compile(r"^([A-Z]{2,6})-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:\.\d+)?)-([CP])$")
RE_DERIBIT_FUT = re.compile(r"^([A-Z]{2,6})-(\d{1,2})([A-Z]{3})(\d{2})$")
RE_PERP = re.compile(r"^([A-Z0-9]{2,15}?)[-/]?(USDT|USDC|USD)(?:[-.](?:SWAP|PERP|P))$|^([A-Z0-9]{2,15})-PERP(?:ETUAL)?$")
RE_LOOSE_OPT = re.compile(r"^([A-Z&\-]+)\s+(\d+(?:\.\d+)?)\s*(CE|PE|CALL|PUT)(?:\s+(\d{1,2}[A-Z]{3}\d{2,4}))?$")


def _blank(text):
    return {"input": text, "symbol": None, "asset_class": None, "exchange": None, "currency": None,
            "tz": None, "yahoo": None, "verified": False, "confidence": "unknown", "notes": [],
            "alternatives": []}


def _finish(res):
    if res.get("exchange") and res["exchange"] in EXCHANGES:
        ex = EXCHANGES[res["exchange"]]
        res["tz"] = res.get("tz") or ex["tz"]
        res["currency"] = res.get("currency") or ex["currency"] or None
        res["exchange_name"] = ex["name"]
    return res


def _date_from(dd, mon, yy):
    m = MONTHS.get(mon)
    if not m:
        return None
    try:
        return datetime.date(2000 + int(yy) if int(yy) < 100 else int(yy), m, int(dd))
    except ValueError:
        return None


def resolve(text, lookups=None):
    """Work out what instrument `text` names. Never raises for a bad symbol:
    it returns confidence "unknown" with the reason in notes."""
    raw = (text or "").strip()
    res = _blank(raw)
    s = raw.upper().replace(" ", "") if not RE_LOOSE_OPT.match(raw.upper()) else raw.upper()
    lk = lookups
    if not s:
        res["notes"].append("empty symbol")
        return res

    # explicit exchange prefix: NSE:RELIANCE, BINANCE:BTCUSDT, MCX:GOLD
    if ":" in s:
        pre, rest = s.split(":", 1)
        if pre in ("NYSE", "NASDAQ", "US", "AMEX", "ARCA"):
            return _us(rest, res, lk)
        sub = resolve(rest, lk)
        if pre in ("NSE", "BSE") and sub.get("asset_class") in (None, "equity"):
            return _equity(rest, pre, res, lk)
        if pre in ("BINANCE", "COINBASE", "KRAKEN", "OKX", "BYBIT", "KUCOIN") and sub.get("asset_class") == "crypto":
            sub["venue"] = pre.lower()
            return sub
        if pre == "MCX" and sub.get("asset_class") == "commodity":
            return sub
        if pre in EXCHANGES:
            sub["exchange"] = pre
            return _finish(sub)
        res["notes"].append("unknown prefix %r" % pre)
        return res

    # ---- derivatives first: their patterns are unambiguous ----
    m = RE_LOOSE_OPT.match(s)
    if m:
        und, strike, right, exp = m.group(1).replace(" ", ""), float(m.group(2)), m.group(3), m.group(4)
        right = "CE" if right in ("CE", "CALL") else "PE"
        res.update(asset_class="option", underlying=und, strike=strike, right=right, confidence="pattern",
                   exchange="BFO" if und in ("SENSEX", "BANKEX") else "NFO", currency="INR")
        if exp:
            mm = re.match(r"(\d{1,2})([A-Z]{3})(\d{2,4})", exp)
            res["expiry"] = str(_date_from(mm.group(1), mm.group(2), mm.group(3)))
        _match_scrip(res, lk)
        return _finish(res)
    m = RE_ANGEL_OPT.match(s)
    if m and MONTHS.get(m.group(3)):
        exp = _date_from(m.group(2), m.group(3), m.group(4))
        und = m.group(1)
        res.update(symbol=s, asset_class="option", underlying=und, expiry=str(exp) if exp else None,
                   strike=float(m.group(5)), right=m.group(6), confidence="pattern",
                   exchange="BFO" if und in ("SENSEX", "BANKEX") else ("CDS" if und[:3] in FIAT and und[3:] in FIAT else "NFO"))
        _match_scrip(res, lk)
        return _finish(res)
    m = RE_ANGEL_FUT.match(s)
    if m and MONTHS.get(m.group(3)):
        exp = _date_from(m.group(2), m.group(3), m.group(4))
        und = m.group(1)
        seg = "MCX" if und in {v[2] for v in COMMODITIES.values() if v[2]} else (
            "BFO" if und in ("SENSEX", "BANKEX") else ("CDS" if und[:3] in FIAT and und[3:] in FIAT else "NFO"))
        res.update(symbol=s, asset_class="future", underlying=und, expiry=str(exp) if exp else None,
                   confidence="pattern", exchange=seg)
        _match_scrip(res, lk)
        return _finish(res)
    m = RE_NSE_MONTH_FUT.match(s)
    if m and MONTHS.get(m.group(3)):
        und, yy, mon = m.group(1), int(m.group(2)), MONTHS[m.group(3)]
        res.update(asset_class="future", underlying=und, confidence="pattern",
                   exchange="BFO" if und in ("SENSEX", "BANKEX") else "NFO")
        try:
            res["expiry"] = str(monthly_expiry(res["exchange"], 2000 + yy, mon))
            res["notes"].append("expiry from the NSE/BSE rule (last Tuesday/Thursday, holiday-adjusted)")
        except MarketError:
            pass
        _match_scrip(res, lk)
        return _finish(res)
    m = RE_OCC.match(s)
    if m:
        exp = datetime.date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
        res.update(symbol=s, asset_class="option", underlying=m.group(1), expiry=str(exp),
                   strike=int(m.group(6)) / 1000.0, right="CE" if m.group(5) == "C" else "PE",
                   option_style="american", exchange="NYSE", currency="USD", confidence="pattern",
                   yahoo=s)
        res["notes"].append("OCC symbol: a US listed option (American style, 100 shares a contract)")
        res["multiplier"] = 100
        return _finish(res)
    m = RE_DERIBIT.match(s)
    if m:
        exp = _date_from(m.group(2), m.group(3), m.group(4))
        res.update(symbol=s, asset_class="option", underlying=m.group(1), expiry=str(exp) if exp else None,
                   strike=float(m.group(5)), right="CE" if m.group(6) == "C" else "PE", exchange="CRYPTO",
                   venue="deribit", currency="USD", option_style="european", confidence="pattern",
                   settlement=m.group(1) + " (inverse)", sector="crypto option")
        return _finish(res)
    m = RE_PERP.match(s)
    if m:
        base = m.group(1) or m.group(3)
        quote = m.group(2) or "USD"
        res.update(asset_class="crypto", instrument="perpetual", base=base, quote=quote, symbol=base + quote,
                   exchange="CRYPTO", currency=quote, confidence="pattern" if base not in COINS else "known coin",
                   verified=False)
        res["venues"] = _venue_symbols(base, quote, perp=True)
        res["sector"] = CRYPTO_SECTOR.get(base)
        return _finish(res)
    m = RE_DERIBIT_FUT.match(s)
    if m and m.group(1) in COINS and MONTHS.get(m.group(3)):
        exp = _date_from(m.group(2), m.group(3), m.group(4))
        res.update(symbol=s, asset_class="crypto", instrument="future", base=m.group(1), quote="USD",
                   expiry=str(exp) if exp else None, exchange="CRYPTO", venue="deribit", currency="USD",
                   confidence="pattern", verified=False)
        return _finish(res)

    # ---- Yahoo-style suffixes and specials ----
    if s.endswith("=X"):
        return _forex(s[:-2], res)
    if s.endswith("=F"):
        name = YAHOO_FUTURES.get(s)
        res.update(symbol=s, yahoo=s, asset_class="commodity" if name else "future", exchange="CME",
                   currency="USD", confidence="pattern", verified=bool(name), underlying=name)
        if name:
            res["description"] = COMMODITIES[name][1]
        return _finish(res)
    if s.startswith("^"):
        for alias, (ysym, exch, nm) in INDEX_ALIASES.items():
            if ysym == s:
                res.update(symbol=alias, yahoo=s, asset_class="index", exchange=exch, name=nm,
                           confidence="known", verified=True)
                return _finish(res)
        res.update(symbol=s, yahoo=s, asset_class="index", confidence="pattern")
        res["notes"].append("a Yahoo index code this tool does not map to an exchange; hours unknown")
        return res
    if "." in s and not s.endswith("."):
        base, suf = s.rsplit(".", 1)
        suf = "." + suf
        if suf in YAHOO_SUFFIX:
            code = YAHOO_SUFFIX[suf]
            if code in ("NSE", "BSE"):
                return _equity(base, code, res, lk)
            res.update(symbol=s, yahoo=s, asset_class="equity", exchange=code, confidence="pattern")
            if suf in SUFFIX_NOTE:
                res["notes"].append(SUFFIX_NOTE[suf])
            if code == "LSE":
                res["price_unit_note"] = "Yahoo quotes most LSE shares in GBp (pence): divide by 100 for GBP"
            if code == "JSE":
                res["price_unit_note"] = "Yahoo quotes JSE shares in ZAc (cents): divide by 100 for ZAR"
            return _finish(res)
        if re.match(r"^[A-Z]{1,4}\.[A-Z]$", s):  # BRK.B, a US class share
            return _us(s, res, lk)

    # ---- named things ----
    if s in INDEX_ALIASES:
        ysym, exch, nm = INDEX_ALIASES[s]
        res.update(symbol=s, yahoo=ysym, asset_class="index", exchange=exch, name=nm, confidence="known",
                   verified=True)
        if s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"):
            res["derivatives"] = "BFO" if s in ("SENSEX", "BANKEX") else "NFO"
        return _finish(res)
    pair = re.sub(r"[/\-_]", "", s)
    if len(pair) == 6 and pair[:3] in FIAT and pair[3:] in FIAT and pair[:3] != pair[3:]:
        return _forex(pair, res)
    crypto = _crypto(s, res, lk)
    if crypto:
        return crypto
    if s in COMMODITIES:
        ysym, desc, mcx = COMMODITIES[s]
        res.update(symbol=s, asset_class="commodity", underlying=s, confidence="known", verified=True)
        if mcx:
            res.update(exchange="MCX", currency="INR", mcx_name=mcx, yahoo_proxy=ysym, proxy_description=desc)
            res["notes"].append("India first: the MCX contract. There is no free MCX price feed, so prices "
                                "come from %s (%s) as a proxy in a different currency and unit." % (ysym, desc)
                                if ysym else "India first: the MCX contract; no free price feed for it.")
            _near_future(res, mcx, "MCX", lk)
        else:
            res.update(exchange="CME", currency="USD", yahoo=ysym, description=desc)
        return _finish(res)

    # ---- plain equity tickers: India first ----
    if re.match(r"^[A-Z0-9&\-]{1,20}$", s):
        in_nse = bool(lk and s in lk.nse)
        in_us = bool(lk and s in lk.us)
        if in_nse:
            out = _equity(s, "NSE", res, lk)
            if in_us:
                out["alternatives"].append({"symbol": s, "exchange": lk.us[s]["exchange"], "name": lk.us[s]["name"],
                                            "hint": "type %s.US or NYSE:%s for the US listing" % (s, s)})
            return out
        if in_us:
            return _us(s, res, lk)
        if s.endswith(".US"):
            return _us(s[:-3], res, lk)
        if lk and lk.us:
            res["notes"].append("not on the NSE or US symbol lists synced on this machine")
            res["confidence"] = "unknown"
            return res
        if lk and lk.nse and re.match(r"^[A-Z]{1,5}$", s):
            out = _us(s, res, lk)
            out["notes"].append("not on NSE's list, so read as a US ticker; not verified against a US list "
                                "(run markets.py sync)")
            return out
        if lk and lk.nse:
            res["notes"].append("not on NSE's list and not a US-style ticker: not a symbol this system recognises")
            return res
        # nothing synced: India-first guess, clearly marked
        res.update(symbol=s, asset_class="equity", exchange="NSE", yahoo=s + ".NS", confidence="assumed",
                   verified=False)
        res["notes"].append("no symbol lists synced (run: markets.py sync). Guessed NSE because the system is "
                            "India first; add .NS, .BO or a US exchange prefix to be sure.")
        if len(s) <= 5 and s.isalpha():
            res["alternatives"].append({"symbol": s, "exchange": "NYSE/NASDAQ", "hint": "NYSE:%s" % s})
        return _finish(res)
    res["notes"].append("not a symbol this system recognises")
    return res


def _equity(sym, code, res, lk):
    sym = sym.upper()
    res.update(symbol=sym, asset_class="equity", exchange=code,
               yahoo=sym + EXCHANGES[code]["yahoo_suffix"], confidence="pattern")
    if lk and lk.loaded:
        if code == "NSE" and sym in lk.nse:
            row = lk.nse[sym]
            res.update(series=row["series"], verified=True, confidence="verified")
            if row.get("name"):
                res.update(name=row["name"], isin=row["isin"])
            if lk.snapshot:
                res["notes"].append("checked against the bundled NSE list of %s" % NSE_SNAPSHOT_DATE)
            if row["series"] in ("BE", "BZ"):
                res["notes"].append("NSE series %s: trade-for-trade (delivery only, no intraday) segment"
                                    % row["series"])
        sc = lk.equity_scrip(sym, code) if "scrip.csv" in lk.loaded else None
        if sc:
            res.update(angel_symbol=sc["symbol"], token=sc["token"], tick_size=sc["tick_size"],
                       lot_size=sc["lotsize"])
            res["verified"] = True
            res["confidence"] = "verified"
        if not res["verified"]:
            res["notes"].append("not on the %s list synced on this machine" % code)
    return _finish(res)


def _us(sym, res, lk):
    sym = sym.upper()
    exch = "NYSE"
    if lk and sym in lk.us:
        exch = "NASDAQ" if lk.us[sym]["exchange"] == "NASDAQ" else "NYSE"
        res.update(name=lk.us[sym]["name"], verified=True, confidence="verified", listing=lk.us[sym]["exchange"])
    else:
        res["confidence"] = "pattern"
    res.update(symbol=sym, asset_class="equity", exchange=exch, yahoo=sym.replace(".", "-"))
    return _finish(res)


def _forex(pair, res):
    pair = pair.upper()
    b, q = pair[:3], pair[3:]
    if b not in FIAT or q not in FIAT:
        res["notes"].append("%s is not a pair of ISO currency codes" % pair)
        return res
    kind = "major" if b in MAJORS and q in MAJORS and "USD" in (b, q) else (
        "minor" if b in MAJORS and q in MAJORS else "exotic")
    res.update(symbol=pair, asset_class="forex", base=b, quote=q, currency=q, exchange="FX", yahoo=pair + "=X",
               confidence="known", verified=True, fx_class=kind)
    if "INR" in (b, q):
        res["notes"].append("INR is not freely convertible offshore: Indian residents trade USDINR, EURINR, "
                            "GBPINR and JPYINR futures and options on NSE (CDS) and BSE, not spot.")
        res["exchange_traded"] = "CDS"
    return _finish(res)


def _venue_symbols(base, quote, perp=False):
    q = quote
    usdq = q if q != "USD" else "USDT"
    v = {"binance": base + usdq + (" (USD-M perpetual)" if perp else ""),
         "okx": "%s-%s%s" % (base, usdq, "-SWAP" if perp else ""),
         "bybit": base + usdq, "kucoin": "%s-%s" % (base, usdq) + ("" if not perp else " (futures: %sUSDTM)" % base)}
    if not perp:
        v["coinbase"] = "%s-%s" % (base, "USD" if q in ("USD", "USDT") else q)
        v["kraken"] = ("XBT" if base == "BTC" else base) + ("USD" if q in ("USD", "USDT") else q)
        v["yahoo"] = "%s-%s" % (base, "USD" if q in ("USDT", "USDC", "USD") else q)
    return v


CORE_COINS = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "USDT", "USDC", "XBT"}


def _crypto(s, res, lk):
    t = re.sub(r"[/\-_]", "", s)
    base = quote = None
    if t in COINS and t not in CORE_COINS and lk and (t in lk.nse or t in lk.us or
                                                      (lk.snapshot and not lk.us and re.match(r"^[A-Z]{1,5}$", t))):
        # a bare word that is also a stock ticker (STX, OP, ICP...) is the stock; the token needs a quote: STXUSDT
        res["alternatives"].append({"symbol": t + "USDT", "exchange": "CRYPTO", "hint": "type %sUSDT for the token" % t})
        return None
    if t in COINS:
        base, quote = t, "USDT"
    else:
        for q in CRYPTO_QUOTES:
            if t.endswith(q) and len(t) > len(q):
                b = t[:-len(q)]
                if b in FIAT:
                    continue
                if b in COINS or ("/" in s or "-" in s or q in ("USDT", "USDC", "FDUSD")):
                    base, quote = b, q
                    break
    if not base:
        return None
    if base == "XBT":
        base = "BTC"
    res.update(symbol=base + quote, asset_class="crypto", instrument="spot", base=base, quote=quote,
               currency=quote, exchange="CRYPTO", confidence="known coin" if base in COINS else "pattern",
               verified=False, name=COINS.get(base))
    if quote == "INR":
        res["venues"] = {}
        res["yahoo"] = "%s-INR" % base
    else:
        res["venues"] = _venue_symbols(base, quote)
        res["yahoo"] = res["venues"]["yahoo"]
    res["notes"].append("a crypto pair is confirmed only when an exchange lists it: crypto.py check %s" % (base + quote))
    res["sector"] = CRYPTO_SECTOR.get(base, "unclassified")
    if base in STABLECOINS:
        res["notes"].append("a stablecoin: its risk is the issuer and the peg, not the chart")
    if base not in COINS:
        res["notes"].append("%s is not on the built-in list of established coins: check it trades on a real "
                            "exchange before analysing it (crypto.py check %s), and treat new tokens as high risk"
                            % (base, base + quote))
    if quote == "INR":
        res["notes"].append("INR pairs trade on Indian exchanges (CoinDCX, WazirX...), which are not wired in "
                            "here; USDT prices are converted with the USD/INR rate instead. India taxes crypto "
                            "gains at 30% plus 1% TDS on sales.")
    return _finish(res)


def _match_scrip(res, lk):
    """Confirm an Indian derivative against Angel One's scrip master."""
    if not (lk and "scrip.csv" in lk.loaded):
        res["notes"].append("contract not verified: run markets.py sync to load Angel One's scrip master "
                            "(lot sizes, tick sizes, real expiries)")
        return
    und = res.get("underlying")
    seg = res.get("exchange")
    kind = res.get("asset_class")
    cands = [r for r in lk.contracts(und, seg)
             if (kind == "future" and r["instrumenttype"].startswith("FUT")) or
             (kind == "option" and r["instrumenttype"].startswith("OPT"))]
    if not cands:
        res["notes"].append("no %s %s contracts on %s in the scrip master" % (und, kind, seg))
        return
    if res.get("strike") is not None and kind == "option":
        cands = [r for r in cands if abs(r["strike"] - res["strike"]) < 1e-6 and r["symbol"].endswith(res["right"])]
    if res.get("expiry"):
        want = datetime.datetime.strptime(res["expiry"], "%Y-%m-%d").strftime("%d%b%Y").upper()
        exact = [r for r in cands if r["expiry"].upper() == want]
        if not exact:
            res["notes"].append("no %s contract expiring %s; listed expiries: %s" % (
                und, res["expiry"], ", ".join(sorted({_iso_exp(r["expiry"]) for r in cands})[:6])))
            res["verified"] = False
            return
        cands = exact
    else:
        cands.sort(key=lambda r: _iso_exp(r["expiry"]))
        cands = cands[:1]
        res["notes"].append("no expiry given: the nearest listed one was used")
    r = cands[0]
    res.update(symbol=r["symbol"], expiry=_iso_exp(r["expiry"]), lot_size=r["lotsize"], tick_size=r["tick_size"],
               token=r["token"], instrument_type=r["instrumenttype"], verified=True, confidence="verified",
               angel_symbol=r["symbol"])
    if kind == "option":
        res["option_style"] = "european"
        res["notes"].append("Indian exchange options are European style and cash or physically settled per "
                            "contract rules (stock options settle physically)")


def _near_future(res, name, seg, lk):
    if not (lk and "scrip.csv" in lk.loaded):
        return
    futs = sorted((r for r in lk.contracts(name, seg) if r["instrumenttype"].startswith("FUT")),
                  key=lambda r: _iso_exp(r["expiry"]))
    today = utc_now().date().isoformat()
    futs = [r for r in futs if _iso_exp(r["expiry"]) >= today]
    if futs:
        r = futs[0]
        res.update(near_contract=r["symbol"], expiry=_iso_exp(r["expiry"]), lot_size=r["lotsize"],
                   tick_size=r["tick_size"], token=r["token"], verified=True)


def _iso_exp(e):
    try:
        return datetime.datetime.strptime(e, "%d%b%Y").date().isoformat()
    except ValueError:
        return e


# --------------------------------------------------------------------------
# Binding the original single-market tools to the instrument's own market
# --------------------------------------------------------------------------

def bind(module, code):
    """Point an original trader-* script's clock at exchange `code`.

    The original tools carry NYSE hours, the NYSE holiday list and a New York
    clock as module globals (OPEN, CLOSE, EARLY, NYSE_HOLIDAYS,
    NYSE_EARLY_CLOSE, eastern_offset). Rebinding those globals makes the same
    tested logic run on the instrument's own exchange. Calling bind(m, "NYSE")
    restores the originals."""
    g = module if isinstance(module, dict) else module.__dict__
    orig = g.get("_MARKETS_ORIG")
    if orig is None:
        orig = {k: g[k] for k in ("OPEN", "CLOSE", "EARLY", "NYSE_HOLIDAYS", "NYSE_EARLY_CLOSE", "eastern_offset",
                                  "eastern_to_utc",
                                  "OPEN_MIN", "CLOSE_MIN", "SESSION_BARS", "EARLY_MIN", "CALENDAR_YEARS") if k in g}
        g["_MARKETS_ORIG"] = orig
    code = (code or "NYSE").upper()
    if code in ("NYSE", "NASDAQ"):
        for k, v in orig.items():
            g[k] = v
        g["MARKET"] = "NYSE"
        g["MARKET_TZ_LABEL"] = "ET"
        return module
    module = _G(g)
    ex = exchange(code)
    if ex.get("always_open") or ex.get("fx_week") or ex.get("globex"):
        raise MarketError("%s trades around the clock; this tool works on exchange sessions. Use "
                          "trader-crypto or trader-markets for it." % code)
    regs = [s for s in ex["sessions"] if s[0] == "regular"]
    if "OPEN" in g:
        module.OPEN = regs[0][1]
    if "CLOSE" in g:
        module.CLOSE = regs[-1][2] if not ex.get("mcx") else T(23, 30)
    if "EARLY" in g:
        # an exchange without early closes gets a time that never matches CLOSE
        module.EARLY = ex.get("early") or datetime.time(23, 59, 58)
    cal = calendars()["calendars"].get(ex["calendar"] or "", {})
    hol = {}
    for d in cal.get("holidays", []):
        hol[datetime.date(*map(int, d.split("-"))) if _date_keys(orig.get("NYSE_HOLIDAYS")) else d] = \
            "in the %s exchange calendar" % ex["calendar"]
    for d, why in cal.get("extra_holidays", {}).items():
        hol[datetime.date(*map(int, d.split("-"))) if _date_keys(orig.get("NYSE_HOLIDAYS")) else d] = why
    if "NYSE_HOLIDAYS" in orig:
        module.NYSE_HOLIDAYS = hol
    if "NYSE_EARLY_CLOSE" in orig:
        ec = {}
        for d, _ in cal.get("early_closes", {}).items():
            ec[datetime.date(*map(int, d.split("-"))) if _date_keys(orig.get("NYSE_EARLY_CLOSE")) else d] = \
                "%s early close" % code
        module.NYSE_EARLY_CLOSE = ec
    zone = ex["tz"]
    if "eastern_offset" in orig:
        module.eastern_offset = lambda utc, _z=zone: utc_offset(_z, utc)
    if "eastern_to_utc" in orig:
        module.eastern_to_utc = lambda local, _z=zone: local_to_utc(local, _z)
    o, c = regs[0][1], (regs[-1][2] if not ex.get("mcx") else T(23, 30))
    if "OPEN_MIN" in orig:
        module.OPEN_MIN = o.hour * 60 + o.minute
    if "CLOSE_MIN" in orig:
        module.CLOSE_MIN = c.hour * 60 + c.minute
    if "SESSION_BARS" in orig:
        module.SESSION_BARS = (module.CLOSE_MIN - module.OPEN_MIN) // 5
    if "EARLY_MIN" in orig:
        e = ex.get("early") or c
        module.EARLY_MIN = e.hour * 60 + e.minute
    if "CALENDAR_YEARS" in orig:
        thr = cal.get("through", "2026-12-31")
        module.CALENDAR_YEARS = tuple(range(2026, int(thr[:4]) + 1))
    module.MARKET = code
    module.MARKET_TZ_LABEL = ZONE_LABEL.get(zone, zone)
    return module


class _G(object):
    """Attribute access onto a globals() dict."""

    def __init__(self, g):
        object.__setattr__(self, "_g", g)

    def __getattr__(self, k):
        try:
            return self._g[k]
        except KeyError:
            raise AttributeError(k)

    def __setattr__(self, k, v):
        self._g[k] = v


CITY = {"NSE": "Mumbai", "BSE": "Mumbai", "NFO": "Mumbai", "BFO": "Mumbai", "CDS": "Mumbai", "MCX": "Mumbai",
        "NYSE": "New York", "NASDAQ": "New York", "LSE": "London", "TSE": "Tokyo", "HKEX": "Hong Kong",
        "SSE": "Shanghai", "SZSE": "Shenzhen", "ASX": "Sydney", "TSX": "Toronto", "XETRA": "Frankfurt",
        "EPA": "Paris", "AMS": "Amsterdam", "SIX": "Zurich", "KRX": "Seoul", "SGX": "Singapore",
        "JSE": "Johannesburg"}

# Day-trading clock per market: no entries before/after, flat by. NYSE keeps the
# pack's original 09:45 / 15:30 / 15:55. India is flat by 15:10 because brokers
# square off intraday (MIS) positions themselves from about 15:15-15:20 IST.
DAY_DEFAULTS = {"NYSE": ("09:45", "15:30", "15:55"), "NASDAQ": ("09:45", "15:30", "15:55"),
                "NSE": ("09:30", "15:00", "15:10"), "BSE": ("09:30", "15:00", "15:10"),
                "NFO": ("09:30", "15:00", "15:10"), "BFO": ("09:30", "15:00", "15:10"),
                "CDS": ("09:15", "16:30", "16:45"), "MCX": ("09:15", "23:00", "23:20")}


def _hm(t):
    return t.strftime("%H:%M")


def day_trade_defaults(code):
    """{"no_entries_before", "no_entries_after", "flat_by"} local times for exchange `code`."""
    code = code.upper()
    if code in DAY_DEFAULTS:
        a, b, c = DAY_DEFAULTS[code]
    else:
        ex = exchange(code)
        regs = [x for x in ex["sessions"] if x[0] == "regular"]
        if not regs:
            raise MarketError("%s has no exchange session" % code)
        o = datetime.datetime.combine(datetime.date(2000, 1, 3), regs[0][1])
        c_ = datetime.datetime.combine(datetime.date(2000, 1, 3), regs[-1][2])
        a, b, c = _hm((o + datetime.timedelta(minutes=15)).time()), _hm((c_ - datetime.timedelta(minutes=30)).time()), \
            _hm((c_ - datetime.timedelta(minutes=5)).time())
    return {"no_entries_before": a, "no_entries_after": b, "flat_by": c}


def config_for(cfg, code):
    """The config a tool should use for exchange `code`.

    Top-level times in trader/config.json are the pack's original New York
    settings, so they apply to NYSE/NASDAQ. Every other market starts from its
    own defaults (day_trade_defaults), and a "markets": {"NSE": {...}} section
    overrides any key for that market."""
    cfg = dict(cfg or {})
    code = (code or "NYSE").upper()
    per = (cfg.get("markets") or {}).get(code) or {}
    if code not in ("NYSE", "NASDAQ"):
        for k, v in day_trade_defaults(code).items():
            cfg[k] = v
    cfg.update(per)
    cfg["market"] = code
    return cfg


class _Localizer(object):
    """Rewrites the New York words in an original tool's output for another market."""

    def __init__(self, stream, code):
        self.stream = stream
        self.code = code
        ex = EXCHANGES[code]
        self.subs = [(re.compile(r"\bNYSE\b"), code),
                     (re.compile(r"\bNew York time\b"), "%s time (%s)" % (CITY.get(code, code), ZONE_LABEL.get(ex["tz"]))),
                     (re.compile(r"\bNew York\b"), CITY.get(code, code)),
                     (re.compile(r"(\d) ET\b"), r"\1 " + ZONE_LABEL.get(ex["tz"], ex["tz"])),
                     (re.compile(r"\(ET\)"), "(%s)" % ZONE_LABEL.get(ex["tz"], ex["tz"]))]

    def write(self, text):
        for rx, rep in self.subs:
            text = rx.sub(rep, text)
        return self.stream.write(text)

    def __getattr__(self, name):
        return getattr(self.stream, name)


CURRENCY_SIGN = {"INR": "\u20b9", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5", "CNY": "CN\u00a5",
                 "HKD": "HK$", "AUD": "A$", "CAD": "C$", "CHF": "CHF ", "KRW": "\u20a9", "SGD": "S$", "ZAR": "R ",
                 "GBp": "GBp ", "ZAc": "ZAc "}


def localize(code, money=None):
    """Rewrite New York words (and "$" when the money is not dollars) in this
    process's output. NYSE/NASDAQ in dollars leaves the output untouched."""
    code = (code or "NYSE").upper()
    money = money or (EXCHANGES.get(code, {}).get("currency") or "USD")
    if code == "LSE":
        money = "GBp"
    if code == "JSE":
        money = "ZAc"
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name)
        base = cur.stream if isinstance(cur, _Localizer) else cur
        if code in ("NYSE", "NASDAQ") and money == "USD":
            setattr(sys, name, base)
            continue
        loc = _Localizer(base, code if code in EXCHANGES else "NYSE")
        if code in ("NYSE", "NASDAQ"):
            loc.subs = []
        if money != "USD":
            loc.subs.append((re.compile(r"\$(?=[\d(.-])"), CURRENCY_SIGN.get(money, money + " ")))
        setattr(sys, name, loc)


def adopt(g, symbol, continuous=False, lookups=None, money=None):
    """Point an original tool (its globals() dict) at `symbol`'s own market.

    -> (exchange code, resolved instrument). A symbol this system cannot place
    on an exchange session keeps the tool's original New York market, and the
    returned instrument says why."""
    lk = lookups if lookups is not None else Lookups()
    inst = resolve(symbol, lk) if symbol else {"exchange": "NYSE", "notes": []}
    code = inst.get("exchange") or "NYSE"
    if code in ("NFO", "BFO"):
        code = "NSE"  # index and stock F&O run NSE's session
    if code not in EXCHANGES or EXCHANGES[code].get("always_open") or EXCHANGES[code].get("fx_week") or \
            EXCHANGES[code].get("globex"):
        if inst.get("asset_class") in ("crypto", "forex") or code in ("CRYPTO", "FX", "CME"):
            raise MarketError("%s is a %s instrument that trades around the clock. This tool works on exchange "
                              "sessions: use trader-crypto (crypto.py) or trader-markets for it."
                              % (symbol, inst.get("asset_class") or code))
        code = "NYSE"
    if continuous and any(p == "lunch" for p, _, _ in EXCHANGES[code]["sessions"]):
        raise MarketError("%s has a lunch break (%s); this tool replays one continuous session, so it does not "
                          "support that exchange yet" % (code, EXCHANGES[code]["name"]))
    bind(g, code)
    localize(code, money)
    return code, inst


def tool_symbol(inst, fallback):
    """The symbol an original tool should send to Yahoo for this instrument."""
    return inst.get("yahoo") or fallback


def _date_keys(d):
    return bool(d) and isinstance(next(iter(d)), datetime.date)


def market_of(symbol, lookups=None):
    """The exchange code that runs `symbol`'s regular session (for bind)."""
    r = resolve(symbol, lookups)
    return r.get("exchange") or "NYSE", r


def load_sibling(name):
    """Import trader-markets/<name>.py from any sibling skill folder."""
    import importlib.util
    path = os.path.join(HERE, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_markets_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_now(text):
    """--now "YYYY-MM-DD HH:MM" in UTC, or with a zone word: IST, ET, UTC."""
    if not text:
        return utc_now()
    t = text.strip()
    zone = "UTC"
    for word, z in (("IST", "Asia/Kolkata"), ("ET", "America/New_York"), ("UTC", "UTC")):
        if t.upper().endswith(" " + word):
            t, zone = t[:-len(word)].strip(), z
            break
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return local_to_utc(datetime.datetime.strptime(t, fmt), zone)
        except ValueError:
            pass
    raise MarketError("--now wants 'YYYY-MM-DD HH:MM' (UTC, or end with IST or ET), got %r" % text)


def _print_resolve(r):
    bits = [r["input"], "->", str(r.get("symbol")), "|", str(r.get("asset_class")), "|",
            str(r.get("exchange")), "|", str(r.get("currency")), "|", "verified" if r.get("verified") else
            "NOT verified (%s)" % r.get("confidence")]
    print(" ".join(bits))
    for k in ("name", "yahoo", "underlying", "expiry", "strike", "right", "lot_size", "tick_size", "token",
              "angel_symbol", "near_contract", "yahoo_proxy", "sector", "fx_class", "instrument"):
        if r.get(k) not in (None, "", []):
            print("    %-14s %s" % (k, r[k]))
    if r.get("venues"):
        print("    %-14s %s" % ("venues", ", ".join("%s=%s" % kv for kv in sorted(r["venues"].items()))))
    for n in r.get("notes", []):
        print("    note: " + n)
    for a in r.get("alternatives", []):
        print("    also: %s on %s (%s)" % (a["symbol"], a["exchange"], a.get("hint", "")))


def main(argv=None):
    p = argparse.ArgumentParser(description="Identify any instrument and its market's hours.")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("resolve", help="what is this symbol")
    r.add_argument("symbols", nargs="+")
    r.add_argument("--json", action="store_true")
    r.add_argument("--data", help="folder with synced symbol lists (default trader/markets)")
    s = sub.add_parser("status", help="is this market open")
    s.add_argument("what", nargs="+", help="exchange codes or symbols")
    s.add_argument("--now", help="pin the clock: 'YYYY-MM-DD HH:MM' UTC (or end with IST/ET)")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("calendar", help="an exchange's holidays")
    c.add_argument("exchange")
    c.add_argument("--year", type=int, default=utc_now().year)
    e = sub.add_parser("expiry", help="Indian derivative expiries: the rule and the scrip master")
    e.add_argument("underlying")
    e.add_argument("--month", help="YYYY-MM (default: this month and the next two)")
    e.add_argument("--data")
    y = sub.add_parser("sync", help="download the NSE, Angel One and US symbol lists")
    y.add_argument("--data")
    y.add_argument("--only", choices=["nse", "scrip", "us"], action="append")
    sub.add_parser("exchanges", help="list every exchange and its hours")
    a = p.parse_args(argv)
    try:
        if a.cmd == "resolve":
            lk = Lookups(a.data)
            out = [resolve(x, lk) for x in a.symbols]
            if a.json:
                print(json.dumps(out, indent=1, default=str))
            else:
                if not lk.loaded:
                    print("(no symbol lists synced: answers below are pattern-based. Run: markets.py sync)")
                for x in out:
                    _print_resolve(x)
            return 0 if all(x.get("asset_class") for x in out) else 1
        if a.cmd == "status":
            now = parse_now(a.now)
            lk = Lookups()
            rows = []
            for w in a.what:
                code = w.upper()
                if code not in EXCHANGES:
                    code, info = market_of(w, lk)
                rows.append(market_status(code, now))
            if a.json:
                print(json.dumps(rows, indent=1))
            else:
                for st in rows:
                    line = "%-7s %-11s %s %s" % (st["exchange"], st["phase"].upper(), st["local_time"], st["tz_label"])
                    if st.get("phase_ends_local"):
                        line += "  (until %s)" % st["phase_ends_local"]
                    if st.get("next_open_local"):
                        line += "  next open %s" % st["next_open_local"]
                    if st.get("next_change_local"):
                        line += "  changes %s" % st["next_change_local"]
                    if st.get("reason"):
                        line += "  [%s]" % st["reason"]
                    if st.get("verified") is False:
                        line += "  UNVERIFIED CALENDAR"
                    print(line)
            return 0
        if a.cmd == "calendar":
            ex = exchange(a.exchange)
            cal = calendars()["calendars"].get(ex.get("calendar") or "", {})
            print("%s (%s), calendar %s, recorded through %s. Source: %s" % (
                a.exchange.upper(), ex["name"], ex.get("calendar"), cal.get("through", "n/a"),
                calendars().get("source")))
            for d in cal.get("holidays", []):
                if d.startswith(str(a.year)):
                    print("  %s %s  closed" % (d, datetime.date(*map(int, d.split("-"))).strftime("%a")))
            for d, why in sorted(cal.get("extra_holidays", {}).items()):
                if d.startswith(str(a.year)):
                    print("  %s closed: %s" % (d, why))
            for d, t in sorted(cal.get("early_closes", {}).items()):
                if d.startswith(str(a.year)):
                    print("  %s early close %s" % (d, t))
            if cal and cal.get("through", "") < "%d-12-31" % a.year:
                print("  NOT VERIFIED after %s: the exchange has not published (or this build has not "
                      "recorded) that part of %d." % (cal.get("through"), a.year))
            return 0
        if a.cmd == "expiry":
            lk = Lookups(a.data)
            und = a.underlying.upper()
            seg = "BFO" if und in ("SENSEX", "BANKEX") else "NFO"
            today = utc_now().date()
            months = []
            if a.month:
                yy, mm = map(int, a.month.split("-"))
                months = [(yy, mm)]
            else:
                for i in range(3):
                    mm = today.month + i
                    months.append((today.year + (mm - 1) // 12, (mm - 1) % 12 + 1))
            listed = sorted({_iso_exp(r["expiry"]) for r in lk.contracts(und, seg)}) if "scrip.csv" in lk.loaded else []
            for yy, mm in months:
                rule = [d.isoformat() for d in rule_expiries(und, seg, yy, mm)]
                got = [d for d in listed if d.startswith("%04d-%02d" % (yy, mm))]
                print("%s %04d-%02d  rule: %s" % (und, yy, mm, ", ".join(rule)))
                if listed:
                    live = [d for d in rule if today.isoformat() <= d]
                    gone = [d for d in rule if d < today.isoformat()]
                    want = [d for d in live if d <= _last_weekly(listed, und, seg)]
                    if live and rule[-1] in live and rule[-1] not in want and listed[-1] >= rule[-1]:
                        want.append(rule[-1])  # the monthly contract is listed months ahead
                    agree = "agrees" if set(got) == set(want) else "DIFFERS: trust the exchange listing"
                    extra = ""
                    if gone:
                        extra += "; already expired: %s" % ", ".join(gone)
                    late = [d for d in live if d not in want]
                    if late:
                        extra += "; not listed yet: %s" % ", ".join(late)
                    print("              listed: %s  (%s%s)" % (", ".join(got) or "none", agree, extra))
                ver = calendar_day("NSE", datetime.date(yy, mm, 28))["verified"]
                if not ver:
                    print("              the NSE holiday list for %d is not recorded: a holiday could move these" % yy)
            if not listed:
                print("(scrip master not synced: only the rule is shown. Run: markets.py sync)")
            return 0
        if a.cmd == "sync":
            sync(a.data, what=tuple(a.only or ("nse", "scrip", "us")))
            return 0
        if a.cmd == "exchanges":
            for code in sorted(EXCHANGES):
                ex = EXCHANGES[code]
                hrs = ", ".join("%s %s-%s" % (ph, x.strftime("%H:%M"), y.strftime("%H:%M")) for ph, x, y in ex["sessions"])
                print("%-7s %-40s %-18s %-4s %s" % (code, ex["name"], ex["tz"], ex["currency"] or "-", hrs or ex["note"]))
            return 0
        p.print_help()
        return 1
    except (MarketError, IOError, OSError, ValueError) as exc:
        print("markets: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
