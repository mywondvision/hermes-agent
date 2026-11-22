"""Exercise the resident connector with loopback TLS and disposable identities."""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
import os
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from plugins.symphony_pm.transport import linux_responses_connector as connector


REQUEST = (b'POST /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\n'
           b'Authorization: Bearer fixture\r\nChatGPT-Account-Id: synthetic\r\n'
           b'Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}')


def _certificate(tmp_path, hostname, label):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(key, hashes.SHA256()))
    bundle, private = tmp_path / (label + '.pem'), tmp_path / (label + '.key')
    bundle.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return bundle, private


@asynccontextmanager
async def _listener(callback, **kwargs):
    async with await asyncio.start_server(callback, '127.0.0.1', 0, **kwargs) as server:
        yield server.sockets[0].getsockname()[1]


@asynccontextmanager
async def _client(context):
    complete = asyncio.Event()

    async def accepted(reader, writer):
        try:
            await connector._serve_request(reader, writer, context)
        finally:
            complete.set()

    async with _listener(accepted, limit=connector.HEAD_LIMIT) as port:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            yield reader, writer
        finally:
            writer.close()
            await asyncio.wait_for(complete.wait(), 5)


@asynccontextmanager
async def _upstream(tmp_path, patch, callback, *, hostname='chatgpt.com', trusted=True):
    cert, private = _certificate(tmp_path, hostname, 'server')
    trust = cert if trusted else _certificate(tmp_path, hostname, 'unrelated')[0]
    patch.setattr(connector, 'CA_FILE', str(trust))
    context = connector._tls_context()
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, private)
    names, calls = [], []
    server.set_servername_callback(lambda sock, name, ctx: names.append(name))

    async with _listener(callback, ssl=server) as local_port:
        async def loopback(host, port, **kwargs):
            calls.append((host, port, kwargs['server_hostname'], kwargs['ssl']))
            return await asyncio.open_connection('127.0.0.1', local_port, **kwargs)
        patch.setattr(connector, 'open_connection', loopback)
        yield context, names, calls


@pytest.mark.parametrize('bad', [
    REQUEST.replace(b'POST ', b'CONNECT '), REQUEST.replace(b'POST ', b'GET '),
    REQUEST.replace(b'/backend-api/codex/responses', b'https://other.test/backend-api/codex/responses'),
    REQUEST.replace(b'responses HTTP', b'responses?proxy=other HTTP'),
    REQUEST.replace(b'chatgpt.com', b'other.test'), REQUEST.replace(b'fixture', b''),
    REQUEST.replace(b'synthetic', b''),
    REQUEST.replace(b'Content-Length: 2', b'Content-Length: 16777217'),
    REQUEST.replace(b'Content-Length:', b'Content-Length: 2\r\nContent-Length:'),
    REQUEST.replace(b'Content-Length:', b'Transfer-Encoding: chunked\r\nContent-Length:'),
    REQUEST.replace(b'Content-Length:', b'Proxy-Authorization: x\r\nContent-Length:'),
    REQUEST.replace(b'Content-Length:', b'X-Forwarded-Host: other.test\r\nContent-Length:'),
    REQUEST.replace(b'Content-Length:', b'Upgrade: websocket\r\nContent-Length:'),
    REQUEST.replace(b'Content-Length:', b'User-Agent: ' + b'x' * connector.HEAD_LIMIT + b'\r\nContent-Length:'),
], ids=['connect', 'get', 'absolute-target', 'query', 'authority', 'empty-token', 'empty-account',
        'body-limit', 'ambiguous-length', 'transfer-encoding', 'proxy-auth', 'destination-header', 'upgrade', 'head-limit'])
def test_invalid_ingress_never_dials(bad, monkeypatch):
    attempts = []

    async def refused(*args, **kwargs):
        attempts.append(args)
        raise AssertionError('unexpected dial')

    async def run():
        monkeypatch.setattr(connector, 'open_connection', refused)
        async with _client(None) as (reader, writer):
            writer.write(bad)
            assert (await asyncio.wait_for(reader.read(), 5)).startswith(b'HTTP/1.1 403 ')
        assert attempts == []
    asyncio.run(run())


def test_verified_tls_preserves_identity_and_sse_streaming(tmp_path, monkeypatch):
    async def run():
        proceed = asyncio.Event()
        requests = []

        async def response(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                requests.append((head, await reader.readexactly(2)))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n'
                             b'Set-Cookie: private\r\n\r\n5\r\nfirst\r\n')
                await writer.drain()
                await proceed.wait()
                writer.write(b'4\r\nlast\r\n0\r\n\r\n')
                await writer.drain()
            finally:
                writer.close()

        async with _upstream(tmp_path, monkeypatch, response) as (context, names, calls):
            async with _client(context) as (reader, writer):
                writer.write(REQUEST)
                head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
                assert b'HTTP/1.1 200 ' in head and b'text/event-stream' in head and b'set-cookie' not in head.lower()
                assert await reader.readexactly(10) == b'5\r\nfirst\r\n'
                proceed.set()
                assert await asyncio.wait_for(reader.read(), 5) == b'4\r\nlast\r\n0\r\n\r\n'
            assert names == ['chatgpt.com'] and calls == [('chatgpt.com', 443, 'chatgpt.com', context)]
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            [(head, body)] = requests
            assert head.startswith(b'POST /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\n')
            assert b'authorization: Bearer fixture\r\n' in head and b'chatgpt-account-id: synthetic\r\n' in head
            assert body == b'{}'
    asyncio.run(run())


