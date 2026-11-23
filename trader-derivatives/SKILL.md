---
name: trader-derivatives
description: >-
  F&O Desk. Futures and options analytics with real contract specifications:
  NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX and NSE stock F&O, MCX
  commodity futures and options, NSE currency derivatives, US listed options
  and crypto options and futures. Reads lot size, tick size and listed
  expiries from Angel One's scrip master; checks expiries against the NSE/BSE
  rule (Tuesday/Thursday, holiday-adjusted); prices options and computes
  Greeks (Black-Scholes-Merton, Black-76) and implied volatility; analyses
  option chains (put-call ratio by OI and volume, max pain, OI walls, OI
  change, ATM IV, skew, the move implied to expiry) live for crypto from
  Deribit and from an exported CSV for NSE and US chains; futures basis,
  cost-of-carry fair value and rollover; OI build-up readings; and payoffs of
  option strategies (spreads, straddles, strangles, condors). Use when the
  user says "lot size", "when does BANKNIFTY expire", "option chain", "PCR",
  "max pain", "open interest", "IV", "greeks", "delta", "theta", "basis",
  "rollover", "long build-up", "short covering", "payoff", "breakeven",
  "straddle", "iron condor", or names an F&O contract.
argument-hint: "[contract | expiries | greeks | iv | chain | basis | buildup | payoff]"
---

# trader-derivatives

Derivatives are leveraged and they expire. This skill gets the contract facts
right first (lot, tick, expiry, style) and then does the option and futures
arithmetic from inputs it can show. It never trades.

`${CLAUDE_SKILL_DIR}` below is this skill's folder. It needs the shared
trader-markets skill next to it, and `markets.py sync` for contract specs.

```bash
python3 "${CLAUDE_SKILL_DIR}/fno.py" contract NIFTY29DEC2626000CE --price 120
python3 "${CLAUDE_SKILL_DIR}/fno.py" contract "BANKNIFTY 52000 PE"         # nearest listed expiry
python3 "${CLAUDE_SKILL_DIR}/fno.py" expiries NIFTY
python3 "${CLAUDE_SKILL_DIR}/fno.py" greeks --spot 25370 --strike 25500 --days 6 --iv 13 --right CE --rate 6.5 --lot 65
python3 "${CLAUDE_SKILL_DIR}/fno.py" iv --spot 25370 --strike 25500 --days 6 --price 82 --right CE --rate 6.5
python3 "${CLAUDE_SKILL_DIR}/fno.py" chain BTC                           # live Deribit chain
python3 "${CLAUDE_SKILL_DIR}/fno.py" chain NIFTY --file nifty.csv --spot 25370
python3 "${CLAUDE_SKILL_DIR}/fno.py" basis --spot 25370 --future 25480 --days 36 --rate 6.5 --oi-near 1.2e6 --oi-next 8e5
python3 "${CLAUDE_SKILL_DIR}/fno.py" crypto-basis BTC                   # Deribit futures vs index
python3 "${CLAUDE_SKILL_DIR}/fno.py" buildup --price-change 1.2 --oi-change 8.5
python3 "${CLAUDE_SKILL_DIR}/fno.py" payoff --spot 25370 --leg "buy CE 25500 82" --leg "sell CE 25700 31" --lot 65
```

## Chains you export

NSE's option-chain page and API block automated cloud requests, and Yahoo's
US chain needs a browser session, so Indian and US chains come from a CSV
the user downloads (NSE option-chain page "Download CSV", or the broker's
chain). Two layouts are read:

- long: one row per strike and side, columns `strike, type (CE/PE), oi, oi_change, volume, iv, ltp`
- wide (NSE style): `CE_OI, CE_CHNG_OI, CE_VOLUME, CE_IV, CE_LTP, STRIKE, PE_LTP, PE_IV, PE_VOLUME, PE_CHNG_OI, PE_OI`

The output names the file as its source. Its time is whenever the user
exported it: ask, and say how old it is.

## Facts to keep straight

- NSE index and stock options are European style. Stock F&O settles physically at expiry: a long stock
  call held into expiry becomes a delivery obligation.
- Lot sizes change (NIFTY is 65 in the current scrip master): always read them, never recall them.
- Many brokers do not accept SL-M (stop-loss market) orders in NSE F&O, so F&O stops are written as
  stop-limit, which can be skipped in a gap.
- Use the risk-free rate the user gives (India: about the 91-day T-bill). The model value is not a
  tradable price; IV from a stale premium is stale.
- Max pain and OI walls describe where open interest sits, not where the price will go.

## Safety

- Education and tooling, not financial advice. SEBI's studies found about nine in ten individual F&O
  traders lost money; say so when the user is new to options.
- Options and futures are off in trader-risk unless the config sets `"allow_options": true` or
  `"allow_futures": true`, and every order still needs the user's own yes in trader-broker.
- Selling options can lose far more than the premium: the payoff shows "unlimited" when it is.
