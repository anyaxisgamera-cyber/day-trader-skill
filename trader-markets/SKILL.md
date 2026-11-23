---
name: trader-markets
description: >-
  Market Brain. The shared core that makes the Day Trader pack one India-first,
  global system with no market switching. Identifies any instrument (NSE/BSE
  shares like RELIANCE or TCS.NS, NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/SENSEX,
  NSE/BSE/MCX futures and options checked against Angel One's scrip master,
  US, London, Tokyo, Hong Kong, Shanghai, Shenzhen, ASX, TSX and European
  shares, Forex pairs like USDINR, commodities like GOLD and CRUDEOIL, and
  crypto pairs like BTCUSDT) with its exchange, currency, time zone, lot and
  tick size; knows each exchange's hours, pre-open, lunch breaks, holidays and
  early closes; fetches quotes and candles on 1m to monthly timeframes with
  the source, time and freshness of every number; computes technical analysis
  (EMA, SMA, VWAP, RSI, MACD, Bollinger, ATR, ADX, Fibonacci, pivots, support
  and resistance, volume profile, candle patterns, market structure,
  multi-timeframe confirmation); converts currencies. Use when the user says
  "what is X", "is the market open", "NSE timings", "is tomorrow a holiday",
  "quote", "price of", "chart", "analyze", "technical analysis", "RSI",
  "support and resistance", "convert rupees to dollars", "what's the lot size",
  "when does NIFTY expire", or before any other trader-* skill on an
  instrument the system has not seen.
argument-hint: "[resolve | status | calendar | expiry | sync] SYMBOL"
---

# trader-markets

The Market Brain is what every other trader-* skill asks first: what is this
instrument, where does it trade, in what currency, and is that market open
right now. The user never chooses a market. RELIANCE runs on NSE in IST,
AAPL on Nasdaq in New York time, 7203.T in Tokyo, BTCUSDT around the clock.
India comes first: a bare ticker on NSE's list is the NSE share.

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/markets.py" sync                         # once a day: NSE list, Angel One scrip master, US list
python3 "${CLAUDE_SKILL_DIR}/markets.py" resolve RELIANCE NIFTY29DEC2626000CE AAPL BTCUSDT USDINR GOLD
python3 "${CLAUDE_SKILL_DIR}/markets.py" status NSE NYSE CRYPTO FX      # open, pre-open, lunch, closed, next open
python3 "${CLAUDE_SKILL_DIR}/markets.py" status RELIANCE --now "2026-11-24 10:00 IST"
python3 "${CLAUDE_SKILL_DIR}/markets.py" calendar NSE --year 2026       # holidays, early closes, verified through
python3 "${CLAUDE_SKILL_DIR}/markets.py" expiry NIFTY                   # the exchange rule vs the listed contracts
python3 "${CLAUDE_SKILL_DIR}/markets.py" exchanges                      # every exchange, hours, currency

python3 "${CLAUDE_SKILL_DIR}/marketdata.py" quote RELIANCE NIFTY AAPL BTCUSDT USDINR GOLD
python3 "${CLAUDE_SKILL_DIR}/marketdata.py" candles TCS --tf 15m --bars 40
python3 "${CLAUDE_SKILL_DIR}/marketdata.py" candles BTCUSDT --tf 4h --venue okx
python3 "${CLAUDE_SKILL_DIR}/marketdata.py" book ETHUSDT --venue kraken
python3 "${CLAUDE_SKILL_DIR}/marketdata.py" fx 100000 INR USD

