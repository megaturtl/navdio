"""CLI and the single MPD control loop."""

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import Config, RadioError
from .http import RadioServer, inherited_listener
from .player import Player
from .state import RadioState
from .stats import Statistics, read_statistics

LOGGER = logging.getLogger(__name__)
CONTROL_SECONDS = 0.25


def run(config: Config, listener: socket.socket, stopping: threading.Event | None = None) -> None:
    stopping = stopping if stopping is not None else threading.Event()
    state = RadioState()
    player = Player(config)
    server = None
    serving = None
    statistics = None
    server_failed = threading.Event()

    def serve(server: RadioServer) -> None:
        try:
            server.serve_forever(poll_interval=0.1)
        except Exception:
            server_failed.set()
        finally:
            if not stopping.is_set():
                server_failed.set()
            state.wake.set()

    try:
        player.start(stopping.is_set)
        if stopping.is_set():
            return
        try:
            statistics = Statistics(config.stats_database)
        except RadioError:
            LOGGER.warning("Statistics unavailable; restart Navdio to resume collection")
        try:
            server = RadioServer(config, state, listener, statistics)
        except OSError:
            raise RadioError("Cannot start companion HTTP server") from None
        serving = threading.Thread(target=serve, args=(server,), name="navdio-http")
        serving.start()
        while not stopping.is_set():
            if server_failed.is_set():
                raise RadioError("Companion HTTP server stopped")
            state.wake.clear()
            desired, revision = state.desired()
            rendered = player.apply(desired)
            state.publish(rendered, revision)
            state.wake.wait(CONTROL_SECONDS)
    finally:
        state.clear()
        try:
            player.close()
        finally:
            try:
                if server is not None:
                    if serving is not None and serving.ident is not None:
                        server.shutdown()
                        serving.join()
                    server.server_close()
            finally:
                if statistics is not None:
                    statistics.close()


def print_statistics(database: Path) -> None:
    report = read_statistics(database)

    def utc(timestamp):
        return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    def size(value):
        amount = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
            if amount < 1024 or unit == "EiB":
                return f"{value} B" if unit == "B" else f"{amount:.2f} {unit}"
            amount /= 1024
        raise AssertionError("Unreachable byte unit")

    print(f"Tracking since: {utc(report['started_at'])}")
    print(f"Snapshot: {utc(report['updated_at'])}")
    connected = report["connected"]
    print(f"Connected listeners: {connected if connected is not None else 'unavailable'}")
    if report["stale"]:
        print("The listener count is unavailable because the saved snapshot is out of date.")
    rows = [("Period", "Audio sent", "Unique IPs", "Total listen time")]
    for key, label in (
        ("hour", "Last hour"),
        ("day", "Last 24 hours"),
        ("week", "Last 7 days"),
        ("month", "Last 30 days"),
        ("all_time", "All time"),
    ):
        period = report["periods"][key]
        hours, remainder = divmod(period["listened_ms"] // 1000, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours}h {minutes:02}m {seconds:02}s"
        rows.append((label, size(period["audio_bytes"]), str(period["unique_ips"]), duration))
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    print()
    for row in rows:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        )
    print("\nCounters update every five seconds, with rolling totals grouped by minute.")
    print(
        "Audio sent measures payload bytes, and total listen time adds up stream connection durations."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared Navidrome Opus radio companion")
    parser.add_argument("--version", action="version", version=f"navdio {__version__}")
    parser.add_argument("--config", metavar="FILE", help="JSON configuration file")
    commands = parser.add_subparsers(dest="command")
    stats = commands.add_parser("stats", help="Show local radio statistics")
    stats.add_argument(
        "--database", default="/var/lib/navdio/stats.sqlite3", type=Path, metavar="FILE"
    )
    args = parser.parse_args(argv)
    if args.command == "stats":
        if not args.database.is_absolute() or args.database == Path("/"):
            parser.error("--database must be an absolute file path")
        try:
            print_statistics(args.database)
        except RadioError as error:
            print(error, file=sys.stderr)
            return 1
        return 0
    if args.config is None:
        parser.error("--config is required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        config = Config.load(args.config)
        with inherited_listener(config) as listener:
            run(config, listener, stopping)
    except RadioError as error:
        LOGGER.error("%s", error)
        return 1
    except Exception:
        LOGGER.error("Companion failed")
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0
