#!/usr/bin/env python3
"""fno.py: futures and options analytics with real contract specs.

    python3 fno.py contract NIFTY29DEC2626000CE          # lot, tick, expiry, style, days left, notional
    python3 fno.py expiries BANKNIFTY                    # listed expiries (scrip master) vs the exchange rule
    python3 fno.py greeks --spot 25370 --strike 25500 --days 6 --iv 13 --right CE --rate 6.5
    python3 fno.py iv --spot 25370 --strike 25500 --days 6 --price 82 --right CE --rate 6.5
    python3 fno.py chain BTC --expiry 26DEC26             # live Deribit chain: PCR, max pain, OI walls, IV skew
    python3 fno.py chain NIFTY --file nifty_chain.csv     # an NSE/Angel One chain you exported (CSV)
    python3 fno.py basis --spot 25370 --future 25480 --days 36 --rate 6.5
    python3 fno.py buildup --price-change 1.2 --oi-change 8.5
    python3 fno.py payoff --spot 25370 --leg "buy CE 25500 82" --leg "sell CE 25700 31" --lot 65

What is live and what is not
  * Contract specs for every NSE, BSE (F&O), MCX and NSE currency contract: Angel One's public scrip
    master (markets.py sync). Lot sizes, tick sizes and expiries come from there, never from memory.
  * Crypto option chains (BTC, ETH, SOL, XRP...): Deribit's public API, live, with open interest and
    mark IV. Crypto futures basis: Deribit futures vs its index.
  * Indian and US option chains: NSE's website blocks automated cloud requests and Yahoo's chain API
    needs a browser session, so they are read from a CSV you export (columns are listed in SKILL.md).
    Nothing is filled in when a chain is missing.

Models
  * Black-Scholes-Merton for options on spot (index and stock options; NSE options are European).
  * Black-76 for options on futures (MCX options on futures, Deribit, which prices off its futures).
  * Implied volatility by bisection on the model price (robust, no derivatives needed).
"""

from __future__ import print_function

import argparse
import csv
import datetime
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MKT = os.environ.get("TRADER_MARKETS_DIR", os.path.join(HERE, "..", "trader-markets"))
sys.path.insert(0, MKT)
import markets as M  # noqa: E402
import marketdata as MD  # noqa: E402

NOTE = "Option values are model outputs from the inputs shown. They are not prices you can trade at."


class FnoError(Exception):
    pass


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs(spot, strike, t, vol, rate=0.0, q=0.0, right="CE", model="bsm"):
    """Price and greeks. t in years, vol/rate/q as decimals.

    model "bsm": Black-Scholes-Merton on spot with continuous yield q.
    model "black76": the underlying `spot` is a futures price (q is ignored).
    Theta is per calendar day; vega and rho per 1 percentage point."""
    call = right.upper() in ("CE", "C", "CALL")
    if t <= 0 or vol <= 0:
        intrinsic = max(0.0, spot - strike) if call else max(0.0, strike - spot)
        return {"price": intrinsic, "delta": (1.0 if spot > strike else 0.0) if call else (-1.0 if spot < strike else 0.0),
                "gamma": 0.0, "theta_day": 0.0, "vega_1pct": 0.0, "rho_1pct": 0.0, "d1": None, "d2": None}
    sq = vol * math.sqrt(t)
    if model == "black76":
        f = spot
        d1 = (math.log(f / strike) + 0.5 * vol * vol * t) / sq
        d2 = d1 - sq
        df = math.exp(-rate * t)
        if call:
            price = df * (f * _ncdf(d1) - strike * _ncdf(d2))
            delta = df * _ncdf(d1)
            theta = -df * f * _npdf(d1) * vol / (2 * math.sqrt(t)) + rate * price
        else:
            price = df * (strike * _ncdf(-d2) - f * _ncdf(-d1))
            delta = -df * _ncdf(-d1)
            theta = -df * f * _npdf(d1) * vol / (2 * math.sqrt(t)) + rate * price
        gamma = df * _npdf(d1) / (f * sq)
        vega = df * f * _npdf(d1) * math.sqrt(t)
        rho = -t * price
    else:
        d1 = (math.log(spot / strike) + (rate - q + 0.5 * vol * vol) * t) / sq
        d2 = d1 - sq
        dq, dr = math.exp(-q * t), math.exp(-rate * t)
        if call:
            price = spot * dq * _ncdf(d1) - strike * dr * _ncdf(d2)
            delta = dq * _ncdf(d1)
            theta = (-spot * dq * _npdf(d1) * vol / (2 * math.sqrt(t)) - rate * strike * dr * _ncdf(d2)
                     + q * spot * dq * _ncdf(d1))
            rho = strike * t * dr * _ncdf(d2)
        else:
            price = strike * dr * _ncdf(-d2) - spot * dq * _ncdf(-d1)
            delta = -dq * _ncdf(-d1)
            theta = (-spot * dq * _npdf(d1) * vol / (2 * math.sqrt(t)) + rate * strike * dr * _ncdf(-d2)
                     - q * spot * dq * _ncdf(-d1))
            rho = -strike * t * dr * _ncdf(-d2)
        gamma = dq * _npdf(d1) / (spot * sq)
        vega = spot * dq * _npdf(d1) * math.sqrt(t)
    return {"price": price, "delta": delta, "gamma": gamma, "theta_day": theta / 365.0, "vega_1pct": vega / 100.0,
            "rho_1pct": rho / 100.0, "d1": d1, "d2": d2}


