"""Loopback Responses server for the two-turn, subscription-authenticated transport contract."""
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import inspect
import json
import re
import shlex
from socketserver import TCPServer
from threading import Thread

from plugins.symphony_pm.transport.linux_carrier import HOME, PATH, PORT, SOCKET


def valid_worker_environment(env):
    import uuid

    # v0.158.0 unified_exec/process_manager.rs sets its own terminal and pager defaults.
    required = {'CODEX_SANDBOX_NETWORK_DISABLED': '1', 'CODEX_VERSION': '0.158.0',
                'CODEX_PERMISSION_PROFILE': 'symphony-provider', 'NO_COLOR': '1', 'TERM': 'dumb',
                'LANG': 'C.UTF-8', 'LC_CTYPE': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'COLORTERM': '',
                'PAGER': 'cat', 'GIT_PAGER': 'cat', 'GH_PAGER': 'cat', 'CODEX_CI': '1'}
    shell = {'PWD': ('/work',), 'SHLVL': ('0', '1', '2'), '_': ('/usr/bin/python3',)}
    identities = {'CODEX_THREAD_ID', 'CODEX_SESSION_ID'}
    if (set(env) - required.keys() - shell.keys() - identities
            or any(env.get(k) != v for k, v in required.items())
            or any(env[k] not in v for k, v in shell.items() if k in env)):
        return False
    try:
        return all(str(uuid.UUID(env.get(k, ''))) == env.get(k) for k in identities)
    except ValueError:
        return False


def worker_command(nonce):
    # A native run emits the marker only in a command that cannot reach the live relay or client home.
    script = f"""import errno, json, os, socket, stat, sys
from pathlib import Path
{inspect.getsource(valid_worker_environment)}
if not valid_worker_environment(os.environ):
    sys.exit(4)
devices = Path('/proc/net/dev').read_text().splitlines()[2:]
if [row.split(':')[0].strip() for row in devices] != ['lo']:
    sys.exit(9)
if len(Path('/proc/net/route').read_text().splitlines()) != 1:
    sys.exit(9)
ipv6 = Path('/proc/net/ipv6_route')
if ipv6.exists() and any(row.split()[-1] != 'lo' for row in ipv6.read_text().splitlines()):
    sys.exit(9)
with socket.socket() as channel:
    channel.settimeout(2)
    result = channel.connect_ex(('127.0.0.1', {PORT}))
if result not in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EACCES, errno.EPERM):
    sys.exit(2)
if os.access({HOME!r}, os.R_OK) or os.access({HOME!r} + '/auth.json', os.R_OK) or os.path.lexists({SOCKET!r}):
    sys.exit(3)
for name in os.listdir('/proc/self/fd'):
    fd = int(name)
    try:
        mode = os.fstat(fd).st_mode
    except OSError as error:
        if error.errno == errno.EBADF:
            continue
        raise
    if fd > 2 or stat.S_ISSOCK(mode):
        sys.exit(8)
ns = {{k: os.readlink('/proc/self/ns/' + k) for k in ('mnt', 'net', 'pid')}}
print('symphony-worker-' + {nonce!r} + ' ' + json.dumps(ns, sort_keys=True))
"""
    return shlex.join(['/usr/bin/python3', '-I', '-B', '-c', script])


