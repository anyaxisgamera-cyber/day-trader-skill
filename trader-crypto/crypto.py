#!/usr/bin/env python3
"""crypto.py: crypto market intelligence from public exchange and on-chain data.

    python3 crypto.py overview                      # market cap, BTC/ETH dominance, Fear & Greed, DeFi TVL, fees, DVOL
    python3 crypto.py check PEPEUSDT                # which exchanges really list it, spread on each
    python3 crypto.py token SOL                     # market cap, FDV, supply, unlock overhang, liquidity, risk flags
    python3 crypto.py derivs BTC                    # funding, open interest, long/short, liquidations, options
    python3 crypto.py scan --top 60 --tf 1h         # unusual volume, moves, breakouts, volatility, funding, OI
    python3 crypto.py plan BTCUSDT --entry 61500 --stop 60200 --target 64100 --account 2000 --risk 1 --leverage 3

Every number is read live from the source named next to it, with its time.
Nothing places orders. Nothing here is a recommendation: a scan hit is a
measurement that matched a rule, not an opportunity.

Sources (free, keyless, public):
  OKX         perpetual funding, open interest, long/short account ratio, liquidations, candles, books
  Binance     spot tickers and candles via data-api.binance.vision (its main and futures APIs answer
              HTTP 451 from US cloud servers, so Binance derivatives data is not available here)
  Coinbase, Kraken, KuCoin   listings, books and candles
  Deribit     BTC/ETH options open interest and implied volatility (DVOL)
  CoinGecko   market cap, FDV, supply, dominance (free tier: about 10-30 calls a minute)
  alternative.me   Crypto Fear & Greed Index (daily)
  DefiLlama   DeFi total value locked, protocol TVL and fees
  mempool.space    Bitcoin fee rates and mempool size; publicnode   Ethereum gas price

Not wired in (need paid keys): whale-transaction feeds, exchange inflow/outflow
series, token unlock calendars. Where the Nansen connector is enabled in
Claude, SKILL.md says how to use it for smart-money flows and holders.
"""

from __future__ import print_function

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MKT = os.environ.get("TRADER_MARKETS_DIR", os.path.join(HERE, "..", "trader-markets"))
sys.path.insert(0, MKT)
import markets as M  # noqa: E402
import marketdata as MD  # noqa: E402
import indicators as I  # noqa: E402

NOTE = "Measurements, not recommendations. Crypto can move 10% in an hour and lose most of its value in a year."
MAX_SANE_LEVERAGE = 3


class CryptoError(Exception):
    pass


def _f(x, nd=2):
    if x is None:
        return "n/a"
    if abs(x) >= 1e9:
        return "%.2fB" % (x / 1e9)
    if abs(x) >= 1e6:
        return "%.2fM" % (x / 1e6)
    if abs(x) >= 1000:
        return "{:,.{}f}".format(x, nd)
    if abs(x) >= 1:
        return "%.*f" % (nd + 2, x)
    return "%.6g" % x


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _src(provider, url, as_of=None, fresh=None):
    return {"provider": provider, "url": url, "as_of_utc": as_of or MD.iso(MD.utc_now()),
            "freshness": fresh or MD.PROVIDER_FRESHNESS.get(provider, ("real-time", ""))[0]}


def _ms(ts):
    return MD.iso(MD.ts_utc(int(ts) // 1000)) if ts else None


# --------------------------------------------------------------------------
# Market overview
# --------------------------------------------------------------------------

def overview(f):
    out, errors = {}, []

    def step(name, fn):
        try:
            out[name] = fn()
        except (MD.DataError, KeyError, IndexError, ValueError, TypeError) as exc:
            errors.append("%s: %s" % (name, exc))

    def cg_global():
        url = "https://api.coingecko.com/api/v3/global"
        d = f.json(url, "coingecko", "global")["data"]
        return {"total_market_cap_usd": d["total_market_cap"]["usd"], "total_volume_usd": d["total_volume"]["usd"],
                "btc_dominance_pct": d["market_cap_percentage"]["btc"],
                "eth_dominance_pct": d["market_cap_percentage"]["eth"],
                "stablecoin_share_pct": sum(d["market_cap_percentage"].get(k, 0) for k in ("usdt", "usdc", "dai")),
                "market_cap_change_24h_pct": d.get("market_cap_change_percentage_24h_usd"),
                "active_coins": d.get("active_cryptocurrencies"),
                "source": _src("coingecko", url, _ms(d.get("updated_at", 0) * 1000), "delayed (minutes)")}

    def fng():
        url = "https://api.alternative.me/fng/?limit=8"
        d = f.json(url, "alternative", "fng")["data"]
        return {"value": int(d[0]["value"]), "label": d[0]["value_classification"],
                "week_ago": int(d[-1]["value"]) if len(d) >= 8 else None,
                "source": _src("alternative.me", url, MD.iso(MD.ts_utc(int(d[0]["timestamp"]))), "daily")}

    def tvl():
        url = "https://api.llama.fi/v2/historicalChainTvl"
        d = f.json(url, "defillama", "tvl")
        last, wk = d[-1], d[-8] if len(d) >= 8 else d[0]
        return {"defi_tvl_usd": last["tvl"], "change_7d_pct": (last["tvl"] / wk["tvl"] - 1) * 100 if wk["tvl"] else None,
                "source": _src("defillama", url, MD.iso(MD.ts_utc(int(last["date"]))), "daily")}

    def btc_fees():
        url = "https://mempool.space/api/v1/fees/recommended"
        d = f.json(url, "mempool", "fees")
        m = f.json("https://mempool.space/api/mempool", "mempool", "mempool")
        return {"fastest_sat_vb": d["fastestFee"], "hour_sat_vb": d["hourFee"], "mempool_tx": m["count"],
                "source": _src("mempool.space", url, None, "real-time")}

    def eth_gas():
        url = "https://ethereum-rpc.publicnode.com"
        req = MD.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice",
                                               "params": []}).encode(),
                         headers={"Content-Type": "application/json", "User-Agent": MD.UA})
        if f.offline:
            d = json.loads(f.get(url, "publicnode", "gas"))
        else:
            d = json.loads(MD.urlopen(req, timeout=15).read().decode())
        return {"gas_gwei": int(d["result"], 16) / 1e9, "source": _src("publicnode (Ethereum RPC)", url, None, "real-time")}

    def dvol():
        res = {}
        for ccy in ("BTC", "ETH"):
            url = "https://www.deribit.com/api/v2/public/get_index_price?index_name=%s_usd" % ccy.lower()
            end = int(MD.utc_now().timestamp() * 1000) if hasattr(MD.utc_now(), "timestamp") else 0
            u2 = ("https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=%s&resolution=3600"
                  "&start_timestamp=%d&end_timestamp=%d" % (ccy, end - 6 * 3600 * 1000, end))
            d = f.json(u2, "deribit", "dvol-" + ccy)["result"]["data"]
            if d:
                res[ccy] = {"dvol": d[-1][4], "as_of_utc": _ms(d[-1][0])}
        res["source"] = _src("deribit", "get_volatility_index_data", None, "real-time (hourly bars)")
        return res

    step("market", cg_global)
    step("fear_greed", fng)
    step("defi", tvl)
    step("bitcoin_network", btc_fees)
    step("ethereum_network", eth_gas)
    step("implied_vol", dvol)
    out["errors"] = errors
    return out


