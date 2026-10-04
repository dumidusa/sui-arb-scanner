"""Minimal Sui GraphQL client (stdlib only).

Sui Foundation mainnet nodes switched JSON-RPC off in July 2026, so this
uses the GraphQL RPC. Endpoint is configurable (--endpoint) so you can use
a paid provider if the public one rate-limits you.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://graphql.mainnet.sui.io/graphql"

TX_FIELDS = """
  digest
  sender { address }
  effects {
    status
    timestamp
    checkpoint { sequenceNumber }
    executionError { message }
    gasEffects { gasSummary { computationCost storageCost storageRebate nonRefundableStorageFee } }
    balanceChanges(first: $bc) { nodes { amount owner { address } coinType { repr } } }
    events(first: $ev) { nodes { contents { type { repr } } } }
  }
  kind {
    ... on ProgrammableTransaction {
      commands(first: $cmd) { nodes { ... on MoveCallCommand { function { fullyQualifiedName } } } }
    }
  }
"""

TX_PAGE_FORWARD = (
    """
query TxPage($first: Int, $after: String, $filter: TransactionFilter, $bc: Int, $ev: Int, $cmd: Int) {
  transactions(first: $first, after: $after, filter: $filter) {
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
}
"""
    % TX_FIELDS
)

TX_PAGE_BACKWARD = (
    """
query TxPageBack($last: Int, $before: String, $filter: TransactionFilter, $bc: Int, $ev: Int, $cmd: Int) {
  transactions(last: $last, before: $before, filter: $filter) {
    pageInfo { hasPreviousPage startCursor }
    nodes { %s }
  }
}
"""
    % TX_FIELDS
)

LATEST_CHECKPOINT = """
query Latest { checkpoint { sequenceNumber timestamp } }
"""

CHECKPOINT_TS = """
query Cps($keys: [UInt53!]!) { multiGetCheckpoints(keys: $keys) { sequenceNumber timestamp } }
"""

AVAILABLE_RANGE = """
query Range {
  serviceConfig { availableRange(type: "Query", field: "transactions") { first { sequenceNumber } last { sequenceNumber } } }
}
"""

EVENTS_PAGE = """
query EvPage($first: Int, $after: String, $filter: EventFilter) {
  events(first: $first, after: $after, filter: $filter) {
    pageInfo { hasNextPage endCursor }
    nodes {
      timestamp
      sender { address }
      contents { type { repr } json }
      transaction { digest effects { checkpoint { sequenceNumber } } }
    }
  }
}
"""

OBJECT_TYPES = """
query Objs($keys: [ObjectKey!]!) {
  multiGetObjects(keys: $keys) { address asMoveObject { contents { type { repr } } } }
}
"""

COIN_META = """
query Meta($t: String!) { coinMetadata(coinType: $t) { decimals symbol } }
"""


class GraphQLError(RuntimeError):
    pass


class _RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.interval
        if t > now:
            time.sleep(t - now)


class GraphQLClient:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, rps: float = 8.0,
                 timeout: float = 60.0, retries: int = 6):
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = retries
        self.limiter = _RateLimiter(rps)

    def query(self, q: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": q, "variables": variables or {}}).encode()
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self.limiter.wait()
            req = urllib.request.Request(
                self.endpoint, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "sui-scanner/0.3"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read())
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                detail = e.read()[:500].decode(errors="replace")
                raise GraphQLError(f"HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                time.sleep(min(30, 2 ** attempt) + random.random())
                continue
            if payload.get("errors"):
                msgs = "; ".join(str(x.get("message")) for x in payload["errors"])
                if payload.get("data") is None:
                    raise GraphQLError(msgs)
            return payload.get("data") or {}
        raise GraphQLError(f"giving up after {self.retries} tries: {last_err}")


class SuiSource:
    """Everything the scanner needs from the chain. Tests swap in a fake."""

    def __init__(self, client: GraphQLClient, tx_page: int = 20,
                 bc_page: int = 20, ev_page: int = 30, cmd_page: int = 10,
                 event_page: int = 50):
        self.c = client
        self.cmd_page = cmd_page
        self.event_page = event_page
        self.tx_page = tx_page
        self.bc_page = bc_page
        self.ev_page = ev_page

    # -- checkpoints -------------------------------------------------------
    def latest_checkpoint(self) -> tuple[int, str]:
        d = self.c.query(LATEST_CHECKPOINT)["checkpoint"]
        return int(d["sequenceNumber"]), d["timestamp"]

    def checkpoint_timestamps(self, seqs: list[int]) -> dict[int, str]:
        d = self.c.query(CHECKPOINT_TS, {"keys": seqs})["multiGetCheckpoints"]
        return {int(x["sequenceNumber"]): x["timestamp"] for x in d if x}

    def first_available_checkpoint(self) -> int | None:
        try:
            r = self.c.query(AVAILABLE_RANGE)["serviceConfig"]["availableRange"]
            return int(r["first"]["sequenceNumber"])
        except Exception:  # not critical
            return None

    # -- transactions ------------------------------------------------------
    def _page(self, q: str, variables: dict) -> dict:
        """Run a page query; on 'too big' errors shrink the page and retry."""
        while True:
            try:
                return self.c.query(q, variables)["transactions"]
            except GraphQLError as e:
                msg = str(e).lower()
                too_big = any(k in msg for k in ("limit", "exceed", "too many", "max", "nodes"))
                size_key = "first" if "first" in variables else "last"
                if too_big and variables[size_key] > 2:
                    variables[size_key] = max(2, variables[size_key] // 2)
                    self.tx_page = variables[size_key]
                    continue
                raise

    def _relax(self, tx_filter: dict, err: GraphQLError) -> bool:
        """If the server rejects a filter combination, drop the least important
        part and let the caller filter client-side instead. Returns True if relaxed."""
        if "kind" in tx_filter:
            print(f"  note: server rejected filter ({err}); retrying without 'kind'", flush=True)
            tx_filter.pop("kind")
            return True
        if "sentAddress" in tx_filter and "afterCheckpoint" in tx_filter:
            print(f"  note: server rejected filter ({err}); retrying without checkpoint bound", flush=True)
            tx_filter.pop("afterCheckpoint")
            return True
        return False

    def tx_pages(self, tx_filter: dict, after: str | None = None):
        """Yield (nodes, end_cursor) oldest -> newest."""
        tx_filter = dict(tx_filter)
        cursor = after
        while True:
            v = {"first": self.tx_page, "after": cursor, "filter": tx_filter,
                 "bc": self.bc_page, "ev": self.ev_page, "cmd": self.cmd_page}
            try:
                page = self._page(TX_PAGE_FORWARD, v)
            except GraphQLError as e:
                if self._relax(tx_filter, e):
                    continue
                raise
            yield page["nodes"], page["pageInfo"]["endCursor"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]

    def tx_pages_backward(self, tx_filter: dict):
        """Yield nodes newest -> oldest (each page is internally oldest->newest)."""
        tx_filter = dict(tx_filter)
        cursor = None
        while True:
            v = {"last": self.tx_page, "before": cursor, "filter": tx_filter,
                 "bc": self.bc_page, "ev": self.ev_page, "cmd": self.cmd_page}
            try:
                page = self._page(TX_PAGE_BACKWARD, v)
            except GraphQLError as e:
                if self._relax(tx_filter, e):
                    continue
                raise
            yield page["nodes"]
            if not page["pageInfo"]["hasPreviousPage"]:
                return
            cursor = page["pageInfo"]["startCursor"]

    # -- events / objects --------------------------------------------------
    def event_pages(self, ev_filter: dict):
        """Yield event node lists, oldest -> newest. Shrinks page on size errors."""
        cursor, size = None, self.event_page
        while True:
            try:
                page = self.c.query(EVENTS_PAGE, {"first": size, "after": cursor,
                                                  "filter": ev_filter})["events"]
            except GraphQLError as e:
                m = str(e).lower()
                if size > 5 and any(k in m for k in ("limit", "exceed", "too many", "max", "nodes")):
                    size //= 2
                    self.event_page = size
                    continue
                raise
            yield page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]

    def pool_coins(self, pool_ids: list[str]) -> dict[str, tuple[str, str]]:
        """pool id -> (coin A, coin B) from the pool object's type parameters."""
        from .swaps import type_params
        from .classify import norm_addr, norm_type
        out = {}
        for i in range(0, len(pool_ids), 20):
            keys = [{"address": p} for p in pool_ids[i:i + 20]]
            rows = self.c.query(OBJECT_TYPES, {"keys": keys})["multiGetObjects"]
            for r in rows:
                if not r:
                    continue
                t = (((r.get("asMoveObject") or {}).get("contents") or {}).get("type") or {}).get("repr")
                ps = type_params(t or "")
                if len(ps) >= 2:
                    out[norm_addr(r["address"])] = (norm_type(ps[0]), norm_type(ps[1]))
        return out

    # -- coins -------------------------------------------------------------
    def coin_meta(self, coin_type: str) -> tuple[int | None, str | None]:
        try:
            d = self.c.query(COIN_META, {"t": coin_type}).get("coinMetadata")
        except GraphQLError:
            return None, None
        if not d:
            return None, None
        return d.get("decimals"), d.get("symbol")
