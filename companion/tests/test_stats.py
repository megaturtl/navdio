import contextlib
import io
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from navdio.config import RadioError
from navdio.stats import DISABLED, UNAVAILABLE, Statistics, read_statistics

NOW = 2_000_000_040
A = "198.51.100.10"
B = "203.0.113.20"
C = "2001:db8::30"


class Clock:
    def __init__(self, value=NOW):
        self.value = value
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def set(self, value):
        with self.lock:
            self.value = value


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "private" / "stats.sqlite3"
        self.clock = Clock()
        self.ticks = Clock(0)

    def collector(self):
        store = Statistics(
            self.database, clock=self.clock, monotonic=self.ticks, flush_seconds=0.01
        )
        self.addCleanup(store.close)
        return store

    def begin(self, store: Statistics, address: str, byte_count: int) -> int:
        handle = store.begin(address, byte_count)
        assert handle is not None
        return handle

    def snapshot(self):
        return read_statistics(self.database, now=self.clock())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        pause = threading.Event()
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            pause.wait(0.005)
        self.fail("Timed out waiting for a statistics snapshot")

    def committed(self, *, audio_bytes=None, connected=None):
        def matching():
            report = self.snapshot()
            if (
                audio_bytes is not None
                and report["periods"]["all_time"]["audio_bytes"] != audio_bytes
            ):
                return None
            if connected is not None and report["connected"] != connected:
                return None
            return report

        return self.wait_for(matching)

    def test_rolling_windows_and_pruning_preserve_all_time(self):
        self.clock.set(NOW - 2592120)
        store = self.collector()
        for age, count, address in (
            (2592120, 1, A),
            (604920, 2, A),
            (86520, 4, B),
            (3720, 8, B),
            (120, 16, C),
        ):
            self.clock.set(NOW - age)
            store.end(self.begin(store, address, count))
        self.clock.set(NOW)
        store.close()
        report = self.snapshot()
        self.assertEqual(list(report["periods"]), ["hour", "day", "week", "month", "all_time"])
        self.assertEqual(
            [entry["audio_bytes"] for entry in report["periods"].values()], [16, 24, 28, 30, 31]
        )
        self.assertEqual(
            [entry["unique_ips"] for entry in report["periods"].values()], [1, 2, 2, 3, 3]
        )
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            oldest, count = connection.execute(
                "SELECT min(minute), count(*) FROM minutes"
            ).fetchone()
        self.assertEqual((oldest, count), (NOW - 604920, 4))
        reopened = self.collector()
        reopened.close()
        self.assertEqual(self.snapshot()["periods"], report["periods"])
        self.assertEqual(self.snapshot()["started_at"], NOW - 2592120)

    def test_listener_time_splits_minutes_and_sums_connections(self):
        self.clock.set(NOW - 1)
        store = Statistics(
            self.database, clock=self.clock, monotonic=self.clock, flush_seconds=3600
        )
        self.addCleanup(store.close)
        first = self.begin(store, A, 1)
        second = self.begin(store, A, 1)
        self.clock.set(NOW + 1)
        store.end(first)
        store.end(first)
        self.clock.set(NOW + 2.5)
        store.end(second)
        self.clock.set(NOW + 60)
        store.close()
        report = self.snapshot()
        self.assertEqual(report["periods"]["all_time"]["listened_ms"], 5500)
        self.assertEqual(report["periods"]["all_time"]["unique_ips"], 1)
        cutoff = read_statistics(self.database, now=NOW + 3600)
        self.assertEqual(cutoff["periods"]["hour"]["listened_ms"], 3500)
        reopened = self.collector()
        reopened.close()
        self.assertEqual(self.snapshot()["periods"], report["periods"])

    def test_listener_time_in_each_rolling_window(self):
        self.clock.set(NOW - 2592120)
        store = Statistics(
            self.database, clock=self.clock, monotonic=self.clock, flush_seconds=3600
        )
        self.addCleanup(store.close)
        for age, seconds in ((2592120, 1), (604920, 2), (86520, 4), (3720, 8), (120, 16)):
            self.clock.set(NOW - age)
            handle = self.begin(store, A, 1)
            self.clock.set(NOW - age + seconds)
            store.end(handle)
        self.clock.set(NOW)
        store.close()
        self.assertEqual(
            [period["listened_ms"] for period in self.snapshot()["periods"].values()],
            [16000, 24000, 28000, 30000, 31000],
        )

    def test_active_time_commits_without_more_audio(self):
        self.ticks = self.clock
        store = self.collector()
        self.begin(store, A, 1)
        self.clock.set(NOW + 90)
        self.wait_for(lambda: self.snapshot()["periods"]["all_time"]["listened_ms"] == 90000)
        self.assertEqual(self.snapshot()["connected"], 1)
        store.close()
        self.assertEqual(self.snapshot()["periods"]["all_time"]["listened_ms"], 90000)
        status, output, error = self.cli("--database", str(self.database))
        self.assertEqual((status, error), (0, ""))
        for label in ("Last hour", "Last 24 hours", "Last 7 days", "Last 30 days", "All time"):
            self.assertRegex(output, label + r"\s+1 B\s+1\s+0h 01m 30s")

    def test_wall_clock_changes_do_not_change_elapsed_time(self):
        store = Statistics(
            self.database, clock=self.clock, monotonic=self.ticks, flush_seconds=3600
        )
        self.addCleanup(store.close)
        handle = self.begin(store, A, 1)
        self.ticks.set(2)
        self.clock.set(NOW - 3600)
        store.end(handle)
        handle = self.begin(store, A, 1)
        self.ticks.set(5)
        self.clock.set(NOW + 3600)
        store.end(handle)
        store.close()
        self.assertEqual(self.snapshot()["periods"]["all_time"]["listened_ms"], 5000)

    def test_upgrade_preserves_existing_statistics(self):
        store = self.collector()
        store.end(self.begin(store, A, 17))
        store.close()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("ALTER TABLE meta DROP COLUMN listened_ms")
            connection.execute("ALTER TABLE minutes DROP COLUMN listened_ms")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        self.ticks = self.clock
        store = self.collector()
        before = self.snapshot()["periods"]["all_time"]
        self.assertEqual(before, {"audio_bytes": 17, "unique_ips": 1, "listened_ms": 0})
        handle = self.begin(store, A, 3)
        self.clock.set(NOW + 2)
        store.end(handle)
        store.close()
        self.assertEqual(
            self.snapshot()["periods"]["all_time"],
            {"audio_bytes": 20, "unique_ips": 1, "listened_ms": 2000},
        )

    def test_cutoff_minute_and_continuing_identity(self):
        self.clock.set(NOW - 3660)
        store = self.collector()
        handle = self.begin(store, A, 1)
        self.clock.set(NOW - 3600)
        store.end(self.begin(store, B, 2))
        self.clock.set(NOW - 3541)
        store.end(self.begin(store, C, 4))
        self.clock.set(NOW + 30)
        store.add(handle, 8)
        report = self.committed(audio_bytes=15)
        # The cutoff minute contributes bytes, while uniqueness follows each address's last-seen time.
        self.assertEqual(
            report["periods"]["hour"], {"audio_bytes": 14, "unique_ips": 2, "listened_ms": 0}
        )
        store.end(handle)
        store.end(handle)
        store.add(handle, 500)
        store.end(self.begin(store, A, 16))
        store.close()
        self.assertEqual(
            self.snapshot()["periods"]["all_time"],
            {"audio_bytes": 31, "unique_ips": 3, "listened_ms": 0},
        )

    def test_normalized_connections_restart_and_private_storage(self):
        store = self.collector()
        first = self.begin(store, A, 11)
        self.begin(store, "::ffff:" + A, 13)
        report = self.committed(audio_bytes=24, connected=2)
        self.assertEqual(
            report["periods"]["all_time"], {"audio_bytes": 24, "unique_ips": 1, "listened_ms": 0}
        )
        store.end(first)
        store.end(first)
        self.committed(connected=1)
        store.close()
        self.assertEqual(self.snapshot()["connected"], 0)
        reopened = self.collector()
        reopened.end(self.begin(reopened, A, 3))
        reopened.close()
        self.assertEqual(
            self.snapshot()["periods"]["all_time"],
            {"audio_bytes": 27, "unique_ips": 1, "listened_ms": 0},
        )
        for address in (A, "::ffff:" + A):
            self.assertNotIn(address.encode(), self.database.read_bytes())
        self.assertEqual(stat.S_IMODE(self.database.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)

    def test_empty_and_invalid_stores(self):
        missing = self.root / "missing" / "stats.sqlite3"
        with self.assertRaisesRegex(RadioError, "^" + UNAVAILABLE + "$"):
            read_statistics(missing)
        self.assertFalse(missing.parent.exists())
        store = self.collector()
        store.close()
        self.assertEqual((self.snapshot()["connected"], self.snapshot()["stale"]), (0, False))
        for period in self.snapshot()["periods"].values():
            self.assertEqual(period, {"audio_bytes": 0, "unique_ips": 0, "listened_ms": 0})
        for name, statement in (
            ("corrupt", None),
            ("unknown-version", "PRAGMA user_version = 42"),
            ("unknown-schema", "CREATE TABLE private_data (secret TEXT)"),
            ("missing-schema", "PRAGMA user_version = 3"),
            ("invalid-key", "UPDATE meta SET identity_key = x'01'"),
        ):
            with self.subTest(name=name):
                database = self.root / f"{name}.sqlite3"
                if statement is None:
                    database.write_bytes(b"not sqlite; private path or token")
                else:
                    if name == "invalid-key":
                        Statistics(database, clock=self.clock).close()
                    with contextlib.closing(sqlite3.connect(database)) as connection:
                        connection.execute("PRAGMA ignore_check_constraints = ON")
                        connection.execute(statement)
                        connection.commit()
                before = database.read_bytes()
                for operation in (read_statistics, Statistics):
                    with self.assertRaisesRegex(RadioError, "^" + UNAVAILABLE + "$"):
                        operation(database)
                    self.assertEqual(database.read_bytes(), before)

    def test_busy_writer_disables_once_and_preserves_last_snapshot(self):
        store = self.collector()
        handle = self.begin(store, A, 17)
        report = self.committed(audio_bytes=17, connected=1)
        with contextlib.closing(sqlite3.connect(self.database)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with mock.patch("navdio.stats._LOG.warning") as warning:
                self.clock.set(NOW + 1)
                store.add(handle, 19)
                self.wait_for(lambda: warning.called)
                self.assertEqual(warning.call_args_list, [mock.call(DISABLED)])
                self.assertIsNone(store.begin(B, 23))
                store.add(handle, 29)
                store.end(handle)
                store.close()
                self.assertEqual(warning.call_count, 1)
            blocker.rollback()
        stale = read_statistics(self.database, now=NOW + 16)
        self.assertTrue(stale["stale"])
        self.assertIsNone(stale["connected"])
        self.assertEqual(stale["updated_at"], report["updated_at"])
        self.assertEqual(
            stale["periods"]["all_time"], {"audio_bytes": 17, "unique_ips": 1, "listened_ms": 0}
        )
        restarted = self.collector()
        self.assertEqual(self.snapshot()["connected"], 0)
        restarted.close()

    def cli(self, *arguments):
        from navdio import app

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for name in ("Player", "RadioServer", "Statistics", "inherited_listener"):
                stack.enter_context(mock.patch.object(app, name, side_effect=AssertionError(name)))
            stack.enter_context(
                mock.patch.object(app.Config, "load", side_effect=AssertionError("Config"))
            )
            stack.enter_context(
                mock.patch.object(app.signal, "signal", side_effect=AssertionError("Signal"))
            )
            stack.enter_context(
                mock.patch.dict(os.environ, {"LISTEN_PID": "invalid", "LISTEN_FDS": "invalid"})
            )
            stack.enter_context(mock.patch("navdio.stats.time.time", return_value=self.clock()))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            status = app.main(["stats", *arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_readonly_fresh_stopped_stale_and_missing(self):
        self.database = self.root / "stats ?#% ü.sqlite3"
        store = self.collector()
        self.begin(store, A, 131072)
        self.committed(audio_bytes=131072, connected=1)
        status, output, error = self.cli("--database", str(self.database))
        self.assertEqual((status, error), (0, ""))
        self.assertRegex(output, r"Connected listeners[^\n]*1")
        for label in ("Last hour", "Last 24 hours", "Last 7 days", "Last 30 days", "All time"):
            self.assertRegex(output, label + r"\s+128\.00 KiB\s+1")
        self.assertNotIn(A, output)
        store.close()
        before = self.database.read_bytes()
        status, output, error = self.cli("--database", str(self.database))
        self.assertEqual((status, error), (0, ""))
        self.assertRegex(output, r"Connected listeners[^\n]*0")
        self.assertEqual(self.database.read_bytes(), before)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE meta SET running = 1, connected = 1")
            connection.commit()
        for age in (16, -1):
            self.clock.set(NOW + age)
            status, output, error = self.cli("--database", str(self.database))
            self.assertEqual((status, error), (0, ""))
            self.assertIn("unavailable", output.lower())
            self.assertIn("128.00 KiB", output)
        self.clock.set(NOW + 16)
        store = self.collector()
        self.wait_for(lambda: self.snapshot()["updated_at"] == NOW + 16)
        self.clock.set(NOW + 60)
        self.wait_for(lambda: self.snapshot()["updated_at"] == NOW + 60)
        self.assertFalse(self.snapshot()["stale"])
        store.close()
        expired = read_statistics(self.database, now=NOW + 86400)
        self.assertEqual(
            expired["periods"]["hour"], {"audio_bytes": 0, "unique_ips": 0, "listened_ms": 0}
        )
        self.assertEqual((expired["connected"], expired["stale"]), (0, False))
        missing = self.root / "missing.sqlite3"
        status, output, error = self.cli("--database", str(missing))
        self.assertEqual((status, output, error.strip()), (1, "", UNAVAILABLE))
        self.assertFalse(missing.exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.cli("--database", "relative.sqlite3")
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
