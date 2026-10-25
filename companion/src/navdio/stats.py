"""Collect radio statistics in SQLite and read them for the CLI."""

import hmac
import ipaddress
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import RadioError

UNAVAILABLE = "Statistics database is unavailable"
DISABLED = "Statistics unavailable; restart Navdio to resume collection"
WINDOWS = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000}
_LOG = logging.getLogger(__name__)
_SCHEMA = (
    """CREATE TABLE meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        identity_key BLOB NOT NULL CHECK (length(identity_key) = 32),
        started_at INTEGER NOT NULL CHECK (started_at >= 0),
        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
        running INTEGER NOT NULL CHECK (running IN (0, 1)),
        connected INTEGER NOT NULL CHECK (connected >= 0),
        audio_bytes INTEGER NOT NULL CHECK (audio_bytes >= 0),
        listened_ms INTEGER NOT NULL DEFAULT 0 CHECK (listened_ms >= 0)
    )""",
    """CREATE TABLE minutes (
        minute INTEGER PRIMARY KEY CHECK (minute >= 0 AND minute % 60 = 0),
        audio_bytes INTEGER NOT NULL CHECK (audio_bytes >= 0),
        listened_ms INTEGER NOT NULL DEFAULT 0 CHECK (listened_ms >= 0)
    )""",
    """CREATE TABLE listeners (
        identity BLOB PRIMARY KEY NOT NULL CHECK (length(identity) = 32),
        last_seen INTEGER NOT NULL CHECK (last_seen >= 0)
    )""",
    "CREATE INDEX listeners_last_seen ON listeners (last_seen)",
)


def _validate(connection: sqlite3.Connection) -> sqlite3.Row:
    if connection.execute("PRAGMA user_version").fetchone()[0] != 3:
        raise ValueError
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != {"meta", "minutes", "listeners"}:
        raise ValueError
    rows = connection.execute(
        "SELECT id, identity_key, started_at, updated_at, running, connected, audio_bytes, listened_ms "
        "FROM meta LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise ValueError
    meta = rows[0]
    if (
        meta["id"] != 1
        or not isinstance(meta["identity_key"], bytes)
        or len(meta["identity_key"]) != 32
        or any(
            type(meta[name]) is not int or meta[name] < 0
            for name in (
                "started_at",
                "updated_at",
                "running",
                "connected",
                "audio_bytes",
                "listened_ms",
            )
        )
        or meta["running"] not in (0, 1)
        or (not meta["running"] and meta["connected"])
    ):
        raise ValueError
    connection.execute("SELECT minute, audio_bytes, listened_ms FROM minutes LIMIT 0")
    connection.execute("SELECT identity, last_seen FROM listeners LIMIT 0")
    return meta


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        _private_directory(path.parent)
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise


def _initialize(database: Path, now: int) -> tuple[sqlite3.Connection, sqlite3.Row]:
    connection = None
    try:
        _private_directory(database.parent)
        try:
            descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        connection = sqlite3.connect(
            database, timeout=0.1, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise ValueError
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO meta VALUES (1, ?, ?, ?, 0, 0, 0, 0)",
                (secrets.token_bytes(32), now, now),
            )
        elif version == 2:
            for table in ("meta", "minutes"):
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN listened_ms INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (listened_ms >= 0)"
                )
        if version in (0, 2):
            connection.execute("PRAGMA user_version = 3")
        meta = _validate(connection)
        connection.execute("COMMIT")
        if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
            raise ValueError
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE meta SET running = 1, connected = 0, updated_at = ? WHERE id = 1", (now,)
        )
        connection.execute("COMMIT")
        return connection, meta
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        if connection is not None:
            connection.close()
        raise RadioError(UNAVAILABLE) from None


