"""Gap lifetime: how long do cross-pool price gaps stay open, and who closes them?

Method (simple on purpose, see README "limits"):
  * every swap gives an execution price for its pool (amount_out / amount_in)
  * at the END of each checkpoint, for every coin pair traded in >= 2 pools,
    spread = highest pool price / lowest pool price - 1
  * spread > threshold  -> gap OPEN
  * later checkpoint with trades on the pair and spread <= threshold -> CLOSED,
    closer = who traded the pair in that checkpoint (arb tx / known bot / user)
  * no fresh trades for `stale_cps` -> EXPIRED (nobody cared)
  * arb txs inside the same checkpoint as the price move are counted separately
    ("closed inside one checkpoint" — invisible at checkpoint resolution)

Decimals cancel out because we only compare prices of the SAME pair.
"""

from __future__ import annotations

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .swaps import DEX_EVENTS, Swap, pool_ids_needing_types, to_swap


@dataclass
class Gap:
    pair: tuple[str, str]
    open_cp: int
    peak: float
    pools: tuple[str, str]
    close_cp: int | None = None
    end: str = "open"  # closed / expired / censored
    closer: str | None = None  # arb / bot / user
    closer_sender: str | None = None


@dataclass
class GapResult:
    gaps: list[Gap] = field(default_factory=list)
    same_cp_arbs: int = 0  # arb txs that fixed a move inside one checkpoint
    pairs_seen: int = 0
    checkpoints: int = 0


def pair_price(s: Swap) -> tuple[tuple[str, str], float]:
    """Price of pair[0] in units of pair[1] (raw units)."""
    a, b = sorted((s.coin_in, s.coin_out))
    if s.coin_in == a:
        return (a, b), s.amount_out / s.amount_in
    return (a, b), s.amount_in / s.amount_out


def detect_gaps(swaps: list[Swap], threshold: float, stale_cps: int,
                bots: set[str], start_cp: int | None = None, end_cp: int | None = None) -> GapResult:
    res = GapResult()
    if not swaps:
        return res
    swaps = sorted(swaps, key=lambda s: s.checkpoint)
    per_digest = defaultdict(int)
    for s in swaps:
        per_digest[s.digest] += 1
    arb_digests = {d for d, n in per_digest.items() if n >= 2}

    by_cp = defaultdict(list)
    for s in swaps:
        by_cp[s.checkpoint].append(s)
    last: dict[tuple, dict[str, tuple[float, int]]] = defaultdict(dict)  # pair -> pool -> (price, cp)
    open_gap: dict[tuple, Gap] = {}
    pairs = set()

    cps = sorted(by_cp)
    for cp in cps:
        touched: dict[tuple, list[Swap]] = defaultdict(list)
        for s in by_cp[cp]:
            pair, px = pair_price(s)
            last[pair][s.pool] = (px, cp)
            touched[pair].append(s)
            pairs.add(pair)

        # same-checkpoint arbs: arb tx on a pair where a non-arb swap also happened this cp
        for pair, ss in touched.items():
            arb_tx = {s.digest for s in ss if s.digest in arb_digests}
            if arb_tx and any(s.digest not in arb_digests for s in ss) and pair not in open_gap:
                res.same_cp_arbs += len(arb_tx)

        check = set(touched) | set(open_gap)
        for pair in check:
            fresh = {p: v for p, (v, c) in last[pair].items() if cp - c <= stale_cps}
            g = open_gap.get(pair)
            if len(fresh) < 2:
                if g:
                    g.end, g.close_cp = "expired", cp
                    res.gaps.append(open_gap.pop(pair))
                continue
            hi = max(fresh, key=fresh.get)
            lo = min(fresh, key=fresh.get)
            spread = fresh[hi] / fresh[lo] - 1
            if g:
                if spread <= threshold and pair in touched:
                    g.end, g.close_cp = "closed", cp
                    ss = touched[pair]
                    arb = [s for s in ss if s.digest in arb_digests]
                    bot = [s for s in ss if s.sender in bots]
                    pick = (arb or bot or ss)[0]
                    g.closer = "arb" if arb else ("bot" if bot else "user")
                    g.closer_sender = pick.sender
                    res.gaps.append(open_gap.pop(pair))
                else:
                    g.peak = max(g.peak, spread)
            elif spread > threshold and pair in touched:
                open_gap[pair] = Gap(pair, cp, spread, (hi, lo))

    for g in open_gap.values():
        g.end = "censored"  # still open when the window ended
        res.gaps.append(g)
    res.pairs_seen = len(pairs)
    s0 = start_cp if start_cp is not None else cps[0]
    s1 = end_cp if end_cp is not None else cps[-1]
    res.checkpoints = s1 - s0 + 1
    return res


