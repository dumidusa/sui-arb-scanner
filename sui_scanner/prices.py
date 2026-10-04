"""USD pricing for profit coins.

Only coins with a known price are converted to USD. Everything else is
reported raw so you can add a price with --price TYPE=USD and re-run
`report` (no re-scan needed).
"""

from __future__ import annotations

import json
import urllib.request

from .classify import SUI, norm_type

# Circle native USDC on Sui. Verify on an explorer if in doubt.
NATIVE_USDC = norm_type(
    "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC")


def fetch_sui_usd(timeout: float = 15.0) -> float | None:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=sui&vs_currencies=usd"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sui-scanner/0.3"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return float(json.loads(r.read())["sui"]["usd"])
    except Exception:
        return None


class Prices:
    def __init__(self, sui_usd: float | None, extra: dict[str, float] | None = None):
        self.map: dict[str, float] = {NATIVE_USDC: 1.0}
        if sui_usd:
            self.map[SUI] = sui_usd
        for k, v in (extra or {}).items():
            self.map[norm_type(k)] = v
        self.sui_usd = sui_usd

    def usd(self, coin: str) -> float | None:
        return self.map.get(norm_type(coin))
