#!/usr/bin/env python3
"""marketdata.py: one door to every free market-data source, with provenance.

Every answer is an envelope that says where the numbers came from:

    {"provider": "okx", "instrument": "BTC-USDT", "exchange": "CRYPTO",
     "kind": "candles", "interval": "15m", "currency": "USDT", "tz": "UTC",
     "fetched_utc": "...", "as_of_utc": "...", "as_of_local": "...",
     "freshness": "real-time" | "delayed" | "end-of-day" | "historical" | "derived",
     "observed_lag_s": 42, "limitations": [...], "url": "...", "data": ...}

    python3 marketdata.py quote RELIANCE NIFTY AAPL BTCUSDT USDINR GOLD
    python3 marketdata.py candles TCS --tf 15m --bars 60
    python3 marketdata.py candles BTCUSDT --tf 4h --venue okx
    python3 marketdata.py book ETHUSDT --venue kraken --depth 10
    python3 marketdata.py fx 100000 INR USD

Sources (all free and keyless, read-only):
  Yahoo Finance chart API   stocks, indices, ETFs, FX, commodity futures, crypto (unofficial, can lag or 429)
  Binance public data       spot candles, trades and books (data-api.binance.vision; api.binance.com and the
                            futures API answer HTTP 451 from US cloud servers)
  Coinbase Exchange, Kraken, OKX, KuCoin   spot candles, tickers, books; OKX also perpetuals
  Frankfurter (ECB)         daily reference FX rates, published around 16:00 CET on TARGET days

Nothing here makes up a number. When a source fails the envelope says so
("error"), and the caller is expected to report the gap, not fill it.
"""

from __future__ import print_function

import argparse
import datetime
import gzip
import json
import os
import sys
import time

try:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import markets as M  # noqa: E402

UA = "Mozilla/5.0 (compatible; day-trader-skill/3.0)"
PAUSE = 0.35
EPOCH = datetime.datetime(1970, 1, 1)

TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400,
              "1d": 86400, "1w": 604800, "1M": 2592000}

# How fresh each provider's numbers are, by its own terms.
PROVIDER_FRESHNESS = {
    "yahoo": ("delayed", "Yahoo's chart API is unofficial and unlicensed for redistribution. Many exchanges "
                         "(including NSE/BSE) reach it delayed; check observed_lag_s, and check your broker "
                         "before acting."),
    "binance": ("real-time", "public market data from data-api.binance.vision (spot only)"),
    "coinbase": ("real-time", "Coinbase Exchange public API"),
    "kraken": ("real-time", "Kraken public API"),
    "okx": ("real-time", "OKX public API"),
    "kucoin": ("real-time", "KuCoin public API"),
    "frankfurter": ("end-of-day", "ECB euro reference rates via Frankfurter, once a day around 16:00 CET; "
                                  "not a tradable price"),
}


class DataError(Exception):
    pass


