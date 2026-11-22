"""Original command frame of an attached native viewer, scoped to its own handler."""
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar('native_viewer_command', default=None)


@contextmanager
def viewer_command(connection, ref, params):
    live = connection.authority.sessions.get(ref.session_id)
    proof = None
    from gateway.session_server_requests import may_settle
    if (params.get('interaction') == 'displayed-plan' and live is not None and connection.native_owner
            and may_settle(connection.authority, connection.actor, ref.session_id, live.event_stream)):
        proof = (live.source, connection.actor.transport_id, params.get('name'), params.get('arg'))
    token = _current.set(proof)
    try:
        yield
    finally:
        _current.reset(token)


def original_viewer_command(source, name, arg):
    proof = _current.get()
    if proof and proof[0] is source and proof[2:] == (name, arg):
        return proof[1]
    return None