class ResponsesStub:
    route = PATH

    def __init__(self, nonce, client_namespaces=None):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', nonce):
            raise ValueError('expected an opaque fixture nonce')
        self.nonce = nonce
        self.call_id = 'call_' + sha256(nonce.encode()).hexdigest()[:24]
        self.expected_output = 'symphony-worker-' + nonce
        self.responses = 0
        self.origin = None
        self.client_namespaces = client_namespaces
        self.worker_namespaces = None
        self.request_methods = []
        self.first_policy_index = None
        self.policy_requests = 0

    def complete(self, body):
        items = body.get('input', [])
        if body.get('stream') is not True:
            raise ValueError('stream required')
        if self.responses == 0:
            if self.nonce not in json.dumps(items) or not any(
                tool.get('type') == 'function' and tool.get('name') == 'exec_command'
                for tool in body.get('tools', [])
            ):
                raise ValueError('missing fixture prompt or command tool')
            item = {'type': 'function_call', 'call_id': self.call_id, 'name': 'exec_command',
                    'arguments': json.dumps({'cmd': worker_command(self.nonce), 'login': False})}
        elif self.responses == 1:
            hits = [i for i in items if i.get('type') == 'function_call_output' and i.get('call_id') == self.call_id]
            if len(hits) != 1 or not self._returned_marker(hits[0].get('output')):
                raise ValueError('command readback mismatch')
            item = {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'fixture complete'}]}
        else:
            raise ValueError('exchange already complete')
        response = {'id': f'resp_fixture_{self.responses + 1}'}
        events = [{'type': 'response.created', 'response': response},
                  {'type': 'response.output_item.done', 'item': item},
                  {'type': 'response.completed', 'response': {**response, 'status': 'completed'}}]
        result = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        self.responses += 1
        return result

    def _returned_marker(self, output):
        if not isinstance(output, str):
            return False
        if self.client_namespaces is None:
            return output == self.expected_output
        lines = output.rstrip().splitlines()
        if (lines.count('Process exited with code 0') != 1
                or any(line.startswith('Process running with session ID') for line in lines)
                or not lines[-1].startswith(self.expected_output + ' ')):
            return False
        worker = json.loads(lines[-1].removeprefix(self.expected_output + ' '))
        client = self.client_namespaces()
        kinds = {'mnt', 'net', 'pid'}
        if not isinstance(worker, dict) or set(worker) != kinds or set(client) != kinds:
            return False
        if any(not re.fullmatch(kind + r':\[[0-9]+\]', value)
               for ns in (worker, client) for kind, value in ns.items()):
            return False
        if any(worker[kind] == client[kind] for kind in kinds):
            return False
        self.worker_namespaces = worker
        return True

    def receipt(self):
        if self.responses != 2:
            raise ValueError('incomplete exchange')
        result = {'nonce': self.nonce, 'call_id': self.call_id, 'responses': self.responses,
                  'tool_output_source': 'synthetic' if self.client_namespaces is None else 'linux-cli'}
        if self.client_namespaces is not None:
            result['worker_namespaces'] = self.worker_namespaces
        return result


@contextmanager
def responses_stub(nonce, *, token='fixture-token', account='fixture-account', tls=None,
                   client_namespaces=None, policy_origin=None, forward_post=None, port=None):
    stub = ResponsesStub(nonce, client_namespaces)
    if policy_origin not in (None, 'NO_CONSTRAINT', 'https://conflict.invalid'):
        raise ValueError('unknown synthetic workspace policy')
    if policy_origin is not None or forward_post is not None:
        if tls is None or (token, account) != ('fixture-token', 'fixture-account'):
            raise ValueError('workspace routing qualification requires HTTPS and the fixture account')
    if port not in (None, 0, PORT):
        raise ValueError('expected a loopback fixture port')

    class Handler(BaseHTTPRequestHandler):
        timeout = 3

        def log_message(self, fmt, *args):
            # Request headers and bodies are deliberately excluded from the fixture report.
            return

        def do_GET(self):
            stub.request_methods.append('GET')
            if self.path != '/backend-api/wham/accounts/check' or policy_origin is None:
                self.send_error(426 if self.path == stub.route else 404)
                return
            if not self.authenticated():
                self.send_error(401)
                return
            body = json.dumps({'accounts': [{'id': account, 'plan_type': 'pro',
                                'workspace_backend_origin': policy_origin, 'account_routing_override': 'NO_CONSTRAINT'}],
                               'default_account_id': account}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if stub.first_policy_index is None:
                stub.first_policy_index = len(stub.request_methods) - 1
            stub.policy_requests += 1

        def authenticated(self):
            return (self.headers.get_all('Authorization') == ['Bearer ' + token]
                    and self.headers.get_all('chatgpt-account-id') == [account])

        def do_POST(self):
            stub.request_methods.append('POST')
            if self.path != stub.route:
                self.send_error(404)
                return
            if not self.authenticated():
                self.send_error(401)
                return
            if self.headers.get('Content-Encoding', 'identity') != 'identity':
                self.send_error(415)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 262144:
                    raise ValueError('invalid fixture request size')
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('incomplete fixture request')
                if forward_post is None:
                    status, media_type, payload = 200, 'text/event-stream', stub.complete(json.loads(raw))
                else:
                    headers = {key.lower(): value for key, value in self.headers.items()}
                    if len(headers) != len(self.headers) or 'transfer-encoding' in headers:
                        raise ValueError('ambiguous fixture headers')
                    status, media_type, payload = forward_post(raw, headers)
            except (ValueError, TypeError, AttributeError):
                self.send_error(400)
                return
            self.send_response(status)
            self.send_header('Content-Type', media_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class LoopbackServer(HTTPServer):
        def server_bind(self):
            # HTTPServer normally resolves the host name. This fixture only serves the numeric loopback.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address

    if port is None:
        port = PORT if tls is not None and client_namespaces is not None else 0
    with LoopbackServer(('127.0.0.1', port), Handler) as server:
        if tls is not None:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        scheme = 'https' if tls is not None else 'http'
        stub.origin = f'{scheme}://127.0.0.1:{server.server_port}'
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        try:
            yield stub
        finally:
            server.shutdown()
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError('fixture server did not stop')