def utc_now():
    return M.utc_now()


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def ts_utc(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


# --------------------------------------------------------------------------
# HTTP with an offline fixture mode
# --------------------------------------------------------------------------

class Fetcher(object):
    """GET JSON or text. With `offline` set to a folder it reads
    <folder>/<provider>-<key>.json instead and never touches the network."""

    def __init__(self, offline=None, pause=PAUSE, opener=None):
        self.offline = offline
        self.pause = pause
        self.opener = opener or urlopen
        self.calls = 0

    def get(self, url, provider, key, timeout=20, retries=1):
        if self.offline:
            for ext in (".json", ".json.gz", ".txt"):
                path = os.path.join(self.offline, "%s-%s%s" % (provider, key, ext))
                if os.path.exists(path):
                    op = gzip.open if ext.endswith(".gz") else open
                    with op(path, "rb") as fh:
                        return fh.read().decode("utf-8")
            raise DataError("offline: no file %s-%s.json in %s" % (provider, key, self.offline))
        if self.calls and self.pause:
            time.sleep(self.pause)
        self.calls += 1
        last = None
        for attempt in range(retries + 1):
            try:
                req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                resp = self.opener(req, timeout=timeout)
                data = resp.read()
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data.decode("utf-8")
            except HTTPError as exc:
                last = "HTTP %s from %s" % (exc.code, url.split("/")[2])
                if exc.code == 451:
                    raise DataError(last + " (blocked for this server's region)")
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(3.0)
                    continue
                raise DataError(last)
            except (URLError, OSError) as exc:
                last = "%s: %s" % (url.split("/")[2], getattr(exc, "reason", exc))
                if attempt < retries:
                    time.sleep(1.5)
                    continue
        raise DataError(last or "request failed")

    def json(self, url, provider, key, **kw):
        text = self.get(url, provider, key, **kw)
        try:
            return json.loads(text)
        except ValueError:
            raise DataError("%s answered with something that is not JSON" % provider)


def envelope(provider, instrument, kind, data, **kw):
    fresh, limit = PROVIDER_FRESHNESS.get(provider, ("unknown", ""))
    env = {"provider": provider, "instrument": instrument, "kind": kind, "fetched_utc": iso(utc_now()),
           "freshness": kw.pop("freshness", fresh), "limitations": [limit] if limit else [], "data": data}
    env.update(kw)
    return env


def failed(provider, instrument, kind, why):
    return {"provider": provider, "instrument": instrument, "kind": kind, "fetched_utc": iso(utc_now()),
            "error": str(why), "data": None, "freshness": "unavailable",
            "limitations": ["no data: %s" % why]}


def _stamp(env, last_ts, interval_s, tz, last_trade_ts=None):
    """as_of = the provider's last trade time when it gives one, else the end
    of the last bar; lag = now - as_of. A bar whose end is still in the future
    is flagged as forming (its numbers will change)."""
    if last_ts is None:
        return env
    end = ts_utc(last_ts + (interval_s or 0))
    now = utc_now()
    if end > now and env.get("data") and isinstance(env["data"], list):
        env["data"][-1]["forming"] = True
        env["last_bar_forming"] = True
    if last_trade_ts:
        end = ts_utc(last_trade_ts)
    env["as_of_utc"] = iso(min(end, now))
    env["as_of_local"] = iso(M.to_local(min(end, now), tz)) if tz in M.ZONES else None
    env["tz"] = tz
    env["observed_lag_s"] = max(0, int((now - end).total_seconds()))
    return env


# --------------------------------------------------------------------------
# Candles
# --------------------------------------------------------------------------

def resample(bars, seconds, anchor=0):
    """Combine bars into `seconds`-long buckets aligned to `anchor` (epoch s).
    A bucket's open time is its first bar's bucket start."""
    out = []
    for b in bars:
        k = (b["t"] - anchor) // seconds * seconds + anchor
        if out and out[-1]["t"] == k:
            o = out[-1]
            o["h"] = max(o["h"], b["h"])
            o["l"] = min(o["l"], b["l"])
            o["c"] = b["c"]
            o["v"] = (o["v"] or 0) + (b["v"] or 0)
            o["n"] += 1
        else:
            out.append({"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"] or 0, "n": 1})
    for o in out:
        o.pop("n", None)
    return out


YAHOO_TF = {"1m": ("1m", "5d"), "3m": ("1m", "5d"), "5m": ("5m", "1mo"), "15m": ("15m", "1mo"),
            "30m": ("30m", "1mo"), "1h": ("60m", "3mo"), "4h": ("60m", "6mo"), "1d": ("1d", "2y"),
            "1w": ("1wk", "5y"), "1M": ("1mo", "10y")}


def parse_yahoo_chart(text):
    doc = json.loads(text) if isinstance(text, str) else text
    chart = doc.get("chart") or {}
    if chart.get("error"):
        raise DataError("Yahoo: %s" % (chart["error"].get("description") or chart["error"]))
    res = (chart.get("result") or [None])[0]
    if not res:
        raise DataError("Yahoo returned no result")
    meta = res.get("meta", {})
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        bars.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": (q.get("volume") or [0] * len(ts))[i] or 0})
    return meta, bars