# ------------------------------------------------------------------ collection
def _log(m):
    print(m, flush=True)


def collect_range(src, store, wid: int, start_cp: int, end_cp: int) -> int:
    """Fetch swap events of every known DEX in [start_cp, end_cp] and store them."""
    total = 0
    for dex, etype in DEX_EVENTS.items():
        flt = {"type": etype, "afterCheckpoint": start_cp - 1, "beforeCheckpoint": end_cp + 1}
        raw = []
        for nodes in src.event_pages(flt):
            raw.extend(nodes)
        need = set()
        for ev in raw:
            j = (ev.get("contents") or {}).get("json") or {}
            p = pool_ids_needing_types(dex, j)
            if p and store.pool(p) is None:
                need.add(p)
        if need:
            for pid, coins in src.pool_coins(sorted(need)).items():
                store.put_pool(pid, coins)
        pc = store.pool_map()
        sw = [s for s in (to_swap(dex, ev, pc) for ev in raw) if s]
        store.put_swaps(wid, sw)
        total += len(sw)
    return total


def plan_gap_windows(src, store, n: int, cps: int) -> None:
    if store.gap_windows():
        return
    plan = store.get_meta("plan")
    if not plan:
        raise SystemExit("run `sample` first")
    start, latest = plan["start_cp"], plan["latest_cp"]
    step = (latest - cps - start) / n
    wins = [(int(start + i * step + step / 2 - cps / 2),) for i in range(n)]
    store.add_gap_windows([(s, s + cps - 1, "history") for (s,) in wins])
    _log(f"gaps: planned {n} windows x {cps} checkpoints (~{cps / plan['cps_per_sec'] / 60:.0f} min each)")


def run_gaps(src, store, n: int, cps: int, workers: int, cex=None) -> None:
    plan_gap_windows(src, store, n, cps)
    todo = [w for w in store.gap_windows() if not w["complete"] and w["mode"] == "history"]
    if not todo:
        _log("gaps: all windows complete")
        return
    _log(f"gaps: collecting swaps for {len(todo)} windows ...")

    def one(w):
        if not w["start_ts"]:
            ts = src.checkpoint_timestamps([w["start_cp"], w["end_cp"]])
            store.update_gap_window(w["id"], start_ts=ts.get(w["start_cp"]), end_ts=ts.get(w["end_cp"]))
            w = {**w, "start_ts": ts.get(w["start_cp"]), "end_ts": ts.get(w["end_cp"])}
        n_sw = collect_range(src, store, w["id"], w["start_cp"], w["end_cp"])
        if cex is not None and w["start_ts"] and w["end_ts"]:
            cex.fetch_range(store, w["start_ts"], w["end_ts"])
        store.update_gap_window(w["id"], complete=1)
        return n_sw

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, w): w for w in todo}
        for i, f in enumerate(as_completed(futs), 1):
            w = futs[f]
            try:
                _log(f"  gap window {i}/{len(todo)} (cp {w['start_cp']}): {f.result()} swaps")
            except Exception as e:
                _log(f"  gap window {w['start_cp']} failed: {e} (re-run to resume)")


def run_live(src, store, minutes: float, poll: float, cex=None) -> None:
    """Follow the chain head, store swaps as one growing 'live' window."""
    latest, ts = src.latest_checkpoint()
    wid = store.add_gap_windows([(latest + 1, latest, "live")])[0]
    store.update_gap_window(wid, start_ts=ts)
    cur = latest
    t_end = time.time() + minutes * 60
    _log(f"live: following chain from checkpoint {latest} for {minutes:.0f} min (Ctrl-C to stop early)")
    try:
        while time.time() < t_end:
            time.sleep(poll)
            head, head_ts = src.latest_checkpoint()
            if head <= cur:
                continue
            n = collect_range(src, store, wid, cur + 1, head)
            if cex is not None:
                w = [x for x in store.gap_windows() if x["id"] == wid][0]
                cex.fetch_range(store, w["start_ts"], head_ts)
            store.update_gap_window(wid, end_cp=head, end_ts=head_ts)
            cur = head
            _log(f"  checkpoint {head}: +{n} swaps")
    except KeyboardInterrupt:
        _log("live: stopped")
    store.update_gap_window(wid, complete=1)