def implied_vol(price, spot, strike, t, rate=0.0, q=0.0, right="CE", model="bsm", lo=1e-4, hi=5.0):
    """Bisection. Returns None when the price is outside what any volatility can give."""
    if t <= 0 or price <= 0:
        return None
    plo = bs(spot, strike, t, lo, rate, q, right, model)["price"]
    phi = bs(spot, strike, t, hi, rate, q, right, model)["price"]
    if not plo - 1e-9 <= price <= phi + 1e-9:
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if bs(spot, strike, t, mid, rate, q, right, model)["price"] < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def years_to(expiry, now_utc=None, exchange="NSE", close=None):
    """Calendar time from now to the expiry's close (NSE 15:30 IST by default)."""
    now_utc = now_utc or M.utc_now()
    ex = M.EXCHANGES.get(exchange, M.EXCHANGES["NSE"])
    if close is None:
        regs = [s for s in ex["sessions"] if s[0] == "regular"]
        close = regs[-1][2] if regs else datetime.time(8)  # Deribit expires 08:00 UTC
    end = M.local_to_utc(datetime.datetime.combine(expiry, close), ex["tz"])
    return max(0.0, (end - now_utc).total_seconds() / (365.0 * 86400))


# --------------------------------------------------------------------------
# Chains
# --------------------------------------------------------------------------

def max_pain(rows):
    """The expiry price at which option holders, in total, would get the least
    (writers the most): the strike that minimises total intrinsic value x OI."""
    strikes = sorted({r["strike"] for r in rows})
    best = None
    for s in strikes:
        pain = 0.0
        for r in rows:
            oi = r.get("oi") or 0
            if r["right"] == "CE":
                pain += max(0.0, s - r["strike"]) * oi
            else:
                pain += max(0.0, r["strike"] - s) * oi
        if best is None or pain < best[1]:
            best = (s, pain)
    return best[0] if best else None


def chain_stats(rows, spot=None):
    calls = [r for r in rows if r["right"] == "CE"]
    puts = [r for r in rows if r["right"] == "PE"]
    coi, poi = sum(r.get("oi") or 0 for r in calls), sum(r.get("oi") or 0 for r in puts)
    cv, pv = sum(r.get("volume") or 0 for r in calls), sum(r.get("volume") or 0 for r in puts)
    out = {"calls": len(calls), "puts": len(puts), "call_oi": coi, "put_oi": poi,
           "pcr_oi": poi / coi if coi else None, "pcr_volume": pv / cv if cv else None,
           "max_pain": max_pain(rows) if coi + poi else None,
           "top_call_oi": sorted(calls, key=lambda r: -(r.get("oi") or 0))[:3],
           "top_put_oi": sorted(puts, key=lambda r: -(r.get("oi") or 0))[:3]}
    if any(r.get("oi_change") is not None for r in rows):
        out["call_oi_change"] = sum(r.get("oi_change") or 0 for r in calls)
        out["put_oi_change"] = sum(r.get("oi_change") or 0 for r in puts)
    if spot:
        with_iv = [r for r in rows if r.get("iv")]
        if with_iv:
            atm = min(with_iv, key=lambda r: abs(r["strike"] - spot))
            out["atm_strike"] = atm["strike"]
            atm_ivs = [r["iv"] for r in with_iv if r["strike"] == atm["strike"]]
            out["atm_iv"] = sum(atm_ivs) / len(atm_ivs)
            otm_p = [r for r in puts if r.get("iv") and r["strike"] <= spot * 0.95]
            otm_c = [r for r in calls if r.get("iv") and r["strike"] >= spot * 1.05]
            if otm_p and otm_c:
                p5 = max(otm_p, key=lambda r: r["strike"])
                c5 = min(otm_c, key=lambda r: r["strike"])
                out["skew_5pct"] = {"put_strike": p5["strike"], "put_iv": p5["iv"], "call_strike": c5["strike"],
                                    "call_iv": c5["iv"], "put_minus_call": p5["iv"] - c5["iv"]}
    return out