@pytest.mark.parametrize(('hostname', 'trusted'), [('other.test', True), ('chatgpt.com', False)])
def test_tls_name_and_trust_required(tmp_path, monkeypatch, hostname, trusted):
    async def run():
        received = []

        async def unexpected(reader, writer):
            received.append(await reader.read(1))
            writer.close()

        async with _upstream(tmp_path, monkeypatch, unexpected, hostname=hostname, trusted=trusted) as (ctx, _, calls):
            async with _client(ctx) as (reader, writer):
                writer.write(REQUEST)
                assert (await asyncio.wait_for(reader.read(), 5)).startswith(b'HTTP/1.1 502 ')
            assert len(calls) == 1 and received == []
    asyncio.run(run())


@pytest.mark.parametrize('reply', [
    b'HTTP/1.1 307 Go\r\nLocation: https://other.test/\r\n',
    b'HTTP/1.1 101 Switch\r\nUpgrade: websocket\r\n',
    b'HTTP/1.1 200 OK\r\nContent-Length: 67108865\r\n',
    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n',
])
def test_bad_upstream_never_redirects_or_upgrades(tmp_path, monkeypatch, reply):
    async def run():
        async def response(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            await reader.readexactly(2)
            writer.write(reply + b'\r\n')
            await writer.drain()
            writer.close()

        async with _upstream(tmp_path, monkeypatch, response) as (ctx, _, calls):
            async with _client(ctx) as (reader, writer):
                writer.write(REQUEST)
                result = await asyncio.wait_for(reader.read(), 5)
                assert result.startswith(b'HTTP/1.1 502 ') and b'other.test' not in result
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('response_started', [False, True])
def test_disconnect_closes_stalled_tls_exchange(tmp_path, monkeypatch, response_started):
    async def run():
        received, gone = asyncio.Event(), asyncio.Event()

        async def stalled(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                await reader.readexactly(2)
                if response_started:
                    writer.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n')
                    await writer.drain()
                received.set()
                with suppress(ConnectionError):
                    assert await reader.read(1) == b''
                gone.set()
            finally:
                writer.close()

        async with _upstream(tmp_path, monkeypatch, stalled) as (ctx, _, _):
            async with _client(ctx) as (reader, writer):
                writer.write(REQUEST)
                await asyncio.wait_for(received.wait(), 5)
                if response_started:
                    await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
                writer.close()
                await asyncio.wait_for(gone.wait(), 5)
    asyncio.run(run())


def test_response_size_overrun_is_incomplete(tmp_path, monkeypatch):
    async def run():
        async def overrun(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            await reader.readexactly(2)
            writer.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                         b'3\r\none\r\n3\r\ntwo\r\n0\r\n\r\n')
            await writer.drain()
            writer.close()

        monkeypatch.setattr(connector, 'MAX_RESPONSE', 3)
        async with _upstream(tmp_path, monkeypatch, overrun) as (ctx, _, _):
            async with _client(ctx) as (reader, writer):
                writer.write(REQUEST)
                assert (await reader.readuntil(b'\r\n\r\n')).startswith(b'HTTP/1.1 200 ')
                assert await asyncio.wait_for(reader.read(), 5) == b'3\r\none\r\n'
    asyncio.run(run())


def test_absolute_deadline_revokes_idle_request(tmp_path, monkeypatch):
    async def run():
        seen = asyncio.Event()
        gone = asyncio.Event()

        async def pending(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                await reader.readexactly(2)
                seen.set()
                with suppress(ConnectionError):
                    assert await reader.read(1) == b''
                gone.set()
            finally:
                writer.close()

        monkeypatch.setattr(connector, 'REQUEST_SECONDS', 2)
        async with _upstream(tmp_path, monkeypatch, pending) as (ctx, _, _):
            async with _client(ctx) as (reader, writer):
                writer.write(REQUEST)
                await asyncio.wait_for(seen.wait(), 5)
                assert await asyncio.wait_for(reader.read(), 5) == b''
                await asyncio.wait_for(gone.wait(), 5)
    asyncio.run(run())


@pytest.mark.platforms('linux')
def test_unix_peer_outside_payload_refused():
    from os import getegid, geteuid

    left, right = socket.socketpair(socket.AF_UNIX)
    with left, right:
        assert not connector._authorized(left, geteuid() + 1, getegid())
        assert not connector._authorized(left, geteuid(), getegid())


@pytest.mark.platforms('linux')
def test_kernel_peer_and_system_slice_required(monkeypatch, tmp_path):
    from os import getegid, geteuid

    member = tmp_path / 'cgroup'
    unit = b'hermes-symphony@' + b'a' * 64 + b'.service/payload\n'

    def peer_membership(path):
        assert path == f'/proc/{os.getpid()}/cgroup'
        return member

    monkeypatch.setattr(connector, 'Path', peer_membership)
    left, right = socket.socketpair(socket.AF_UNIX)
    with left, right:
        member.write_bytes(b'0::/system.slice/system-hermes\\x2dsymphony.slice/' + unit)
        assert connector._authorized(left, geteuid(), getegid())
        member.write_bytes(b'0::/system.slice/unrelated.service/fake.slice/' + unit)
        assert not connector._authorized(left, geteuid(), getegid())