def yahoo_candles(f, ysym, tf, rng=None):
    yi, yr = YAHOO_TF[tf]
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false" % (
        ysym.replace("^", "%5E"), rng or yr, yi)
    meta, bars = parse_yahoo_chart(f.get(url, "yahoo", "%s-%s-%s" % (ysym, tf, rng or yr)))
    gmt = int(meta.get("gmtoffset") or 0)
    step = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600}.get(yi)
    if step and len(bars) > 1 and (bars[-1]["t"] + gmt) % step:
        bars = bars[:-1]  # Yahoo appends the latest tick as an off-grid bar: drop it, the grid bar has it
    if tf == "3m":
        bars = resample(bars, 180, anchor=0)
    elif tf == "4h":
        # anchor 4-hour buckets to the exchange's local midnight, not UTC
        bars = resample(bars, 14400, anchor=-gmt)
    return meta, bars, url


def _num(x):
    return float(x) if x not in (None, "") else None


CRYPTO_TF = {
    "binance": {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h",
                "1d": "1d", "1w": "1w", "1M": "1M"},
    "okx": {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H",
            "1d": "1Dutc", "1w": "1Wutc", "1M": "1Mutc"},
    "coinbase": {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400},
    "kraken": {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080},
    "kucoin": {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1hour",
               "4h": "4hour", "1d": "1day", "1w": "1week"},
}


def crypto_candles(f, venue, inst, tf, bars=200):
    """inst is the resolver's dict for a crypto pair."""
    base, quote = inst["base"], inst["quote"]
    perp = inst.get("instrument") == "perpetual"
    if tf not in CRYPTO_TF.get(venue, {}):
        raise DataError("%s has no %s candles" % (venue, tf))
    code = CRYPTO_TF[venue][tf]
    usdq = quote if quote != "USD" else "USDT"
    if venue == "binance":
        if perp:
            raise DataError("Binance futures (fapi.binance.com) answers HTTP 451 from this server's region; "
                            "use --venue okx for perpetuals")
        sym = base + usdq
        url = "https://data-api.binance.vision/api/v3/klines?symbol=%s&interval=%s&limit=%d" % (sym, code, min(bars, 1000))
        rows = f.json(url, "binance", "%s-%s" % (sym, tf))
        if isinstance(rows, dict):
            raise DataError("Binance: %s" % rows.get("msg"))
        out = [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                "v": float(r[5]), "quote_volume": float(r[7]), "trades": int(r[8])} for r in rows]
        return out, url, sym
    if venue == "okx":
        sym = "%s-%s%s" % (base, usdq, "-SWAP" if perp else "")
        url = "https://www.okx.com/api/v5/market/candles?instId=%s&bar=%s&limit=%d" % (sym, code, min(bars, 300))
        doc = f.json(url, "okx", "%s-%s" % (sym, tf))
        if doc.get("code") not in ("0", 0):
            raise DataError("OKX: %s" % doc.get("msg"))
        out = [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                "v": float(r[5]), "confirmed": r[8] == "1"} for r in doc.get("data", [])]
        return sorted(out, key=lambda b: b["t"]), url, sym
    if venue == "coinbase":
        if perp:
            raise DataError("Coinbase Exchange lists spot only here")
        sym = "%s-%s" % (base, "USD" if quote in ("USD", "USDT") else quote)
        url = "https://api.exchange.coinbase.com/products/%s/candles?granularity=%d" % (sym, code)
        rows = f.json(url, "coinbase", "%s-%s" % (sym, tf))
        if isinstance(rows, dict):
            raise DataError("Coinbase: %s" % rows.get("message"))
        out = [{"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4]),
                "v": float(r[5])} for r in rows]
        return sorted(out, key=lambda b: b["t"])[-bars:], url, sym
    if venue == "kraken":
        if perp:
            raise DataError("Kraken futures are not wired in; use --venue okx for perpetuals")
        sym = ("XBT" if base == "BTC" else base) + ("USD" if quote in ("USD", "USDT") else quote)
        url = "https://api.kraken.com/0/public/OHLC?pair=%s&interval=%d" % (sym, code)
        doc = f.json(url, "kraken", "%s-%s" % (sym, tf))
        if doc.get("error"):
            raise DataError("Kraken: %s" % ", ".join(doc["error"]))
        key = [k for k in doc["result"] if k != "last"][0]
        out = [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                "v": float(r[6]), "vwap": float(r[5]), "trades": int(r[7])} for r in doc["result"][key]]
        return out[-bars:], url, key
    if venue == "kucoin":
        if perp:
            raise DataError("KuCoin futures are not wired in; use --venue okx for perpetuals")
        sym = "%s-%s" % (base, usdq)
        url = "https://api.kucoin.com/api/v1/market/candles?type=%s&symbol=%s" % (code, sym)
        doc = f.json(url, "kucoin", "%s-%s" % (sym, tf))
        if doc.get("code") != "200000":
            raise DataError("KuCoin: %s" % doc.get("msg"))
        out = [{"t": int(r[0]), "o": float(r[1]), "c": float(r[2]), "h": float(r[3]), "l": float(r[4]),
                "v": float(r[5])} for r in doc.get("data", [])]
        return sorted(out, key=lambda b: b["t"])[-bars:], url, sym
    raise DataError("unknown venue %r" % venue)


