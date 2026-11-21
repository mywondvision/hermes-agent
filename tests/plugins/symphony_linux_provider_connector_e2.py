"""Expose the packaged connector only to the fake TLS peer inside one qualification payload."""
import asyncio
from contextlib import closing, contextmanager
import http.client
import socket
import ssl
from unittest.mock import patch
from urllib.parse import urlsplit

from plugins.symphony_pm.transport import linux_responses_connector as connector


@contextmanager
def fake_connector(origin, public_ca, observed_scope):
    endpoint = urlsplit(origin)
    if (endpoint.scheme != 'https' or endpoint.hostname != '127.0.0.1' or endpoint.port is None
            or origin != f'https://127.0.0.1:{endpoint.port}'):
        raise ValueError('expected a numeric loopback fake upstream')
    with patch.object(connector, 'CA_FILE', str(public_ca)):
        context = connector._tls_context()
    calls = []

    async def dial(host, port, *, ssl, server_hostname, **kwargs):
        if (host, port, server_hostname) != (connector.HOST, 443, connector.HOST) or ssl is not context:
            raise ValueError('unexpected TLS destination')
        # Supply a numeric, already-connected socket so no resolver can run. The actual
        # connector still requires the synthetic certificate to validate for chatgpt.com.
        observed = observed_scope()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as local:
            local.settimeout(3)
            local.connect(('127.0.0.1', endpoint.port))
            assert local.getpeername() == ('127.0.0.1', endpoint.port)
            local.setblocking(False)
            reader, writer = await asyncio.open_connection(sock=local.dup(), ssl=ssl,
                                                          server_hostname=server_hostname, **kwargs)
        secure = writer.get_extra_info('ssl_object')
        assert secure is not None and secure.version() in ('TLSv1.2', 'TLSv1.3')
        calls.append({**observed, 'peer': ['127.0.0.1', endpoint.port], 'tls': secure.version()})
        return reader, writer

    def send(channel, body, headers):
        names = ('authorization', 'chatgpt-account-id', 'content-type')
        forwarded = {key: headers[key] for key in names}
        if (forwarded['authorization'], forwarded['chatgpt-account-id']) != ('Bearer fixture-token', 'fixture-account'):
            raise ValueError('expected synthetic identity')
        forwarded['Host'] = connector.HOST
        with closing(http.client.HTTPConnection(connector.HOST, timeout=8)) as client:
            client.auto_open = False
            client.sock = channel
            client.request('POST', connector.PATH.decode('ascii'), body, forwarded)
            response = client.getresponse()
            data = response.read((1 << 20) + 1)
            if len(data) > 1 << 20:
                raise ValueError('unexpected fake response size')
            return response.status, response.getheader('Content-Type', ''), data

    async def transfer(body, headers):
        server, client = socket.socketpair()
        with server, client:
            server.setblocking(False)
            client.settimeout(8)
            reader, writer = await asyncio.open_connection(sock=server.dup(), limit=connector.HEAD_LIMIT)
            async with asyncio.timeout(12):
                serving = asyncio.create_task(connector._serve_request(reader, writer, context))
                try:
                    return await asyncio.to_thread(send, client, body, headers)
                finally:
                    await serving
                    await writer.wait_closed()

    def forward(body, headers):
        return asyncio.run(transfer(body, headers))

    with patch.object(connector, 'open_connection', dial):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        yield forward, calls
