import base64
import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from navdio.config import Config
from navdio.http import RadioServer
from navdio.state import RadioState


class SourceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.request_headers: list[dict[str, str]] = []
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", 0), SourceHandler)


class SourceHandler(BaseHTTPRequestHandler):
    server: SourceServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self):
        self.server.request_headers.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Type", "audio/ogg")
        self.end_headers()
        try:
            while not self.server.stopping.wait(0.01):
                self.wfile.write(b"OggS" + bytes(128))
        except OSError:
            pass


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "song.wav").write_bytes(b"fixture")
        self.source = SourceServer()
        self.source_thread = threading.Thread(
            target=self.source.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.source_thread.start()
        config = Config(
            {"1": self.root},
            b"test-only-token",
            port=0,
            source_port=self.source.server_port,
            max_listeners=1,
        )
        self.state = RadioState()
        self.server = RadioServer(config, self.state)
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
        self.source.stopping.set()
        self.source.shutdown()
        self.source_thread.join(timeout=2)
        self.source.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.connections.append(connection)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return connection, response

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
        config = Config({"1": self.root}, b"test-only-token", listen_address="::1", port=0)
        try:
            server = RadioServer(config, self.state)
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
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


if __name__ == "__main__":
    unittest.main()
