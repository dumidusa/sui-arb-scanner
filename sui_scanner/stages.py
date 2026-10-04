"""Stage 1 (sample) and stage 2 (bot deep dive)."""

from __future__ import annotations

import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .classify import classify, parse_node
from .stats import bootstrap_rate, rel_width
from .store import Store


def ts_to_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- stage 1
def plan_windows(src, store: Store, days: float, n_windows: int, window_cps: int) -> dict:
    plan = store.get_meta("plan")
    if plan:
        _log(f"resuming plan: {len(store.windows())} windows, start checkpoint {plan['start_cp']}")
        return plan

    latest, latest_ts = src.latest_checkpoint()
    probe = max(1, latest - 20_000)
    ts = src.checkpoint_timestamps([probe, latest])
    secs = (ts_to_dt(ts[latest]) - ts_to_dt(ts[probe])).total_seconds()
    cps_per_sec = (latest - probe) / secs if secs > 0 else 4.0

    start = int(latest - days * 86_400 * cps_per_sec)
    first_avail = src.first_available_checkpoint()
    if first_avail is not None and start <= first_avail:
        _log(f"note: node history starts at checkpoint {first_avail}; clamping")
        start = first_avail + 1

    usable = latest - window_cps - start
    step = usable / n_windows
    wins = []
    for i in range(n_windows):
        s = int(start + i * step + step / 2 - window_cps / 2)
        wins.append((s, s + window_cps - 1))
    store.add_windows(wins)

    plan = {"start_cp": start, "latest_cp": latest, "latest_ts": latest_ts,
            "cps_per_sec": cps_per_sec, "days": days,
            "window_cps": window_cps, "n_windows": n_windows}
    store.set_meta("plan", plan)
    _log(f"plan: {n_windows} windows x {window_cps} checkpoints over {days} days "
         f"(~{cps_per_sec:.2f} checkpoints/s, start {start}, latest {latest})")
    return plan


def _scan_window(src, store: Store, w: dict, swap_re: re.Pattern) -> int:
    if not w["start_ts"]:
        ts = src.checkpoint_timestamps([w["start_cp"], w["end_cp"]])
        store.update_window(w["id"], start_ts=ts.get(w["start_cp"]), end_ts=ts.get(w["end_cp"]))
    flt = {"afterCheckpoint": w["start_cp"] - 1, "beforeCheckpoint": w["end_cp"] + 1,
           "kind": "PROGRAMMABLE_TX"}
    count = w["tx_count"] or 0
    for nodes, cursor in src.tx_pages(flt, after=w["cursor"]):
        rows = []
        for n in nodes:
            tx = parse_node(n, swap_re)
            if tx.checkpoint is not None and not (w["start_cp"] <= tx.checkpoint <= w["end_cp"]):
                continue
            rows.append((tx, classify(tx)))
        store.put_txs("sample", rows)
        count += len(rows)
        store.update_window(w["id"], cursor=cursor, tx_count=count)
    store.update_window(w["id"], complete=1)
    return count


