"""CLI:  python -m sui_scanner <command> [options]

Commands
  check    quick connection test (run this first)
  all      sample + bots + report (the normal way to run it)
  sample   stage 1: scan sampled windows over the last N days
  bots     stage 2: deep-dive top bot wallets
  gaps     stage 3: gap lifetime + CEX<->DEX lag on history windows
  live     follow the chain head for N minutes (gap lifetime + CEX lag, live)
  report   build report.md + bots.csv from the database (re-run any time)
"""

from __future__ import annotations

import argparse
import re
import sys

from .cex import Binance, default_stables
from .gaps import run_gaps, run_live
from .gql import DEFAULT_ENDPOINT, GraphQLClient, SuiSource
from .classify import DEFAULT_SWAP_PATTERN
from .prices import Prices, fetch_sui_usd
from .report import build_report
from .stages import run_bots, run_sample
from .store import Store


def check(src: SuiSource) -> int:
    from .classify import classify, parse_node
    from collections import Counter
    latest, ts = src.latest_checkpoint()
    print(f"connected: latest checkpoint {latest} at {ts}")
    first = src.first_available_checkpoint()
    if first is not None:
        print(f"history available from checkpoint {first}")
    flt = {"afterCheckpoint": latest - 41, "beforeCheckpoint": latest - 20, "kind": "PROGRAMMABLE_TX"}
    kinds = Counter()
    for nodes, _ in src.tx_pages(flt):
        for n in nodes:
            kinds[classify(parse_node(n, re.compile(DEFAULT_SWAP_PATTERN))).kind] += 1
        if sum(kinds.values()) >= 200:
            break
    print(f"sample of {sum(kinds.values())} recent txs: {dict(kinds)}")
    print("OK - you can run: python -m sui_scanner all")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sui_scanner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "all", "sample", "bots", "gaps", "live", "report"])
    ap.add_argument("--db", default="scan.db", help="SQLite file (resume-safe)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Sui GraphQL endpoint")
    ap.add_argument("--rps", type=float, default=8.0, help="max requests/second")
    ap.add_argument("--workers", type=int, default=4, help="parallel windows/bots")
    ap.add_argument("--page", type=int, default=20, help="transactions per request")
    ap.add_argument("--swap-pattern", default=DEFAULT_SWAP_PATTERN,
                    help="regex on event struct name that counts as a swap")
    # sample
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--windows", type=int, default=24, help="number of sampled windows")
    ap.add_argument("--window-cps", type=int, default=120, help="checkpoints per window")
    ap.add_argument("--target-precision", type=float, default=0.3,
                    help="add windows until arb-count range is within ±this (0 = off)")
    ap.add_argument("--max-windows", type=int, default=96)
    # gaps / live / cex
    ap.add_argument("--gap-windows", type=int, default=12)
    ap.add_argument("--gap-window-cps", type=int, default=2400, help="checkpoints per gap window (~10 min)")
    ap.add_argument("--gap-threshold", type=float, default=0.006, help="spread that counts as a gap")
    ap.add_argument("--stale-cps", type=int, default=2400, help="pool price older than this is ignored")
    ap.add_argument("--minutes", type=float, default=30, help="live: how long to follow the chain")
    ap.add_argument("--poll", type=float, default=4, help="live: seconds between polls")
    ap.add_argument("--no-cex", action="store_true", help="skip Binance data")
    ap.add_argument("--cex-symbol", default="SUIUSDT")
    ap.add_argument("--cex-threshold", type=float, default=0.004, help="DEX vs CEX gap that counts")
    ap.add_argument("--stable", action="append", default=[], help="extra 6-decimal stablecoin type")
    ap.add_argument("--max-tx-usd", type=float, default=100.0,
                    help="leave out single arb txs worth more than this (usually not arbs)")
    ap.add_argument("--min-attempts", type=int, default=3,
                    help="wallet needs this many arb attempts in the sample to count as a bot")
    ap.add_argument("--public-package", action="append", default=[],
                    help="package to never use for grouping wallets (repeatable)")
    # bots
    ap.add_argument("--top", type=int, default=10, help="bots to deep-dive")
    ap.add_argument("--max-txs", type=int, default=3000, help="max txs read per bot")
    ap.add_argument("--bot", action="append", default=[], help="extra bot address (repeatable)")
    # report
    ap.add_argument("--sui-price", type=float, help="SUI in USD (default: fetch from CoinGecko)")
    ap.add_argument("--price", action="append", default=[], metavar="TYPE=USD",
                    help="price for a coin type, e.g. 0x..::coin::COIN=1.0 (repeatable)")
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--csv", default="bots.csv")
    ap.add_argument("--offline", action="store_true", help="report without network calls")
    a = ap.parse_args(argv)

    store = Store(a.db)
    swap_re = re.compile(a.swap_pattern)
    src = SuiSource(GraphQLClient(a.endpoint, rps=a.rps), tx_page=a.page)

    if a.command == "check":
        return check(src)
    if a.command in ("all", "sample"):
        run_sample(src, store, a.days, a.windows, a.window_cps, a.workers, swap_re,
                   target=a.target_precision or None, max_windows=a.max_windows)
    if a.command in ("all", "bots"):
        run_bots(src, store, a.top, a.max_txs, a.workers, swap_re, a.bot)
    cex = None if a.no_cex else Binance(a.cex_symbol)
    if a.command in ("all", "gaps"):
        run_gaps(src, store, a.gap_windows, a.gap_window_cps, a.workers, cex)
    if a.command == "live":
        if not store.get_meta("plan"):
            run_sample(src, store, a.days, a.windows, a.window_cps, a.workers, swap_re)
        run_live(src, store, a.minutes, a.poll, cex)
    if a.command in ("all", "report", "live"):
        sui = a.sui_price or (None if a.offline else fetch_sui_usd())
        extra = {}
        for p in a.price:
            t, _, v = p.rpartition("=")
            extra[t] = float(v)
        md = build_report(store, Prices(sui, extra), None if a.offline else src, a.csv,
                          gap_threshold=a.gap_threshold, stale_cps=a.stale_cps,
                          cex_threshold=a.cex_threshold, stables=default_stables(a.stable),
                          public_packages=a.public_package, max_tx_usd=a.max_tx_usd,
                          min_attempts=a.min_attempts)
        with open(a.out, "w") as f:
            f.write(md)
        print("\n" + md)
        print(f"\nSaved {a.out} and {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
