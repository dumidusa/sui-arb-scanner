# Sui Arb Scanner 🔍

**A read-only research tool that measures how much money arbitrage bots really make on the Sui blockchain — before building a trading bot.**

![python](https://img.shields.io/badge/python-3.10%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-brightgreen) ![mode](https://img.shields.io/badge/mode-read--only-lightgrey) ![version](https://img.shields.io/badge/version-0.3.0-orange)

> **Note:** This is a personal research project, built mostly with AI assistance (Claude).
> I used it to check real on-chain data before deciding which blockchain to build a
> trading system on. It **never signs or sends a transaction**.

## Why I built it

```
Idea: "Sui is new and less crowded → easy arbitrage money?"
          ↓
Don't guess — measure it on mainnet
          ↓
Scanner reads 14 days of real transactions
          ↓
Answer: how big is the market, who wins, how fast gaps close
```

## What I found (real mainnet run, v0.3)

| Question | Result |
|---|---|
| Arbitrage profit for all bots per day | **≈ $0/day** (after removing user txs and noise) |
| How fast price gaps between pools close | **~90% closed in the same checkpoint** (< 0.25 s) |
| Sui DEX lag behind Binance | **~0.3 s** — too small to trade |
| Decision | 🔴 **Not worth building on Sui right now** → moved research to Solana |

_A negative result is still a result: this tool saved months of building for an empty market._

## What it measures


```
python -m sui_scanner all
   │
   ├─ 1. SAMPLE  windows over the last 14 days → label every tx
   │             adds windows by itself until the estimate is precise enough
   ├─ 2. BOTS    deep-dive top bot wallets (their own history)
   ├─ 3. GAPS    12 × ~10-min windows of DEX swaps (+ Binance 1-second prices)
   │             → gap lifetime, who closes gaps, CEX↔DEX lag
   └─ REPORT     report.md + bots.csv
                 pie (+ likely range) · operators · gap speed · CEX lag
                 → 🟢 continue / 🟡 borderline / 🔴 stop

python -m sui_scanner live --minutes 60   → same gap + CEX analysis, on the live chain
```

## Run it

Needs **Python 3.10+**. No packages to install.

```bash
cd sui-scanner
python -m sui_scanner check            # 1. connection test (10 s)
python -m sui_scanner all              # 2. full run (~20–40 min)
python -m sui_scanner live --minutes 60   # 3. optional: watch the live chain
```

Open `report.md` (VS Code → right-click → Open Preview). Stopped or crashed?
Run the same command again — everything resumes from `scan.db`.
A v0.1 `scan.db` is upgraded automatically.

### Options

| Option | Default | Use |
|---|---|---|
| `--days` | 14 | how far back |
| `--windows` / `--window-cps` | 24 / 120 | first sample windows (~30 s each) |
| `--target-precision` | 0.3 | keep adding windows until arb count is within ±30% (0 = off) |
| `--max-windows` | 96 | upper limit for that |
| `--top` / `--max-txs` | 10 / 3000 | bot deep dive |
| `--gap-windows` / `--gap-window-cps` | 12 / 2400 | gap windows (~10 min each) |
| `--gap-threshold` | 0.006 | spread between pools that counts as a gap (0.6%) |
| `--cex-threshold` | 0.004 | DEX vs Binance gap that counts (0.4%) |
| `--no-cex` | – | skip Binance |
| `--minutes` / `--poll` | 30 / 4 | live mode |
| `--rps` | 8 | requests/second — lower it on HTTP 429 |
| `--endpoint` | public Sui GraphQL | use a provider URL if the public one is slow |
| `--sui-price`, `--price TYPE=USD` | CoinGecko | prices for USD values |
| `--public-package PKG` | – | never use this package to group wallets |
| `--max-tx-usd` | 100 | leave out single "arb" txs worth more (mostly users, listed for checking) |
| `--min-attempts` | 3 | wallet needs this many arb attempts in the sample to count as a bot |

Change thresholds or prices **without re-scanning**:

```bash
python -m sui_scanner report --offline --sui-price 1.15 --gap-threshold 0.004
```

## What each report section means

| Section | Question it answers |
|---|---|
| 1. Pie | How much do all bots earn per day? **Likely range** = 80% bootstrap range over windows |
| 2. Competition | Per wallet, then **per operator** (wallets grouped). Top-1 operator share |
| 3. Deep dive | Bots' own history → cross-check of section 1 |
| 4. Gap lifetime | How long cross-pool gaps stay open, who closes them, **slowest pairs** |
| 5. CEX ↔ DEX | How far Sui DEX SUI/USDC is from Binance, how long it lasts, **lag in seconds** |
| 6. Decision gate | 🟢 / 🟡 / 🔴 plus speed-race vs room-for-slower-bot |

## How things are detected

**Arb tx:** success, ≥2 swap events, sender's coins only went up (SUI may drop by gas).
A "losing arb" must not send coins to anyone else. Since v0.3 the report also leaves out
one-off wallets and single huge txs (first mainnet run: those were users, not bots).

**Operators** (two explainable links, all listed in the report):
- *private package*: wallets call the same Move package that only bots call
- *sweep*: wallets send coins to the same address (ignored if normal users also pay that address, e.g. an exchange)

**Gap:** at the end of each checkpoint, highest/lowest **execution price** of the
same pair across pools. Swap events decoded for Cetus, Turbos, Aftermath, Kriya CLMM,
FlowX CLMM, FlowX AMM, BlueMove (formats from the open-source fuzzland/sui-mev bot).

**CEX lag:** after Binance moves ≥0.2% in 5 s, seconds until a Sui SUI/USDC swap prints
within half the threshold of the new Binance price.

## Known limits

| Limit | Effect |
|---|---|
| Tested on one mainnet period | Market changes — re-run before trusting old numbers |
| Sampled windows | Read the likely range, not just the point estimate |
| Coins with no price | Profit looks smaller → add `--price` |
| Grouping is heuristic | Can miss links (separate packages/collection wallets) or, rarely, merge via a public package → check listed links |
| Gap uses execution prices | Includes fees/slippage; good for **speed/competition**, not exact profit |
| Gaps fixed inside one checkpoint | Counted separately ("same checkpoint"), lifetime unknown (<0.25 s) |
| Only 7 DEXes decoded for gaps | DeepBook and newer DEXes not in gap/CEX analysis (still counted in arb detection) |
| CEX uses SUIUSDT vs SUI/USDC | USDT/USDC treated 1:1; Binance may be blocked in some regions (`--no-cex`) |
| Live mode polls GraphQL | ~1 poll per 4 s, not a true stream; fine for lifetimes > 1 s |
| Sponsored-gas txs | Profit slightly overstated |

Sui switched JSON-RPC off on Foundation mainnet nodes (July 2026); this uses **GraphQL**.
If the default URL stops working, check docs.sui.io for the current GraphQL endpoint.

## Code map

```
sui_scanner/
  gql.py      GraphQL client + queries (validated against Sui's official schema)
  classify.py label one tx (arb / arb_loss / failed / other)
  stages.py   sample windows (+ adaptive), bot deep dive
  stats.py    bootstrap likely range
  cluster.py  group wallets into operators
  swaps.py    decode DEX swap events
  gaps.py     gap lifetime + history/live collection
  cex.py      Binance 1 s prices + lag analysis
  report.py   report.md / bots.csv
  store.py    SQLite (resume-safe, thread-safe)
tests/        synthetic chain, mock GraphQL + Binance server
```

Run tests: `pip install pytest && python -m pytest tests` (28 tests)

## Tech

- Python 3.10+ standard library only (urllib, sqlite3, threading)
- Sui **GraphQL** RPC, Binance 1-second klines, CoinGecko prices
- Bootstrap statistics for confidence ranges
- Resume-safe SQLite storage

## Disclaimer

Research and education only. Not financial advice. Numbers depend on the time
window scanned and will change as the market changes.

## License

MIT