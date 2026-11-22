"""Resident, fixed-destination Linux connector for the trusted Symphony Responses relay.

Installed as a standalone script with a pinned isolated interpreter. Everything on
the request path stays in this process. There is no URL, proxy or trust-store option.
"""
import asyncio
from asyncio import open_connection
from contextlib import closing
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import struct


HOST = 'chatgpt.com'
PATH = b'/backend-api/codex/responses'
SOCKET = Path('/run/hermes-symphony-connector/responses.sock')
CA_FILE = '/opt/hermes-symphony/connector/etc/ssl/certs/ca-certificates.crt'
HEAD_LIMIT = 32 << 10
MAX_BODY = 16 << 20
MAX_RESPONSE = 64 << 20
REQUEST_SECONDS = 300
IDLE_SECONDS = 10
MAX_ACTIVE = 4
ALLOWED = frozenset(b'host authorization chatgpt-account-id content-type content-length content-encoding '
                    b'accept accept-encoding openai-beta originator version user-agent session-id '
                    b'x-client-request-id x-codex-turn-metadata x-codex-installation-id'.split())


class InvalidMessage(ValueError):
    """Never include request or response data in an exception."""


async def _head(reader):
    data = await reader.readuntil(b'\r\n\r\n')
    if len(data) > HEAD_LIMIT:
        raise InvalidMessage
    first, *fields = data[:-4].split(b'\r\n')
    headers = {}
    for field in fields:
        key, sep, value = field.partition(b':')
        if not sep or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise InvalidMessage
        if not re.fullmatch(rb'[\x20-\x7e]*', value):
            raise InvalidMessage
        headers.setdefault(key.lower(), []).append(value.strip(b' '))
    return first, headers


async def _request(reader):
    first, fields = await _head(reader)
    if (first != b'POST ' + PATH + b' HTTP/1.1' or not fields.keys() <= ALLOWED
            or any(len(values) != 1 for values in fields.values())):
        raise InvalidMessage
    headers = {k: v[0] for k, v in fields.items()}
    length = headers.get(b'content-length', b'')
    if (headers.get(b'host') != HOST.encode() or headers.get(b'accept-encoding', b'identity') != b'identity'
            or not re.fullmatch(rb'Bearer [!-~]+', headers.get(b'authorization', b''))
            or not re.fullmatch(rb'[!-~]+', headers.get(b'chatgpt-account-id', b''))
            or headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower() != b'application/json'
            or not re.fullmatch(rb'[1-9][0-9]{0,7}', length) or int(length) > MAX_BODY):
        raise InvalidMessage
    # Generate the request line and Host ourselves; the exact original body can
    # be compressed by the client and must be forwarded byte for byte.
    head = [b'POST ' + PATH + b' HTTP/1.1', b'Host: chatgpt.com', b'Connection: close']
    head.extend(k + b': ' + v for k, v in headers.items() if k != b'host')
    return b'\r\n'.join(head) + b'\r\n\r\n', await reader.readexactly(int(length))


def _tls_context():
    # Explicit trust file and SSLContext avoid proxy, trust and TLS-key logging
    # environment variables. PROTOCOL_TLS_CLIENT enables required chain + hostname verification.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=CA_FILE)
    return context


async def _write(writer, data):
    writer.write(data)
    await asyncio.wait_for(writer.drain(), IDLE_SECONDS)


async def _response_head(reader):
    first, fields = await _head(reader)
    match = re.fullmatch(rb'HTTP/1\.[01] ([245][0-9]{2})(?: [ -~]*)?', first)
    if not match:
        raise InvalidMessage
    for name in (b'content-type', b'content-encoding', b'content-length', b'transfer-encoding'):
        if len(fields.get(name, ())) > 1:
            raise InvalidMessage
    length = fields.get(b'content-length', [None])[0]
    transfer = fields.get(b'transfer-encoding', [None])[0]
    if length is not None and (not re.fullmatch(rb'[0-9]{1,8}', length) or int(length) > MAX_RESPONSE or transfer):
        raise InvalidMessage
    if transfer is not None and transfer.lower() != b'chunked':
        raise InvalidMessage
    code = match[1]
    head = [b'HTTP/1.1 ' + code + b' Response', b'Connection: close']
    for key in (b'content-type', b'content-encoding'):
        if key in fields:
            head.append(key + b': ' + fields[key][0])
    if code == b'204':
        if transfer is not None or length is not None:
            raise InvalidMessage
        return b'\r\n'.join(head) + b'\r\n\r\n', 0, False
    head.append(b'Transfer-Encoding: chunked')
    return b'\r\n'.join(head) + b'\r\n\r\n', int(length) if length is not None else None, bool(transfer)


