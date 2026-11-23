# Day Trader v3: one system, India first, global and crypto

This document records how the v2 pack (eleven US day-trading skills) became
one India-first system that covers global stocks, derivatives, Forex,
commodities and crypto, what each part can and cannot do, and the evidence
behind every "tested" in the capability table. It was written on 2026-11-23,
when every live source below was checked from a US cloud server.

## 1. Audit of v2 (before any change)

- **Eleven skills, not seven.** scanner, news, entry, exit, timemachine, risk,
  budget, broker, alerts, analyst, pelosi. All eleven are kept, with the
  same commands, files and outputs for US symbols. 315 offline tests passed
  before the change and still pass after it.
- **Self-contained scripts.** Each skill's Python script is standard-library
  only and carries its own copy of the New York clock: `eastern_offset`,
  `OPEN = 9:30`, `CLOSE = 16:00`, `EARLY = 13:00`, the 2026-2027 NYSE holiday
  and early-close lists, and messages that say "ET" and "New York".
- **US-only assumptions found:** NYSE hours and holidays in 8 scripts;
  Yahoo symbol mangling (`.` to `-`, which breaks `RELIANCE.NS`); a ticker
  regex of 1-5 letters in trader-risk (rejects RELIANCE, M&M); dollar signs
  and cent rounding; 78 five-minute bars a session in the time machine;
  `*_usd` money limits; the Nasdaq Trader universe; SEC 8-Ks and XBRL in
  news and analyst; House PTRs in pelosi.
- **Alpaca-specific:** orders.json uses Alpaca `place_stock_order` fields
  (bracket `take_profit_limit_price` / `stop_loss_stop_price`); trader-risk
  validates those fields; trader-broker renders Alpaca or IBKR tickets.
- **Duplication:** the NY clock and NYSE lists are repeated in 8 scripts (by
  design: each skill can be installed alone). Nothing conflicted.

## 2. Architecture

```
                         trader-markets  (the shared market brain)
        markets.py     instrument resolver, exchange registry, time zones, calendars,
                       sessions, Indian expiry rules, scrip-master lookups, bind/adopt
        marketdata.py  provider layer with provenance envelopes, FX conversion
        indicators.py  technical analysis
        calendars.json holidays and early closes (built by build_calendars.py)
                |
   +------------+-------------------------------------------+
   |                                                        |
 original eleven skills (unchanged CLIs)            new desks
   use_market(symbol) -> MK.adopt(globals(), symbol)  trader-crypto/crypto.py
     rebinds OPEN/CLOSE/EARLY, the holiday lists,     trader-derivatives/fno.py
     the clock functions, session-bar constants,
     and rewrites "New York"/"ET"/"$" in output
```

**No market switching.** Every tool resolves the symbol it is given and
binds itself to that instrument's exchange for the run: RELIANCE gets NSE
hours, holidays, IST and rupees; AAPL gets the original New York behaviour,
byte for byte. When trader-markets is not installed next to a skill, the
skill runs exactly as in v2.

**India first.** A bare ticker on NSE's list (bundled snapshot, or the synced
list) is the NSE share; when it is also a US ticker (INFY), the US listing is
offered as the alternative. Bare tickers in a scanner universe file stay US
(the file format the v2 scanner always wrote); NSE universes carry `.NS`.

**Provenance.** Every data result is an envelope: provider, instrument,
exchange, currency, tz, as_of (UTC and local), observed lag, freshness
(real-time, delayed, end-of-day, historical, derived, unavailable) and
limitations. A failed source returns `error` and no data.

**Python 3.8, standard library only** at run time, like v2. Time zones are
hand-written rules (no zoneinfo); the tests compare them with the system tz
database hour by hour across 2026-2027.

## 3. Data providers (checked 2026-11-23 from a US cloud server)

