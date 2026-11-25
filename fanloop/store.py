"""The loop's SQLite store: threshold versions, samples, events, episodes, marks
and small key-value state (spec 5.5). WAL mode; one writer connection under one lock."""
import json
import queue
import sqlite3
import sys
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fanloop.episodes import Episode
from fanloop.levels import Level
from fanloop.readings import CardReading, IpmiReading
from fanloop.thresholds import AmbientRule, Change, Thresholds

DAY_S = 86400
READERS = 4  # pooled read-only connections for the API

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threshold_versions (
    version INTEGER PRIMARY KEY, created_at REAL NOT NULL, source TEXT NOT NULL,
    reason TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS threshold_versions_created ON threshold_versions (created_at);
CREATE TABLE IF NOT EXISTS threshold_changes (
    version INTEGER NOT NULL REFERENCES threshold_versions (version), seq INTEGER NOT NULL,
    param TEXT NOT NULL, old_json TEXT NOT NULL, new_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL, level INTEGER, busy INTEGER NOT NULL, throttled INTEGER NOT NULL,
    temp0 REAL, temp1 REAL, power0 REAL, power1 REAL, util0 REAL, util1 REAL,
    inlet REAL, exhaust REAL, sys_power REAL, data_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY, start REAL NOT NULL, "end" REAL NOT NULL, data_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS episodes_start ON episodes (start);
CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
"""


def _change_value(v: Any) -> Any:
    return v.to_dict() if isinstance(v, AmbientRule) else v


def _card(cards: Sequence[CardReading], index: int) -> Optional[CardReading]:
    return next((c for c in cards if c.index == index), None)


class Store:
    """Writes go through one connection under one lock. The API's large reads
    (samples, events, episodes, versions) use a small pool of read-only connections,
    which WAL lets run alongside a write, so a slow query never delays the loop."""

    def __init__(self, path: str, defaults: Thresholds):
        self._defaults = defaults
        self._path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._readers: List[sqlite3.Connection] = []   # every pooled connection, to close them
        self._pool: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue()
        self._readers_lock = threading.Lock()  # not the write lock: reads must never wait for it
        self.current()  # first start: store the starting thresholds as version 1

    def close(self) -> None:
        with self._readers_lock:
            for conn in self._readers:
                conn.close()
            self._readers.clear()
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _q(self, sql: str, args: Sequence = ()) -> List[tuple]:
        return self._conn.execute(sql, args).fetchall()

    def _read(self, sql: str, args: Sequence = ()) -> List[tuple]:
        """A read that doesn't take the write lock (a :memory: database has only one connection)."""
        if self._path == ":memory:":
            with self._lock:
                return self._q(sql, args)
        conn = self._take_reader()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            self._pool.put(conn)

    def _take_reader(self) -> sqlite3.Connection:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._readers_lock:
            if len(self._readers) < READERS:
                conn = sqlite3.connect(self._path, timeout=5, check_same_thread=False)
                conn.execute("PRAGMA query_only=ON")
                self._readers.append(conn)
                return conn
        return self._pool.get(timeout=10)  # all in use: wait for one (other readers, never the loop)

    # -- threshold versions ------------------------------------------------

    def current(self) -> Tuple[int, Thresholds]:
        with self._lock:
            rows = self._q("SELECT version, data_json FROM threshold_versions ORDER BY version DESC LIMIT 1")
            if not rows:
                self._insert_version(self._defaults, "defaults", "starting thresholds (spec 5.3)", [], 0.0)
                return 1, self._defaults
            return rows[0][0], Thresholds.from_dict(json.loads(rows[0][1]))

    def get_version(self, v: int) -> Thresholds:
        with self._lock:
            rows = self._q("SELECT data_json FROM threshold_versions WHERE version = ?", (v,))
            if not rows:
                raise KeyError(f"no threshold version {v}")
            return Thresholds.from_dict(json.loads(rows[0][0]))

    def add_version(self, t: Thresholds, source: str, reason: str, changes: Sequence[Change], now: float) -> int:
        with self._lock:
            return self._insert_version(t, source, reason, changes, now)

    def _insert_version(self, t, source, reason, changes, now) -> int:
        with self._conn:  # one transaction: the version and its changes
            cur = self._conn.execute(
                "INSERT INTO threshold_versions (created_at, source, reason, data_json) VALUES (?, ?, ?, ?)",
                (now, source, reason, json.dumps(t.to_dict())))
            version = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO threshold_changes (version, seq, param, old_json, new_json) VALUES (?, ?, ?, ?, ?)",
                [(version, i, c.param, json.dumps(_change_value(c.from_)), json.dumps(_change_value(c.to)))
                 for i, c in enumerate(changes)])
        return version

    def versions(self, since_version: int = 0, since_ts: float = 0) -> List[dict]:
        rows = self._read(
            "SELECT version, created_at, source, reason, data_json FROM threshold_versions "
            "WHERE version > ? AND created_at >= ? ORDER BY version", (since_version, since_ts))
        out = []
        for version, created_at, source, reason, data in rows:
            changes = [{"param": p, "from": json.loads(o), "to": json.loads(n)} for p, o, n in self._read(
                "SELECT param, old_json, new_json FROM threshold_changes WHERE version = ? ORDER BY seq",
                (version,))]
            out.append({"version": version, "created_at": created_at, "source": source, "reason": reason,
                        "thresholds": json.loads(data), "changes": changes})
        return out

    def count_versions(self, source: str, since_ts: float) -> int:
        with self._lock:
            return self._q("SELECT COUNT(*) FROM threshold_versions WHERE source = ? AND created_at >= ?",
                           (source, since_ts))[0][0]

    # -- samples -------------------------------------------------------------

    def add_sample(self, ts: float, cards: Sequence[CardReading], ipmi: Optional[IpmiReading],
                   level: Optional[Level], busy: bool) -> None:
        c0, c1 = _card(cards, 0), _card(cards, 1)
        data = {
            "cards": [{"index": c.index, "temp_c": c.temp_c, "power_w": c.power_w, "util_pct": c.util_pct,
                       "clock_mhz": c.clock_mhz, "sw_power_cap": c.sw_power_cap, "sw_thermal": c.sw_thermal,
                       "hw_thermal": c.hw_thermal} for c in cards],
            "fans_rpm": ipmi.fans_rpm if ipmi else {},
        }
        row = (ts, int(level) if level is not None else None, int(busy),
               int(any(c.sw_thermal or c.hw_thermal for c in cards)),
               c0.temp_c if c0 else None, c1.temp_c if c1 else None,
               c0.power_w if c0 else None, c1.power_w if c1 else None,
               c0.util_pct if c0 else None, c1.util_pct if c1 else None,
               ipmi.inlet_c if ipmi else None, ipmi.exhaust_c if ipmi else None,
               ipmi.power_w if ipmi else None, json.dumps(data))
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)

    def samples(self, start: float, end: float, max_points: int = 300) -> List[dict]:
        """Bucket averages between start and end (inclusive), at most max_points rows.
        Level is the loudest in the bucket; busy and throttled are 'any'."""
        if end <= start or max_points < 1:
            return []
        bucket = (end - start) / max_points
        rows = self._read(
            "SELECT MIN(CAST((ts - ?) / ? AS INTEGER), ?) AS b, COUNT(*), "
            "AVG(temp0), AVG(temp1), MAX(temp0), MAX(temp1), AVG(power0), AVG(power1), "
            "AVG(util0), AVG(util1), AVG(inlet), AVG(exhaust), AVG(sys_power), "
            "MAX(level), MAX(busy), MAX(throttled) "
            "FROM samples WHERE ts >= ? AND ts <= ? GROUP BY b ORDER BY b",
            (start, bucket, max_points - 1, start, end))
        out = []
        for (b, n, t0, t1, t0max, t1max, p0, p1, u0, u1, inlet, exhaust, power, level, busy, thr) in rows:
            out.append({"ts": start + b * bucket, "n": n, "temp0": t0, "temp1": t1, "temp0_max": t0max,
                        "temp1_max": t1max, "power0": p0, "power1": p1, "util0": u0, "util1": u1,
                        "inlet": inlet, "exhaust": exhaust, "sys_power": power,
                        "level": Level(level).name.lower() if level is not None else None,
                        "busy": bool(busy), "throttled": bool(thr)})
        return out

    def prune(self, now: float, sample_days: int = 30) -> int:
        """Delete raw samples older than sample_days; everything else is kept."""
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM samples WHERE ts < ?", (now - sample_days * DAY_S,)).rowcount

    # -- events and episodes ---------------------------------------------------

    def add_event(self, ts: float, kind: str, detail: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO events (ts, kind, detail_json) VALUES (?, ?, ?)",
                               (ts, kind, json.dumps(detail)))

    def events(self, since_ts: float, kinds: Optional[Sequence[str]] = None, limit: int = 500) -> List[dict]:
        sql, args = "SELECT ts, kind, detail_json FROM events WHERE ts >= ?", [since_ts]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        sql += " ORDER BY ts, id LIMIT ?"
        args.append(limit)
        return [{"ts": ts, "kind": kind, "detail": json.loads(d)} for ts, kind, d in self._read(sql, args)]

    def add_episode(self, ep: Episode) -> int:
        with self._lock, self._conn:
            return self._conn.execute('INSERT INTO episodes (start, "end", data_json) VALUES (?, ?, ?)',
                                      (ep.start, ep.end, json.dumps(ep.to_dict()))).lastrowid

    def episodes(self, since_ts: float = 0, version: Optional[int] = None, limit: int = 200) -> List[Episode]:
        rows = self._read("SELECT data_json FROM episodes WHERE start >= ? ORDER BY start, id", (since_ts,))
        eps = [Episode.from_dict(json.loads(r[0])) for r in rows]
        if version is not None:
            eps = [e for e in eps if e.versions == [version]]
        return eps[:limit]

    # -- marks -------------------------------------------------------------------

    def mark_insert(self, started_at: float) -> int:
        with self._lock, self._conn:
            return self._conn.execute("INSERT INTO marks (started_at, outcome) VALUES (?, 'open')",
                                      (started_at,)).lastrowid

    def mark_close(self, mark_id: int, ended_at: float, outcome: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE marks SET ended_at = ?, outcome = ? WHERE id = ?", (ended_at, outcome, mark_id))

    @staticmethod
    def _mark(row) -> dict:
        return {"id": row[0], "started_at": row[1], "ended_at": row[2], "outcome": row[3]}

    def mark_get(self, mark_id: int) -> Optional[dict]:
        with self._lock:
            rows = self._q("SELECT id, started_at, ended_at, outcome FROM marks WHERE id = ?", (mark_id,))
        return self._mark(rows[0]) if rows else None

    def marks_open(self) -> List[dict]:
        with self._lock:
            return [self._mark(r) for r in self._q(
                "SELECT id, started_at, ended_at, outcome FROM marks WHERE outcome = 'open' ORDER BY id")]

    def marks(self, since_ts: float) -> List[dict]:
        with self._lock:
            return [self._mark(r) for r in self._q(
                "SELECT id, started_at, ended_at, outcome FROM marks WHERE started_at >= ? ORDER BY id", (since_ts,))]

    # -- key-value state, backup ---------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            rows = self._q("SELECT value_json FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO kv (key, value_json) VALUES (?, ?) "
                               "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json",
                               (key, json.dumps(value)))

    def backup_to(self, dest_path: str) -> None:
        """A consistent copy, even while the loop keeps writing (SQLite backup API)."""
        dest = sqlite3.connect(dest_path)
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()


def inspect(path: str) -> dict:
    """Counts from a database file, opened read-only (used to check a restored backup)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        def count(table):
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {
            "current_version": conn.execute("SELECT MAX(version) FROM threshold_versions").fetchone()[0],
            "versions": count("threshold_versions"),
            "episodes": count("episodes"),
            "events": count("events"),
            "samples": count("samples"),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m fanloop.store <database file>")
    print(json.dumps(inspect(sys.argv[1])))