def print_overview(o):
    m = o.get("market")
    if m:
        print("Crypto market (CoinGecko, as of %s UTC)" % m["source"]["as_of_utc"])
        print("  total market cap $%s (%+.2f%% 24h), 24h volume $%s" % (
            _f(m["total_market_cap_usd"]), m["market_cap_change_24h_pct"] or 0, _f(m["total_volume_usd"])))
        print("  dominance: BTC %.2f%%, ETH %.2f%%, top stablecoins %.2f%%" % (
            m["btc_dominance_pct"], m["eth_dominance_pct"], m["stablecoin_share_pct"]))
    g = o.get("fear_greed")
    if g:
        print("  Fear & Greed %d (%s), a week ago %s  [alternative.me, %s UTC]" % (
            g["value"], g["label"], g["week_ago"], g["source"]["as_of_utc"]))
    d = o.get("defi")
    if d:
        print("  DeFi TVL $%s (%+.2f%% over 7 days)  [DefiLlama, %s]" % (_f(d["defi_tvl_usd"]), d["change_7d_pct"] or 0,
                                                                        d["source"]["as_of_utc"][:10]))
    b = o.get("bitcoin_network")
    if b:
        print("  Bitcoin fees: next block %s sat/vB, within an hour %s sat/vB; %s txs waiting  [mempool.space]" % (
            b["fastest_sat_vb"], b["hour_sat_vb"], "{:,}".format(b["mempool_tx"])))
    e = o.get("ethereum_network")
    if e:
        print("  Ethereum gas %.3f gwei  [Ethereum RPC via publicnode]" % e["gas_gwei"])
    v = o.get("implied_vol")
    if v:
        print("  implied volatility (Deribit DVOL): %s" % ", ".join(
            "%s %.1f" % (k, x["dvol"]) for k, x in v.items() if k != "source"))
    for err in o.get("errors", []):
        print("  NO DATA %s" % err)


# --------------------------------------------------------------------------
# Listing check
# --------------------------------------------------------------------------

def check(f, inst):
    base, quote = inst["base"], inst["quote"]
    usdq = quote if quote != "USD" else "USDT"
    rows = []

    def add(venue, sym, ok, why="", extra=None):
        r = {"venue": venue, "symbol": sym, "listed": ok, "note": why}
        r.update(extra or {})
        rows.append(r)

    try:
        d = f.json("https://data-api.binance.vision/api/v3/exchangeInfo?symbol=%s%s" % (base, usdq), "binance",
                   "info-%s%s" % (base, usdq))
        s = (d.get("symbols") or [{}])[0]
        add("binance", base + usdq, s.get("status") == "TRADING", s.get("status", d.get("msg", "")))
    except MD.DataError as exc:
        add("binance", base + usdq, False, str(exc))
    try:
        d = f.json("https://www.okx.com/api/v5/public/instruments?instType=SPOT&instId=%s-%s" % (base, usdq), "okx",
                   "inst-%s-%s" % (base, usdq))
        ok = bool(d.get("data")) and d["data"][0].get("state") == "live"
        add("okx", "%s-%s" % (base, usdq), ok, d["data"][0].get("state") if d.get("data") else d.get("msg"))
    except MD.DataError as exc:
        add("okx", "%s-%s" % (base, usdq), False, str(exc))
    cq = "USD" if quote in ("USD", "USDT") else quote
    try:
        d = f.json("https://api.exchange.coinbase.com/products/%s-%s" % (base, cq), "coinbase", "prod-%s-%s" % (base, cq))
        add("coinbase", "%s-%s" % (base, cq), d.get("status") == "online" and not d.get("trading_disabled"),
            d.get("status", d.get("message", "")))
    except MD.DataError as exc:
        add("coinbase", "%s-%s" % (base, cq), False, str(exc))
    kb = "XBT" if base == "BTC" else base
    try:
        d = f.json("https://api.kraken.com/0/public/AssetPairs?pair=%s%s" % (kb, cq), "kraken", "pair-%s%s" % (kb, cq))
        add("kraken", kb + cq, not d.get("error") and bool(d.get("result")), ", ".join(d.get("error", [])))
    except MD.DataError as exc:
        add("kraken", kb + cq, False, str(exc))
    try:
        d = f.json("https://api.kucoin.com/api/v2/symbols/%s-%s" % (base, usdq), "kucoin", "sym-%s-%s" % (base, usdq))
        ok = d.get("code") == "200000" and bool(d.get("data")) and d["data"].get("enableTrading")
        add("kucoin", "%s-%s" % (base, usdq), bool(ok), d.get("msg", ""))
    except MD.DataError as exc:
        add("kucoin", "%s-%s" % (base, usdq), False, str(exc))
    for r in rows:
        if r["listed"] and r["venue"] in ("okx", "kraken", "binance", "coinbase", "kucoin"):
            b = MD.depth(dict(inst, instrument="spot"), r["venue"], 10, f)
            if b.get("data") and b["data"].get("mid"):
                r.update(spread_bps=b["data"]["spread_bps"], depth_10_levels_quote=b["data"]["bid_depth"] +
                         b["data"]["ask_depth"])
    return {"instrument": inst["symbol"], "venues": rows, "listed_on": [r["venue"] for r in rows if r["listed"]],
            "verified": any(r["listed"] for r in rows), "as_of_utc": MD.iso(MD.utc_now())}