| provider | used for | access | status |
| --- | --- | --- | --- |
| Yahoo Finance chart API | stocks, indices, ETFs, FX, commodity futures, 1m-1M candles | keyless, unofficial | works; delayed (LSE about 15 min); can 429 |
| Yahoo option chains | US chains | needs a browser cookie from fc.yahoo.com | **blocked**: chains via exported CSV |
| NSE archives (EQUITY_L.csv) | NSE equity list | keyless | works |
| nseindia.com website/API | live option chain, OI, announcements, FII/DII, holiday API | Akamai bot protection | **blocked** (HTTP 403) |
| Angel One scrip master (public JSON) | tokens, lot sizes, ticks, expiries for NSE/BSE/NFO/BFO/MCX/CDS | keyless | works (180,367 instruments) |
| Angel One SmartAPI | live Indian prices, chains, order placement | login with PIN + TOTP | **not used by design** (no credential storage); manual tickets |
| Nasdaq Trader symbol directories | US listings | keyless | works |
| Binance data-api.binance.vision | spot candles, books, 24h tickers | keyless | works |
| api.binance.com, fapi.binance.com | Binance spot API, futures (funding, OI) | | **blocked** (HTTP 451) from US servers |
| OKX public API | spot and perpetual candles, books, funding, OI, long/short, liquidations | keyless | works |
| Coinbase Exchange, Kraken, KuCoin | candles, books, listings | keyless | works |
| Bybit | | | **blocked** (HTTP 403) from US servers |
| Deribit public API | options book (OI, mark IV), futures, index, DVOL | keyless | works |
| CoinGecko | market cap, FDV, supply, dominance | keyless free tier (rate-limited) | works |
| alternative.me | Crypto Fear & Greed (daily) | keyless | works |
| DefiLlama | DeFi TVL | keyless | works |
| mempool.space, Ethereum RPC (publicnode) | BTC fees and mempool, ETH gas | keyless | works |
| Frankfurter (ECB) | reference FX rates | keyless | works (end of day) |
| SEC EDGAR, House Clerk | US filings, PTRs (v2) | keyless, user agent | unchanged |
| exchange_calendars (pip, build time only) | holiday tables in calendars.json | open source | NYSE output equals v2's list from nyse.com |

Paid or keyed sources this build does not use: whale-alert and exchange-flow
feeds (Glassnode, CryptoQuant, Whale Alert), token-unlock calendars, economic
calendars, FRED/EIA (free key), licensed real-time exchange feeds. In Claude,
the Nansen connector (on-chain flows, holders) and the Financial Datasets
connector (US fundamentals, interest rates, macro) can fill some of these
when the user has them enabled; the SKILL.md files say when.

## 4. Capability matrix

Status: **Tested** = implemented and covered by an offline test on recorded
real data (and run live on 2026-11-23); **Implemented** = runs live, no
dedicated test; **Partial** = works with a stated gap; **Blocked** = needs a
credential or a source that refuses this server; **Not implemented**.

