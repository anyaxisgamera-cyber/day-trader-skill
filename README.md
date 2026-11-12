# The Day Trader skill (curated, research-only)

A curated fork of the Day Trader skill pack, pinned to the audited commit `3d0f4ef`.
Seven research and education skills are kept. Order execution, entry/exit timing and the
congressional-trade tracker are removed. There is no broker connection and nothing in this
repository can place an order.

Original project: Jake Schincariol (MIT). See `LICENSE`.

## What is installed

The skills live in `.claude/skills/`, where Claude Code finds them automatically when it is
started in this folder (project-local install).

| command | job |
| --- | --- |
| `/trader-scanner` | Scans US-listed stocks for setups (opening-range breakouts, flags, oversold). |
| `/trader-news` | Headlines, SEC 8-K filings and the day's move for the tickers you name. |
| `/trader-timemachine` | Replays a paper plan 10,000 times from the stock's own recent bars, plus the real history. |
| `/trader-analyst` | Quarterly revenue, margins and EPS from SEC EDGAR XBRL facts. |
| `/trader-risk` | Checks a paper order against your limits: PASS or BLOCK, educational only. |
| `/trader-budget` | Sizes a paper position to a budget and a max loss, or splits a budget by weights. |
| `/trader-alerts` | Manual price and market-clock checks. Runs only when you type `/trader-alerts`. |

Removed: `trader-broker`, `trader-entry`, `trader-exit`, `trader-pelosi`.

## Install

Use this fork at the audited commit. Do not use the upstream install commands.

```bash
git clone https://github.com/anyaxisgamera-cyber/day-trader-skill.git
cd day-trader-skill
git checkout claude/day-trader-skill-audit-euil8t   # the curated branch
claude                                              # start Claude Code here
```

To use the skills in another project, copy the folders from `.claude/skills/` into that
project's `.claude/skills/` and copy `.claude/settings.json` and `CLAUDE.md` with them.

The tools need Python 3.8 or newer and nothing else. For SEC data (news filings, the
analyst), set your identity in an environment variable rather than in a committed file:

```bash
export SEC_USER_AGENT="Your Name you@example.com"
```

## Safety configuration

- `.claude/settings.json`: normal permission mode, auto and bypass modes disabled, no allow
  rules, broker MCP servers and scheduling tools denied, `/loop` switched off, and
  `trader-alerts` hidden from automatic use so it runs only when you invoke it.
- `CLAUDE.md`: the same rules for Claude in plain words.
- `.gitignore`: `trader/` (your working data and any personal config) is never committed.

## The tools

All standard-library Python, run from the repository root:

```bash
python3 .claude/skills/trader-scanner/scan.py orb --at 10:15
python3 .claude/skills/trader-news/news.py --tickers TSLA --hours 12
python3 .claude/skills/trader-timemachine/timemachine.py run --symbol TSLA --date 2026-10-19 \
    --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM"
python3 .claude/skills/trader-analyst/facts.py TSLA
python3 .claude/skills/trader-budget/size.py --write            # needs a hand-written trader/plan.json
python3 .claude/skills/trader-risk/check.py
python3 .claude/skills/trader-alerts/alerts.py quote TSLA
```

Offline tests (fixtures only, no network):

```bash
python3 -m unittest discover -s tests -v
```

## Fine print

**Not financial advice.** This is software that reads public data and does arithmetic. Most
people who day trade lose money.

**Research and paper practice only.** Orders written by `/trader-budget` are local files for
practice. Nothing sends them to a broker.

**The Time Machine is a simulation, not a prediction.** It is built from the last 60 days of
5-minute bars. Earnings, halts and headlines can do things those days never did.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an official API:
it can lag, change or rate-limit you. SEC EDGAR asks for a real name and email in the
User-Agent.

**Calendar.** NYSE holidays are built in for 2026 and 2027 only.

**Taxes.** Nothing here is tax advice.