async def _chunk_size(reader):
    line = await asyncio.wait_for(reader.readuntil(b'\r\n'), IDLE_SECONDS)
    if len(line) > 1024 or not re.fullmatch(rb'[0-9A-Fa-f]{1,8}(?:;[ -~]*)?\r\n', line):
        raise InvalidMessage
    return int(line[:-2].split(b';', 1)[0], 16)


async def _stream_body(reader, client, length, chunked):
    total = 0
    while True:
        part_size = await _chunk_size(reader) if chunked else length
        remaining = part_size
        if remaining is not None and remaining > MAX_RESPONSE - total:
            raise InvalidMessage
        while remaining is None or remaining:
            part = await asyncio.wait_for(reader.read(min(65536, remaining or 65536)), IDLE_SECONDS)
            if not part:
                if remaining:
                    raise InvalidMessage
                break
            total += len(part)
            if total > MAX_RESPONSE:
                raise InvalidMessage
            await _write(client, b'%x\r\n' % len(part) + part + b'\r\n')
            if remaining is not None:
                remaining -= len(part)
        if not chunked:
            break
        # Extensions are harmless; trailers are outside this connector's contract.
        if await asyncio.wait_for(reader.readexactly(2), IDLE_SECONDS) != b'\r\n':
            raise InvalidMessage
        if not part_size:
            break
    await _write(client, b'0\r\n\r\n')


async def _exchange(request, client, context):
    try:
        reader, upstream = await asyncio.wait_for(
            open_connection(HOST, 443, ssl=context, server_hostname=HOST, limit=HEAD_LIMIT), IDLE_SECONDS)
    except (OSError, TimeoutError):
        await _write(client, b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
        return
    try:
        for block in request:
            await _write(upstream, block)
        try:
            head, length, chunked = await asyncio.wait_for(_response_head(reader), IDLE_SECONDS)
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            await _write(client, b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            return
        await _write(client, head)
        if not head.startswith(b'HTTP/1.1 204 '):
            await _stream_body(reader, client, length, chunked)
    finally:
        # An idle upstream must see transport shutdown even before the first
        # response header or SSE event, and even if TLS shutdown would stall.
        upstream.transport.abort()


async def _serve_request(reader, client, context):
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            try:
                request = await asyncio.wait_for(_request(reader), IDLE_SECONDS)
            except (InvalidMessage, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                await _write(client, b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return
            # The relay sends exactly one complete request, then reads until the
            # response. EOF or additional bytes revoke this exchange immediately.
            gone = asyncio.create_task(reader.read(1))
            transfer = asyncio.create_task(_exchange(request, client, context))
            try:
                await asyncio.wait((gone, transfer), return_when=asyncio.FIRST_COMPLETED)
            finally:
                gone.cancel()
                transfer.cancel()
                await asyncio.gather(gone, transfer, return_exceptions=True)
    except (OSError, TimeoutError):
        pass
    finally:
        client.close()


def _authorized(peer, uid, gid):
    pid, peer_uid, peer_gid = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if peer_uid != uid or peer_gid != gid or pid <= 1:
        return False
    try:
        membership = Path(f'/proc/{pid}/cgroup').read_bytes()
    except OSError:
        return False
    # Only system-manager slices may precede the attempt. An account's own
    # delegated service can freely create subdirectories named like this unit.
    return bool(re.fullmatch(rb'0::/system\.slice/(?:[\w.\\-]+\.slice/)*'
                             rb'hermes-symphony@[0-9a-f]{64}\.service/payload\n', membership))


async def _serve():
    import grp
    from os import getegid, geteuid
    import pwd

    uid = pwd.getpwnam('hermes-symphony').pw_uid
    gid = grp.getgrnam('hermes-symphony').gr_gid
    directory = SOCKET.parent
    info = directory.lstat()
    if (uid == 0 or gid == 0 or geteuid() != 0 or getegid() != gid or directory.resolve() != directory
            or (info.st_uid, info.st_gid, info.st_mode) != (0, gid, stat.S_IFDIR | 0o750)):
        raise RuntimeError('Invalid connector identity or directory')
    context = _tls_context()
    active = 0

    async def accept(reader, writer):
        nonlocal active
        if active >= MAX_ACTIVE or not _authorized(writer.get_extra_info('socket'), uid, gid):
            writer.close()
            return
        active += 1
        try:
            await _serve_request(reader, writer, context)
        finally:
            active -= 1

    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as listener:
        listener.bind(str(SOCKET))
        os.chmod(SOCKET, 0o660)
        listener.listen(MAX_ACTIVE)
        listener.setblocking(False)
        server = await asyncio.start_unix_server(accept, sock=listener, limit=HEAD_LIMIT)
        async with server:
            await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(_serve())
