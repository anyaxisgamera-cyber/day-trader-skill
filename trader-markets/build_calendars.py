#!/usr/bin/env python3
"""Rebuild calendars.json, the exchange holiday and early-close tables.

Maintainer tool, not needed at run time. It reads the open-source
exchange_calendars package (pip install exchange_calendars) and writes, per
exchange, every weekday that is not a trading session and every early close,
through the last date that package has recorded. Dates past "through" are
never guessed: markets.py reports them as unverified.

    python3 trader-markets/build_calendars.py            # -> trader-markets/calendars.json

Cross-checks done when this file was last built (2026-11-23):
  * XNYS matches the NYSE list the original skills carry (20 holidays 2026-2027,
    3 early closes), read from nyse.com on 2026-10-13.
  * XBOM (used for NSE and BSE) has 2026-11-24 as a holiday; Angel One's scrip
    master moves the November 2026 NFO monthly expiry from Tuesday the 24th to
    Monday the 23rd, which agrees.
"""

import datetime
import json
import os
import sys

CODES = ["XBOM", "XNYS", "XLON", "XTKS", "XHKG", "XSHG", "XASX", "XTSE", "XETR", "XPAR",
         "XAMS", "XSWX", "XKRX", "XSES", "XJSE"]

# Holidays known after a calendar's "through" date, each with its own evidence.
EXTRA = {
    "XBOM": {"2027-01-26": "Republic Day (fixed national holiday; Angel One's scrip master moves the "
                           "Jan 2027 NFO monthly expiry from Tue 26 to Mon 25)"},
}


def main():
    try:
        import exchange_calendars as xc
        import pandas as pd
    except ImportError:
        sys.exit("needs: pip install exchange_calendars")
    out = {"source": "exchange_calendars %s (github.com/gerrymanoim/exchange_calendars)" % xc.__version__,
           "built": datetime.date.today().isoformat(), "calendars": {}}
    for code in CODES:
        cal, end = None, None
        for end in ("2027-12-31", "2026-12-31"):
            try:
                cal = xc.get_calendar(code, start="2025-12-20", end=end)
                break
            except Exception:  # noqa: BLE001 - the package raises several kinds when a year is unrecorded
                continue
        if cal is None:
            continue
        last = cal.last_session
        hol = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-01", end) if d > last or not cal.is_session(d)]
        early = {d.strftime("%Y-%m-%d"): cal.session_close(d).tz_convert(cal.tz).strftime("%H:%M")
                 for d in cal.early_closes if d.year >= 2026 and d <= last}
        out["calendars"][code] = {"tz": str(cal.tz), "through": end,  # the package covers the whole range asked
                                  "holidays": hol, "early_closes": early,
                                  "extra_holidays": EXTRA.get(code, {})}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendars.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("wrote %s (%d calendars)" % (path, len(out["calendars"])))


if __name__ == "__main__":
    main()
