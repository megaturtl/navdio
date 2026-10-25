import base64
import http.client
import io
import ipaddress
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch

from PIL import Image

from navdio.config import Config
from navdio.http import RadioServer
from navdio.state import RadioState
from navdio.stats import Statistics, read_statistics


class SourceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.request_headers: list[dict[str, str]] = []
        self.stopping = threading.Event()
        self.payload: bytes | None = None
        self.next_payload = b""
        self.continue_stream = threading.Event()
        self.release = threading.Event()
        self.status = 200
        super().__init__(("127.0.0.1", 0), SourceHandler)


class SourceHandler(BaseHTTPRequestHandler):
    @property
    def source(self) -> SourceServer:
        return cast(SourceServer, self.server)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self):
        self.source.request_headers.append(dict(self.headers))
        self.send_response(self.source.status)
        self.send_header("Content-Type", "audio/ogg")
        self.end_headers()
        try:
            if self.source.status != 200:
                return
            if self.source.payload is not None:
                self.wfile.write(self.source.payload)
                if self.source.next_payload:
                    if not self.source.continue_stream.wait(5):
                        return
                    self.wfile.write(self.source.next_payload)
                self.source.release.wait(5)
                return
            while not self.source.stopping.wait(0.01):
                self.wfile.write(b"OggS" + bytes(128))
        except OSError:
            pass