def _scan_open(src, store: Store, workers: int, swap_re: re.Pattern) -> None:
    todo = store.windows(only_open=True)
    if not todo:
        return
    _log(f"sample: scanning {len(todo)} windows with {workers} workers ...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_scan_window, src, store, w, swap_re): w for w in todo}
        for f in as_completed(futs):
            w = futs[f]
            try:
                n = f.result()
                done += 1
                _log(f"  window {done}/{len(todo)} (checkpoints {w['start_cp']}-{w['end_cp']}): {n} txs")
            except Exception as e:  # keep going; window stays open for resume
                _log(f"  window {w['start_cp']} failed: {e}  (re-run to resume)")


def window_arb_counts(store: Store) -> tuple[list[float], list[float]]:
    """Per complete window: (arb tx count, seconds covered)."""
    plan = store.get_meta("plan") or {}
    per_cp = 1.0 / plan.get("cps_per_sec", 4.0)
    vals, secs = [], []
    for w in store.windows():
        if not (w["complete"] and w["start_ts"] and w["end_ts"]):
            continue
        n = store.query("SELECT COUNT(*) FROM txs WHERE stage='sample' AND kind IN ('arb','arb_loss') "
                        "AND checkpoint BETWEEN ? AND ?", (w["start_cp"], w["end_cp"]))[0][0]
        vals.append(float(n))
        secs.append((ts_to_dt(w["end_ts"]) - ts_to_dt(w["start_ts"])).total_seconds() + per_cp)
    return vals, secs


def _add_random_windows(store: Store, k: int, seed: int) -> int:
    plan = store.get_meta("plan")
    wcps = plan["window_cps"]
    taken = [(w["start_cp"], w["end_cp"]) for w in store.windows()]
    rnd = random.Random(seed)
    added = []
    for _ in range(k * 50):
        if len(added) >= k:
            break
        s = rnd.randint(plan["start_cp"], plan["latest_cp"] - wcps)
        e = s + wcps - 1
        if all(e < a or s > b for a, b in taken + added):
            added.append((s, e))
    store.add_windows(added)
    return len(added)


def run_sample(src, store: Store, days: float, n_windows: int, window_cps: int,
               workers: int, swap_re: re.Pattern, target: float | None = None,
               max_windows: int = 96) -> None:
    plan_windows(src, store, days, n_windows, window_cps)
    _scan_open(src, store, workers, swap_re)
    if not target:
        return
    rnd_round = 0
    while True:
        vals, secs = window_arb_counts(store)
        point, lo, hi = bootstrap_rate(vals, secs)
        rw = rel_width(point, lo, hi)
        total = len(store.windows())
        _log(f"sample: arb txs/day ≈ {point:,.0f} (range {lo:,.0f}–{hi:,.0f}, ±{rw:.0%}) "
             f"from {len(vals)} windows")
        if rw <= target:
            _log(f"sample: precision target ±{target:.0%} reached")
            return
        if total >= max_windows:
            _log(f"sample: hit --max-windows {max_windows}; estimate stays ±{rw:.0%}")
            return
        k = min(max(6, total // 2), max_windows - total)
        rnd_round += 1
        got = _add_random_windows(store, k, seed=1000 + rnd_round + total)
        if not got:
            _log("sample: no room for more windows")
            return
        _log(f"sample: too noisy → adding {got} random windows")
        _scan_open(src, store, workers, swap_re)


# ---------------------------------------------------------------- stage 2
def pick_bots(store: Store, top: int) -> list[str]:
    rows = store.query(
        "SELECT sender, COUNT(*) c FROM txs WHERE stage='sample' AND kind IN ('arb','arb_loss') "
        "AND sender IS NOT NULL GROUP BY sender ORDER BY c DESC LIMIT ?", (top,))
    return [r[0] for r in rows]


def _deep_one(src, store: Store, addr: str, start_cp: int, max_txs: int,
              swap_re: re.Pattern) -> tuple[int, bool]:
    flt = {"sentAddress": addr, "afterCheckpoint": start_cp}
    n, first_ts, last_ts, capped = 0, None, None, False
    reached_start = False
    for nodes in src.tx_pages_backward(flt):
        rows = []
        for node in nodes:
            tx = parse_node(node, swap_re)
            if tx.checkpoint is not None and tx.checkpoint <= start_cp:
                reached_start = True  # server ignored the bound; filter here
                continue
            rows.append((tx, classify(tx)))
            if tx.timestamp:
                first_ts = tx.timestamp if not first_ts or tx.timestamp < first_ts else first_ts
                last_ts = tx.timestamp if not last_ts or tx.timestamp > last_ts else last_ts
        store.put_txs("deep", rows)
        n += len(rows)
        if reached_start:
            break
        if n >= max_txs:
            capped = True
            break
    store.set_deep(addr, n, first_ts, last_ts, capped, True)
    return n, capped


def run_bots(src, store: Store, top: int, max_txs: int, workers: int,
             swap_re: re.Pattern, extra: list[str] | None = None) -> None:
    plan = store.get_meta("plan")
    if not plan:
        raise SystemExit("run `sample` first (bots stage needs the time range)")
    from .classify import norm_addr
    bots = pick_bots(store, top) + [norm_addr(a) for a in (extra or [])]
    bots = list(dict.fromkeys(bots))
    done_set = {r[0] for r in store.query("SELECT address FROM deep WHERE done=1")}
    todo = [b for b in bots if b not in done_set]
    if not todo:
        _log("bots: nothing to do")
        return
    _log(f"bots: deep-diving {len(todo)} bot wallets (max {max_txs} txs each) ...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_deep_one, src, store, b, plan["start_cp"], max_txs, swap_re): b
                for b in todo}
        for f in as_completed(futs):
            b = futs[f]
            try:
                n, capped = f.result()
                _log(f"  {b[:10]}…: {n} txs{' (capped)' if capped else ''}")
            except Exception as e:
                _log(f"  {b[:10]}… failed: {e}  (re-run to resume)")
