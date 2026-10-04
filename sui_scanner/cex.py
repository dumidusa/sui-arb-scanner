"""CEX vs DEX lag: Binance SUI price vs Sui DEX SUI/USDC swap prices.

Binance: public 1-second klines (no API key). Note: SUIUSDT is used as the
CEX price and compared with SUI/USDC on Sui; USDT vs USDC is treated as 1:1.

Metrics (pure function `analyze`):
  deviation   DEX swap price vs CEX price in the same second
  episodes    stretches where |deviation| > threshold (a CEX<->DEX arb chance),
              with their duration in seconds
  lag         after a fast CEX move, seconds until a DEX swap prints within
              half the threshold of the new CEX price
"""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime

from .classify import norm_type
from .prices import NATIVE_USDC
from .classify import SUI

BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]


def _hosts() -> list[str]:
    env = os.environ.get("SUI_SCANNER_BINANCE_HOSTS")  # override for tests / proxies
    return env.split(",") if env else BINANCE_HOSTS


def iso_ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


class Binance:
    def __init__(self, symbol: str = "SUIUSDT", hosts=None, timeout: float = 20.0):
        self.symbol = symbol
        self.hosts = hosts or _hosts()
        self.timeout = timeout

    def _get(self, path: str) -> list:
        err = None
        for h in self.hosts:
            for attempt in range(3):
                try:
                    req = urllib.request.Request(h + path, headers={"User-Agent": "sui-scanner/0.3"})
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        return json.loads(r.read())
                except urllib.error.HTTPError as e:
                    err = e
                    if e.code in (418, 429, 500, 502, 503):
                        time.sleep(2 ** attempt)
                        continue
                    break  # 451 (region) / 4xx -> try next host
                except (urllib.error.URLError, TimeoutError) as e:
                    err = e
                    time.sleep(1)
        raise RuntimeError(f"Binance unreachable: {err}")

    def klines_1s(self, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        out, t = [], start_ms
        while t <= end_ms:
            rows = self._get(f"/api/v3/klines?symbol={self.symbol}&interval=1s"
                             f"&startTime={t}&endTime={end_ms}&limit=1000")
            if not rows:
                break
            out.extend((int(r[0]), float(r[4])) for r in rows)
            t = int(rows[-1][0]) + 1000
            if len(rows) < 1000:
                break
        return out

    def fetch_range(self, store, start_iso: str, end_iso: str) -> int:
        s, e = iso_ms(start_iso) // 1000 * 1000, iso_ms(end_iso)
        have = store.cex_last_ms(s, e)
        if have is not None:
            s = have + 1000
        if s > e:
            return 0
        try:
            rows = self.klines_1s(s, e)
        except RuntimeError as ex:
            print(f"  cex: {ex} (skipping CEX data for this range)", flush=True)
            return 0
        store.put_cex(rows)
        return len(rows)


def dex_sui_prices(swaps, stables: set[str]) -> list[tuple[int, float, str]]:
    """(ts_ms, USD per SUI, pool) from swaps between SUI and a stablecoin (6 decimals)."""
    out = []
    for s in swaps:
        cin, cout = norm_type(s.coin_in), norm_type(s.coin_out)
        if not s.ts:
            continue
        if cin == SUI and cout in stables:
            px = (s.amount_out / 1e6) / (s.amount_in / 1e9)
        elif cout == SUI and cin in stables:
            px = (s.amount_in / 1e6) / (s.amount_out / 1e9)
        else:
            continue
        out.append((iso_ms(s.ts), px, s.pool))
    return sorted(out)


def analyze(dex: list[tuple[int, float, str]], cex: dict[int, float], threshold: float,
            move: float = 0.002, move_secs: int = 5, max_lag: int = 120) -> dict:
    """dex: (ts_ms, price, pool) sorted. cex: second_ms -> close."""
    if not dex or not cex:
        return {}

    def cex_at(ms):
        sec = ms // 1000 * 1000
        for back in range(0, 3):  # tolerate up to 2 missing seconds
            v = cex.get(sec - back * 1000)
            if v:
                return v
        return None

    devs = []
    for ms, px, _ in dex:
        c = cex_at(ms)
        if c:
            devs.append((ms, px / c - 1))
    if not devs:
        return {}
    absd = sorted(abs(d) for _, d in devs)

    # episodes of |dev| > threshold (consecutive DEX prints)
    episodes, cur = [], None
    for ms, d in devs:
        if abs(d) > threshold:
            if cur is None:
                cur = [ms, ms, abs(d)]
            else:
                cur[1], cur[2] = ms, max(cur[2], abs(d))
        elif cur is not None:
            episodes.append((cur[0], ms, cur[2]))  # closes at first print back inside
            cur = None
    censored = 1 if cur is not None else 0

    # lag after fast CEX moves
    secs = sorted(cex)
    lags, last_ev = [], -10 ** 18
    j = 0
    for i, t in enumerate(secs):
        t0 = t - move_secs * 1000
        if t0 not in cex or t - last_ev < 30_000:
            continue
        if abs(cex[t] / cex[t0] - 1) < move:
            continue
        last_ev = t
        target = cex[t]
        while j < len(dex) and dex[j][0] < t:
            j += 1
        k = j
        while k < len(dex) and dex[k][0] <= t + max_lag * 1000:
            if abs(dex[k][1] / target - 1) <= threshold / 2:
                lags.append((dex[k][0] - t) / 1000)
                break
            k += 1
        else:
            lags.append(None)  # DEX did not catch up within max_lag

    caught = [x for x in lags if x is not None]
    durs = [(e - s) / 1000 for s, e, _ in episodes]
    return {
        "prints": len(devs),
        "median_abs_dev": statistics.median(absd),
        "p90_abs_dev": absd[int(0.9 * (len(absd) - 1))],
        "episodes": len(episodes),
        "episodes_censored": censored,
        "episode_median_s": statistics.median(durs) if durs else None,
        "episode_max_dev": max((m for _, _, m in episodes), default=None),
        "cex_moves": len(lags),
        "lag_median_s": statistics.median(caught) if caught else None,
        "lag_not_caught": sum(1 for x in lags if x is None),
        "_absd": absd, "_durs": durs, "_lags": caught, "_max": [m for _, _, m in episodes],
    }


def default_stables(extra: list[str] | None = None) -> set[str]:
    return {NATIVE_USDC, *(norm_type(x) for x in (extra or []))}
