"""Synthetic chain shaped exactly like Sui GraphQL responses, for offline tests."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

SUI_T = "0x2::sui::SUI"
USDC_T = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
DEEP_T = "0xdeeb::deep::DEEP"
CETUS = "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb"
TURBOS = "0x91bfbc386a41afcfd9b2533058d7e915a1d3829089cc268ff4333d54d6339ca1"

BOT_A = "0xa" + "1" * 63
BOT_B = "0xb" + "2" * 63
USER = "0xc" + "3" * 63

T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
CPS = 4.0  # checkpoints per second


def cp_ts(cp: int) -> str:
    return (T0 + timedelta(seconds=cp / CPS)).isoformat().replace("+00:00", "Z")


def swap_ev(pkg, mod="pool", name="SwapEvent"):
    return {"contents": {"type": {"repr": f"{pkg}::{mod}::{name}<{SUI_T}, {USDC_T}>"}}}


def node(digest, sender, cp, status="SUCCESS", gas=(2_000_000, 1_000_000, 900_000),
         changes=(), events=(), err=None):
    return {
        "digest": digest,
        "sender": {"address": sender},
        "effects": {
            "status": status,
            "timestamp": cp_ts(cp),
            "checkpoint": {"sequenceNumber": cp},
            "executionError": {"message": err} if err else None,
            "gasEffects": {"gasSummary": {"computationCost": str(gas[0]), "storageCost": str(gas[1]),
                                          "storageRebate": str(gas[2]),
                                          "nonRefundableStorageFee": "0"}},
            "balanceChanges": {"nodes": [{"amount": str(a), "owner": {"address": o},
                                          "coinType": {"repr": t}} for o, t, a in changes]},
            "events": {"nodes": list(events)},
        },
    }


def gas_net(gas=(2_000_000, 1_000_000, 900_000)):
    return gas[0] + gas[1] - gas[2]


def build_chain(n_cp=200_000, seed=7):
    """Bot A: frequent USDC arbs. Bot B: SUI arbs + many fails. User: swaps."""
    rnd = random.Random(seed)
    txs = []
    i = 0
    g = gas_net()
    for cp in range(0, n_cp, 3):
        r = rnd.random()
        i += 1
        if r < 0.10:  # bot A wins 0.5 USDC
            txs.append(node(f"d{i}", BOT_A, cp, changes=[(BOT_A, SUI_T, -g), (BOT_A, USDC_T, 500_000)],
                            events=[swap_ev(CETUS), swap_ev(TURBOS)]))
        elif r < 0.13:  # bot B wins 0.2 SUI
            txs.append(node(f"d{i}", BOT_B, cp, changes=[(BOT_B, SUI_T, 200_000_000 - g)],
                            events=[swap_ev(CETUS), swap_ev(CETUS, "pool", "SwapEvent")]))
        elif r < 0.25:  # bot B fails
            txs.append(node(f"d{i}", BOT_B, cp, status="FAILURE", changes=[(BOT_B, SUI_T, -g)],
                            err="MoveAbort ... profit check"))
        elif r < 0.27:  # bot B loses 0.01 SUI on a cyclic arb
            txs.append(node(f"d{i}", BOT_B, cp, changes=[(BOT_B, SUI_T, -10_000_000 - g)],
                            events=[swap_ev(CETUS), swap_ev(TURBOS)]))
        elif r < 0.60:  # user sells SUI for USDC via 2 pools (aggregator)
            txs.append(node(f"d{i}", USER, cp, changes=[(USER, SUI_T, -5_000_000_000 - g), (USER, USDC_T, 17_000_000)],
                            events=[swap_ev(CETUS), swap_ev(TURBOS)]))
        elif r < 0.62:  # bot A arb paid in unknown coin DEEP
            txs.append(node(f"d{i}", BOT_A, cp, changes=[(BOT_A, SUI_T, -g), (BOT_A, DEEP_T, 3_000_000)],
                            events=[swap_ev(CETUS), swap_ev(TURBOS)]))
        else:  # plain transfer
            txs.append(node(f"d{i}", USER, cp, changes=[(USER, SUI_T, -g)]))
    return txs


class FakeSource:
    def __init__(self, txs, page=7, events=None):
        self.txs = txs
        self.events = events or []
        self.latest = max(t["effects"]["checkpoint"]["sequenceNumber"] for t in txs)
        self.page = page
        self.calls = 0

    def latest_checkpoint(self):
        return self.latest, cp_ts(self.latest)

    def checkpoint_timestamps(self, seqs):
        return {s: cp_ts(s) for s in seqs}

    def first_available_checkpoint(self):
        return 0

    def _match(self, t, f):
        cp = t["effects"]["checkpoint"]["sequenceNumber"]
        if "afterCheckpoint" in f and not cp > f["afterCheckpoint"]:
            return False
        if "beforeCheckpoint" in f and not cp < f["beforeCheckpoint"]:
            return False
        if "sentAddress" in f and t["sender"]["address"] != f["sentAddress"]:
            # scanner normalises addresses; fixtures are already 64-hex
            return False
        return True

    def tx_pages(self, f, after=None):
        sel = [t for t in self.txs if self._match(t, f)]
        start = int(after) if after else 0
        while start < len(sel):
            self.calls += 1
            chunk = sel[start:start + self.page]
            start += len(chunk)
            yield chunk, str(start)

    def tx_pages_backward(self, f):
        sel = [t for t in self.txs if self._match(t, f)]
        end = len(sel)
        while end > 0:
            self.calls += 1
            s = max(0, end - self.page)
            yield sel[s:end]
            end = s

    def event_pages(self, f):
        sel = []
        for e in self.events:
            cp = e["transaction"]["effects"]["checkpoint"]["sequenceNumber"]
            if e["contents"]["type"]["repr"] != f.get("type"):
                continue
            if "afterCheckpoint" in f and not cp > f["afterCheckpoint"]:
                continue
            if "beforeCheckpoint" in f and not cp < f["beforeCheckpoint"]:
                continue
            sel.append(e)
        for i in range(0, len(sel), 13):
            self.calls += 1
            yield sel[i:i + 13]

    def pool_coins(self, ids):
        from sui_scanner.swaps import type_params
        from sui_scanner.classify import norm_type
        out = {}
        for p in ids:
            if p in POOL_TYPES:
                a, b = type_params(POOL_TYPES[p])[:2]
                out[p] = (norm_type(a), norm_type(b))
        return out

    def coin_meta(self, t):
        if "usdc" in t.lower():
            return 6, "USDC"
        if "deep" in t.lower():
            return 6, "DEEP"
        return None, None


# ------------------------------------------------------------------ swap events
from sui_scanner.swaps import DEX_EVENTS  # noqa: E402

P1 = "0x" + "e1" * 32  # cetus SUI/USDC
P2 = "0x" + "e2" * 32  # turbos SUI/USDC
POOL_TYPES = {
    P1: f"{CETUS}::pool::Pool<{SUI_T}, {USDC_T}>",
    P2: f"{TURBOS}::pool::Pool<{SUI_T}, {USDC_T}, 0x91bf::fee3000bps::FEE3000BPS>",
}
LIFE_CYCLE = [0, 2, 3, 10, 50]  # checkpoints until an arb fixes each move (0 = same checkpoint)
EPISODE_EVERY = 400


def _ev(dex, j, digest, sender, cp):
    return {"timestamp": cp_ts(cp), "sender": {"address": sender},
            "contents": {"type": {"repr": DEX_EVENTS[dex]}, "json": j},
            "transaction": {"digest": digest, "effects": {"checkpoint": {"sequenceNumber": cp}}}}


def cetus_sell_sui(price, digest, sender, cp, sui=1.0):
    j = {"pool": P1, "atob": True, "amount_in": str(int(sui * 1e9)), "amount_out": str(int(sui * price * 1e6))}
    return _ev("cetus", j, digest, sender, cp)


def turbos_sell_sui(price, digest, sender, cp, sui=1.0):
    j = {"pool": P2, "a_to_b": True, "amount_a": str(int(sui * 1e9)), "amount_b": str(int(sui * price * 1e6))}
    return _ev("turbos", j, digest, sender, cp)


def build_events(n_cp=200_000):
    """Every EPISODE_EVERY checkpoints a user pushes Cetus 1% up; an arb fixes it
    after LIFE_CYCLE[k] checkpoints. Turbos gets a small user trade each episode."""
    evs, k = [], 0
    for t in range(1000, n_cp - 100, EPISODE_EVERY):
        life = LIFE_CYCLE[k % len(LIFE_CYCLE)]
        k += 1
        evs.append(turbos_sell_sui(3.0, f"u2-{t}", USER, t))
        evs.append(cetus_sell_sui(3.03, f"u1-{t}", USER, t))
        d = f"arb-{t}"
        evs.append(cetus_sell_sui(3.0, d, BOT_A, t + life))
        evs.append(turbos_sell_sui(3.0, d, BOT_A, t + life))
    return evs


def cex_klines(start_ms, end_ms):
    out, t = [], start_ms // 1000 * 1000
    while t <= end_ms and len(out) < 1000:
        out.append([t, "3.0", "3.0", "3.0", "3.0", "1", t + 999, "0", 0, "0", "0", "0"])
        t += 1000
    return out