# --------------------------------------------------------------------------
# Token fundamentals and risk
# --------------------------------------------------------------------------

def coingecko_id(f, base):
    d = f.json("https://api.coingecko.com/api/v3/search?query=%s" % base, "coingecko", "search-" + base)
    exact = [c for c in d.get("coins", []) if c.get("symbol", "").upper() == base.upper()]
    if not exact:
        raise MD.DataError("CoinGecko lists no coin with symbol %s" % base)
    exact.sort(key=lambda c: c.get("market_cap_rank") or 10 ** 9)
    return exact[0]["id"], exact[1:4]


def token(f, inst):
    base = inst["base"]
    cid, lookalikes = coingecko_id(f, base)
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=%s&price_change_percentage=24h,7d,30d"
           % cid)
    rows = f.json(url, "coingecko", "markets-" + cid)
    if not rows:
        raise MD.DataError("CoinGecko returned no market row for %s" % cid)
    c = rows[0]
    mc, fdv = _num(c.get("market_cap")), _num(c.get("fully_diluted_valuation"))
    circ, tot, mx = _num(c.get("circulating_supply")), _num(c.get("total_supply")), _num(c.get("max_supply"))
    vol = _num(c.get("total_volume"))
    res = {"coingecko_id": cid, "name": c.get("name"), "symbol": base, "rank": c.get("market_cap_rank"),
           "price_usd": _num(c.get("current_price")), "market_cap_usd": mc, "fdv_usd": fdv,
           "circulating_supply": circ, "total_supply": tot, "max_supply": mx,
           "volume_24h_usd": vol, "change_24h_pct": c.get("price_change_percentage_24h_in_currency"),
           "change_7d_pct": c.get("price_change_percentage_7d_in_currency"),
           "change_30d_pct": c.get("price_change_percentage_30d_in_currency"),
           "ath_usd": _num(c.get("ath")), "from_ath_pct": c.get("ath_change_percentage"), "ath_date": c.get("ath_date"),
           "sector": inst.get("sector"), "source": _src("coingecko", url, (c.get("last_updated") or "").replace("T", " ")[:19],
                                                       "delayed (minutes)")}
    res["fdv_to_mcap"] = fdv / mc if fdv and mc else None
    res["circulating_pct"] = circ / (mx or tot) * 100 if circ and (mx or tot) else None
    res["volume_to_mcap_pct"] = vol / mc * 100 if vol and mc else None
    flags = []
    if res["fdv_to_mcap"] and res["fdv_to_mcap"] > 1.5:
        flags.append("FDV is %.1fx the market cap: %.0f%% of the supply is not circulating yet, so future unlocks "
                     "can add selling pressure" % (res["fdv_to_mcap"], 100 - (res["circulating_pct"] or 0)))
    if mc and mc < 100e6:
        flags.append("small cap (under $100M): thin books and easy to move")
    if res["volume_to_mcap_pct"] is not None and res["volume_to_mcap_pct"] < 1:
        flags.append("24h volume under 1% of market cap: low liquidity")
    if res["volume_to_mcap_pct"] is not None and res["volume_to_mcap_pct"] > 50:
        flags.append("24h volume over 50% of market cap: speculative turnover")
    if inst.get("sector") == "Meme":
        flags.append("meme coin: price rests on attention, not cash flows")
    if base in M.STABLECOINS:
        dev = abs((res["price_usd"] or 1) - 1) * 100
        flags.append("stablecoin: %.2f%% from its $1 peg now; its risk is the issuer's reserves" % dev)
    if res["rank"] is None or (res["rank"] or 0) > 300:
        flags.append("outside the top 300 by market cap (or unranked)")
    if lookalikes:
        flags.append("other coins share or resemble the symbol %s on CoinGecko (%s): check the contract address "
                     "before buying" % (base, ", ".join(x["id"] for x in lookalikes)))
    if mx is None:
        flags.append("no maximum supply: issuance can continue")
    res["risk_flags"] = flags
    res["unlock_note"] = ("Token unlock schedules are not in a free API. The FDV/market-cap gap above is the size "
                          "of the overhang; the project's tokenomics page or a paid unlock tracker has the dates.")
    return res


