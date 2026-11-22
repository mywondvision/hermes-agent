"""Profile-owned, purpose-limited sockets on the existing session listener."""
import asyncio
import json
from contextlib import suppress
from threading import RLock

from starlette.websockets import WebSocket, WebSocketDisconnect


PROTOCOL = 'hermes.symphony-plan.v1'
_providers = set()
_lock = RLock()


def register(provider, scope):
    with _lock:
        _providers.add(provider)

    def remove(_deadline):
        with _lock:
            _providers.discard(provider)

    scope.on_close(remove)


def forward_provider(authority):
    """Select exactly one serving profile's ordinary-input routing provider."""
    with _lock:
        providers = tuple(_providers)
    from hermes_cli.plugin_request_binding import canonical_home
    matches = [provider for provider in providers
               if getattr(provider, 'forward_profile', None) == canonical_home(authority.profile_id)]
    if len(matches) != 1:
        raise PermissionError('forward registration unavailable')
    return matches[0]


async def serve(scope, receive, send, runner, descriptor):
    ws = WebSocket(scope, receive, send)
    if (scope.get('subprotocols') != [PROTOCOL] or ws.headers.get('origin')
            or not ws.client or ws.client.host not in {'127.0.0.1', '::1'}
            or not descriptor or descriptor['state'] != 'ready' or runner._draining):
        await ws.close(code=4403)
        return
    await ws.accept(subprotocol=PROTOCOL)
    connection = None
    inbox = asyncio.Queue(maxsize=16)
    disconnected = False

    async def receive_frames():
        nonlocal disconnected
        try:
            while True:
                raw = await ws.receive_text()
                if len(raw) > 65536 or runner._draining or descriptor['state'] != 'ready' or inbox.full():
                    await ws.close(code=4403)
                    return
                inbox.put_nowait(raw)
        except WebSocketDisconnect:
            return
        finally:
            disconnected = True
            if connection is not None:
                connection.close()
            # A closed socket cannot authorize a queued request. Wake an idle dispatcher.
            while not inbox.empty():
                inbox.get_nowait()
            inbox.put_nowait(None)

    reader = asyncio.create_task(receive_frames())
    try:
        while True:
            raw = await inbox.get()
            if raw is None or disconnected:
                return
            rid = None
            try:
                frame = json.loads(raw)
                if (type(frame) is not dict or set(frame) != {'jsonrpc', 'id', 'method', 'params'}
                        or frame['jsonrpc'] != '2.0' or type(frame['id']) not in (int, str)
                        or type(frame['params']) is not dict):
                    raise ValueError('invalid frame')
                rid, method, params = frame['id'], frame['method'], frame['params']
                if connection is None:
                    if method != 'route.challenge':
                        raise PermissionError()
                    with _lock:
                        providers = tuple(_providers)
                    candidates = [p for p in providers if p.matches(runner, params)]
                    if len(candidates) != 1:
                        raise PermissionError()
                    connection = candidates[0].connect(runner, ws.send_json)
                result = await connection.dispatch(method, params)
                response = {'result': result}
            except (ValueError, PermissionError):
                response = {'error': {'code': -32003, 'message': 'PLAN_ROUTE_REFUSED'}}
            if not disconnected:
                await ws.send_json({'jsonrpc': '2.0', 'id': rid, **response})
    except WebSocketDisconnect:
        pass
    finally:
        if connection is not None:
            connection.close()
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader
