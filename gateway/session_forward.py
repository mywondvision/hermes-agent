"""Narrow same-user IPC from isolated Desktop to the owner of a registered local conversation."""
import asyncio
from dataclasses import asdict

from gateway.owner_identity import host_owner_subject
from gateway.session_authorities import all_authorities, owner_scope
from gateway.session_contract import Principal, SessionRef, Submission
from gateway.session_desktop_operator_store import routing_index, routing_session_id
from gateway.session_scoped_ws import forward_provider
from hermes_state_runtime import RuntimeStoreError


def _binding(authority, ref, actor, *, routing_lookup=None):
    live = authority.sessions.get(ref.session_id)
    if not live or not live.source or live.source.platform.value != 'local':
        raise PermissionError('unregistered local session')
    authority.authorize(actor, ref, 'session:submit')
    provider = forward_provider(authority)
    if routing_lookup is None:
        kind, generation, mode = provider.forward_binding(authority, ref.session_id)
    else:
        kind, generation, mode = provider.forward_binding(authority, ref.session_id, routing_lookup=routing_lookup)
    return dict(version=1, route=kind, generation=generation, ref=asdict(ref),
                mode='personal' if mode == 'personal' else 'shared')


async def accept_forward(runner, params, peer, *, submit):
    keys = {'profile_id', 'session_id', 'instance_id'}
    if submit:
        keys.update({'generation', 'text', 'submission_id'})
    if (set(params) != keys or not all(type(v) is str and v.strip() for v in params.values())
            or peer != host_owner_subject() or getattr(runner, '_draining', False)):
        raise PermissionError('invalid forward request')
    if submit and params['submission_id'].startswith('hosted:'):
        raise PermissionError('reserved submission identity')
    selected = [a for a in all_authorities(runner) if a.profile_id == params['profile_id']
                and a.instance_id == params['instance_id']]
    if len(selected) != 1:
        raise PermissionError('owner changed')
    authority, = selected
    ref = SessionRef(authority.profile_id, params['session_id'])
    actor = Principal(peer, ref.profile_id, frozenset({'session:submit'}), 'shared-token-forward')
    with owner_scope(authority):
        binding = _binding(authority, ref, actor)
        if not submit:
            return binding
        if (binding['route'] != 'forward' or binding['mode'] != 'shared'
                or binding['generation'] != params['generation']):
            raise PermissionError('binding changed')

        routing_scope, separate_db = await routing_index(authority)

        def verify(conn=None):
            def lookup(route):
                return routing_session_id(conn, routing_scope, route, separate_db=separate_db)

            if _binding(authority, ref, actor, routing_lookup=lookup if conn is not None else None) != binding:
                raise PermissionError('binding changed')

        request = Submission(params['submission_id'], ref, {'text': params['text']}, 'queue')
        try:
            receipt = await authority.submit(actor, request, _authorize_write=verify,
                                             _replay_guard=verify, _shared_forward=True)
        except RuntimeStoreError as exc:
            if exc.reason == 'admission_conflict':
                return {'conflict': True}
            raise
        verify()
        return dict(ref=asdict(receipt.ref), submission_id=request.request_id,
                    admission_id=receipt.admission_id, status=receipt.status)


def install_forward_verbs(server, runner, loop):
    # Private control handlers run off-loop, after the OS peer identity check.
    def handler(submit):
        def call(params, peer):
            task = accept_forward(runner, params, peer, submit=submit)
            return asyncio.run_coroutine_threadsafe(task, loop).result(timeout=20)
        return call

    server.private_handlers.update({'forward-probe': handler(False), 'forward-submit': handler(True)})
    from gateway.session_desktop_operator import install_operator_verbs
    install_operator_verbs(server, runner, loop)