def print_token(t):
    print("%s (%s), rank %s, sector %s  [CoinGecko %s, as of %s UTC]" % (
        t["name"], t["symbol"], t["rank"], t["sector"], t["coingecko_id"], t["source"]["as_of_utc"]))
    print("  price $%s  24h %+.2f%%  7d %+.2f%%  30d %+.2f%%  (%.1f%% from its high of $%s on %s)" % (
        _f(t["price_usd"]), t["change_24h_pct"] or 0, t["change_7d_pct"] or 0, t["change_30d_pct"] or 0,
        t["from_ath_pct"] or 0, _f(t["ath_usd"]), (t["ath_date"] or "")[:10]))
    print("  market cap $%s, FDV $%s (%sx), 24h volume $%s (%s%% of cap)" % (
        _f(t["market_cap_usd"]), _f(t["fdv_usd"]), "%.2f" % t["fdv_to_mcap"] if t["fdv_to_mcap"] else "n/a",
        _f(t["volume_24h_usd"]), "%.1f" % t["volume_to_mcap_pct"] if t["volume_to_mcap_pct"] is not None else "n/a"))
    print("  supply: circulating %s, total %s, max %s (%s%% circulating)" % (
        _f(t["circulating_supply"]), _f(t["total_supply"]), _f(t["max_supply"]),
        "%.1f" % t["circulating_pct"] if t["circulating_pct"] else "n/a"))
    for fl in t["risk_flags"]:
        print("  risk: " + fl)
    print("  " + t["unlock_note"])


# --------------------------------------------------------------------------
# Derivatives: funding, OI, long/short, liquidations, options
# --------------------------------------------------------------------------

def derivs(f, base):
    base = base.upper()
    inst_id = "%s-USDT-SWAP" % base
    out = {"instrument": inst_id, "venue": "okx", "errors": []}
    try:
        url = "https://www.okx.com/api/v5/public/funding-rate?instId=" + inst_id
        d = f.json(url, "okx", "funding-" + base)["data"][0]
        rate = float(d["fundingRate"])
        per_day = 24.0 / 8  # OKX funds every 8 hours on most USDT swaps
        out["funding"] = {"current_rate": rate, "next_funding_utc": _ms(d["fundingTime"]),
                          "annualised_pct": rate * per_day * 365 * 100, "premium": _num(d.get("premium")),
                          "cap": _num(d.get("maxFundingRate")), "source": _src("okx", url)}
        url = "https://www.okx.com/api/v5/public/funding-rate-history?instId=%s&limit=21" % inst_id
        h = [float(x["realizedRate"]) for x in f.json(url, "okx", "fundhist-" + base)["data"]]
        if h:
            out["funding"]["avg_7d"] = sum(h) / len(h)
            out["funding"]["avg_7d_annualised_pct"] = sum(h) / len(h) * per_day * 365 * 100
    except (MD.DataError, KeyError, IndexError, ValueError) as exc:
        out["errors"].append("funding: %s" % exc)
    try:
        url = "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=" + inst_id
        d = f.json(url, "okx", "oi-" + base)["data"][0]
        out["open_interest"] = {"contracts": float(d["oi"]), "coins": float(d["oiCcy"]), "usd": float(d["oiUsd"]),
                                "as_of_utc": _ms(d["ts"]), "source": _src("okx", url)}
        url = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=%s&period=1H" % base
        rows = f.json(url, "okx", "oivol-" + base)["data"]  # newest first, all OKX contracts on this coin
        if len(rows) >= 25:
            now_, day = float(rows[0][1]), float(rows[24][1])
            out["open_interest"]["all_contracts_usd"] = now_
            out["open_interest"]["change_24h_pct"] = (now_ / day - 1) * 100 if day else None
    except (MD.DataError, KeyError, IndexError, ValueError) as exc:
        out["errors"].append("open interest: %s" % exc)
    try:
        url = "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=%s&period=1H" % base
        rows = f.json(url, "okx", "lsr-" + base)["data"]
        out["long_short"] = {"accounts_ratio": float(rows[0][1]), "day_ago": float(rows[24][1]) if len(rows) > 24 else None,
                             "as_of_utc": _ms(rows[0][0]), "source": _src("okx", url),
                             "meaning": "accounts net long divided by accounts net short on OKX; it counts "
                                        "accounts, not size"}
    except (MD.DataError, KeyError, IndexError, ValueError) as exc:
        out["errors"].append("long/short: %s" % exc)
    try:
        url = ("https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&uly=%s-USDT&state=filled" % base)
        d = f.json(url, "okx", "liq-" + base)["data"]
        det = d[0]["details"] if d else []
        url_ct = "https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId=" + inst_id
        ct = float(f.json(url_ct, "okx", "ctval-" + base)["data"][0]["ctVal"])
        longs = [x for x in det if x["posSide"] == "long"]
        shorts = [x for x in det if x["posSide"] == "short"]

        def usd(xs):
            return sum(float(x["sz"]) * ct * float(x["bkPx"]) for x in xs)

        prices = sorted(float(x["bkPx"]) for x in det)
        clusters = _clusters(prices)
        span = (_ms(min(int(x["ts"]) for x in det)), _ms(max(int(x["ts"]) for x in det))) if det else (None, None)
        out["liquidations"] = {"count": len(det), "long_usd": usd(longs), "short_usd": usd(shorts),
                               "window_utc": span, "price_clusters": clusters[:4], "source": _src("okx", url),
                               "meaning": "the most recent filled liquidations OKX publishes (a short window), "
                                          "not the whole market"}
    except (MD.DataError, KeyError, IndexError, ValueError) as exc:
        out["errors"].append("liquidations: %s" % exc)
    if base in ("BTC", "ETH", "SOL", "XRP"):
        try:
            url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=%s&kind=option" % base
            rows = f.json(url, "deribit", "opts-" + base)["result"]
            calls = [r for r in rows if r["instrument_name"].endswith("-C")]
            puts = [r for r in rows if r["instrument_name"].endswith("-P")]
            coi, poi = sum(r["open_interest"] for r in calls), sum(r["open_interest"] for r in puts)
            und = rows[0].get("underlying_price") if rows else None
            atm = sorted((r for r in rows if r.get("mark_iv")), key=lambda r: abs(_strike(r["instrument_name"]) - (und or 0)))
            out["options"] = {"venue": "deribit", "calls_oi": coi, "puts_oi": poi,
                              "put_call_oi_ratio": poi / coi if coi else None, "contracts_listed": len(rows),
                              "atm_iv_pct": atm[0]["mark_iv"] if atm else None, "underlying": und,
                              "source": _src("deribit", url)}
        except (MD.DataError, KeyError, IndexError, ValueError) as exc:
            out["errors"].append("options: %s" % exc)
    return out


def _strike(name):
    try:
        return float(name.split("-")[2])
    except (IndexError, ValueError):
        return 0.0


def _clusters(prices, tol=0.0005):
    out = []
    for p in prices:
        if out and abs(p - out[-1]["avg"]) / p <= tol:
            c = out[-1]
            c["n"] += 1
            c["avg"] += (p - c["avg"]) / c["n"]
        else:
            out.append({"avg": p, "n": 1})
    return sorted(out, key=lambda c: -c["n"])


def print_derivs(d):
    print("%s on %s" % (d["instrument"], d["venue"].upper()))
    fu = d.get("funding")
    if fu:
        print("  funding %.4f%% per 8h (%.1f%% a year if it stayed), 7-day average %.4f%% (%.1f%% a year); "
              "next %s UTC" % (fu["current_rate"] * 100, fu["annualised_pct"], (fu.get("avg_7d") or 0) * 100,
                               fu.get("avg_7d_annualised_pct") or 0, fu["next_funding_utc"]))
        print("    positive funding: longs pay shorts; negative: shorts pay longs")
    oi = d.get("open_interest")
    if oi:
        ch = oi.get("change_24h_pct")
        print("  open interest %s coins ($%s) on this swap; all OKX %s contracts $%s%s  [as of %s]" % (
            _f(oi["coins"]), _f(oi["usd"]), d["instrument"].split("-")[0], _f(oi.get("all_contracts_usd")),
            (" (%+.1f%% in 24h)" % ch) if ch is not None else "", oi["as_of_utc"]))
    ls = d.get("long_short")
    if ls:
        print("  long/short account ratio %.2f (a day ago %s): %s" % (ls["accounts_ratio"], ls["day_ago"], ls["meaning"]))
    lq = d.get("liquidations")
    if lq:
        print("  latest %d liquidations (%s to %s UTC): longs $%s, shorts $%s" % (
            lq["count"], lq["window_utc"][0], lq["window_utc"][1], _f(lq["long_usd"]), _f(lq["short_usd"])))
        if lq["price_clusters"]:
            print("    clustered near: %s" % ", ".join("%s (x%d)" % (_f(c["avg"]), c["n"]) for c in lq["price_clusters"]))
    op = d.get("options")
    if op:
        print("  Deribit options: OI calls %s / puts %s (put/call %.2f), ATM implied vol %s%%, %d contracts listed" % (
            _f(op["calls_oi"]), _f(op["puts_oi"]), op["put_call_oi_ratio"] or 0, op["atm_iv_pct"], op["contracts_listed"]))
    for e in d.get("errors", []):
        print("  NO DATA " + e)


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

SCAN_RULES = [
    ("volume", "unusual volume: last completed bar >= 3x its 20-bar average"),
    ("move", "sudden move: last completed bar's change >= 2.5x ATR14 / price"),
    ("breakout", "close above the prior 20-bar high"),
    ("breakdown", "close below the prior 20-bar low"),
    ("vol_expansion", "volatility expansion: ATR14 >= 1.5x its value 20 bars earlier"),
    ("trend_change", "EMA9 crossed EMA20 on the last completed bar"),
    ("rsi_extreme", "RSI14 above 75 or below 25"),
    ("funding", "funding anomaly: |funding| >= 0.05% per 8h (OKX)"),
    ("oi", "open interest moved >= 10% in 24h (OKX, all contracts)"),
]


def universe(f, top=50, quote="USDT"):
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    rows = f.json(url, "binance", "ticker24")
    keep = []
    for r in rows:
        s = r["symbol"]
        if not s.endswith(quote):
            continue
        b = s[:-len(quote)]
        if b in M.STABLECOINS or b.endswith(("UP", "DOWN", "BULL", "BEAR")) or b in M.FIAT:
            continue
        lp, hi, lo = float(r["lastPrice"]), float(r["highPrice"]), float(r["lowPrice"])
        if 0.97 <= lp <= 1.03 and hi - lo <= 0.01:
            continue  # trades like a dollar stablecoin (pinned near 1.00): nothing to scan
        keep.append((float(r["quoteVolume"]), b, float(r["priceChangePercent"])))
    keep.sort(reverse=True)
    return [(b, chg, qv) for qv, b, chg in keep[:top]], url


def scan(f, tf="1h", top=40, symbols=None, with_derivs=True, progress=None):
    if symbols:
        uni = [(s.upper().replace("USDT", ""), None, None) for s in symbols]
        src = "symbols given"
    else:
        uni, src = universe(f, top)
    btc = MD.candles(M.resolve("BTCUSDT"), tf, 120, "binance", f)
    btc_ret = None
    if btc.get("data"):
        bb = [b for b in btc["data"] if not b.get("forming")]
        btc_ret = bb[-1]["c"] / bb[-21]["c"] - 1 if len(bb) > 21 else None
    funding, oi24 = {}, {}
    if with_derivs:
        try:
            for x in f.json("https://www.okx.com/api/v5/public/funding-rate?instId=ANY", "okx", "funding-all")["data"]:
                if x["instId"].endswith("-USDT-SWAP"):
                    funding[x["instId"].split("-")[0]] = float(x["fundingRate"])
        except (MD.DataError, KeyError, ValueError):
            pass
    hits, failed = [], []
    for i, (base, _, _) in enumerate(uni):
        if progress:
            progress(i + 1, len(uni), base)
        env = MD.candles(M.resolve(base + "USDT"), tf, 120, "binance", f)
        if env.get("error"):
            failed.append("%s: %s" % (base, env["error"]))
            continue
        bars = [b for b in env["data"] if not b.get("forming")]
        if len(bars) < 45:
            failed.append("%s: only %d bars" % (base, len(bars)))
            continue
        cl = I.closes(bars)
        at = I.atr(bars)
        vols = [b["v"] for b in bars]
        v20 = sum(vols[-21:-1]) / 20.0
        rs = I.rsi(cl)[-1]
        e9, e20 = I.ema(cl, 9), I.ema(cl, 20)
        tags = []
        if v20 and vols[-1] >= 3 * v20:
            tags.append(("volume", "%.1fx avg volume" % (vols[-1] / v20)))
        chg = cl[-1] / cl[-2] - 1
        if at[-1] and abs(chg) * cl[-1] >= 2.5 * at[-1]:
            tags.append(("move", "%+.2f%% in one bar (%.1f ATR)" % (chg * 100, abs(chg) * cl[-1] / at[-1])))
        bo = I.breakout(bars, 20)
        if bo and bo["type"] == "breakout":
            tags.append(("breakout", "above %s by %.2f%%" % (_f(bo["level"]), bo["by_pct"])))
        if bo and bo["type"] == "breakdown":
            tags.append(("breakdown", "below %s by %.2f%%" % (_f(bo["level"]), bo["by_pct"])))
        if at[-1] and at[-21] and at[-1] >= 1.5 * at[-21]:
            tags.append(("vol_expansion", "ATR up %.0f%% in 20 bars" % ((at[-1] / at[-21] - 1) * 100)))
        if e9[-2] is not None and e20[-2] is not None:
            if e9[-2] <= e20[-2] and e9[-1] > e20[-1]:
                tags.append(("trend_change", "EMA9 crossed above EMA20"))
            elif e9[-2] >= e20[-2] and e9[-1] < e20[-1]:
                tags.append(("trend_change", "EMA9 crossed below EMA20"))
        if rs is not None and (rs > 75 or rs < 25):
            tags.append(("rsi_extreme", "RSI %.0f" % rs))
        fr = funding.get(base)
        if fr is not None and abs(fr) >= 0.0005:
            tags.append(("funding", "funding %+.3f%%/8h" % (fr * 100)))
        rel = None
        if btc_ret is not None and len(cl) > 21:
            rel = (cl[-1] / cl[-21] - 1) - btc_ret
        if tags:
            hits.append({"symbol": base + "USDT", "close": cl[-1], "bar_utc": MD.iso(MD.ts_utc(bars[-1]["t"])),
                         "tags": tags, "rs_vs_btc_20bars_pct": rel * 100 if rel is not None else None,
                         "funding_8h": fr, "rsi": rs})
    if with_derivs and hits:
        for h in hits[:15]:
            b = h["symbol"][:-4]
            try:
                rows = f.json("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=%s&period=1H"
                              % b, "okx", "oivol-" + b)["data"]
                if len(rows) > 24 and float(rows[24][1]):
                    ch = (float(rows[0][1]) / float(rows[24][1]) - 1) * 100
                    h["oi_change_24h_pct"] = ch
                    if abs(ch) >= 10:
                        h["tags"].append(("oi", "open interest %+.0f%% in 24h" % ch))
            except (MD.DataError, KeyError, ValueError, IndexError):
                pass
    hits.sort(key=lambda h: -len(h["tags"]))
    return {"tf": tf, "universe": src, "scanned": len(uni), "failed": failed, "hits": hits,
            "btc_return_20bars_pct": btc_ret * 100 if btc_ret is not None else None,
            "as_of_utc": MD.iso(MD.utc_now()), "sources": ["binance spot candles (data-api.binance.vision)",
                                                           "okx funding and open interest"],
            "note": "A scan lists charts that matched a rule. It is not a list of coins to buy."}


# --------------------------------------------------------------------------
# Position plan: size, liquidation, funding cost
# --------------------------------------------------------------------------

def liquidation_price(entry, leverage, side="long", mmr=0.005):
    """Isolated-margin, linear (USDT) contract, fees ignored:
    long  liq = entry * (1 - 1/leverage + mmr)
    short liq = entry * (1 + 1/leverage - mmr)"""
    if leverage <= 1:
        return None
    if side == "long":
        return entry * (1 - 1.0 / leverage + mmr)
    return entry * (1 + 1.0 / leverage - mmr)