class HTTPHarness(unittest.TestCase):
    max_listeners = 1
    audio_payload: bytes | None = None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "song.wav").write_bytes(b"fixture")
        self.source = SourceServer()
        self.source.payload = self.audio_payload
        self.source_thread = threading.Thread(
            target=self.source.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.source_thread.start()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        config = Config(
            {"1": self.root},
            b"test-only-token",
            port=self.listener.getsockname()[1],
            source_port=self.source.server_port,
            max_listeners=self.max_listeners,
        )
        self.state = RadioState()
        self.database = self.root / "stats.sqlite3"
        self.statistics = Statistics(self.database, flush_seconds=0.02)
        self.server = RadioServer(config, self.state, self.listener.dup(), self.statistics)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.start()
        self.connections = []
        self.addCleanup(self.cleanup_servers)

    def cleanup_servers(self):
        for connection in self.connections:
            connection.close()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.statistics.close()
        self.listener.close()
        self.source.stopping.set()
        self.source.release.set()
        self.source.continue_stream.set()
        self.source.shutdown()
        self.source_thread.join(timeout=2)
        self.source.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.connections.append(connection)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return connection, response

    def snapshot(self, predicate):
        deadline = time.monotonic() + 3
        wake = threading.Event()
        while time.monotonic() < deadline:
            snapshot = read_statistics(self.database)
            if predicate(snapshot):
                return snapshot
            wake.wait(0.01)
        self.fail("Statistics snapshot did not reach the expected state")

    def publish(self, payload, **headers):
        body = json.dumps(payload).encode()
        defaults = {"Authorization": "Bearer test-only-token", "Content-Type": "application/json"}
        return self.request("POST", "/_navdio/events", body, {**defaults, **headers})[1]

    def payload(self):
        cover = io.BytesIO()
        Image.new("RGB", (80, 40), "blue").save(cover, format="PNG")
        return {
            "version": 1,
            "type": "playback",
            "username": "private-user",
            "playerId": "private-player",
            "playerName": "Player",
            "state": "playing",
            "positionMs": 0,
            "playbackRate": 1,
            "timestamp": 1000,
            "track": {
                "id": "private-track",
                "title": "Title",
                "album": "Album",
                "artist": "Artist",
                "artists": None,
                "duration": 100,
                "libraryId": 1,
                "path": "song.wav",
            },
            "coverArt": base64.b64encode(cover.getvalue()).decode(),
        }


class HTTPTests(HTTPHarness):
    def test_authentication_and_schema_failures_do_not_mutate(self):
        heartbeat = {"version": 1, "type": "heartbeat"}
        self.assertEqual(self.publish(heartbeat, Authorization="Bearer wrong").status, 401)
        self.assertIsNone(self.state.last_event)
        self.assertEqual(self.publish({**heartbeat, "version": 2}).status, 422)
        self.assertIsNone(self.state.last_event)
        self.assertEqual(self.publish(heartbeat).status, 204)
        previous = self.state.last_event
        payload = self.payload()
        payload["coverArt"] = "invalid"
        self.assertEqual(self.publish(payload).status, 422)
        self.assertEqual(self.state.last_event, previous)
        self.assertEqual(self.state.sessions, {})

    def test_body_limits_and_framing(self):
        headers = {"Authorization": "Bearer test-only-token", "Content-Type": "application/json"}
        self.assertEqual(
            self.request("POST", "/_navdio/events", b"", {**headers, "Content-Length": "65537"})[
                1
            ].status,
            413,
        )
        self.assertEqual(
            self.request(
                "POST", "/_navdio/events", b"", {**headers, "Transfer-Encoding": "chunked"}
            )[1].status,
            400,
        )
        self.assertEqual(self.request("POST", "/_navdio/events", b"{", headers)[1].status, 400)
        self.assertEqual(
            self.request(
                "POST", "/_navdio/events", b"{}", {**headers, "Content-Type": "text/plain"}
            )[1].status,
            415,
        )
        self.assertIsNone(self.state.last_event)

    def test_public_info_cover_and_exact_routes(self):
        self.assertEqual(self.publish(self.payload()).status, 204)
        observation, revision = self.state.desired()
        self.state.publish(observation, revision)
        _, response = self.request("GET", "/radio/info")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        info = json.loads(response.read())
        self.assertNotIn("coverArtUrl", info)
        self.assertNotIn("private", json.dumps(info))
        _, response = self.request("GET", "/radio/cover")
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        with Image.open(io.BytesIO(response.read())) as image:
            self.assertEqual(image.size, (50, 50))
        for path in ("/radio/info?token=secret", "/radio/info/", "/_navdio/events", "/healthz"):
            self.assertEqual(self.request("GET", path)[1].status, 404)

    def test_icy_is_never_negotiated_and_listener_slot_is_released(self):
        first, response = self.request("GET", "/radio/stream", headers={"Icy-MetaData": "1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "audio/ogg")
        self.assertIsNone(response.getheader("icy-metaint"))
        self.assertTrue(response.read1(132).startswith(b"OggS"))
        self.assertFalse(
            any(key.lower() == "icy-metadata" for key in self.source.request_headers[0])
        )
        self.assertEqual(self.request("GET", "/radio/stream")[1].status, 503)
        response.close()
        first.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            connection, next_response = self.request("GET", "/radio/stream")
            if next_response.status == 200:
                next_response.close()
                connection.close()
                break
            connection.close()
            time.sleep(0.02)
        else:
            self.fail("Disconnected stream retained its listener slot")

    def test_shutdown_interrupts_stream_and_joins_request_threads(self):
        _, response = self.request("GET", "/radio/stream")
        self.assertEqual(response.status, 200)
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.assertEqual(self.server.connections, set())
        self.assertEqual(self.server.upstreams, set())

    def test_duplicate_lengths_are_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.connections.append(connection)
        connection.putrequest("POST", "/_navdio/events")
        connection.putheader("Authorization", "Bearer test-only-token")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "2")
        connection.putheader("Content-Length", "2")
        connection.endheaders(b"{}")
        self.assertEqual(connection.getresponse().status, 400)
        self.assertIsNone(self.state.last_event)

    def test_expired_source_clears_cover_and_info(self):
        self.assertEqual(self.publish(self.payload()).status, 204)
        observation, revision = self.state.desired()
        self.state.publish(observation, revision)
        last_event = self.state.last_event
        assert last_event is not None
        deadline = last_event + 15
        self.state.clock = lambda: deadline
        self.assertEqual(json.loads(self.request("GET", "/radio/info")[1].read())["state"], "idle")
        self.assertEqual(self.request("GET", "/radio/cover")[1].status, 404)

    @unittest.skipUnless(socket.has_ipv6, "IPv6 sockets unavailable")
    def test_explicit_ipv6_listener(self):
        listener = socket.socket(socket.AF_INET6)
        self.addCleanup(listener.close)
        try:
            listener.bind(("::1", 0))
            listener.listen(32)
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        config = Config(
            {"1": self.root},
            b"test-only-token",
            listen_address="::1",
            port=listener.getsockname()[1],
        )
        server = RadioServer(config, self.state, listener.dup(), None)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        connection = http.client.HTTPConnection("::1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/radio/info")
            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close()
            server.shutdown()
            serving.join(timeout=2)
            server.server_close()

    def test_external_owner_reserves_port_across_worker_restart(self):
        config = self.server.config
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.statistics.close()
        self.statistics = Statistics(self.database, flush_seconds=0.02)
        with socket.socket() as impostor:
            with self.assertRaises(OSError):
                impostor.bind(("127.0.0.1", config.port))
        self.server = RadioServer(config, self.state, self.listener.dup(), self.statistics)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.start()
        _, response = self.request("GET", "/radio/info")
        self.assertEqual(response.status, 200)
        with socket.socket() as impostor:
            with self.assertRaises(OSError):
                impostor.bind(("127.0.0.1", config.port))


class HTTPStatisticsTests(HTTPHarness):
    max_listeners = 2
    audio_payload = b"OggS" + bytes(12284)

    def test_exact_payload_totals_and_listener_limit(self):
        responses = []
        for _ in range(2):
            _, response = self.request("GET", "/radio/stream")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(12288), self.audio_payload)
            responses.append(response)
        snapshot = self.snapshot(
            lambda value: (
                value["connected"] == 2 and value["periods"]["all_time"]["audio_bytes"] == 24576
            )
        )
        for period in snapshot["periods"].values():
            self.assertEqual((period["audio_bytes"], period["unique_ips"]), (24576, 1))
        self.assertEqual(self.request("GET", "/radio/stream")[1].status, 503)
        self.source.release.set()
        for response in responses:
            self.assertEqual(response.read(), b"")
        snapshot = self.snapshot(lambda value: value["connected"] == 0)
        self.assertEqual(snapshot["periods"]["all_time"]["audio_bytes"], 24576)
        self.assertEqual(snapshot["periods"]["all_time"]["unique_ips"], 1)
        self.assertGreater(snapshot["periods"]["all_time"]["listened_ms"], 0)

    def test_failed_upstream_setup_records_no_audio_or_listener(self):
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            self.server.config = replace(
                self.server.config, source_port=unavailable.getsockname()[1]
            )
            self.assertEqual(self.request("GET", "/radio/stream")[1].status, 503)
        self.statistics.close()
        snapshot = read_statistics(self.database)
        self.assertEqual(snapshot["connected"], 0)
        self.assertEqual(
            snapshot["periods"]["all_time"],
            {"audio_bytes": 0, "unique_ips": 0, "listened_ms": 0},
        )

    def test_rejected_upstream_records_no_audio_or_listener(self):
        self.source.status = 503
        self.assertEqual(self.request("GET", "/radio/stream")[1].status, 503)
        self.statistics.close()
        self.assertEqual(
            read_statistics(self.database)["periods"]["all_time"],
            {"audio_bytes": 0, "unique_ips": 0, "listened_ms": 0},
        )

    def test_empty_source_records_no_audio_or_listener(self):
        self.source.payload = b""
        self.source.release.set()
        _, response = self.request("GET", "/radio/stream")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"")
        self.statistics.close()
        self.assertEqual(
            read_statistics(self.database)["periods"]["all_time"],
            {"audio_bytes": 0, "unique_ips": 0, "listened_ms": 0},
        )

    def test_failed_first_downstream_write_records_no_audio_or_listener(self):
        original_write = socket.socket.sendall
        failed = threading.Event()

        def fail_audio_write(writer, data):
            if writer.getsockname()[1] == self.server.server_port and not data.startswith(b"HTTP/"):
                failed.set()
                raise BrokenPipeError
            return original_write(writer, data)

        with patch.object(socket.socket, "sendall", fail_audio_write):
            _, response = self.request("GET", "/radio/stream")
            self.assertEqual(response.status, 200)
            self.assertTrue(failed.wait(2), "The fixture did not attempt its first audio write")
            self.assertEqual(response.read(), b"")
        self.statistics.close()
        self.assertEqual(
            read_statistics(self.database)["periods"]["all_time"],
            {"audio_bytes": 0, "unique_ips": 0, "listened_ms": 0},
        )

    def test_disconnect_and_shutdown_clear_connected_listeners(self):
        self.source.payload = None
        streams = [self.request("GET", "/radio/stream") for _ in range(2)]
        for _, response in streams:
            self.assertTrue(response.read(132).startswith(b"OggS"))
        self.snapshot(lambda value: value["connected"] == 2)
        connection, response = streams[0]
        response.close()
        connection.close()
        snapshot = self.snapshot(lambda value: value["connected"] == 1)
        self.assertEqual(snapshot["periods"]["all_time"]["unique_ips"], 1)
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())
        snapshot = self.snapshot(lambda value: value["connected"] == 0)
        self.assertGreaterEqual(snapshot["periods"]["all_time"]["audio_bytes"], 264)
        self.assertEqual(snapshot["periods"]["all_time"]["unique_ips"], 1)
        self.assertEqual(self.server.connections, set())
        self.assertEqual(self.server.upstreams, set())

    def stream_with_headers(self, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.connections.append(connection)
        connection.putrequest("GET", "/radio/stream")
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(12288), self.audio_payload)
        return response

    def test_untrusted_real_ip_and_forwarded_headers_are_ignored(self):
        for address in ("198.51.100.10", "203.0.113.20"):
            self.stream_with_headers(
                [
                    ("X-Real-IP", address),
                    ("X-Forwarded-For", address),
                    ("Forwarded", f"for={address}"),
                ]
            )
        snapshot = self.snapshot(lambda value: value["connected"] == 2)
        self.assertEqual(snapshot["periods"]["all_time"]["unique_ips"], 1)

    @unittest.skipUnless(socket.has_ipv6, "IPv6 sockets unavailable")
    def test_mapped_peer_matches_ipv4_trusted_proxy_network(self):
        listener = socket.socket(socket.AF_INET6)
        self.addCleanup(listener.close)
        try:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind(("::ffff:127.0.0.1", 0))
            listener.listen(32)
        except OSError:
            self.skipTest("IPv4-mapped IPv6 loopback unavailable")
        config = replace(
            self.server.config,
            listen_address="::ffff:127.0.0.1",
            port=listener.getsockname()[1],
            stats_trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),),
        )
        server = RadioServer(config, self.state, listener.dup(), self.statistics)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        responses = []
        try:
            for address in ("198.51.100.10", "203.0.113.20"):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                self.connections.append(connection)
                connection.request("GET", "/radio/stream", headers={"X-Real-IP": address})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(12288), self.audio_payload)
                responses.append(response)
            snapshot = self.snapshot(lambda value: value["connected"] == 2)
            self.assertEqual(snapshot["periods"]["all_time"]["unique_ips"], 2)
        finally:
            server.shutdown()
            serving.join(timeout=2)
            server.server_close()
            for response in responses:
                response.close()

    def test_trusted_real_ip_distinguishes_normalized_addresses(self):
        self.server.config = replace(
            self.server.config, stats_trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),)
        )
        self.source.release.set()
        for index, (address, unique_ips) in enumerate(
            (("198.51.100.10", 1), ("::ffff:198.51.100.10", 1), ("203.0.113.20", 2)), 1
        ):
            with self.subTest(address=address):
                response = self.stream_with_headers([("X-Real-IP", address)])
                self.assertEqual(response.read(), b"")
                snapshot = self.snapshot(
                    lambda value, index=index: (
                        value["connected"] == 0
                        and value["periods"]["all_time"]["audio_bytes"] == index * 12288
                    )
                )
                self.assertEqual(snapshot["periods"]["all_time"]["unique_ips"], unique_ips)

    def test_invalid_trusted_headers_fall_back_to_peer(self):
        self.server.config = replace(
            self.server.config, stats_trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),)
        )
        self.source.release.set()
        cases = (
            [],
            [("X-Real-IP", "")],
            [("X-Real-IP", "198.51.100.10"), ("X-Real-IP", "203.0.113.20")],
            [("X-Real-IP", "198.51.100.10, 203.0.113.20")],
            [("X-Real-IP", "fe80::1%lo")],
            [("X-Real-IP", "198.51.100.10:80")],
            [("X-Real-IP", "[2001:db8::1]:80")],
            [("X-Real-IP", "[2001:db8::1]")],
            [("X-Real-IP", "not-an-address")],
            [("X-Forwarded-For", "198.51.100.10"), ("Forwarded", "for=203.0.113.20")],
        )
        for index, headers in enumerate(cases, 1):
            with self.subTest(headers=headers):
                response = self.stream_with_headers(headers)
                self.assertEqual(response.read(), b"")
                snapshot = self.snapshot(
                    lambda value, index=index: (
                        value["connected"] == 0
                        and value["periods"]["all_time"]["audio_bytes"] == index * 12288
                    )
                )
                self.assertEqual(snapshot["periods"]["all_time"]["unique_ips"], 1)

    def test_proxy_headers_do_not_affect_authentication_or_public_info(self):
        self.server.config = replace(
            self.server.config, stats_trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),)
        )
        headers = {
            "X-Real-IP": "198.51.100.10",
            "X-Forwarded-For": "203.0.113.20",
            "Forwarded": "for=192.0.2.30",
        }
        heartbeat = {"version": 1, "type": "heartbeat"}
        self.assertEqual(
            self.publish(heartbeat, **headers, Authorization="Bearer wrong").status, 401
        )
        self.assertIsNone(self.state.last_event)
        self.assertEqual(self.publish(heartbeat, **headers).status, 204)
        _, response = self.request("GET", "/radio/info", headers=headers)
        info = response.read().decode()
        for address in headers.values():
            self.assertNotIn(address, info)
        self.assertEqual(
            read_statistics(self.database)["periods"]["all_time"],
            {"audio_bytes": 0, "unique_ips": 0, "listened_ms": 0},
        )

    def test_writer_failure_does_not_interrupt_audio_or_shutdown(self):
        assert self.audio_payload is not None
        self.source.next_payload = self.audio_payload
        _, response = self.request("GET", "/radio/stream")
        self.assertEqual(response.read(12288), self.audio_payload)
        self.snapshot(
            lambda value: (
                value["connected"] == 1 and value["periods"]["all_time"]["audio_bytes"] == 12288
            )
        )
        blocker = sqlite3.connect(self.database, timeout=1)
        try:
            with self.assertLogs("navdio.stats", level="WARNING") as warnings:
                blocker.execute("BEGIN IMMEDIATE")
                deadline = time.monotonic() + 3
                wake = threading.Event()
                while not warnings.records and time.monotonic() < deadline:
                    wake.wait(0.01)
                self.assertEqual(len(warnings.records), 1)
                self.assertEqual(
                    warnings.records[0].getMessage(),
                    "Statistics unavailable; restart Navdio to resume collection",
                )
                self.source.continue_stream.set()
                self.assertEqual(response.read(12288), self.audio_payload)
                self.server.shutdown()
                self.thread.join(timeout=2)
                self.server.server_close()
                self.statistics.close()
                self.assertFalse(self.thread.is_alive())
                self.assertEqual(self.server.connections, set())
                self.assertEqual(len(warnings.records), 1)
        finally:
            blocker.rollback()
            blocker.close()
        stored = read_statistics(self.database)
        stale = read_statistics(self.database, now=stored["updated_at"] + 16)
        self.assertTrue(stale["stale"])
        self.assertIsNone(stale["connected"])
        self.assertEqual(stale["periods"]["all_time"]["audio_bytes"], 12288)


