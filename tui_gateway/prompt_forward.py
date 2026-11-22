"""Probe and submit on one verified, standalone SSH Desktop socket; never issue a native ticket."""
import json
from pathlib import Path
from uuid import uuid4

from hermes_cli.gateway_client import private_control_query
from hermes_cli.gateway_runtime import control_home_for
from tui_gateway.owner_forward import discover_owner


class ForwardClient:
    def __init__(self, home):
        self.home = Path(home).resolve() if home and Path(home).is_absolute() else None
        self.binding = None
        self.operator_transport = uuid4().hex
        self.operator_target = None
        self.operator_closed = False

    def _target(self, sid, transport):
        from tui_gateway import server
        with server._sessions_lock:
            session = server._sessions.get(sid)
            if not server._session_transport_contains(session, transport):
                raise PermissionError()
            key = session.get('session_key')
            if not isinstance(key, str) or not key:
                raise PermissionError()
            home = Path(session.get('profile_home') or server._launch_home()).resolve()
        # A standalone resume may select another served profile. Only the home
        # pinned on the SSH listener at startup can be used for this private IPC.
        if home != self.home:
            raise PermissionError()
        endpoint = discover_owner(home)
        if Path(endpoint.profile_id).resolve() != home:
            raise PermissionError()
        ref = dict(profile_id=endpoint.profile_id, session_id=key)
        return control_home_for(home, endpoint), endpoint.instance_id, ref

    @staticmethod
    def _call(target, verb, *, _timeout=22, **params):
        home, instance, ref = target
        frame = dict(protocol=1, id=1, verb=verb, params=dict(**ref, instance_id=instance, **params))
        encoded = json.dumps(frame, ensure_ascii=False).encode('utf-8') + b'\n'
        if len(encoded) > 65535:
            raise ValueError('input too large for control socket')
        reply = json.loads(private_control_query(home, encoded, timeout=_timeout))
        if reply.get('ok') is not True or reply.get('id') != 1 or reply.get('protocol') != 1:
            raise PermissionError()
        return reply['result']

    def probe(self, sid, transport):
        self.binding = None
        try:
            target = self._target(sid, transport)
            reply = self._call(target, 'forward-probe')
            if (set(reply) != {'version', 'ref', 'route', 'mode', 'generation'} or reply['version'] != 1
                    or reply['mode'] not in ('shared', 'personal') or reply['ref'] != target[2]
                    or reply['route'] not in {'ordinary', 'forward'}
                    or not isinstance(reply['generation'], str) or not reply['generation']):
                raise PermissionError()
        except Exception:  # No positive routing evidence was obtained.
            return {'version': 1, 'route': 'hold'}
        self.binding = (sid, target, reply)
        return reply

    def permits_legacy(self, sid, transport):
        old = self.binding
        if not old or old[0] != sid or old[2]['route'] != 'ordinary':
            return False
        return self.probe(sid, transport) == old[2] and self.binding == old

    def submit(self, params, transport):
        sid, ref, generation = params['session_id'], params['ref'], params['generation']
        binding = self.binding
        if (not binding or binding[0] != sid or binding[2]['route'] != 'forward'
                or binding[2]['mode'] != 'shared'):
            raise ForwardHeld()
        try:
            target = self._target(sid, transport)
        except Exception as exc:
            raise ForwardHeld() from exc
        if binding[1] != target or ref != target[2] or generation != binding[2]['generation']:
            raise ForwardHeld()
        request_id = params['submission_id']
        reply = self._call(target, 'forward-submit', text=params['text'], generation=generation,
                           submission_id=request_id)
        if reply == {'conflict': True}:
            raise ForwardConflict()
        if (not isinstance(reply, dict) or reply.get('ref') != ref or reply.get('submission_id') != request_id
                or not isinstance(reply.get('admission_id'), str) or not reply['admission_id']
                or reply.get('status') not in {'queued', 'started', 'unknown', 'terminal'}):
            raise ValueError('unconfirmed admission')
        return reply

    def operator(self, verb, params, transport):
        if self.operator_closed:
            raise ForwardHeld()
        try:
            target = self._target(params['session_id'], transport)
        except (OSError, PermissionError, ValueError) as exc:
            raise ForwardHeld() from exc
        if self.operator_target is not None and target != self.operator_target:
            # A different owner/conversation must negotiate on its own attached connection.
            raise ForwardHeld()
        self.operator_target = target
        if self.operator_closed:
            raise ForwardHeld()
        return self._call(target, verb, transport_id=self.operator_transport,
                          **{key: value for key, value in params.items() if key != 'session_id'})

    def close_operator(self):
        self.operator_closed = True
        if self.operator_target is not None:
            self._call(self.operator_target, 'operator-close', _timeout=2, transport_id=self.operator_transport)


class ForwardConflict(ValueError):
    """The owner found different content at the same durable identity."""


class ForwardHeld(ValueError):
    """No input was submitted to the IPC transport."""
