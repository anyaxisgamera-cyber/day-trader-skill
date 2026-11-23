#!/usr/bin/env python3
"""indicators.py: technical analysis computed from real candles, never guessed.

    python3 indicators.py analyze RELIANCE --tf 15m
    python3 indicators.py analyze BTCUSDT --tf 1h --tf 4h --tf 1d     # multi-timeframe confirmation
    python3 indicators.py analyze NIFTY --tf 1d --json

Every indicator is a plain function over a list of bars
{"t", "o", "h", "l", "c", "v"} (oldest first) and returns None where there
are not enough bars yet, so a value is never padded or invented.

Conventions (the common ones, stated so they can be checked):
  SMA/EMA      EMA seeded with the SMA of the first n closes, alpha = 2/(n+1)
  RSI(14)      Wilder smoothing (alpha = 1/n), seeded with the simple average gain/loss
  MACD         EMA(12) - EMA(26), signal EMA(9) of MACD, histogram = MACD - signal
  Bollinger    SMA(20) +/- 2 population standard deviations
  ATR(14)      Wilder smoothing of the true range
  ADX(14)      Wilder's +DI, -DI, DX and ADX
  VWAP         anchored at each session (or UTC day for 24/7 markets), typical price (h+l+c)/3
  Hist. vol    standard deviation of log returns, annualised by sqrt(periods a year)
"""

from __future__ import print_function

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# --------------------------------------------------------------------------
# Moving averages and oscillators
# --------------------------------------------------------------------------

def closes(bars):
    return [b["c"] for b in bars]


def sma(values, n):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def _wilder(values, n, start):
    """Wilder's running average of values[start:], seeded by the simple mean of the first n."""
    out = [None] * len(values)
    if len(values) - start < n:
        return out
    a = sum(values[start:start + n]) / n
    out[start + n - 1] = a
    for i in range(start + n, len(values)):
        a = (a * (n - 1) + values[i]) / n
        out[i] = a
    return out