ACTIVATION_CHILD = """
import json
import os
import sys
from unittest.mock import patch
from navdio.app import main
from navdio.config import Config
from navdio.http import inherited_listener

fd, filename, pid, count, mode = sys.argv[1:]
if int(fd) >= 0:
    os.dup2(int(fd), 3, inheritable=True)
    os.set_inheritable(3, True)
    if int(fd) != 3:
        os.close(int(fd))
if pid != "missing":
    os.environ["LISTEN_PID"] = str(os.getpid()) if pid == "self" else pid
if count != "missing":
    os.environ["LISTEN_FDS"] = count
os.environ["LISTEN_FDNAMES"] = "http"
if mode == "inspect":
    with inherited_listener(Config.load(filename)) as listener:
        result = {"port": listener.getsockname()[1],
                  "inheritable": listener.get_inheritable()}
    status = 0
else:
    with patch("navdio.app.Player.start") as start:
        status = main(["--config", filename])
        result = {"mpd_started": start.called}
try:
    result["fd3_inheritable"] = os.get_inheritable(3)
except OSError:
    result["fd3_inheritable"] = None
result["activation"] = [name for name in os.environ if name.startswith("LISTEN_")]
print(json.dumps(result))
raise SystemExit(status)
"""


class ActivationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.port = self.listener.getsockname()[1]
        token = self.root / "token"
        token.write_text("test-only-token")
        self.config = self.root / "config.json"
        self.configuration = {
            "libraries": {"1": str(self.root)},
            "tokenFile": str(token),
            "runtimeDirectory": str(self.root / "runtime"),
            "port": self.port,
            "sourcePort": 1 if self.port != 1 else 2,
        }

    def child(self, descriptor=None, pid="self", count="1", mode="main", **config):
        self.config.write_text(json.dumps({**self.configuration, **config}))
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("LISTEN_")
        }
        environment["PYTHONPATH"] = os.pathsep.join(sys.path)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                ACTIVATION_CHILD,
                str(descriptor if descriptor is not None else -1),
                str(self.config),
                pid,
                count,
                mode,
            ],
            pass_fds=() if descriptor is None else (descriptor,),
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def assert_rejected(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("test-only-token", result.stderr)
        self.assertNotIn(str(self.root), result.stderr)
        observation = json.loads(result.stdout)
        self.assertFalse(observation["mpd_started"])
        self.assertEqual(observation["activation"], [])

    def test_missing_and_invalid_activation_fail_before_mpd(self):
        self.assert_rejected(self.child(pid="missing", count="missing"))
        self.assert_rejected(self.child())
        for pid, count in (
            ("missing", "1"),
            ("0", "1"),
            ("invalid", "1"),
            ("self", "missing"),
            ("self", "0"),
            ("self", "2"),
            ("self", "-1"),
            ("self", "invalid"),
        ):
            with self.subTest(pid=pid, count=count):
                self.assert_rejected(self.child(self.listener.fileno(), pid=pid, count=count))

    def test_wrong_socket_or_endpoint_is_rejected(self):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with self.subTest(kind=kind), socket.socket(type=kind) as listener:
                listener.bind(("127.0.0.1", 0))
                self.assert_rejected(self.child(listener.fileno()))
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.root / "unix.sock"))
            listener.listen()
            self.assert_rejected(self.child(listener.fileno()))
        with self.config.open("w") as not_socket:
            self.assert_rejected(self.child(not_socket.fileno()))
        self.assert_rejected(self.child(self.listener.fileno(), port=self.port % 65535 + 1))
        self.assert_rejected(self.child(self.listener.fileno(), listenAddress="0.0.0.0"))
        self.assert_rejected(self.child(self.listener.fileno(), listenAddress="::1"))

    def test_valid_activation_consumes_fd3_and_clears_environment(self):
        result = self.child(self.listener.fileno(), mode="inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        observation = json.loads(result.stdout)
        self.assertEqual(observation["port"], self.port)
        self.assertFalse(observation["inheritable"])
        self.assertEqual(observation["activation"], [])
        with socket.socket() as impostor:
            with self.assertRaises(OSError):
                impostor.bind(("127.0.0.1", self.port))

    @unittest.skipUnless(socket.has_ipv6, "IPv6 sockets unavailable")
    def test_valid_ipv6_activation(self):
        with socket.socket(socket.AF_INET6) as listener:
            try:
                listener.bind(("::1", 0))
                listener.listen()
            except OSError:
                self.skipTest("IPv6 loopback unavailable")
            result = self.child(
                listener.fileno(),
                mode="inspect",
                listenAddress="::1",
                port=listener.getsockname()[1],
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
