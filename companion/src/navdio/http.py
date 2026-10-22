"""Bounded event intake and the three public radio endpoints."""

import http.client
import json
import logging
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .config import Config, RadioError
from .state import MAX_BODY_BYTES, EventError, RadioState, parse_event

IO_TIMEOUT = 5
LOGGER = logging.getLogger(__name__)


def inherited_listener(config: Config) -> socket.socket:
    activation = {
        name: os.environ.pop(name, None) for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES")
    }
    listener = None
    try:
        if activation["LISTEN_PID"] != str(os.getpid()) or activation["LISTEN_FDS"] != "1":
            raise ValueError
        os.set_inheritable(3, False)
        listener = socket.socket(fileno=3)
        family = socket.AF_INET6 if ":" in config.listen_address else socket.AF_INET
        if (
            listener.family != family
            or listener.type != socket.SOCK_STREAM
            or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        ):
            raise ValueError
        address = listener.getsockname()
        if address[1] != config.port or socket.inet_pton(family, address[0]) != socket.inet_pton(
            family, config.listen_address
        ):
            raise ValueError
        listener.setblocking(True)
        return listener
    except (OSError, ValueError):
        if listener is not None:
            listener.close()
        raise RadioError("Invalid companion socket activation") from None


class RadioServer(ThreadingMixIn, HTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32

    def __init__(self, config: Config, state: RadioState, listener: socket.socket):
        self.config = config
        self.state = state
        self.listener_slots = threading.BoundedSemaphore(config.max_listeners)
        self.request_slots = threading.BoundedSemaphore(config.max_listeners + 16)
        self.connections_lock = threading.Lock()
        self.connections: set[socket.socket] = set()
        self.upstreams: set[http.client.HTTPConnection] = set()
        self.closing = False
        self.address_family = listener.family
        super().__init__(listener.getsockname(), RadioHandler, bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.socket.set_inheritable(False)
        self.server_name = socket.getfqdn(config.listen_address)
        self.server_port = self.server_address[1]

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(IO_TIMEOUT)
        return connection, address

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self.connections_lock:
            if self.closing:
                self.request_slots.release()
                self.shutdown_request(request)
                return
            self.connections.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.connections_lock:
                self.connections.discard(request)
            self.request_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.connections_lock:
                self.connections.discard(request)
            self.request_slots.release()

    def handle_error(self, request, client_address):
        # The standard traceback can expose parsed data and filesystem paths.
        LOGGER.error("HTTP request failed")

    def register_upstream(self, connection):
        with self.connections_lock:
            if self.closing:
                return False
            self.upstreams.add(connection)
            return True

    def unregister_upstream(self, connection):
        with self.connections_lock:
            self.upstreams.discard(connection)

    def server_close(self):
        with self.connections_lock:
            self.closing = True
            connections = list(self.connections)
            upstreams = list(self.upstreams)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for connection in upstreams:
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        super().server_close()


class RadioHandler(BaseHTTPRequestHandler):
    server: RadioServer
    server_version = "navdio"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def log_error(self, format: str, *args: object) -> None:
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code)

    def reply(self, status, body=b"", content_type="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        if self.path != "/_navdio/events":
            self.reply(404)
            return
        authorizations = self.headers.get_all("Authorization", [])
        authorization = authorizations[0] if len(authorizations) == 1 else ""
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            credential.encode("utf-8"), self.server.config.token
        ):
            self.reply(401, extra={"WWW-Authenticate": "Bearer"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1:
            self.reply(400)
            return
        if not lengths:
            self.reply(411)
            return
        if not lengths[0].isascii() or not lengths[0].isdigit():
            self.reply(400)
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self.reply(400)
            return
        if length > MAX_BODY_BYTES:
            self.reply(413)
            return
        if (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            self.reply(415)
            return
        try:
            body = bytearray()
            deadline = time.monotonic() + IO_TIMEOUT
            while len(body) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.reply(408)
                    return
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(4096, length - len(body)))
                if not chunk:
                    self.reply(400)
                    return
                body.extend(chunk)
            self.connection.settimeout(IO_TIMEOUT)
            event = parse_event(body, self.server.config.libraries)
            self.server.state.accept(event)
        except EventError as error:
            self.reply(400 if str(error) == "Invalid event JSON" else 422)
            return
        except OSError:
            self.close_connection = True
            return
        self.reply(204)

    def do_GET(self):
        if self.path == "/radio/info":
            body = json.dumps(
                self.server.state.info(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.reply(200, body)
        elif self.path == "/radio/cover":
            cover = self.server.state.cover()
            if cover:
                self.reply(200, cover, "image/jpeg")
            else:
                self.reply(404)
        elif self.path == "/radio/stream":
            self.stream()
        else:
            self.reply(404)

    def stream(self):
        server = self.server
        if not server.listener_slots.acquire(blocking=False):
            self.reply(503)
            return
        upstream = http.client.HTTPConnection(
            "127.0.0.1", server.config.source_port, timeout=IO_TIMEOUT
        )
        response = None
        sent = False
        try:
            if not server.register_upstream(upstream):
                self.reply(503)
                return
            # ICY metadata adds non-Ogg bytes. Do not request or relay it.
            upstream.request("GET", "/", headers={"Connection": "close"})
            response = upstream.getresponse()
            if response.status != 200 or response.getheader("icy-metaint") is not None:
                self.reply(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/ogg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            sent = True
            while not server.closing:
                chunk = response.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException, ValueError):
            if not sent:
                try:
                    self.reply(503)
                except OSError:
                    pass
        finally:
            self.close_connection = True
            if response is not None:
                response.close()
            upstream.close()
            server.unregister_upstream(upstream)
            server.listener_slots.release()