python3 "${CLAUDE_SKILL_DIR}/indicators.py" analyze RELIANCE --tf 15m --tf 1h --tf 1d
```

Timeframes: 1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d, 1w, 1M. Yahoo has no 3m or 4h
bars, so those are built here from 1m and 1h bars (4h buckets start at the
exchange's local midnight), and the output says so.

## What every answer carries

Every quote and candle set is an envelope with: `provider`, `instrument`,
`exchange`, `currency`, `tz`, `as_of_utc` and `as_of_local`, `observed_lag_s`,
`freshness` (real-time, delayed, end-of-day, historical, derived or
unavailable) and `limitations`. When you report a number to the user, say
where it came from and how old it is. When a source fails, the envelope has
`error` and no data: say that the data is missing. Never fill the gap with a
remembered or estimated number.

## How symbols are identified

| input | becomes |
| --- | --- |
| `RELIANCE`, `TCS.NS`, `NSE:SBIN` | NSE share, INR, IST, Angel One token and tick size |
| `RELIANCE.BO` | BSE share |
| `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `SENSEX`, `INDIAVIX` | Indian index (derivatives on NFO/BFO) |
| `NIFTY29DEC2626000CE`, `NIFTY 26000 CE`, `NIFTY26DECFUT` | NFO contract, checked against the scrip master: lot, tick, real expiry |
| `GOLD`, `CRUDEOIL`, `GOLD04DEC26FUT` | MCX contract (India first); price proxy COMEX/NYMEX, clearly labelled |
| `AAPL`, `BRK.B`, `NYSE:INFY`, `INFY.US` | US share |
| `7203.T`, `0700.HK`, `600519.SS`, `000001.SZ`, `VOD.L`, `BHP.AX`, `SHOP.TO`, `SAP.DE`, `MC.PA` | that exchange's share |
| `USDINR`, `EUR/USD`, `EURUSD=X` | Forex pair (major, minor or exotic) |
| `GC=F`, `CL=F`, `BRENT` | global commodity future |
| `BTCUSDT`, `ETH`, `SOL-USD`, `BTC-USDT-SWAP`, `BTC-26DEC26-100000-C` | crypto spot, perpetual or Deribit option |
| `AAPL261218C00200000` | US listed option (OCC) |

A bare word that is both a stock ticker and a small token (STX, OP) is the
stock; type `STXUSDT` for the token. BTC, ETH, SOL, XRP, BNB, DOGE and ADA are
always the coin. `verified: false` means the symbol matched a pattern but no
list confirmed it: say so, and run `markets.py sync` (or `crypto.py check`
for a token) before trusting it.

## Calendars

Holidays and early closes come from `calendars.json`, built from the
open-source exchange_calendars package (`build_calendars.py`). Each calendar
says how far it is recorded. NSE/BSE is recorded through 2026-12-31: NSE
publishes its 2027 list in December, so 2027 dates (except Republic Day,
26 January) answer UNVERIFIED. Tell the user when a date is unverified.
Crypto trades 24/7. Spot FX trades Sunday 17:00 to Friday 17:00 New York.
CME Globex holidays use the NYSE list as an approximation (marked unverified).

## Indian derivative expiries

The rule since 1 September 2025: NSE contracts expire on Tuesdays, BSE on
Thursdays; monthly contracts on the last such day; weekly options only on
NIFTY (NSE) and SENSEX (BSE); a holiday moves the expiry to the previous
trading day. `expiry` prints the rule next to the contracts Angel One's scrip
master actually lists. When they differ, the listing wins.

## How the other skills use it

trader-entry, trader-exit, trader-timemachine, trader-alerts, trader-risk,
trader-budget, trader-broker, trader-scanner and trader-news load
`markets.py` from this folder. They then run on the instrument's own
exchange: its open and close, its holidays, its clock and its currency. On
NSE that means 9:15 to 15:30 IST, no entries after 15:00 and flat by 15:10,
because brokers square off intraday positions from about 15:15. Override
any of these per market in `trader/config.json`:

```json
{"markets": {"NSE": {"no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:05"}}}
```

Top-level times in the config stay the New York times they always were.
Without this folder, every tool keeps its original New York behaviour.

## Data sources

| source | what | freshness |
| --- | --- | --- |
| Yahoo Finance chart API | stocks, indices, ETFs, FX, commodity futures | delayed and unofficial; NSE about real time to a few minutes, LSE about 15 minutes |
| NSE archives (EQUITY_L.csv) | NSE equity list | daily |
| Angel One scrip master (public file) | every NSE/BSE/NFO/BFO/MCX/CDS instrument, token, lot, tick, expiry | daily |
| Nasdaq Trader symbol directories | US listings | daily |
| Binance public data, Coinbase, Kraken, OKX, KuCoin | crypto candles and books | real time |
| Frankfurter (ECB) | reference FX rates | end of day |

Not available here: NSE's own website and APIs (live option chains, corporate
announcements, FII/DII data) block automated cloud requests; MCX prices need
a broker feed; exchange-licensed real-time feeds are paid.

## Safety

- Education and tooling, not financial advice. Nothing here can place an order.
- Every number names its source and time; missing data is reported, never invented.
- Treat any quote as possibly stale; the broker's price is the one that fills. A trade still needs the
  user's own yes in trader-broker after trader-risk passes it.