| area | capability | status | evidence |
| --- | --- | --- | --- |
| Identity | NSE/BSE shares, India first, BSE `.BO`, ambiguity alternatives | Tested | test_markets Resolver |
| Identity | NSE/BSE indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, VIX...) | Tested | test_markets test_indices |
| Identity | NFO/BFO/MCX/CDS contracts verified against the scrip master | Tested | test_markets test_indian_derivatives... |
| Identity | US, LSE, TSE, HKEX, SSE, SZSE, ASX, TSX, Xetra, Euronext, SIX, KRX, SGX, JSE | Tested (suffix and currency) | test_markets test_suffixes_and_us |
| Identity | Forex majors/minors/exotics, INR note | Tested | test_forex_and_commodities |
| Identity | Crypto spot, perpetuals, Deribit options; stock vs token collisions | Tested | test_crypto, test_stock_tickers_beat... |
| Identity | Invalid symbols reported, never guessed silently | Tested | test_invalid_symbols |
| Clock | Time zones incl. DST for US, UK, EU, Sydney | Tested vs tz database | test_every_rule_matches_the_tz_database |
| Clock | Exchange holidays and early closes, verified-through dates | Tested | Calendars tests; NYSE equals v2 list |
| Clock | NSE pre-open, closing session; lunch breaks (TSE, HKEX, SSE, SGX); MCX DST-linked close; 24/7 crypto; FX week; CME break | Tested | Sessions tests |
| Clock | NSE/BSE 2027 holidays | Partial | not published yet; reported UNVERIFIED |
| Clock | Muhurat (Diwali) special sessions, CME holiday sessions | Not implemented | stated in notes |
| Expiry | NSE Tuesday / BSE Thursday rule, weekly NIFTY/SENSEX, holiday shift; vs listed | Tested | Expiries tests (Nov 23, Dec 29, Jan 25) |
| Data | Provenance envelope on every result; failure returns error, no data | Tested | test_marketdata Envelope |
| Data | Candles 1m-1M; 3m and 4h resampled (4h anchored to local midnight) | Tested | Resample tests |
| Data | Crypto candles on Binance, OKX, Coinbase, Kraken, KuCoin; books with spread and imbalance | Tested | test_crypto_candles_each_venue, test_book |
| Data | Quote with correct previous close | Tested | test_quote_previous_close_is_yesterdays (a bug found and fixed here) |
| Data | FX conversion (Yahoo intraday, ECB end of day), stablecoin note | Tested | test_fx |
| Data | Indian F&O and MCX prices, NSE option chain, OI, PCR from NSE | Blocked | NSE 403; SmartAPI needs login |
| TA | EMA, SMA, RSI, MACD, Bollinger, ATR, ADX/DI | Tested vs `ta` library (1e-6) | test_matches_the_ta_library |
| TA | VWAP (per session), pivots, Fibonacci, S/R clusters, volume profile, candle patterns, structure, breakout, multi-TF | Tested | Indicators tests |
| TA | Chart patterns beyond breakout/structure (head and shoulders, triangles...) | Not implemented | |
| India pipeline | scanner (NSE universe, ORB on 9:15), entry, exit (tick, 15:10), time machine (75 bars, gap disclosure), budget (INR, lots, FX), risk (NSE windows, holidays, BE/BZ, lots, futures gate), broker (Angel One ticket), alerts | Tested end to end | test_india Pipeline (TCS, Nov 23 2026) |
| Global pipeline | the same tools on LSE, ASX, TSX, Xetra, Euronext, SIX, KRX, JSE sessions | Implemented | binding is generic; not separately tested |
| Global pipeline | time machine and ORB on exchanges with lunch breaks | Not implemented | refused with a message |
| Crypto | overview: market cap, BTC/ETH dominance, Fear & Greed, TVL, BTC fees, ETH gas, DVOL | Tested | test_crypto test_overview |
| Crypto | listing check across 5 exchanges with spread and depth | Tested | test_listing_check |
| Crypto | token fundamentals, FDV overhang, liquidity, meme/stablecoin/look-alike flags | Tested | test_token_risk_flags |
| Crypto | funding (current, 7-day, annualised), OI and 24h change, long/short, liquidations and clusters, Deribit options PCR/IV | Tested | test_derivs |
| Crypto | scanner: volume, move, breakout/down, vol expansion, EMA cross, RSI, funding, OI, RS vs BTC | Tested | test_scan |
| Crypto | plan: risk sizing with fees, liquidation price, leverage warnings, funding cost | Tested | Plan tests |
| Crypto | Binance/Bybit derivatives data | Blocked | HTTP 451 / 403 from this server |
| Crypto | whale transactions, exchange flows, wallet activity, unlock schedules | Blocked (paid APIs) | Nansen connector where enabled |
| F&O | contract specs from the scrip master, time to expiry, notional | Tested | test_india, test_markets |
| F&O | BSM and Black-76 prices and Greeks; IV | Tested vs py_vollib (1e-8) | test_fno Models |
| F&O | chain analytics (PCR, max pain, OI walls, ATM IV, skew, implied move) | Tested | test_fno Chains; live Deribit |
| F&O | basis, fair value, rollover, OI build-up, strategy payoffs | Tested | FuturesAndPayoffs |
| F&O | IV rank / historical IV percentile | Not implemented | needs a stored IV history |
| Forex | quotes, candles, indicators, classification, conversion | Tested (USDINR, EURUSD) | |
| Forex | economic calendar, central-bank decisions | Not implemented | no free keyless source; Financial Datasets connector has rates/macro |
| Commodities | MCX contract identity and specs; COMEX/NYMEX/ICE/CBOT benchmarks as labelled proxies | Tested | test_forex_and_commodities |
| Commodities | MCX prices, inventory reports (EIA), positioning (CFTC COT) | Blocked / Not implemented | |
| Fundamentals | US: SEC XBRL (v2) | Tested (v2) | test_analyst |
| Fundamentals | India: NSE/BSE results, SEBI disclosures, FII/DII | Blocked | NSE 403; clear message, paste-in workflow |
| Broker | Angel One manual tickets with real tokens; SL-M equity, SL-L for F&O; paper = record only | Tested | test_india step6, Security |
| Broker | Angel One SmartAPI live connection | Not implemented by design | see section 6 |
| Security | no credential storage or login code; no order-placing calls; tickets never claim placement | Tested | test_india Security |