CRYPTO_VENUES = ("binance", "okx", "coinbase", "kraken", "kucoin")


def candles(symbol, tf="1d", bars=200, venue=None, fetcher=None, lookups=None):
    """Candles for any instrument, from the right provider, in an envelope."""
    if tf not in TF_SECONDS:
        raise DataError("timeframe %r: use one of %s" % (tf, ", ".join(TIMEFRAMES)))
    f = fetcher or Fetcher()
    inst = symbol if isinstance(symbol, dict) else M.resolve(symbol, lookups if lookups is not None else M.Lookups())
    name = inst.get("symbol") or inst.get("input")
    if not inst.get("asset_class"):
        return failed("none", name, "candles", "unrecognised instrument: " + "; ".join(inst.get("notes", [])))
    if inst["asset_class"] == "crypto" and (venue or "") != "yahoo":
        order = [venue] if venue else list(CRYPTO_VENUES)
        errs = []
        for v in order:
            try:
                rows, url, vsym = crypto_candles(f, v, inst, tf, bars)
                if not rows:
                    raise DataError("no candles")
                env = envelope(v, vsym, "candles", rows[-bars:], interval=tf, exchange="CRYPTO",
                               currency=inst["quote"] if v not in ("coinbase", "kraken") or inst["quote"] != "USDT" else "USD",
                               url=url, resolved=_slim(inst))
                if errs:
                    env["limitations"].append("tried first: " + "; ".join(errs))
                return _stamp(env, rows[-1]["t"], TF_SECONDS[tf], "UTC")
            except DataError as exc:
                errs.append("%s: %s" % (v, exc))
        return failed(order[0], name, "candles", " | ".join(errs))
    ysym = inst.get("yahoo") or inst.get("yahoo_proxy")
    if not ysym:
        why = "no free price source for this instrument"
        if inst["asset_class"] in ("option", "future") and inst.get("exchange") in ("NFO", "BFO", "MCX", "CDS"):
            why = ("Indian derivative prices need a broker feed (Angel One SmartAPI, logged in by you) or NSE's "
                   "site, which blocks automated cloud requests; contract specs are available, prices are not")
        return failed("none", name, "candles", why)
    try:
        meta, rows, url = yahoo_candles(f, ysym, tf)
    except DataError as exc:
        return failed("yahoo", ysym, "candles", exc)
    if not rows:
        return failed("yahoo", ysym, "candles", "Yahoo returned no bars")
    tz = inst.get("tz") or "UTC"
    env = envelope("yahoo", ysym, "candles", rows[-bars:], interval=tf, exchange=inst.get("exchange"),
                   currency=meta.get("currency") or inst.get("currency"), url=url, resolved=_slim(inst),
                   yahoo_exchange=meta.get("fullExchangeName"), yahoo_tz=meta.get("exchangeTimezoneName"))
    if inst.get("yahoo_proxy") and not inst.get("yahoo"):
        env["proxy_for"] = inst.get("near_contract") or inst.get("symbol")
        env["limitations"].append("PROXY: %s is not the %s contract. It is the global benchmark (%s), in its own "
                                  "currency and unit." % (ysym, inst.get("exchange"), inst.get("proxy_description")))
    if tf in ("3m", "4h"):
        env["freshness_note"] = "%s bars are built here from Yahoo %s bars" % (tf, YAHOO_TF[tf][0])
    if meta.get("currency") in ("GBp", "ZAc", "ILA"):
        env["limitations"].append("prices are in %s (hundredths of the currency)" % meta["currency"])
    return _stamp(env, rows[-1]["t"], TF_SECONDS[tf] if tf not in ("1d", "1w", "1M") else 0, tz,
                  last_trade_ts=meta.get("regularMarketTime"))


