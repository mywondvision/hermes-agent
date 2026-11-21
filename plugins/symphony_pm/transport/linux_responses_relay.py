"""Per-attempt HTTP/1.1 Responses ingress in the service's private loopback namespace."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import select
import socket
from socketserver import TCPServer
from threading import Event, Lock, Thread
import time

from .linux_carrier import PATH, PORT

MAX_BODY = 16 << 20
MAX_RESPONSE = 64 << 20
REVOKE_INTERVAL = 0.04
HEADERS = frozenset(('authorization', 'chatgpt-account-id', 'content-type', 'content-encoding',
                     'accept', 'openai-beta', 'originator', 'version', 'user-agent', 'session-id',
                     'x-client-request-id', 'x-codex-turn-metadata', 'x-codex-installation-id'))


class ResponsesHandler(BaseHTTPRequestHandler):
    # A response body ends at EOF, so compressed and SSE streams need no buffering or rechunking.
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass

    def setup(self):
        self.request.settimeout(self.server.timeout_for_request())
        super().setup()

    def do_GET(self):
        self.send_error(426 if self.path == PATH and self.headers.get('Upgrade', '').lower() == 'websocket' else 403)

    def do_POST(self):
        lengths = self.headers.get_all('Content-Length', [])
        if (self.path != PATH or self.headers.get_all('Host') != [self.server.authority]
                or self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdecimal() or len(lengths[0]) > 8):
            self.send_error(403)
            return
        size = int(lengths[0])
        if not 0 < size <= MAX_BODY:
            self.send_error(413)
            return
        headers = {key.lower(): value for key, value in self.headers.items() if key.lower() in HEADERS}
        if (any(len(self.headers.get_all(k)) != 1 or '\r' in v or '\n' in v for k, v in headers.items())
                or not headers.get('authorization', '').startswith('Bearer ')
                or not headers.get('chatgpt-account-id') or not headers.get('content-type', '').startswith('application/json')):
            self.send_error(403)
            return
        body = self.rfile.read(size)
        if len(body) != size:
            self.send_error(400)
            return
        if not self.server.authorize():
            self.send_error(403)
            return
        headers['Host'] = 'chatgpt.com'
        conn = http.client.HTTPConnection('chatgpt.com', timeout=self.server.timeout_for_request())
        try:
            # Supply an already peer-verified socket. HTTPConnection must never initiate DNS/TCP
            # on an absent socket; connecting and sending occur only on the pinned connector.
            conn.auto_open = False
            conn.sock = self.server.peer(self.server.timeout_for_request())
            conn.sock.settimeout(self.server.timeout_for_request())
            with self.server.active_request(self.connection, conn.sock) as revoked:
                if not self.server.authorize():
                    self.send_error(403)
                    return
                conn.request('POST', PATH, body, headers)
                self.stream_response(conn, revoked)
        finally:
            conn.close()

    def stream_response(self, conn, revoked):
        # A cross-thread shutdown can signal EOF to the peer while a local blocking
        # receive still waits for data. Keep the first-byte wait interruptible.
        expires = time.monotonic() + self.server.timeout_for_request()
        while not revoked.is_set():
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('LINUX_PROVIDER_TIMEOUT')
            if select.select([conn.sock], [], [], min(REVOKE_INTERVAL, remaining))[0]:
                break
        if revoked.is_set():
            return
        response = conn.getresponse()
        if 300 <= response.status < 400 or response.status == 101:
            self.send_error(502)
            return
        self.send_response(response.status)
        for key in ('Content-Type', 'Content-Encoding'):
            value = response.getheader(key)
            if value and '\r' not in value and '\n' not in value:
                self.send_header(key, value)
        self.end_headers()
        total = 0
        while total < MAX_RESPONSE and self.server.timeout_for_request() > 0:
            chunk = response.read1(min(65536, MAX_RESPONSE - total))
            if not chunk:
                return
            self.wfile.write(chunk)
            total += len(chunk)


class LoopbackRelay(HTTPServer):
    allow_reuse_address = False
    request_queue_size = 4

    def __init__(self, peer, deadline):
        self.peer, self.deadline = peer, deadline
        self.authorize = lambda: False
        self.lock = Lock()
        self.active = None
        super().__init__(('127.0.0.1', PORT), ResponsesHandler)
        self.authority = f'127.0.0.1:{self.server_port}'

    @contextmanager
    def active_request(self, downstream, upstream):
        with downstream.dup() as client, upstream.dup() as connector:
            revoked = Event()
            with self.lock:
                self.active = client, connector, revoked
            try:
                yield revoked
            finally:
                with self.lock:
                    self.active = None

    def revoke_if_closed(self, ending=False):
        with self.lock:
            if self.active is None:
                return
            client, connector, revoked = self.active
            if revoked.is_set():
                return
            try:
                readable, _, _ = select.select([client], [], [], 0)
                disconnected = bool(readable) and client.recv(1, socket.MSG_PEEK) == b''
            except OSError:
                disconnected = True
            if ending or disconnected or not self.authorize() or time.monotonic() >= self.deadline:
                revoked.set()
                for link in (connector, client):
                    try:
                        link.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        # Either peer can finish between the observation and shutdown.
                        continue

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def timeout_for_request(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('LINUX_PROVIDER_TIMEOUT')
        return min(10, remaining)

    def handle_error(self, request, client_address):
        # Never print a request, header, credential, body or exception into the service journal.
        pass


@contextmanager
def running(peer, deadline):
    with LoopbackRelay(peer, deadline) as server:
        # One bounded serving thread belongs to the payload process and its original cgroup.
        server.timeout = 0.1
        def serve():
            while server.deadline > time.monotonic():
                server.handle_request()
        ending = Event()
        def watch():
            while not ending.wait(REVOKE_INTERVAL):
                server.revoke_if_closed()
        worker = Thread(target=serve, daemon=True)
        monitor = Thread(target=watch, daemon=True)
        worker.start()
        monitor.start()
        try:
            yield server
        finally:
            server.deadline = 0
            server.authorize = lambda: False
            server.revoke_if_closed(ending=True)
            ending.set()
            monitor.join(timeout=1)
            worker.join(timeout=11)
            if worker.is_alive() or monitor.is_alive():
                raise TimeoutError('LINUX_PROVIDER_RELAY_STOP')
