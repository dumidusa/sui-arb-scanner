"""SQLite storage so long scans can resume after a crash or Ctrl-C."""

from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY,
  start_cp INTEGER, end_cp INTEGER,
  start_ts TEXT, end_ts TEXT,
  cursor TEXT, tx_count INTEGER DEFAULT 0, complete INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS txs (
  digest TEXT, stage TEXT,
  checkpoint INTEGER, ts TEXT, sender TEXT, status TEXT, kind TEXT,
  swaps INTEGER, gas_net INTEGER, gross TEXT, venues TEXT, error TEXT,
  calls TEXT, recipients TEXT,
  PRIMARY KEY (digest, stage)
);
CREATE INDEX IF NOT EXISTS txs_sender ON txs(sender, stage, kind);
CREATE TABLE IF NOT EXISTS deep (
  address TEXT PRIMARY KEY,
  txs INTEGER, first_ts TEXT, last_ts TEXT, capped INTEGER, done INTEGER
);
CREATE TABLE IF NOT EXISTS coins (coin TEXT PRIMARY KEY, decimals INTEGER, symbol TEXT);
CREATE TABLE IF NOT EXISTS gap_windows (
  id INTEGER PRIMARY KEY, start_cp INTEGER, end_cp INTEGER, mode TEXT,
  start_ts TEXT, end_ts TEXT, complete INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS swaps (
  wid INTEGER, digest TEXT, idx INTEGER, checkpoint INTEGER, ts TEXT, sender TEXT,
  dex TEXT, pool TEXT, coin_in TEXT, coin_out TEXT, amount_in TEXT, amount_out TEXT,
  ord INTEGER,
  PRIMARY KEY (wid, digest, idx)
);
CREATE TABLE IF NOT EXISTS pools (pool TEXT PRIMARY KEY, coin_a TEXT, coin_b TEXT);
CREATE TABLE IF NOT EXISTS cex (ms INTEGER PRIMARY KEY, price REAL);
"""


class _Rows(list):
    lastrowid = None

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class _LockedDB:
    """sqlite3 connection shared by worker threads: every call under one RLock."""

    def __init__(self, path: str, lock):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.lock = lock

    def execute(self, sql, params=()):
        with self.lock:
            cur = self.c.execute(sql, params)
            rows = _Rows(cur.fetchall())
            rows.lastrowid = cur.lastrowid
            return rows

    def executemany(self, sql, seq):
        with self.lock:
            self.c.executemany(sql, seq)

    def executescript(self, sql):
        with self.lock:
            self.c.executescript(sql)

    def commit(self):
        with self.lock:
            self.c.commit()


class Store:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.db = _LockedDB(path, self.lock)
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(txs)")}
        for c in ("calls", "recipients"):  # upgrade a v0.1 database
            if c not in cols:
                self.db.execute(f"ALTER TABLE txs ADD COLUMN {c} TEXT")
        self.db.commit()

    # meta
    def get_meta(self, k: str):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def set_meta(self, k: str, v) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))
            self.db.commit()

    # windows
    def add_windows(self, wins: list[tuple[int, int]]) -> None:
        with self.lock:
            for s, e in wins:
                self.db.execute(
                    "INSERT INTO windows (start_cp, end_cp) VALUES (?,?)", (s, e))
            self.db.commit()

    def windows(self, only_open: bool = False) -> list[dict]:
        q = "SELECT id,start_cp,end_cp,start_ts,end_ts,cursor,tx_count,complete FROM windows"
        if only_open:
            q += " WHERE complete=0"
        cols = ["id", "start_cp", "end_cp", "start_ts", "end_ts", "cursor", "tx_count", "complete"]
        return [dict(zip(cols, r)) for r in self.db.execute(q + " ORDER BY start_cp")]

    def update_window(self, wid: int, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        with self.lock:
            self.db.execute(f"UPDATE windows SET {sets} WHERE id=?", (*kw.values(), wid))
            self.db.commit()

    # txs
    def put_txs(self, stage: str, rows: list[tuple]) -> None:
        """rows: (tx, label)"""
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO txs (digest, stage, checkpoint, ts, sender, status, kind, "
                "swaps, gas_net, gross, venues, error, calls, recipients) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(t.digest, stage, t.checkpoint, t.timestamp, t.sender, t.status,
                  lab.kind, t.swap_events, t.gas_net, json.dumps(lab.gross),
                  json.dumps(t.venues), t.error, json.dumps(t.calls),
                  json.dumps(t.recipients)) for t, lab in rows])
            self.db.commit()

    def query(self, sql: str, params=()):
        return self.db.execute(sql, params).fetchall()

    # deep dive
    def set_deep(self, addr: str, txs: int, first_ts, last_ts, capped: bool, done: bool):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO deep VALUES (?,?,?,?,?,?)",
                            (addr, txs, first_ts, last_ts, int(capped), int(done)))
            self.db.commit()

    # coins
    def coin(self, coin: str):
        return self.db.execute("SELECT decimals, symbol FROM coins WHERE coin=?", (coin,)).fetchone()

    def put_coin(self, coin: str, decimals, symbol) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO coins VALUES (?,?,?)", (coin, decimals, symbol))
            self.db.commit()

    # gap windows / swaps / pools / cex
    def add_gap_windows(self, wins: list[tuple[int, int, str]]) -> list[int]:
        ids = []
        with self.lock:
            for s_, e_, mode in wins:
                cur = self.db.execute(
                    "INSERT INTO gap_windows (start_cp, end_cp, mode) VALUES (?,?,?)", (s_, e_, mode))
                ids.append(cur.lastrowid)
            self.db.commit()
        return ids

    def gap_windows(self) -> list[dict]:
        cols = ["id", "start_cp", "end_cp", "mode", "start_ts", "end_ts", "complete"]
        return [dict(zip(cols, r)) for r in self.db.execute(
            "SELECT id,start_cp,end_cp,mode,start_ts,end_ts,complete FROM gap_windows ORDER BY start_cp")]

    def update_gap_window(self, wid: int, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        with self.lock:
            self.db.execute(f"UPDATE gap_windows SET {sets} WHERE id=?", (*kw.values(), wid))
            self.db.commit()

    def put_swaps(self, wid: int, swaps) -> None:
        idx = {}
        rows = []
        for k, s_ in enumerate(swaps):
            i = idx.get(s_.digest, 0)
            idx[s_.digest] = i + 1
            rows.append((wid, s_.digest, f"{s_.dex}:{s_.pool}:{i}", s_.checkpoint, s_.ts, s_.sender,
                         s_.dex, s_.pool, s_.coin_in, s_.coin_out, str(s_.amount_in), str(s_.amount_out), k))
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO swaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def swaps(self, wid: int):
        from .swaps import Swap
        return [Swap(dex, pool, cin, cout, int(ain), int(aout), d, snd, cp, ts)
                for d, cp, ts, snd, dex, pool, cin, cout, ain, aout in self.db.execute(
                    "SELECT digest, checkpoint, ts, sender, dex, pool, coin_in, coin_out, amount_in, amount_out "
                    "FROM swaps WHERE wid=? ORDER BY checkpoint, dex, ord", (wid,))]

    def pool(self, pid: str):
        return self.db.execute("SELECT coin_a, coin_b FROM pools WHERE pool=?", (pid,)).fetchone()

    def put_pool(self, pid: str, coins) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pools VALUES (?,?,?)", (pid, coins[0], coins[1]))
            self.db.commit()

    def pool_map(self) -> dict:
        return {p: (a, b) for p, a, b in self.db.execute("SELECT pool, coin_a, coin_b FROM pools")}

    def put_cex(self, rows) -> None:
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO cex VALUES (?,?)", rows)
            self.db.commit()

    def cex_last_ms(self, s_ms: int, e_ms: int):
        r = self.db.execute("SELECT MAX(ms) FROM cex WHERE ms BETWEEN ? AND ?", (s_ms, e_ms)).fetchone()
        return r[0] if r and r[0] is not None else None

    def cex_range(self, s_ms: int, e_ms: int) -> dict[int, float]:
        return dict(self.db.execute("SELECT ms, price FROM cex WHERE ms BETWEEN ? AND ?", (s_ms, e_ms)))