def _slim(inst):
    return {k: inst.get(k) for k in ("input", "symbol", "asset_class", "exchange", "currency", "verified",
                                     "confidence") if inst.get(k) is not None}


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------

def quote(symbol, venue=None, fetcher=None, lookups=None):
    """Last price, day change and session status, from the right provider."""
    f = fetcher or Fetcher()
    inst = symbol if isinstance(symbol, dict) else M.resolve(symbol, lookups if lookups is not None else M.Lookups())
    name = inst.get("symbol") or inst.get("input")
    if not inst.get("asset_class"):
        return failed("none", name, "quote", "unrecognised instrument: " + "; ".join(inst.get("notes", [])))
    if inst["asset_class"] == "crypto" and venue != "yahoo":
        env = candles(inst, "1m", 2, venue=venue, fetcher=f)
        if env.get("error"):
            return dict(env, kind="quote")
        last = env["data"][-1]
        day = candles(inst, "1d", 2, venue=env["provider"], fetcher=f)
        prev = day["data"][-2]["c"] if day.get("data") and len(day["data"]) > 1 else None
        env.update(kind="quote", data={"price": last["c"], "prev_close_utc_day": prev,
                                       "change_pct_utc_day": round((last["c"] / prev - 1) * 100, 3) if prev else None,
                                       "day_volume": day["data"][-1]["v"] if day.get("data") else None},
                   market=M.market_status("CRYPTO"))
        return env
    ysym = inst.get("yahoo") or inst.get("yahoo_proxy")
    env = candles(inst, "1m" if inst["asset_class"] != "option" else "1d", 5, fetcher=f)
    if env.get("error"):
        return dict(env, kind="quote")
    last = env["data"][-1]
    env["kind"] = "quote"
    data = {"price": last["c"], "bar_time_utc": iso(ts_utc(last["t"]))}
    try:
        # range=1d: Yahoo's chartPreviousClose is then the previous session's close (with a longer
        # range it is the close before the whole range, which would overstate the day's move)
        meta, _, _ = yahoo_candles(f, ysym, "1d", rng="1d")
        data.update(prev_close=meta.get("chartPreviousClose") or meta.get("previousClose"),
                    day_high=meta.get("regularMarketDayHigh"), day_low=meta.get("regularMarketDayLow"),
                    day_volume=meta.get("regularMarketVolume"), regular_price=meta.get("regularMarketPrice"),
                    regular_time_utc=iso(ts_utc(meta["regularMarketTime"])) if meta.get("regularMarketTime") else None)
        pc = data.get("prev_close")
        if pc:
            data["change_pct"] = round((data["regular_price"] or last["c"]) / pc * 100 - 100, 3)
    except DataError as exc:
        env["limitations"].append("no daily context: %s" % exc)
    env["data"] = data
    code = inst.get("exchange")
    if code in M.EXCHANGES:
        env["market"] = M.market_status(code)
        if not env["market"].get("is_open"):
            env["limitations"].append("%s is %s now: this is the last traded price, not a live one"
                                      % (code, env["market"]["phase"]))
    return env


