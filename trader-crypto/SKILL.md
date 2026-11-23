---
name: trader-crypto
description: >-
  Crypto Desk. Global cryptocurrency intelligence from public exchange and
  on-chain data: market overview (total market cap, BTC and ETH dominance,
  stablecoin share, Fear & Greed, DeFi TVL, Bitcoin fees and mempool,
  Ethereum gas, Deribit implied volatility); listing checks across Binance,
  OKX, Coinbase, Kraken and KuCoin with spreads and depth; token fundamentals
  and risk (market cap, FDV, supply, unlock overhang, liquidity, meme and
  stablecoin flags, look-alike symbols); derivatives (funding rates,
  annualised funding, open interest and its 24h change, long/short account
  ratio, recent liquidations and their price clusters, Deribit options put/
  call and ATM IV); a multi-exchange scanner (unusual volume, sudden moves,
  breakouts, breakdowns, volatility expansion, trend changes, RSI extremes,
  funding anomalies, open-interest jumps, relative strength vs BTC); and
  position plans with volatility-aware sizing, liquidation price, leverage
  warnings, fees and funding cost. Use when the user says "how is crypto",
  "bitcoin dominance", "fear and greed", "funding rate", "open interest",
  "liquidations", "is this coin legit", "is X listed on Binance", "scan
  crypto", "altcoins moving", "liquidation price", "how much leverage",
  "size my BTC trade", or names any coin or pair.
argument-hint: "[overview | check | token | derivs | scan | plan] SYMBOL"
---

# trader-crypto

Crypto is a first-class market in this pack, not an add-on: it trades 24/7,
on many exchanges at once, with leverage and funding that stocks do not have.
This skill reads that structure from public data and does the arithmetic.
It never trades, never connects to an exchange account, and never treats a
scan hit as an opportunity.

`${CLAUDE_SKILL_DIR}` below is this skill's folder. It uses the shared
trader-markets skill next to it for symbols, candles and indicators.

```bash
python3 "${CLAUDE_SKILL_DIR}/crypto.py" overview
python3 "${CLAUDE_SKILL_DIR}/crypto.py" check PEPEUSDT
python3 "${CLAUDE_SKILL_DIR}/crypto.py" token SOL
python3 "${CLAUDE_SKILL_DIR}/crypto.py" derivs BTC
python3 "${CLAUDE_SKILL_DIR}/crypto.py" scan --top 60 --tf 1h --write    # -> trader/crypto_watchlist.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" scan --symbols BTCUSDT,ETHUSDT,SOLUSDT --tf 4h
python3 "${CLAUDE_SKILL_DIR}/crypto.py" plan BTCUSDT --stop 60200 --target 64100 --account 2000 --risk 1 --leverage 2 --hold-hours 24
```

For charts and indicators on any pair and timeframe (1m to 1M) use
trader-markets: `indicators.py analyze BTCUSDT --tf 1h --tf 4h --tf 1d`, and
`marketdata.py book BTCUSDT --venue okx` for the order book.

## What each command reads

| command | sources | notes |
| --- | --- | --- |
| overview | CoinGecko global, alternative.me, DefiLlama, mempool.space, Ethereum RPC, Deribit DVOL | each line names its source and time |
| check | Binance, OKX, Coinbase, Kraken, KuCoin listing endpoints and books | a pair is verified only when a real exchange lists it |
| token | CoinGecko search and markets | flags: FDV overhang, small cap, thin or frantic volume, meme, stablecoin peg, look-alike symbols, no max supply |
| derivs | OKX funding, open interest, rubik long/short and OI history, liquidation orders; Deribit options | OKX's view only: other venues differ |
| scan | Binance spot candles (top N USDT pairs by volume), OKX funding and OI | stablecoin-like pairs are skipped |
| plan | live price, ATR, OKX funding | isolated linear USDT contract math |

## Reading the numbers honestly

- Funding is per 8 hours on OKX USDT swaps. Positive: longs pay shorts. The
  annualised figure assumes today's rate holds, which it never does.
- The long/short ratio counts accounts, not money.
- Liquidations are the most recent ones OKX publishes, a short window, not
  the whole market.
- Liquidation price assumes isolated margin, a 0.5% maintenance rate and no
  fees; the exchange's own figure is the real one. If liquidation comes
  before the stop, the stop never fills: the plan says so.
- Above 3x leverage the plan warns every time. Do not encourage leverage;
  if the user asks for high leverage, show the liquidation distance and the
  warning plainly.

## Not available (and what to do instead)

- **Binance derivatives**: api.binance.com and fapi.binance.com answer HTTP
  451 from US cloud servers. Binance spot comes from its public data mirror;
  derivatives come from OKX. Bybit answers 403 from here.
- **Whale transactions, exchange inflows and outflows, wallet activity,
  token holders**: these need paid APIs. When the Nansen connector is enabled
  in Claude, use it (token flows, who bought or sold, top holders, smart-money
  balances) and cite it; otherwise say the data is not available.
- **Token unlock calendars**: not in a free API. `token` shows the size of
  the overhang (FDV vs market cap); dates come from the project's tokenomics.
- **INR pairs** (CoinDCX, WazirX): not wired in. Convert USDT prices with
  trader-markets `fx`. India taxes crypto gains at 30% plus 1% TDS.

## Safety

- Education and tooling, not financial advice. A scan lists charts that matched a rule, not coins to buy.
- New and small tokens can be scams, rug pulls or honeypots: check the listing, the contract and the
  liquidity before anything else, and treat an unverified pair as high risk.
- Nothing here places an order or holds exchange keys. Any trade is the user's own decision and the user's
  own yes, placed by the user on their exchange.