def plan(entry, stop, target, account, risk_pct, leverage=1.0, side=None, fee_pct=0.1, funding_8h=None,
         hold_hours=0, atr_=None, mmr=0.005):
    if entry <= 0 or stop <= 0:
        raise CryptoError("entry and stop must be positive prices")
    side = side or ("long" if stop < entry else "short")
    if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
        raise CryptoError("the stop must be below the entry for a long, above it for a short")
    if target is not None and ((side == "long" and target <= entry) or (side == "short" and target >= entry)):
        raise CryptoError("the target must be on the profit side of the entry")
    risk_amt = account * risk_pct / 100.0
    per_unit = abs(entry - stop) + entry * fee_pct / 100.0 * 2  # stop distance plus round-trip fees
    qty = risk_amt / per_unit
    notional = qty * entry
    margin = notional / max(leverage, 1.0)
    warn = []
    if margin > account:
        qty_cap = account * max(leverage, 1.0) / entry
        warn.append("the risk budget needs %s of margin, more than the account; size capped to the account" % _f(margin))
        qty, notional, margin = qty_cap, qty_cap * entry, account
    liq = liquidation_price(entry, leverage, side, mmr)
    if liq is not None:
        if (side == "long" and liq >= stop) or (side == "short" and liq <= stop):
            warn.append("LIQUIDATION (%s) comes before the stop (%s): the stop would never fill. Lower the leverage."
                        % (_f(liq), _f(stop)))
        buffer_pct = abs(stop - liq) / entry * 100
        if buffer_pct < 1:
            warn.append("only %.2f%% between the stop and liquidation: a wick or slippage can liquidate you" % buffer_pct)
    if leverage > MAX_SANE_LEVERAGE:
        warn.append("leverage %.0fx: above %dx a normal day's move can wipe out the margin. This pack does not "
                    "encourage it." % (leverage, MAX_SANE_LEVERAGE))
    rr = abs(target - entry) / abs(entry - stop) if target is not None else None
    fund_cost = None
    if funding_8h is not None and hold_hours:
        periods = hold_hours / 8.0
        sign = 1 if side == "long" else -1
        fund_cost = notional * funding_8h * periods * sign
    vol_note = None
    if atr_:
        stops_in_atr = abs(entry - stop) / atr_
        vol_note = "the stop is %.1f ATR away%s" % (stops_in_atr, " (inside normal noise: likely to be hit by chance)"
                                                    if stops_in_atr < 1 else "")
    return {"side": side, "entry": entry, "stop": stop, "target": target, "qty": qty, "notional": notional,
            "margin": margin, "leverage": leverage, "risk_amount": min(risk_amt, qty * per_unit),
            "loss_at_stop_incl_fees": qty * per_unit, "reward_risk": rr,
            "profit_at_target": qty * abs(target - entry) - qty * entry * fee_pct / 100 * 2 if target else None,
            "liquidation_price": liq, "maintenance_margin_rate": mmr if liq else None,
            "funding_cost_estimate": fund_cost, "fee_pct_per_side": fee_pct, "volatility": vol_note, "warnings": warn}