def depth(symbol, venue="okx", n=10, fetcher=None):
    """Top of the order book for a crypto pair (stocks have no free book)."""
    f = fetcher or Fetcher()
    inst = symbol if isinstance(symbol, dict) else M.resolve(symbol, M.Lookups())
    if inst.get("asset_class") != "crypto":
        return failed("none", inst.get("symbol"), "book", "order books here are crypto only: exchange stock "
                                                          "books need a licensed or broker feed")
    base, quote_ = inst["base"], inst["quote"]
    usdq = quote_ if quote_ != "USD" else "USDT"
    perp = inst.get("instrument") == "perpetual"
    try:
        if venue == "binance":
            sym = base + usdq
            url = "https://data-api.binance.vision/api/v3/depth?symbol=%s&limit=%d" % (sym, max(5, min(n, 100)))
            d = f.json(url, "binance", "depth-" + sym)
            bids, asks = d["bids"], d["asks"]
        elif venue == "okx":
            sym = "%s-%s%s" % (base, usdq, "-SWAP" if perp else "")
            url = "https://www.okx.com/api/v5/market/books?instId=%s&sz=%d" % (sym, min(n, 400))
            d = f.json(url, "okx", "depth-" + sym)["data"][0]
            bids, asks = d["bids"], d["asks"]
        elif venue == "coinbase":
            sym = "%s-%s" % (base, "USD" if quote_ in ("USD", "USDT") else quote_)
            url = "https://api.exchange.coinbase.com/products/%s/book?level=2" % sym
            d = f.json(url, "coinbase", "depth-" + sym)
            bids, asks = d["bids"], d["asks"]
        elif venue == "kraken":
            sym = ("XBT" if base == "BTC" else base) + ("USD" if quote_ in ("USD", "USDT") else quote_)
            url = "https://api.kraken.com/0/public/Depth?pair=%s&count=%d" % (sym, n)
            d = f.json(url, "kraken", "depth-" + sym)
            r = list(d["result"].values())[0]
            bids, asks = r["bids"], r["asks"]
        elif venue == "kucoin":
            sym = "%s-%s" % (base, usdq)
            url = "https://api.kucoin.com/api/v1/market/orderbook/level2_20?symbol=%s" % sym
            d = f.json(url, "kucoin", "depth-" + sym)["data"]
            bids, asks = d["bids"], d["asks"]
        else:
            raise DataError("unknown venue %r" % venue)
    except (DataError, KeyError, IndexError) as exc:
        return failed(venue, inst.get("symbol"), "book", exc)
    bids = [(float(b[0]), float(b[1])) for b in bids[:n]]
    asks = [(float(a[0]), float(a[1])) for a in asks[:n]]
    env = envelope(venue, sym, "book", {"bids": bids, "asks": asks}, exchange="CRYPTO", url=url, tz="UTC",
                   currency=("USD" if venue in ("coinbase", "kraken") and quote_ in ("USD", "USDT") else usdq),
                   as_of_utc=iso(utc_now()))
    if bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        env["data"].update(mid=mid, spread=asks[0][0] - bids[0][0],
                           spread_bps=round((asks[0][0] - bids[0][0]) / mid * 1e4, 3),
                           bid_depth=sum(p * q for p, q in bids), ask_depth=sum(p * q for p, q in asks),
                           imbalance=round((sum(q for _, q in bids) - sum(q for _, q in asks)) /
                                           max(1e-12, sum(q for _, q in bids) + sum(q for _, q in asks)), 4))
    return env


