"""Read-only Sui arbitrage scanner (MVP).

Stage 1 (sample):  scan sampled checkpoint windows across the last N days,
                   classify every transaction, find arbitrage bots.
Stage 2 (bots):    deep-dive the top bots' own transaction history.
Report:            pie size, competitor share, fail rates, venues.

Nothing here signs or sends transactions.
"""

__version__ = "0.3.0"