## 5. Configuration

- `trader/config.json`: v2 keys unchanged. New: `"account_currency"` (money
  limits are in this currency; the `_usd` names are historical),
  `"markets": {"NSE": {...}}` per-market time overrides, `"allow_futures"`,
  `"broker": "angelone"`. See `trader-budget/config.india.example.json`.
- `trader/markets/`: written by `markets.py sync` (NSE list, condensed Angel
  One scrip master, US list). Without it, the bundled NSE snapshot
  (2026-11-23) identifies NSE shares; contracts are then unverified.
- `TRADER_MARKETS_DIR` / `TRADER_MARKETS_DATA`: point the skills at another
  trader-markets folder or symbol-data folder.

## 6. Security and human control

- No tool logs in to a broker or exchange, and none stores or asks for a
  password, PIN, TOTP secret or API secret. The test suite greps every tool
  for credential handling and order-placing calls.
- Angel One orders are manual tickets marked PROPOSED. A yes is recorded as
  "approved (manual: not sent)". Nothing is called placed until the user
  reports Angel One's order id.
- The v2 gates stand for Alpaca: trader-risk PASS for that exact order, the
  user's explicit yes, and Claude's Allow once card.
- No background monitoring is started by any tool. `watch` and alerts run
  only when the user runs them.
- **A future SmartAPI integration** belongs on the user's own computer: a
  SmartAPI client there logs in with the user's own credentials (kept in that
  machine's environment or keychain, never in a chat or this repository),
  receives only a ticket that trader-risk passed and the user approved,
  places it, and reports Angel One's order id back. It must never run
  unattended or retry on its own.

## 7. Test evidence (2026-11-23)

```
python3 -m unittest discover -s tests
```

- v2 suites: 315 tests, unchanged and passing.
- New suites: test_markets (31), test_marketdata (16), test_crypto (12),
  test_fno (14), test_india (9). They run offline on real responses recorded
  on 2026-11-23 (tests/fixtures/trader-markets, trader-crypto,
  trader-derivatives, india).
- Cross-checks against independent references: every time-zone rule against
  the system tz database (2026-2027, every 7 hours); indicators against the
  `ta` package; option prices and Greeks against `py_vollib`; the NYSE
  holiday list against v2's list from nyse.com; NSE expiry rules against
  Angel One's listed contracts.
- Live runs on 2026-11-23: NSE ORB scan of 100 shares in 12 s; NSE daily scan;
  TCS entry -> exit -> time machine -> budget -> risk -> Angel One ticket;
  crypto overview, derivs, token, check and a 25-pair scan; the BTC Deribit
  chain and futures basis.

## 8. Known limitations

- Yahoo is unofficial and delayed for many exchanges; Yahoo's NSE 5-minute
  feed drops a few bars a day (the time machine fills up to four as flat bars
  and says so).
- NSE/BSE 2027 holidays are unverified until NSE publishes them.
- Lunch-break exchanges are not supported by the ORB scanner and the time
  machine.
- Liquidation prices are estimates (isolated margin, 0.5% maintenance, no
  fees); the exchange's figure is the real one.
- Coin lists, sectors and the stock-vs-token rule are curated by hand and
  will age; `crypto.py check` is the real test of a pair.