# --------------------------------------------------------------------------
# Currency conversion
# --------------------------------------------------------------------------

def fx_rate(base, quote_, fetcher=None, prefer="yahoo"):
    """Units of `quote_` per one `base`, with its source. USDT and USDC are
    treated as USD and the answer says so."""
    f = fetcher or Fetcher()
    notes = []
    b, q = base.upper(), quote_.upper()
    for stable in ("USDT", "USDC", "FDUSD"):
        if b == stable:
            b = "USD"
            notes.append("%s treated as 1 USD (it is a stablecoin; its peg can break)" % stable)
        if q == stable:
            q = "USD"
            notes.append("%s treated as 1 USD (it is a stablecoin; its peg can break)" % stable)
    if b == q:
        return {"rate": 1.0, "provider": "identity", "freshness": "exact", "limitations": notes,
                "pair": b + q}
    errs = []
    order = ["yahoo", "frankfurter"] if prefer == "yahoo" else ["frankfurter", "yahoo"]
    for src in order:
        try:
            if src == "yahoo":
                meta, bars, url = yahoo_candles(f, "%s%s=X" % (b, q), "5m", rng="1d")
                if not bars:
                    raise DataError("no bars")
                env = envelope("yahoo", b + q + "=X", "fx", None, url=url, currency=q)
                env = _stamp(env, bars[-1]["t"], 300, "UTC")
                env.update(rate=bars[-1]["c"], pair=b + q)
            else:
                url = "https://api.frankfurter.dev/v1/latest?base=%s&symbols=%s" % (b, q)
                d = f.json(url, "frankfurter", "%s%s" % (b, q))
                if q not in d.get("rates", {}):
                    raise DataError("ECB publishes no %s/%s rate" % (b, q))
                env = envelope("frankfurter", b + q, "fx", None, url=url, currency=q, as_of_utc=d.get("date"))
                env.update(rate=float(d["rates"][q]), pair=b + q)
            env["limitations"] += notes
            if errs:
                env["limitations"].append("tried first: " + "; ".join(errs))
            return env
        except DataError as exc:
            errs.append("%s: %s" % (src, exc))
    return {"rate": None, "error": " | ".join(errs), "pair": b + q, "provider": "none",
            "limitations": notes + ["no FX rate: " + " | ".join(errs)]}


def convert(amount, base, quote_, fetcher=None):
    r = fx_rate(base, quote_, fetcher)
    if r.get("rate") is None:
        return dict(r, amount=None)
    return dict(r, amount=amount * r["rate"], input_amount=amount)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _fmt(x):
    if x is None:
        return "n/a"
    if abs(x) >= 1000:
        return "{:,.2f}".format(x)
    if abs(x) >= 1:
        return "%.4f" % x
    return "%.8g" % x


def print_env_head(env):
    if env.get("error"):
        print("  NO DATA from %s: %s" % (env.get("provider"), env["error"]))
        return
    print("  source %s | %s | %s | as of %s UTC%s | lag %ss | %s" % (
        env.get("provider"), env.get("instrument"), env.get("currency") or "?", env.get("as_of_utc"),
        (" (%s local)" % env["as_of_local"]) if env.get("as_of_local") else "", env.get("observed_lag_s", "?"),
        env.get("freshness")))
    for lim in env.get("limitations", []):
        print("  limit: " + lim)


