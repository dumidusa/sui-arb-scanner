"""Turn one Sui transaction into a labelled record.

Pure functions only (no network) so they are easy to test.

Labels
------
arb       success, >= 2 swap events, sender only GAINED coins (SUI may drop by gas only)
arb_loss  success, >= 2 swap events, sender's only change is SUI dropping more than gas
          (a cyclic trade that lost money)
failed    execution status FAILURE (counted against a sender later, if that sender is a bot)
other     everything else (normal user swaps, transfers, mints ...)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_SWAP_PATTERN = r"(?i)swap|orderfilled|order_filled"


def norm_type(t: str) -> str:
    """Normalise a Move type so '0x2::sui::SUI' and the long form compare equal."""
    if not t or "::" not in t:
        return t
    addr, rest = t.split("::", 1)
    try:
        return f"0x{int(addr, 16):064x}::{rest}"
    except ValueError:
        return t


def norm_addr(a: str | None) -> str | None:
    if not a:
        return a
    try:
        return f"0x{int(a, 16):064x}"
    except ValueError:
        return a


SUI = norm_type("0x2::sui::SUI")


@dataclass
class Tx:
    digest: str
    sender: str | None
    status: str  # SUCCESS / FAILURE
    checkpoint: int | None
    timestamp: str | None
    gas_net: int  # MIST: computation + storage - rebate
    swap_events: int
    venues: list[str] = field(default_factory=list)  # package::module of swap events
    changes: dict[str, int] = field(default_factory=dict)  # sender balance changes
    error: str | None = None
    calls: list[str] = field(default_factory=list)  # Move packages called
    recipients: list[str] = field(default_factory=list)  # other owners that gained coins


@dataclass
class Label:
    kind: str  # arb / arb_loss / failed / other
    gross: dict[str, int]  # coin -> raw amount gained BEFORE gas
    gas_net: int


def _event_type(ev: dict) -> str | None:
    c = ev.get("contents") or {}
    t = (c.get("type") or {}).get("repr")
    return t


def parse_node(node: dict, swap_re: re.Pattern) -> Tx:
    """Parse a GraphQL `Transaction` node (shape from gql.TX_FIELDS)."""
    sender = norm_addr(((node.get("sender") or {}).get("address")))
    eff = node.get("effects") or {}
    gs = ((eff.get("gasEffects") or {}).get("gasSummary")) or {}
    gas_net = (
        int(gs.get("computationCost") or 0)
        + int(gs.get("storageCost") or 0)
        - int(gs.get("storageRebate") or 0)
    )

    swaps = 0
    venues: list[str] = []
    for ev in ((eff.get("events") or {}).get("nodes")) or []:
        t = _event_type(ev)
        if not t:
            continue
        base = t.split("<", 1)[0]
        name = base.split("::")[-1]
        if swap_re.search(name):
            swaps += 1
            parts = base.split("::")
            if len(parts) >= 2:
                venues.append(f"{norm_addr(parts[0])}::{parts[1]}")

    changes: dict[str, int] = {}
    recipients: set[str] = set()
    for bc in ((eff.get("balanceChanges") or {}).get("nodes")) or []:
        owner = norm_addr(((bc.get("owner") or {}).get("address")))
        amt = int(bc.get("amount") or 0)
        if owner is None:
            continue
        if owner != sender:
            if amt > 0:
                recipients.add(owner)
            continue
        coin = norm_type(((bc.get("coinType") or {}).get("repr")) or "")
        changes[coin] = changes.get(coin, 0) + amt

    calls: set[str] = set()
    cmds = (((node.get("kind") or {}).get("commands") or {}).get("nodes")) or []
    for c in cmds:
        fq = ((c or {}).get("function") or {}).get("fullyQualifiedName")
        if fq and "::" in fq:
            calls.add(norm_addr(fq.split("::", 1)[0]))

    cp = (eff.get("checkpoint") or {}).get("sequenceNumber")
    err = (eff.get("executionError") or {}).get("message")
    return Tx(
        digest=node["digest"],
        sender=sender,
        status=eff.get("status") or "UNKNOWN",
        checkpoint=int(cp) if cp is not None else None,
        timestamp=eff.get("timestamp"),
        gas_net=gas_net,
        swap_events=swaps,
        venues=sorted(set(venues)),
        changes=changes,
        error=err,
        calls=sorted(calls),
        recipients=sorted(recipients),
    )


def classify(tx: Tx, gas_tolerance: int = 1_000) -> Label:
    if tx.status == "FAILURE":
        return Label("failed", {}, tx.gas_net)
    if tx.swap_events < 2:
        return Label("other", {}, tx.gas_net)

    sui_change = tx.changes.get(SUI, 0)
    others = {c: a for c, a in tx.changes.items() if c != SUI and a != 0}
    if any(a < 0 for a in others.values()):
        return Label("other", {}, tx.gas_net)  # sold a coin = normal swap

    sui_gross = sui_change + tx.gas_net  # SUI change ignoring gas
    gross = dict(others)
    if sui_gross > gas_tolerance:
        gross[SUI] = sui_gross

    if sui_gross < -gas_tolerance:
        if others or tx.recipients:
            return Label("other", {}, tx.gas_net)  # spent SUI to buy a coin / sent it away
        return Label("arb_loss", {SUI: sui_gross}, tx.gas_net)

    if not gross:
        return Label("arb_loss", {}, tx.gas_net)  # break-even before gas
    return Label("arb", gross, tx.gas_net)