def read_chain_csv(path):
    """Columns (case-insensitive; extras ignored): strike, right (CE/PE) or type,
    oi, oi_change, volume, iv (percent), ltp. One row per strike and side.
    An NSE-style wide export with CALL and PUT columns side by side is also read
    when its headers are CE_OI, CE_IV, CE_LTP, PE_OI, ... (see SKILL.md)."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        hdr = {h.lower().strip(): h for h in (rd.fieldnames or [])}

        def g(row, *names):
            for n in names:
                if n in hdr and row.get(hdr[n]) not in (None, "", "-"):
                    try:
                        return float(str(row[hdr[n]]).replace(",", ""))
                    except ValueError:
                        return None
            return None

        wide = "ce_oi" in hdr or "pe_oi" in hdr
        for row in rd:
            k = g(row, "strike", "strike price", "strikeprice")
            if k is None:
                continue
            if wide:
                for side in ("CE", "PE"):
                    p = side.lower() + "_"
                    rows.append({"strike": k, "right": side, "oi": g(row, p + "oi"), "oi_change": g(row, p + "chng_oi", p + "oi_change"),
                                 "volume": g(row, p + "volume"), "iv": g(row, p + "iv"), "ltp": g(row, p + "ltp")})
            else:
                right = str(row.get(hdr.get("right") or hdr.get("type") or hdr.get("option type", ""), "")).upper().strip()
                right = "CE" if right in ("CE", "C", "CALL") else "PE" if right in ("PE", "P", "PUT") else None
                if not right:
                    continue
                rows.append({"strike": k, "right": right, "oi": g(row, "oi", "open interest"),
                             "oi_change": g(row, "oi_change", "chng in oi", "change in oi"), "volume": g(row, "volume"),
                             "iv": g(row, "iv", "implied volatility"), "ltp": g(row, "ltp", "last", "price")})
    if not rows:
        raise FnoError("no option rows found in %s (see SKILL.md for the columns)" % path)
    return rows


def deribit_chain(f, ccy, expiry_code=None):
    url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=%s&kind=option" % ccy.upper()
    data = f.json(url, "deribit", "opts-" + ccy.upper())["result"]
    by_exp = {}
    for r in data:
        parts = r["instrument_name"].split("-")
        if len(parts) != 4:
            continue
        by_exp.setdefault(parts[1], []).append(r)
    if not by_exp:
        raise FnoError("Deribit lists no %s options" % ccy)

    def exp_date(code):
        return M._date_from(code[:-5], code[-5:-2], code[-2:])

    codes = sorted(by_exp, key=lambda c: exp_date(c) or datetime.date.max)
    code = expiry_code.upper() if expiry_code else codes[0]
    if code not in by_exp:
        raise FnoError("no %s expiry %s on Deribit; listed: %s" % (ccy, code, ", ".join(codes)))
    rows = []
    und = None
    for r in by_exp[code]:
        p = r["instrument_name"].split("-")
        und = r.get("underlying_price") or und
        rows.append({"strike": float(p[2]), "right": "CE" if p[3] == "C" else "PE", "oi": r.get("open_interest"),
                     "volume": r.get("volume"), "iv": r.get("mark_iv"), "ltp": r.get("mark_price"),
                     "price_unit": ccy.upper() + " (inverse: premium is quoted in the coin)"})
    return rows, und, exp_date(code), codes, url


# --------------------------------------------------------------------------
# Futures, build-up, payoffs
# --------------------------------------------------------------------------

def basis(spot, future, days, rate=None, q=0.0):
    b = future - spot
    out = {"basis": b, "basis_pct": b / spot * 100, "days": days}
    if days > 0:
        out["annualised_pct"] = (future / spot - 1) * 365.0 / days * 100
    if rate is not None and days > 0:
        t = days / 365.0
        fair = spot * math.exp((rate - q) * t)
        out["fair_value"] = fair
        out["vs_fair"] = future - fair
        out["vs_fair_pct"] = (future / fair - 1) * 100
    out["state"] = "contango (future above spot)" if b > 0 else ("backwardation (future below spot)" if b < 0 else "flat")
    return out


def buildup(price_change, oi_change):
    """The four standard readings of price and open interest moving together."""
    if price_change > 0 and oi_change > 0:
        return "long build-up: price up with new positions opening"
    if price_change < 0 and oi_change > 0:
        return "short build-up: price down with new positions opening"
    if price_change > 0 and oi_change < 0:
        return "short covering: price up while positions close"
    if price_change < 0 and oi_change < 0:
        return "long unwinding: price down while positions close"
    return "no change in one of the two: nothing to read"


def parse_leg(text):
    """'buy CE 25500 82 [x2]' or 'sell PE 25000 40' or 'buy FUT 25480'."""
    p = text.split()
    if len(p) < 3:
        raise FnoError("a leg is: buy|sell CE|PE|FUT STRIKE_OR_PRICE [PREMIUM] [xQTY]")
    side = p[0].lower()
    kind = p[1].upper()
    if side not in ("buy", "sell") or kind not in ("CE", "PE", "FUT"):
        raise FnoError("bad leg %r" % text)
    qty = 1.0
    rest = [x for x in p[2:] if not x.lower().startswith("x")]
    for x in p[2:]:
        if x.lower().startswith("x"):
            qty = float(x[1:])
    k = float(rest[0])
    prem = float(rest[1]) if len(rest) > 1 else 0.0
    return {"side": side, "kind": kind, "strike": k, "premium": prem, "qty": qty}


def payoff(legs, s_at_expiry):
    total = 0.0
    for L in legs:
        sign = 1 if L["side"] == "buy" else -1
        if L["kind"] == "FUT":
            total += sign * (s_at_expiry - L["strike"]) * L["qty"]
            continue
        intr = max(0.0, s_at_expiry - L["strike"]) if L["kind"] == "CE" else max(0.0, L["strike"] - s_at_expiry)
        total += sign * (intr - L["premium"]) * L["qty"]
    return total


def payoff_table(legs, spot, lot=1, width=0.15, steps=60):
    lo, hi = spot * (1 - width), spot * (1 + width)
    strikes = sorted({L["strike"] for L in legs})
    pts = sorted(set([lo + (hi - lo) * i / steps for i in range(steps + 1)] + strikes + [spot]))
    vals = [(s, payoff(legs, s) * lot) for s in pts]
    bes = []
    for (a, pa), (b, pb) in zip(vals, vals[1:]):
        if pa == 0:
            bes.append(a)
        elif pa * pb < 0:
            bes.append(a + (b - a) * (-pa) / (pb - pa))
    # tails: slope beyond the range tells if loss/profit is unlimited
    far_up = payoff(legs, spot * 10) * lot - payoff(legs, spot * 5) * lot
    far_dn = payoff(legs, 0.0001) * lot - payoff(legs, spot * 0.01) * lot
    return {"points": vals, "breakevens": sorted(set(round(b, 2) for b in bes)),
            "max_profit": "unlimited" if far_up > 1e-6 else max(v for _, v in vals),
            "max_loss": "unlimited" if far_up < -1e-6 else min(min(v for _, v in vals), payoff(legs, 0.0001) * lot),
            "downside_tail": "rises as price falls" if far_dn < -1e-6 else "flat", "net_premium": -sum(
                (1 if L["side"] == "buy" else -1) * L["premium"] * L["qty"] for L in legs if L["kind"] != "FUT") * lot}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _f(x, nd=2):
    if x is None:
        return "n/a"
    if isinstance(x, str):
        return x
    return "{:,.{}f}".format(x, nd)


def cmd_contract(a):
    lk = M.Lookups(a.data)
    r = M.resolve(a.symbol, lk)
    if r.get("asset_class") not in ("option", "future") and not r.get("near_contract"):
        print("%s is not a derivative contract (it resolves to %s)" % (a.symbol, r.get("asset_class")))
        return 1
    print("%s -> %s (%s on %s, %s)" % (a.symbol, r.get("symbol"), r.get("asset_class"), r.get("exchange"),
                                       "VERIFIED against the scrip master" if r.get("verified") else "NOT verified"))
    for k in ("underlying", "expiry", "strike", "right", "lot_size", "tick_size", "token", "option_style",
              "instrument_type", "multiplier", "near_contract"):
        if r.get(k) not in (None, ""):
            print("  %-15s %s" % (k, r[k]))
    if r.get("expiry"):
        exp = datetime.date(*map(int, r["expiry"].split("-")))
        code = r.get("exchange") if r.get("exchange") in M.EXCHANGES else "NSE"
        t = years_to(exp, exchange=code)
        days = t * 365
        print("  %-15s %.2f calendar days to the %s close" % ("time left", days, code))
        if days < 1:
            print("  EXPIRY DAY: gamma is extreme and the contract settles at today's close")
    for n in r.get("notes", []):
        print("  note: " + n)
    if a.price and r.get("lot_size"):
        print("  %-15s %s x %s = %s %s per lot" % ("notional", _f(a.price), r["lot_size"], _f(a.price * r["lot_size"]),
                                                 r.get("currency") or ""))
    return 0 if r.get("verified") else 1


def cmd_expiries(a):
    return M.main(["expiry", a.underlying] + (["--month", a.month] if a.month else []) + (["--data", a.data] if a.data else []))


def cmd_greeks(a):
    t = a.days / 365.0
    g = bs(a.spot, a.strike, t, a.iv / 100.0, a.rate / 100.0, a.q / 100.0, a.right, a.model)
    if a.json:
        print(json.dumps(g, indent=1))
        return 0
    print("%s %s strike %s, spot %s, %.2f days, IV %.2f%%, rate %.2f%%, yield %.2f%% (%s)" % (
        a.right.upper(), "option", _f(a.strike), _f(a.spot), a.days, a.iv, a.rate, a.q, a.model))
    print("  model value %s" % _f(g["price"], 4))
    print("  delta %.4f  gamma %.6f  theta %s/day  vega %s per 1%% IV  rho %s per 1%% rate" % (
        g["delta"], g["gamma"], _f(g["theta_day"], 4), _f(g["vega_1pct"], 4), _f(g["rho_1pct"], 4)))
    if a.lot:
        print("  per lot of %d: value %s, delta %s units, theta %s/day, vega %s" % (
            a.lot, _f(g["price"] * a.lot), _f(g["delta"] * a.lot), _f(g["theta_day"] * a.lot), _f(g["vega_1pct"] * a.lot)))
    print(NOTE)
    return 0


def cmd_iv(a):
    v = implied_vol(a.price, a.spot, a.strike, a.days / 365.0, a.rate / 100.0, a.q / 100.0, a.right, a.model)
    if v is None:
        print("no volatility gives a price of %s: the premium is below intrinsic value or absurdly high" % a.price)
        return 1
    print("implied volatility %.2f%% (%s, %.2f days, rate %.2f%%)" % (v * 100, a.model, a.days, a.rate))
    return 0


def cmd_chain(a):
    f = MD.Fetcher(offline=a.offline)
    und = a.underlying.upper()
    if a.file:
        rows = read_chain_csv(a.file)
        spot, exp, src = a.spot, a.expiry, "file %s (supplied by you; its time is whatever you exported)" % a.file
        codes = None
    elif und in ("BTC", "ETH", "SOL", "XRP", "BNB", "PAXG"):
        rows, spot, exp, codes, url = deribit_chain(f, und, a.expiry)
        src = "deribit (live) %s" % url
    else:
        print("No free live chain for %s. NSE's website blocks automated cloud requests and Yahoo's chain API "
              "needs a browser session. Export the chain (NSE option-chain page 'Download CSV', or your broker's "
              "chain) and run: fno.py chain %s --file CHAIN.csv --spot PRICE" % (und, und))
        return 1
    st = chain_stats(rows, spot)
    if a.json:
        print(json.dumps({"underlying": und, "expiry": str(exp), "spot": spot, "source": src, "stats": st,
                          "as_of_utc": MD.iso(MD.utc_now())}, indent=1, default=str))
        return 0
    print("%s options, expiry %s, underlying %s  [%s, read %s UTC]" % (und, exp, _f(spot), src, MD.iso(MD.utc_now())))
    if codes:
        print("  other expiries: %s" % ", ".join(codes[:12]))
    print("  open interest: calls %s, puts %s, put/call %s; by volume %s" % (
        _f(st["call_oi"], 0), _f(st["put_oi"], 0), _f(st["pcr_oi"]), _f(st["pcr_volume"])))
    if st.get("call_oi_change") is not None:
        print("  OI change: calls %+.0f, puts %+.0f" % (st["call_oi_change"], st["put_oi_change"]))
    print("  max pain %s (where option buyers in total would collect the least at expiry)" % _f(st["max_pain"]))
    print("  biggest call OI: %s" % ", ".join("%s (%s)" % (_f(r["strike"], 0), _f(r["oi"], 0)) for r in st["top_call_oi"]))
    print("  biggest put OI:  %s" % ", ".join("%s (%s)" % (_f(r["strike"], 0), _f(r["oi"], 0)) for r in st["top_put_oi"]))
    if st.get("atm_iv"):
        print("  ATM strike %s, IV %.2f%%" % (_f(st["atm_strike"], 0), st["atm_iv"]))
        if exp and spot:
            ed = exp if isinstance(exp, datetime.date) else None
            if ed:
                t = years_to(ed, exchange="CRYPTO" if und in ("BTC", "ETH", "SOL", "XRP", "BNB", "PAXG") else "NSE",
                             close=datetime.time(8) if und in ("BTC", "ETH", "SOL", "XRP", "BNB", "PAXG") else None)
                move = spot * st["atm_iv"] / 100 * math.sqrt(t)
                print("  one-standard-deviation move to expiry implied by ATM IV: +/- %s (%.2f%%)" % (_f(move), move / spot * 100))
    if st.get("skew_5pct"):
        s = st["skew_5pct"]
        print("  skew: %s put IV %.2f%% vs %s call IV %.2f%% (puts %+.2f points)" % (
            _f(s["put_strike"], 0), s["put_iv"], _f(s["call_strike"], 0), s["call_iv"], s["put_minus_call"]))
    print("  Reading: high put OI marks levels writers defend below, high call OI above; neither is a promise.")
    return 0


def cmd_basis(a):
    b = basis(a.spot, a.future, a.days, a.rate / 100.0 if a.rate is not None else None, a.q / 100.0)
    print("future %s vs spot %s over %d days: basis %s (%+.3f%%), %s" % (_f(a.future), _f(a.spot), a.days, _f(b["basis"]),
                                                                        b["basis_pct"], b["state"]))
    if "annualised_pct" in b:
        print("  annualised %.2f%%" % b["annualised_pct"])
    if "fair_value" in b:
        print("  cost-of-carry fair value %s; the future is %s (%+.3f%%) from it" % (_f(b["fair_value"]), _f(b["vs_fair"]),
                                                                                    b["vs_fair_pct"]))
    if a.oi_near is not None and a.oi_next is not None:
        tot = a.oi_near + a.oi_next
        print("  rollover: %.1f%% of open interest is already in the next contract" % (a.oi_next / tot * 100 if tot else 0))
    return 0


def cmd_crypto_basis(a):
    f = MD.Fetcher(offline=a.offline)
    ccy = a.underlying.upper()
    url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=%s&kind=future" % ccy
    rows = f.json(url, "deribit", "futs-" + ccy)["result"]
    idx = f.json("https://www.deribit.com/api/v2/public/get_index_price?index_name=%s_usd" % ccy.lower(), "deribit",
                 "index-" + ccy)["result"]["index_price"]
    print("%s futures on Deribit vs its index %s  [live, %s UTC]" % (ccy, _f(idx), MD.iso(MD.utc_now())))
    def _ord(r):
        c = r["instrument_name"].split("-")[1] if "PERPETUAL" not in r["instrument_name"] else ""
        return M._date_from(c[:-5], c[-5:-2], c[-2:]) or datetime.date.max if c else datetime.date.min
    for r in sorted(rows, key=_ord):
        name = r["instrument_name"]
        px = r.get("mark_price")
        if "PERPETUAL" in name:
            print("  %-22s %s  (perpetual: funding keeps it near the index)" % (name, _f(px)))
            continue
        code = name.split("-")[1]
        exp = M._date_from(code[:-5], code[-5:-2], code[-2:])
        days = (exp - M.utc_now().date()).days if exp else 0
        b = basis(idx, px, max(days, 1))
        print("  %-22s %s  basis %+.2f%%, %.2f%% a year, OI %s" % (name, _f(px), b["basis_pct"], b.get("annualised_pct", 0),
                                                               _f(r.get("open_interest"), 0)))
    return 0


def cmd_payoff(a):
    legs = [parse_leg(x) for x in a.leg]
    t = payoff_table(legs, a.spot, a.lot)
    print("strategy at expiry, %d leg(s), lot %s, spot %s" % (len(legs), a.lot, _f(a.spot)))
    for L in legs:
        print("  %s %s %s @ %s x%g" % (L["side"], L["kind"], _f(L["strike"]), _f(L["premium"]), L["qty"]))
    print("  net premium %s (%s)" % (_f(t["net_premium"]), "received" if t["net_premium"] > 0 else "paid"))
    print("  max profit %s, max loss %s, breakevens %s" % (_f(t["max_profit"]), _f(t["max_loss"]),
                                                          ", ".join(_f(b) for b in t["breakevens"]) or "none in range"))
    step = max(1, len(t["points"]) // 12)
    for s, v in t["points"][::step]:
        print("    at %s: %s" % (_f(s), _f(v)))
    print("  Expiry payoff only: before expiry, time value and IV changes move the P&L.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Futures and options analytics.")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("contract")
    c.add_argument("symbol")
    c.add_argument("--price", type=float, help="a price to show the notional per lot")
    c.add_argument("--data")
    e = sub.add_parser("expiries")
    e.add_argument("underlying")
    e.add_argument("--month")
    e.add_argument("--data")
    for name in ("greeks", "iv"):
        g = sub.add_parser(name)
        g.add_argument("--spot", type=float, required=True, help="spot, or the futures price with --model black76")
        g.add_argument("--strike", type=float, required=True)
        g.add_argument("--days", type=float, required=True, help="calendar days to expiry")
        g.add_argument("--right", default="CE", choices=["CE", "PE", "C", "P", "call", "put"])
        g.add_argument("--rate", type=float, default=0.0, help="risk-free rate, percent (India: about the 91-day T-bill)")
        g.add_argument("--q", type=float, default=0.0, help="dividend yield, percent")
        g.add_argument("--model", default="bsm", choices=["bsm", "black76"])
        g.add_argument("--lot", type=int)
        g.add_argument("--json", action="store_true")
        if name == "greeks":
            g.add_argument("--iv", type=float, required=True, help="volatility, percent")
        else:
            g.add_argument("--price", type=float, required=True, help="the option premium")
    ch = sub.add_parser("chain")
    ch.add_argument("underlying")
    ch.add_argument("--expiry", help="Deribit code like 26DEC26 (default: nearest)")
    ch.add_argument("--file", help="a chain CSV you exported")
    ch.add_argument("--spot", type=float)
    ch.add_argument("--json", action="store_true")
    ch.add_argument("--offline")
    b = sub.add_parser("basis")
    b.add_argument("--spot", type=float, required=True)
    b.add_argument("--future", type=float, required=True)
    b.add_argument("--days", type=int, required=True)
    b.add_argument("--rate", type=float)
    b.add_argument("--q", type=float, default=0.0)
    b.add_argument("--oi-near", type=float)
    b.add_argument("--oi-next", type=float)
    cb = sub.add_parser("crypto-basis")
    cb.add_argument("underlying")
    cb.add_argument("--offline")
    bu = sub.add_parser("buildup")
    bu.add_argument("--price-change", type=float, required=True)
    bu.add_argument("--oi-change", type=float, required=True)
    pf = sub.add_parser("payoff")
    pf.add_argument("--spot", type=float, required=True)
    pf.add_argument("--leg", action="append", required=True)
    pf.add_argument("--lot", type=int, default=1)
    a = p.parse_args(argv)
    try:
        if a.cmd == "contract":
            return cmd_contract(a)
        if a.cmd == "expiries":
            return cmd_expiries(a)
        if a.cmd == "greeks":
            return cmd_greeks(a)
        if a.cmd == "iv":
            return cmd_iv(a)
        if a.cmd == "chain":
            return cmd_chain(a)
        if a.cmd == "basis":
            return cmd_basis(a)
        if a.cmd == "crypto-basis":
            return cmd_crypto_basis(a)
        if a.cmd == "buildup":
            print(buildup(a.price_change, a.oi_change))
            return 0
        if a.cmd == "payoff":
            return cmd_payoff(a)
    except (FnoError, MD.DataError, M.MarketError, IOError, OSError) as exc:
        print("fno: %s" % exc, file=sys.stderr)
        return 1
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