def rsi(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [0.0] + [max(0.0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [0.0] + [max(0.0, values[i - 1] - values[i]) for i in range(1, len(values))]
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    for i in range(len(values)):
        if ag[i] is None:
            continue
        out[i] = 100.0 if al[i] == 0 else 100.0 - 100.0 / (1 + ag[i] / al[i])
    return out


def macd(values, fast=12, slow=26, signal=9):
    ef, es = ema(values, fast), ema(values, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig = [None] * len(values)
    if first is not None:
        tail = ema(line[first:], signal)
        sig[first:] = tail
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(values, n=20, k=2.0):
    mid = sma(values, n)
    up, lo, width = [None] * len(values), [None] * len(values), [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        up[i], lo[i] = m + k * sd, m - k * sd
        width[i] = (up[i] - lo[i]) / m if m else None
    return mid, up, lo, width


def true_range(bars):
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            out.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return out


def atr(bars, n=14):
    tr = true_range(bars)
    return _wilder(tr, n, 1) if len(tr) > n else [None] * len(tr)


def adx(bars, n=14):
    """(adx, plus_di, minus_di) lists."""
    L = len(bars)
    pdm, mdm = [0.0] * L, [0.0] * L
    for i in range(1, L):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        pdm[i] = up if up > dn and up > 0 else 0.0
        mdm[i] = dn if dn > up and dn > 0 else 0.0
    tr = true_range(bars)
    if L <= 2 * n:
        return [None] * L, [None] * L, [None] * L
    # Wilder sums (not averages) for TR and DM; the ratio is the same
    atr_ = _wilder(tr, n, 1)
    pdi_s, mdi_s = _wilder(pdm, n, 1), _wilder(mdm, n, 1)
    pdi, mdi, dx = [None] * L, [None] * L, [None] * L
    for i in range(L):
        if atr_[i]:
            pdi[i] = 100.0 * pdi_s[i] / atr_[i]
            mdi[i] = 100.0 * mdi_s[i] / atr_[i]
            s = pdi[i] + mdi[i]
            dx[i] = 100.0 * abs(pdi[i] - mdi[i]) / s if s else 0.0
    first = next(i for i, v in enumerate(dx) if v is not None)
    adx_ = [None] * L
    tail = _wilder(dx, n, first)
    for i in range(L):
        adx_[i] = tail[i]
    return adx_, pdi, mdi


def vwap(bars, session_key):
    """Session-anchored VWAP; session_key(bar) says which session a bar is in."""
    out, pv, vv, cur = [], 0.0, 0.0, None
    for b in bars:
        k = session_key(b)
        if k != cur:
            pv, vv, cur = 0.0, 0.0, k
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * (b["v"] or 0)
        vv += b["v"] or 0
        out.append(pv / vv if vv else None)
    return out


def hist_vol(values, periods_per_year):
    rets = [math.log(values[i] / values[i - 1]) for i in range(1, len(values)) if values[i - 1] > 0 and values[i] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return sd * math.sqrt(periods_per_year)


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------

def pivots_classic(h, l, c):
    p = (h + l + c) / 3.0
    return {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + (h - l), "S2": p - (h - l),
            "R3": h + 2 * (p - l), "S3": l - 2 * (h - p)}


def fibonacci(high, low, trend="up"):
    """Retracement levels of the swing; in an up-move they are measured down from the high."""
    rng = high - low
    ratios = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
    if trend == "up":
        return {("%.1f%%" % (r * 100)): high - rng * r for r in ratios}
    return {("%.1f%%" % (r * 100)): low + rng * r for r in ratios}


def swing_points(bars, k=3):
    """Fractal swings: a high higher than the k bars on each side (and lows alike)."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(h > bars[j]["h"] for j in range(i - k, i + k + 1) if j != i):
            highs.append((i, h))
        if all(l < bars[j]["l"] for j in range(i - k, i + k + 1) if j != i):
            lows.append((i, l))
    return highs, lows


def support_resistance(bars, k=3, tolerance=0.004, top=4):
    """Cluster swing points within `tolerance` (fraction of price); a level touched
    more often ranks higher. Returns levels below (support) and above (resistance) the last close."""
    highs, lows = swing_points(bars, k)
    pts = sorted([p for _, p in highs] + [p for _, p in lows])
    clusters = []
    for p in pts:
        if clusters and abs(p - sum(clusters[-1]) / len(clusters[-1])) / p <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    last = bars[-1]["c"]
    levels = [{"price": sum(c) / len(c), "touches": len(c)} for c in clusters]
    sup = sorted([x for x in levels if x["price"] < last], key=lambda x: (-x["touches"], last - x["price"]))[:top]
    res = sorted([x for x in levels if x["price"] > last], key=lambda x: (-x["touches"], x["price"] - last))[:top]
    return sorted(sup, key=lambda x: -x["price"]), sorted(res, key=lambda x: x["price"])


def volume_profile(bars, bins=24, value_area=0.70):
    lo, hi = min(b["l"] for b in bars), max(b["h"] for b in bars)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for b in bars:
        # spread each bar's volume evenly over the price bins its range covers
        a = int((b["l"] - lo) / step)
        z = min(bins - 1, int((b["h"] - lo) / step))
        a = min(a, bins - 1)
        share = (b["v"] or 0) / (z - a + 1)
        for i in range(a, z + 1):
            vol[i] += share
    total = sum(vol)
    if total <= 0:
        return None
    poc = max(range(bins), key=lambda i: vol[i])
    inside, lo_i, hi_i = vol[poc], poc, poc
    while inside / total < value_area and (lo_i > 0 or hi_i < bins - 1):
        down = vol[lo_i - 1] if lo_i > 0 else -1
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            inside += vol[hi_i]
        else:
            lo_i -= 1
            inside += vol[lo_i]
    mid = lambda i: lo + step * (i + 0.5)  # noqa: E731
    return {"poc": mid(poc), "value_area_low": lo + step * lo_i, "value_area_high": lo + step * (hi_i + 1),
            "bins": [{"price": mid(i), "volume": vol[i]} for i in range(bins)]}


# --------------------------------------------------------------------------
# Patterns and structure
# --------------------------------------------------------------------------

def candle_patterns(bars):
    """Named patterns on the last completed bars. Each is a fact about shapes,
    not a signal: the name says what the candles look like, nothing more."""
    out = []
    if len(bars) < 3:
        return out
    a, b, c = bars[-3], bars[-2], bars[-1]

    def body(x):
        return abs(x["c"] - x["o"])

    def rng(x):
        return max(1e-12, x["h"] - x["l"])

    def upper(x):
        return x["h"] - max(x["o"], x["c"])

    def lower(x):
        return min(x["o"], x["c"]) - x["l"]

    if body(c) <= 0.1 * rng(c):
        out.append("doji")
    if lower(c) >= 2 * body(c) and upper(c) <= 0.3 * max(body(c), 1e-12) + 0.1 * rng(c) and body(c) > 0.05 * rng(c):
        out.append("hammer shape (long lower wick)")
    if upper(c) >= 2 * body(c) and lower(c) <= 0.3 * max(body(c), 1e-12) + 0.1 * rng(c) and body(c) > 0.05 * rng(c):
        out.append("shooting-star shape (long upper wick)")
    if b["c"] < b["o"] and c["c"] > c["o"] and c["c"] >= b["o"] and c["o"] <= b["c"] and body(c) > body(b):
        out.append("bullish engulfing")
    if b["c"] > b["o"] and c["c"] < c["o"] and c["o"] >= b["c"] and c["c"] <= b["o"] and body(c) > body(b):
        out.append("bearish engulfing")
    if c["h"] <= b["h"] and c["l"] >= b["l"]:
        out.append("inside bar")
    if c["h"] > b["h"] and c["l"] < b["l"]:
        out.append("outside bar")
    if body(c) >= 0.95 * rng(c):
        out.append("marubozu (%s)" % ("bullish" if c["c"] > c["o"] else "bearish"))
    small_mid = body(b) <= 0.3 * body(a)
    if a["c"] < a["o"] and small_mid and c["c"] > c["o"] and c["c"] > (a["o"] + a["c"]) / 2:
        out.append("morning star")
    if a["c"] > a["o"] and small_mid and c["c"] < c["o"] and c["c"] < (a["o"] + a["c"]) / 2:
        out.append("evening star")
    return out


def market_structure(bars, k=3):
    """Higher highs / higher lows (up), lower highs / lower lows (down), or mixed;
    plus a break of structure when the last close crosses the latest swing."""
    highs, lows = swing_points(bars, k)
    res = {"structure": "not enough swings", "last_swing_high": None, "last_swing_low": None, "break": None}
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        res["structure"] = ("uptrend (higher highs, higher lows)" if hh and hl else
                            "downtrend (lower highs, lower lows)" if not hh and not hl else
                            "mixed (%s highs, %s lows)" % ("higher" if hh else "lower", "higher" if hl else "lower"))
    if highs:
        res["last_swing_high"] = highs[-1][1]
        if bars[-1]["c"] > highs[-1][1]:
            res["break"] = "close above the last swing high (bullish break of structure)"
    if lows:
        res["last_swing_low"] = lows[-1][1]
        if bars[-1]["c"] < lows[-1][1]:
            res["break"] = "close below the last swing low (bearish break of structure)"
    return res


def breakout(bars, n=20):
    """Close above the prior n-bar high, or below the prior n-bar low."""
    if len(bars) <= n:
        return None
    prior = bars[-n - 1:-1]
    hi, lo = max(b["h"] for b in prior), min(b["l"] for b in prior)
    c = bars[-1]["c"]
    if c > hi:
        return {"type": "breakout", "level": hi, "by_pct": (c / hi - 1) * 100}
    if c < lo:
        return {"type": "breakdown", "level": lo, "by_pct": (c / lo - 1) * 100}
    return {"type": "inside range", "high": hi, "low": lo}


# --------------------------------------------------------------------------
# One report per timeframe
# --------------------------------------------------------------------------

PERIODS_PER_YEAR = {"equity_day": 252, "crypto_day": 365}


def _last(xs):
    for v in reversed(xs):
        if v is not None:
            return v
    return None


def analyse(bars, tf="1d", session_key=None, periods_per_year=252, drop_forming=True):
    """Everything above on one bar series, as plain numbers."""
    if drop_forming and bars and bars[-1].get("forming"):
        bars = bars[:-1]
    n = len(bars)
    if n < 30:
        return {"tf": tf, "bars": n, "error": "only %d completed bars: too few for the indicators (need 30+)" % n}
    cl = closes(bars)
    m_line, m_sig, m_hist = macd(cl)
    bb_mid, bb_up, bb_lo, bb_w = bollinger(cl)
    adx_, pdi, mdi = adx(bars)
    e9, e20, e50, e200 = ema(cl, 9), ema(cl, 20), ema(cl, 50), ema(cl, 200)
    s50, s200 = sma(cl, 50), sma(cl, 200)
    intraday = tf not in ("1d", "1w", "1M")
    vw = vwap(bars, session_key or (lambda b: b["t"] // 86400)) if intraday else [None]
    sup, res = support_resistance(bars)
    swing_hi = max(b["h"] for b in bars[-60:])
    swing_lo = min(b["l"] for b in bars[-60:])
    last = bars[-1]
    vol20 = sma([b["v"] or 0 for b in bars], 20)
    trend = None
    if _last(e20) is not None and _last(e50) is not None:
        if cl[-1] > _last(e20) > _last(e50):
            trend = "up (close > EMA20 > EMA50)"
        elif cl[-1] < _last(e20) < _last(e50):
            trend = "down (close < EMA20 < EMA50)"
        else:
            trend = "sideways or turning (EMAs not stacked)"
    out = {
        "tf": tf, "bars": n, "last_close": cl[-1], "last_bar_t": last["t"],
        "sma50": _last(s50), "sma200": _last(s200) if n >= 200 else None,
        "ema9": _last(e9), "ema20": _last(e20), "ema50": _last(e50), "ema200": _last(e200) if n >= 200 else None,
        "rsi14": _last(rsi(cl)), "macd": _last(m_line), "macd_signal": _last(m_sig), "macd_hist": _last(m_hist),
        "bb_upper": _last(bb_up), "bb_mid": _last(bb_mid), "bb_lower": _last(bb_lo), "bb_width": _last(bb_w),
        "atr14": _last(atr(bars)), "adx14": _last(adx_), "plus_di": _last(pdi), "minus_di": _last(mdi),
        "vwap": vw[-1], "volume": last["v"], "volume_vs_20avg": (last["v"] / _last(vol20)) if _last(vol20) else None,
        "hist_vol_annual": hist_vol(cl[-61:], periods_per_year),
        "trend": trend, "adx_reading": None,
        "support": sup, "resistance": res,
        "fibonacci_60bar": fibonacci(swing_hi, swing_lo, "up" if cl[-1] >= (swing_hi + swing_lo) / 2 else "down"),
        "pivots_from_prev_bar": pivots_classic(bars[-2]["h"], bars[-2]["l"], bars[-2]["c"]),
        "volume_profile": volume_profile(bars[-120:]),
        "patterns": candle_patterns(bars),
        "structure": market_structure(bars),
        "breakout20": breakout(bars, 20),
    }
    if out["adx14"] is not None:
        out["adx_reading"] = "trending" if out["adx14"] >= 25 else ("weak trend" if out["adx14"] >= 20 else "no trend")
    if out["volume_profile"]:
        out["volume_profile"].pop("bins", None)
    return out


def confirm(reports):
    """Multi-timeframe confirmation: do the trends agree?"""
    votes = []
    for r in reports:
        t = (r.get("trend") or "")
        votes.append(1 if t.startswith("up") else -1 if t.startswith("down") else 0)
    if not votes:
        return "no timeframes"
    if all(v == 1 for v in votes):
        return "aligned up on every timeframe"
    if all(v == -1 for v in votes):
        return "aligned down on every timeframe"
    return "mixed: " + ", ".join("%s %s" % (r["tf"], {1: "up", -1: "down", 0: "sideways"}[v])
                                 for r, v in zip(reports, votes))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _f(x, nd=2):
    if x is None:
        return "n/a"
    return ("{:,.%df}" % nd).format(x)


def print_report(r, env):
    if r.get("error"):
        print("[%s] %s" % (r["tf"], r["error"]))
        return
    print("[%s] %d bars, last close %s" % (r["tf"], r["bars"], _f(r["last_close"], 4)))
    print("  trend %s; ADX %s (%s), +DI %s / -DI %s" % (r["trend"], _f(r["adx14"]), r["adx_reading"],
                                                        _f(r["plus_di"]), _f(r["minus_di"])))
    print("  EMA9 %s  EMA20 %s  EMA50 %s  EMA200 %s  SMA200 %s" % (
        _f(r["ema9"]), _f(r["ema20"]), _f(r["ema50"]), _f(r["ema200"]), _f(r["sma200"])))
    print("  RSI14 %s  MACD %s / signal %s / hist %s" % (_f(r["rsi14"]), _f(r["macd"], 4), _f(r["macd_signal"], 4),
                                                         _f(r["macd_hist"], 4)))
    print("  Bollinger %s / %s / %s (width %s)  ATR14 %s  VWAP %s" % (
        _f(r["bb_lower"]), _f(r["bb_mid"]), _f(r["bb_upper"]), _f(r["bb_width"], 4), _f(r["atr14"]), _f(r["vwap"])))
    vp = r.get("volume_profile") or {}
    print("  volume %s (%sx the 20-bar average)  volume POC %s, value area %s-%s" % (
        _f(r["volume"], 0), _f(r["volume_vs_20avg"]), _f(vp.get("poc")), _f(vp.get("value_area_low")),
        _f(vp.get("value_area_high"))))
    print("  support %s" % (", ".join("%s (x%d)" % (_f(s["price"]), s["touches"]) for s in r["support"]) or "none found"))
    print("  resistance %s" % (", ".join("%s (x%d)" % (_f(s["price"]), s["touches"]) for s in r["resistance"]) or "none found"))
    st = r["structure"]
    print("  structure: %s%s" % (st["structure"], ("; " + st["break"]) if st.get("break") else ""))
    b = r.get("breakout20") or {}
    if b.get("type") in ("breakout", "breakdown"):
        print("  20-bar %s through %s (%+.2f%%)" % (b["type"], _f(b["level"]), b["by_pct"]))
    if r["patterns"]:
        print("  candles: " + ", ".join(r["patterns"]))
    if r.get("hist_vol_annual"):
        print("  historical volatility (60 bars, annualised) %.1f%%" % (r["hist_vol_annual"] * 100))


def main(argv=None):
    import markets as M
    import marketdata as MD
    p = argparse.ArgumentParser(description="Technical analysis from real candles.")
    sub = p.add_subparsers(dest="cmd")
    a_ = sub.add_parser("analyze")
    a_.add_argument("symbol")
    a_.add_argument("--tf", action="append", choices=MD.TIMEFRAMES)
    a_.add_argument("--bars", type=int, default=300)
    a_.add_argument("--venue")
    a_.add_argument("--json", action="store_true")
    a_.add_argument("--offline")
    a = p.parse_args(argv)
    if a.cmd != "analyze":
        p.print_help()
        return 1
    f = MD.Fetcher(offline=a.offline)
    inst = M.resolve(a.symbol, M.Lookups())
    if not inst.get("asset_class"):
        print("unrecognised: %s" % "; ".join(inst.get("notes", [])))
        return 1
    tz = inst.get("tz") or "UTC"
    crypto = inst["asset_class"] == "crypto"
    reports, envs = [], []
    for tf in a.tf or ["1d"]:
        env = MD.candles(inst, tf, a.bars, a.venue, f)
        envs.append(env)
        if env.get("error"):
            reports.append({"tf": tf, "error": "no data: " + env["error"]})
            continue
        off = M.utc_offset(tz, M.utc_now()) * 3600 if tz in M.ZONES else 0
        key = (lambda b, o=off: (b["t"] + o) // 86400)
        ppy = {"1d": 365 if crypto else 252, "1w": 52, "1M": 12}.get(tf)
        if ppy is None:
            per_day = (86400 if crypto else 6.25 * 3600) / MD.TF_SECONDS[tf]
            ppy = per_day * (365 if crypto else 252)
        r = analyse(env["data"], tf, key, ppy)
        r["source"] = {k: env.get(k) for k in ("provider", "instrument", "currency", "as_of_utc", "freshness",
                                               "observed_lag_s", "limitations")}
        reports.append(r)
    verdict = confirm([r for r in reports if not r.get("error")])
    if a.json:
        print(json.dumps({"instrument": inst, "reports": reports, "multi_timeframe": verdict}, indent=1, default=str))
        return 0
    print("%s -> %s (%s, %s, %s)" % (a.symbol, inst.get("symbol"), inst.get("asset_class"), inst.get("exchange"),
                                      inst.get("currency")))
    for r, env in zip(reports, envs):
        if not r.get("error"):
            s = r["source"]
            print("  data: %s %s, %s, as of %s UTC, %s" % (s["provider"], s["instrument"], s["currency"],
                                                           s["as_of_utc"], s["freshness"]))
            for lim in s.get("limitations") or []:
                print("  limit: " + lim)
        print_report(r, env)
    if len(reports) > 1:
        print("multi-timeframe: " + verdict)
    print("These are measurements of past prices, not predictions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