class Statistics:
    def __init__(
        self,
        database: Path,
        *,
        clock: Callable[[], float] = time.time,
        flush_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not math.isfinite(flush_seconds) or flush_seconds <= 0:
            raise ValueError("flush_seconds must be positive")
        self._clock = clock
        self._flush_seconds = flush_seconds
        self._monotonic = monotonic
        self._last_tick = int(monotonic() * 1000)
        connection, meta = _initialize(database, int(clock()))
        self._key = meta["identity_key"]
        self._audio_bytes = meta["audio_bytes"]
        self._listened_ms = meta["listened_ms"]
        self._active: dict[int, bytes] = {}
        self._minutes: dict[int, list[int]] = {}
        self._listeners: dict[bytes, int] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._enabled = True
        self._closed = False
        self._thread = threading.Thread(
            target=self._write_loop, args=(connection,), name="navdio-statistics", daemon=True
        )
        try:
            self._thread.start()
        except RuntimeError:
            connection.close()
            raise RadioError(UNAVAILABLE) from None

    def _account_time(self) -> None:
        tick = int(self._monotonic() * 1000)
        elapsed = max(0, tick - self._last_tick)
        self._last_tick = tick
        count = len(self._active)
        if not count or not self._enabled:
            return
        self._listened_ms += elapsed * count
        now = int(self._clock() * 1000)
        # Limit minute history to the longest rolling window.
        start = max(now - elapsed, (now // 60000 - WINDOWS["month"] // 60) * 60000)
        while start < now:
            minute = start // 60000 * 60
            end = min(now, (minute + 60) * 1000)
            self._minutes.setdefault(minute, [0, 0])[1] += (end - start) * count
            start = end

    def _record(self, identity: bytes, byte_count: int) -> None:
        now = int(self._clock())
        minute = now // 60 * 60
        self._minutes.setdefault(minute, [0, 0])[0] += byte_count
        self._listeners[identity] = max(now, self._listeners.get(identity, 0))
        self._audio_bytes += byte_count

    def begin(self, address: str, byte_count: int) -> int | None:
        if type(byte_count) is not int or byte_count <= 0:
            raise ValueError("byte_count must be positive")
        if "%" in address:
            raise ValueError("address must be a bare IP")
        peer = ipaddress.ip_address(address)
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        identity = hmac.digest(self._key, peer.packed, "sha256")
        with self._lock:
            if not self._enabled or self._closed:
                return None
            self._account_time()
            self._next_id += 1
            stream_id = self._next_id
            self._active[stream_id] = identity
            self._record(identity, byte_count)
            return stream_id

    def add(self, stream_id: int, byte_count: int) -> None:
        if type(byte_count) is not int or byte_count <= 0:
            raise ValueError("byte_count must be positive")
        with self._lock:
            if self._enabled and not self._closed and stream_id in self._active:
                self._record(self._active[stream_id], byte_count)

    def end(self, stream_id: int) -> None:
        with self._lock:
            self._account_time()
            self._active.pop(stream_id, None)

    def close(self) -> None:
        with self._lock:
            self._account_time()
            self._closed = True
            self._active.clear()
            self._stop.set()
        self._thread.join()

    def _write_loop(self, connection: sqlite3.Connection) -> None:
        try:
            while True:
                self._stop.wait(self._flush_seconds)
                with self._lock:
                    self._account_time()
                    now = int(self._clock())
                    stopped = self._closed
                    connected = len(self._active)
                    audio_bytes = self._audio_bytes
                    listened_ms = self._listened_ms
                    minutes, self._minutes = self._minutes, {}
                    listeners, self._listeners = self._listeners, {}
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    """INSERT INTO minutes VALUES (?, ?, ?)
                    ON CONFLICT(minute) DO UPDATE SET
                    audio_bytes = audio_bytes + excluded.audio_bytes,
                    listened_ms = listened_ms + excluded.listened_ms""",
                    ((minute, values[0], values[1]) for minute, values in minutes.items()),
                )
                connection.executemany(
                    """INSERT INTO listeners VALUES (?, ?)
                    ON CONFLICT(identity) DO UPDATE SET last_seen = max(last_seen, excluded.last_seen)""",
                    listeners.items(),
                )
                connection.execute(
                    "DELETE FROM minutes WHERE minute < ?", ((now - WINDOWS["month"]) // 60 * 60,)
                )
                connection.execute(
                    """UPDATE meta SET updated_at = ?, running = ?, connected = ?,
                    audio_bytes = ?, listened_ms = ? WHERE id = 1""",
                    (now, int(not stopped), connected, audio_bytes, listened_ms),
                )
                connection.execute("COMMIT")
                if stopped:
                    return
        except Exception:
            with self._lock:
                self._enabled = False
                self._active.clear()
                self._minutes.clear()
                self._listeners.clear()
            _LOG.warning(DISABLED)
        finally:
            connection.close()


def read_statistics(database: Path, *, now: float | None = None) -> dict:
    connection = None
    try:
        when = time.time() if now is None else now
        if not math.isfinite(when):
            raise ValueError
        connection = sqlite3.connect(
            database.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.1, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        meta = _validate(connection)
        periods = {}
        for name, duration in WINDOWS.items():
            audio_bytes = connection.execute(
                """SELECT coalesce(sum(audio_bytes), 0), coalesce(sum(listened_ms), 0)
                FROM minutes WHERE minute BETWEEN ? AND ?""",
                (math.floor((when - duration) / 60) * 60, math.floor(when / 60) * 60),
            ).fetchone()
            unique = connection.execute(
                "SELECT count(*) FROM listeners WHERE last_seen >= ?", (when - duration,)
            ).fetchone()[0]
            periods[name] = {
                "audio_bytes": audio_bytes[0],
                "listened_ms": audio_bytes[1],
                "unique_ips": unique,
            }
        periods["all_time"] = {
            "audio_bytes": meta["audio_bytes"],
            "listened_ms": meta["listened_ms"],
            "unique_ips": connection.execute("SELECT count(*) FROM listeners").fetchone()[0],
        }
        stale = bool(meta["running"] and not 0 <= when - meta["updated_at"] <= 15)
        return {
            "started_at": meta["started_at"],
            "updated_at": meta["updated_at"],
            "connected": None if stale else meta["connected"] if meta["running"] else 0,
            "stale": stale,
            "periods": periods,
        }
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        raise RadioError(UNAVAILABLE) from None
    finally:
        if connection is not None:
            connection.close()