def main(argv=None):
    p = argparse.ArgumentParser(description="Market data with provenance.")
    sub = p.add_subparsers(dest="cmd")
    q = sub.add_parser("quote")
    q.add_argument("symbols", nargs="+")
    q.add_argument("--venue", choices=CRYPTO_VENUES + ("yahoo",))
    c = sub.add_parser("candles")
    c.add_argument("symbol")
    c.add_argument("--tf", default="1d", choices=TIMEFRAMES)
    c.add_argument("--bars", type=int, default=20)
    c.add_argument("--venue", choices=CRYPTO_VENUES + ("yahoo",))
    b = sub.add_parser("book")
    b.add_argument("symbol")
    b.add_argument("--venue", default="okx", choices=CRYPTO_VENUES)
    b.add_argument("--depth", type=int, default=10)
    x = sub.add_parser("fx")
    x.add_argument("amount", type=float)
    x.add_argument("base")
    x.add_argument("quote")
    for s in (q, c, b, x):
        s.add_argument("--json", action="store_true")
        s.add_argument("--offline", help="read saved <provider>-<key>.json files from this folder")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 1
    f = Fetcher(offline=a.offline)
    lk = M.Lookups()
    rc = 0
    if a.cmd == "quote":
        out = []
        for s in a.symbols:
            env = quote(s, a.venue, f, lk)
            out.append(env)
            if a.json:
                continue
            r = M.resolve(s, lk)
            d = env.get("data") or {}
            print("%s (%s, %s)" % (s, r.get("asset_class"), r.get("exchange")))
            if d:
                ch = d.get("change_pct", d.get("change_pct_utc_day"))
                print("  price %s %s%s" % (_fmt(d.get("regular_price") or d.get("price")), env.get("currency") or "",
                                           ("  (%+.2f%% vs previous close)" % ch) if ch is not None else ""))
            print_env_head(env)
            if env.get("market"):
                m = env["market"]
                print("  market %s is %s (%s %s)" % (m["exchange"], m["phase"], m["local_time"], m["tz_label"]))
            rc |= 1 if env.get("error") else 0
        if a.json:
            print(json.dumps(out, indent=1, default=str))
        return rc
    if a.cmd == "candles":
        env = candles(a.symbol, a.tf, a.bars, a.venue, f, lk)
        if a.json:
            print(json.dumps(env, indent=1, default=str))
            return 1 if env.get("error") else 0
        print("%s %s candles" % (a.symbol, a.tf))
        print_env_head(env)
        tz = env.get("tz") or "UTC"
        for bar in (env.get("data") or [])[-a.bars:]:
            t = M.to_local(ts_utc(bar["t"]), tz) if tz in M.ZONES else ts_utc(bar["t"])
            print("  %s  O %s  H %s  L %s  C %s  V %s" % (t.strftime("%Y-%m-%d %H:%M"), _fmt(bar["o"]), _fmt(bar["h"]),
                                                          _fmt(bar["l"]), _fmt(bar["c"]), _fmt(bar["v"])))
        return 1 if env.get("error") else 0
    if a.cmd == "book":
        env = depth(a.symbol, a.venue, a.depth, f)
        if a.json:
            print(json.dumps(env, indent=1, default=str))
            return 1 if env.get("error") else 0
        print_env_head(env)
        d = env.get("data") or {}
        if d.get("mid"):
            print("  mid %s  spread %s (%.2f bps)  imbalance %+.2f" % (_fmt(d["mid"]), _fmt(d["spread"]),
                                                                       d["spread_bps"], d["imbalance"]))
            for (bp, bq), (ap, aq) in zip(d["bids"], d["asks"]):
                print("  %14s x %-12s | %14s x %s" % (_fmt(bp), _fmt(bq), _fmt(ap), _fmt(aq)))
        return 1 if env.get("error") else 0
    if a.cmd == "fx":
        r = convert(a.amount, a.base, a.quote, f)
        if a.json:
            print(json.dumps(r, indent=1, default=str))
        elif r.get("amount") is None:
            print("no rate: %s" % r.get("error"))
            return 1
        else:
            print("%s %s = %s %s  (rate %s from %s, %s, as of %s)" % (
                _fmt(a.amount), a.base.upper(), _fmt(r["amount"]), a.quote.upper(), _fmt(r["rate"]), r["provider"],
                r.get("freshness"), r.get("as_of_utc")))
            for lim in r.get("limitations", []):
                print("  limit: " + lim)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
