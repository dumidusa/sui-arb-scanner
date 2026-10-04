"""Decode DEX swap events into one common shape.

Order matters: the API returns each DEX's events in chain order, and the
last swap of a pool inside a checkpoint sets that pool's price. Keep the
list order when storing/reading (store.swaps sorts by checkpoint, dex, ord).

Event types and JSON field names come from the open-source Sui arb bot
fuzzland/sui-mev (crates/dex-indexer/src/protocols). Event type names keep
the ORIGINAL package id, so they stay valid after DEX package upgrades.
DEXes that launch new event types later need a new adapter here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .classify import norm_addr, norm_type

DEX_EVENTS = {
    "cetus": "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb::pool::SwapEvent",
    "turbos": "0x91bfbc386a41afcfd9b2533058d7e915a1d3829089cc268ff4333d54d6339ca1::pool::SwapEvent",
    "aftermath": "0xc4049b2d1cc0f6e017fda8260e4377cecd236bd7f56a54fee120816e72e2e0dd::events::SwapEventV2",
    "kriya_clmm": "0xf6c05e2d9301e6e91dc6ab6c3ca918f7d55896e1f1edd64adc0e615cde27ebf1::trade::SwapEvent",
    "flowx_clmm": "0x25929e7f29e0a30eb4e692952ba1b5b65a3a4d65ab5f2a32e1ba3edcb587f26d::pool::Swap",
    "flowx_amm": "0xba153169476e8c3114962261d1edc70de5ad9781b83cc617ecc8c1923191cae0::pair::Swapped",
    "bluemove": "0xb24b6789e088b876afabca733bed2299fbc9e2d6369be4d1acfa17d8145454d9::swap::Swap_Event",
}

# DEXes whose event has no coin types -> coins come from the pool object's type <A, B, ...>
NEEDS_POOL_TYPE = {"cetus", "turbos", "kriya_clmm", "flowx_clmm"}

DEX_PACKAGES = {norm_addr(t.split("::")[0]) for t in DEX_EVENTS.values()}


@dataclass
class Swap:
    dex: str
    pool: str
    coin_in: str
    coin_out: str
    amount_in: int
    amount_out: int
    digest: str
    sender: str | None
    checkpoint: int
    ts: str | None


def type_params(t: str) -> list[str]:
    """'0x..::pool::Pool<A, B<C>, D>' -> ['A', 'B<C>', 'D'] (top level only)."""
    if "<" not in t:
        return []
    inner = t[t.index("<") + 1: t.rindex(">")]
    out, depth, cur = [], 0, ""
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _coin(s: str) -> str:
    s = s if s.startswith("0x") else "0x" + s
    return norm_type(s)


def _i(x) -> int:
    return int(x) if x is not None else 0


def decode(dex: str, j: dict, pool_coins: dict[str, tuple[str, str]]) -> tuple | None:
    """Return (pool, coin_in, coin_out, amount_in, amount_out) or None."""
    if dex in ("cetus", "turbos", "kriya_clmm", "flowx_clmm"):
        pool = norm_addr(j.get("pool") or j.get("pool_id"))
        coins = pool_coins.get(pool)
        if not coins:
            return None
        a, b = coins
        if dex == "cetus":
            a2b, ain, aout = j["atob"], _i(j["amount_in"]), _i(j["amount_out"])
        elif dex == "turbos":
            a2b = j["a_to_b"]
            ain, aout = (_i(j["amount_a"]), _i(j["amount_b"])) if a2b else (_i(j["amount_b"]), _i(j["amount_a"]))
        else:  # kriya_clmm / flowx_clmm
            a2b = j["x_for_y"]
            ain, aout = (_i(j["amount_x"]), _i(j["amount_y"])) if a2b else (_i(j["amount_y"]), _i(j["amount_x"]))
        return (pool, a, b, ain, aout) if a2b else (pool, b, a, ain, aout)

    if dex == "aftermath":
        tin, tout = j.get("types_in") or [], j.get("types_out") or []
        if len(tin) != 1 or len(tout) != 1:
            return None  # multi-asset swap: skip
        return (norm_addr(j["pool_id"]), _coin(tin[0]), _coin(tout[0]),
                _i(j["amounts_in"][0]), _i(j["amounts_out"][0]))

    if dex == "flowx_amm":
        x, y = _coin(j["coin_x"]), _coin(j["coin_y"])
        xin = _i(j["amount_x_in"])
        pool = norm_addr(j["pair"]) if j.get("pair") else f"flowx_amm:{x}|{y}"
        if xin > 0:
            return pool, x, y, xin, _i(j["amount_y_out"])
        return pool, y, x, _i(j["amount_y_in"]), _i(j["amount_x_out"])

    if dex == "bluemove":
        xin = _i(j["amount_x_in"])
        if xin > 0:
            return (norm_addr(j["pool_id"]), _coin(j["token_x_in"]), _coin(j["token_y_out"]),
                    xin, _i(j["amount_y_out"]))
        return (norm_addr(j["pool_id"]), _coin(j["token_y_in"]), _coin(j["token_x_out"]),
                _i(j["amount_y_in"]), _i(j["amount_x_out"]))
    return None


def pool_ids_needing_types(dex: str, j: dict) -> str | None:
    if dex in NEEDS_POOL_TYPE:
        return norm_addr(j.get("pool") or j.get("pool_id"))
    return None


def to_swap(dex: str, ev: dict, pool_coins) -> Swap | None:
    """ev: GraphQL Event node from gql.EVENTS_PAGE."""
    j = ((ev.get("contents") or {}).get("json")) or {}
    try:
        d = decode(dex, j, pool_coins)
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not d or d[3] <= 0 or d[4] <= 0:
        return None
    tx = ev.get("transaction") or {}
    cp = (((tx.get("effects") or {}).get("checkpoint")) or {}).get("sequenceNumber")
    if cp is None:
        return None
    return Swap(dex, d[0], d[1], d[2], d[3], d[4], tx.get("digest") or "",
                norm_addr((ev.get("sender") or {}).get("address")), int(cp), ev.get("timestamp"))
