"""CLI and the single MPD control loop."""

import argparse
import logging
import os
import signal
import socket
import threading

from . import __version__
from .config import Config, RadioError
from .http import RadioServer, inherited_listener
from .player import Player
from .state import RadioState

LOGGER = logging.getLogger(__name__)
CONTROL_SECONDS = 0.25


def run(config: Config, listener: socket.socket, stopping: threading.Event | None = None) -> None:
    stopping = stopping if stopping is not None else threading.Event()
    state = RadioState()
    player = Player(config)
    server = None
    serving = None
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
            server = RadioServer(config, state, listener)
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
            if server is not None:
                if serving is not None and serving.ident is not None:
                    server.shutdown()
                    serving.join()
                server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared Navidrome Opus radio companion")
    parser.add_argument("--version", action="version", version=f"navdio {__version__}")
    parser.add_argument("--config", required=True, metavar="FILE", help="JSON configuration file")
    args = parser.parse_args(argv)
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