def print_plan(p, sym, ccy):
    print("%s %s plan (a calculation, not an order)" % (sym, p["side"].upper()))
    print("  entry %s  stop %s  target %s  reward:risk %s" % (_f(p["entry"]), _f(p["stop"]), _f(p["target"]),
                                                              "%.2f" % p["reward_risk"] if p["reward_risk"] else "n/a"))
    print("  size %s units = %s %s notional; margin %s at %.1fx" % (_f(p["qty"]), _f(p["notional"]), ccy,
                                                                     _f(p["margin"]), p["leverage"]))
    print("  loss at the stop incl. fees %s %s; profit at the target after fees %s %s" % (
        _f(p["loss_at_stop_incl_fees"]), ccy, _f(p["profit_at_target"]), ccy))
    if p["liquidation_price"]:
        print("  liquidation about %s (isolated, maintenance margin %.2f%%, before fees and funding)" % (
            _f(p["liquidation_price"]), p["maintenance_margin_rate"] * 100))
    if p["funding_cost_estimate"] is not None:
        print("  funding over the hold: %s %s (%s pays) at today's rate, which changes every 8 hours" % (
            _f(abs(p["funding_cost_estimate"])), ccy, "you" if p["funding_cost_estimate"] > 0 else "you receive; the other side"))
    if p["volatility"]:
        print("  " + p["volatility"])
    for w in p["warnings"]:
        print("  WARNING: " + w)
    print("  A stop is a plan, not a guarantee: crypto gaps through stops in fast markets.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="Crypto market intelligence.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("overview")
    c = sub.add_parser("check")
    c.add_argument("symbol")
    t = sub.add_parser("token")
    t.add_argument("symbol")
    d = sub.add_parser("derivs")
    d.add_argument("symbol")
    s = sub.add_parser("scan")
    s.add_argument("--tf", default="1h", choices=["5m", "15m", "30m", "1h", "4h", "1d"])
    s.add_argument("--top", type=int, default=40, help="the N most traded USDT pairs on Binance spot")
    s.add_argument("--symbols", help="comma list instead of the top N")
    s.add_argument("--no-derivs", action="store_true")
    s.add_argument("--write", action="store_true", help="also write trader/crypto_watchlist.json")
    pl = sub.add_parser("plan")
    pl.add_argument("symbol")
    pl.add_argument("--entry", type=float)
    pl.add_argument("--stop", type=float, required=True)
    pl.add_argument("--target", type=float)
    pl.add_argument("--account", type=float, required=True, help="account size in the quote currency")
    pl.add_argument("--risk", type=float, default=1.0, help="percent of the account to lose at the stop (default 1)")
    pl.add_argument("--leverage", type=float, default=1.0)
    pl.add_argument("--side", choices=["long", "short"])
    pl.add_argument("--fee", type=float, default=0.1, help="taker fee percent per side (default 0.1)")
    pl.add_argument("--hold-hours", type=float, default=0)
    for x in (c, t, d, s, pl, sub.choices["overview"]):
        x.add_argument("--json", action="store_true")
        x.add_argument("--offline")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 1
    f = MD.Fetcher(offline=a.offline)
    try:
        if a.cmd == "overview":
            o = overview(f)
            print(json.dumps(o, indent=1, default=str)) if a.json else print_overview(o)
            print(NOTE) if not a.json else None
            return 0 if not o["errors"] else 1
        if a.cmd == "scan":
            inst = {}
        else:
            inst = M.resolve(a.symbol)
        if a.cmd == "scan":
            pass
        elif a.cmd in ("check", "token", "plan") and inst.get("asset_class") != "crypto":
            inst = M.resolve(a.symbol + "USDT") if inst.get("asset_class") is None else inst
        if a.cmd not in ("derivs", "scan") and inst.get("asset_class") != "crypto":
            print("%s is not a crypto pair (it resolves to %s on %s)" % (a.symbol, inst.get("asset_class"),
                                                                        inst.get("exchange")))
            return 1
        if a.cmd == "check":
            r = check(f, inst)
            if a.json:
                print(json.dumps(r, indent=1))
            else:
                print("%s listings checked %s UTC" % (r["instrument"], r["as_of_utc"]))
                for v in r["venues"]:
                    extra = ("  spread %.2f bps, 10-level depth %s" % (v["spread_bps"], _f(v["depth_10_levels_quote"]))
                             if v.get("spread_bps") is not None else "")
                    print("  %-9s %-12s %s%s%s" % (v["venue"], v["symbol"], "LISTED" if v["listed"] else "not listed",
                                                   (" (%s)" % v["note"]) if v["note"] and not v["listed"] else "", extra))
                if not r["verified"]:
                    print("  Not listed on any of the five exchanges checked: treat it as unverified and high risk.")
            return 0 if r["verified"] else 1
        if a.cmd == "token":
            r = token(f, inst)
            print(json.dumps(r, indent=1, default=str)) if a.json else print_token(r)
            return 0
        if a.cmd == "derivs":
            base = inst.get("base") or a.symbol.upper().replace("USDT", "")
            r = derivs(f, base)
            print(json.dumps(r, indent=1, default=str)) if a.json else print_derivs(r)
            return 0 if not r["errors"] else 1
        if a.cmd == "scan":
            syms = [x.strip() for x in a.symbols.split(",")] if a.symbols else None
            r = scan(f, a.tf, a.top, syms, not a.no_derivs,
                     progress=(lambda i, n, b: sys.stderr.write("\r  %d/%d %s     " % (i, n, b))) if not a.json else None)
            sys.stderr.write("\n")
            if a.write:
                os.makedirs("trader", exist_ok=True)
                with open(os.path.join("trader", "crypto_watchlist.json"), "w") as fh:
                    json.dump(r, fh, indent=1, default=str)
            if a.json:
                print(json.dumps(r, indent=1, default=str))
                return 0
            print("Crypto scan, %s bars, %d pairs (%s), %s UTC. BTC over 20 bars: %s" % (
                r["tf"], r["scanned"], r["universe"] if a.symbols else "top %d USDT pairs by Binance spot volume" % a.top,
                r["as_of_utc"], "%+.2f%%" % r["btc_return_20bars_pct"] if r["btc_return_20bars_pct"] is not None else "n/a"))
            for h in r["hits"]:
                rs = h.get("rs_vs_btc_20bars_pct")
                print("  %-12s %s  %s%s" % (h["symbol"], _f(h["close"]), "; ".join(x[1] for x in h["tags"]),
                                            ("  (vs BTC %+.1f%%)" % rs) if rs is not None else ""))
            if not r["hits"]:
                print("  nothing matched a rule")
            if r["failed"]:
                print("  %d could not be read: %s" % (len(r["failed"]), "; ".join(r["failed"][:5])))
            print("  " + r["note"])
            return 0
        if a.cmd == "plan":
            entry = a.entry
            q = MD.quote(inst, fetcher=f)
            if entry is None:
                if not q.get("data"):
                    print("no live price for %s and no --entry given: %s" % (inst["symbol"], q.get("error")))
                    return 1
                entry = q["data"]["price"]
            env = MD.candles(inst, "1h", 60, None, f)
            at = I.atr([b for b in env["data"] if not b.get("forming")])[-1] if env.get("data") else None
            fr = None
            try:
                fr = float(f.json("https://www.okx.com/api/v5/public/funding-rate?instId=%s-USDT-SWAP" % inst["base"],
                                  "okx", "funding-" + inst["base"])["data"][0]["fundingRate"])
            except (MD.DataError, KeyError, IndexError, ValueError):
                pass
            r = plan(entry, a.stop, a.target, a.account, a.risk, a.leverage, a.side, a.fee,
                     fr if a.leverage > 1 else None, a.hold_hours, at)
            if a.json:
                print(json.dumps(r, indent=1))
            else:
                if a.entry is None:
                    print("entry = live %s price %s (%s, %s UTC)" % (q["provider"], _f(entry), q["freshness"], q["as_of_utc"]))
                print_plan(r, inst["symbol"], inst["quote"])
            return 0
    except (CryptoError, MD.DataError, M.MarketError) as exc:
        print("crypto: %s" % exc, file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
