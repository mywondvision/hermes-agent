"""Temporary two-channel peer for the ordinary SSH conversation acceptance run."""
import asyncio
from contextlib import closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlencode
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session_ingress import viewer_admission_id, viewer_turn
from hermes_cli.plugin_request_binding import canonical_home
from hermes_cli import web_server
from hermes_state_registry import close_all_under
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_session_authorities_multiplex import _runner
from tests.plugins.symphony_plan_peer import Peer, Socket, run
from tests.plugins.test_symphony_pm_adapter import submit_payload
from tests.plugins.test_symphony_pm_serving import _call


async def initialize_task_gateway(runner):
    from gateway import session_authority

    initialize = session_authority.initialize_session_authority

    async def initialize_profile(runner, *, profile_id, **kwargs):
        # The emulated Linux peer uses one exact path spelling in both signed channels.
        return await initialize(runner, profile_id=canonical_home(profile_id), **kwargs)

    with patch.object(session_authority, 'initialize_session_authority', initialize_profile):
        await initialize_gateway_runtime(runner)


class TaskPeer(Peer):
    async def ordinary(self, emit):
        minted = await asyncio.to_thread(self.client.post, '/api/webapp/socket-ticket',
            params={'socket': 'gateway', 'profile': 'default'}, headers={'X-Hermes-Session-Token': self.token})
        minted.raise_for_status()
        query = urlencode(dict(native_dial='browser', ticket=minted.json()['ticket'], profile='default')).encode()
        socket = Socket(self.runner, emit)
        await socket.open(app=web_server.app, query=query)
        return socket

    async def init_task(self, cmd):
        manifest = super().start(cmd['public_key'], cmd['host_fingerprint'])
        home = Path(manifest['profile'])
        patch = self.service.enter_context(pytest.MonkeyPatch.context())
        import hermes_state
        patch.setenv('HERMES_HOME', str(home))
        patch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
        self.service.callback(close_all_under, home)
        self.runner = _runner(home, [('default', home)])
        process_ownership.reserve([home])
        self.service.callback(process_ownership.release, home)
        self.service.callback(self.runner.close_all_session_db_handles)
        self.service.callback(self.runner.session_store.close_all_db_handles)
        await initialize_task_gateway(self.runner)
        self.authority = self.runner.session_authority
        self.runner._running, self.runner._gateway_loop = True, asyncio.get_running_loop()
        self.runner.session_runtime_descriptor['state'] = 'ready'
        patch.setattr('gateway.run._gateway_runner_ref', lambda: self.runner)
        self.token = secrets.token_urlsafe(32)
        for field, value in dict(session_authority=self.authority, ui_surface='webapp', auth_required=False,
                                 bound_host='127.0.0.1').items():
            patch.setattr(web_server.app.state, field, value, raising=False)
        patch.setattr(web_server, '_SESSION_TOKEN', self.token)
        patch.setattr(web_server, '_DASHBOARD_EMBEDDED_CHAT_ENABLED', True)
        self.client = TestClient(web_server.app, base_url='http://127.0.0.1')
        self.service.callback(self.client.close)
        replies = asyncio.Queue()
        creator = await self.ordinary(replies.put_nowait)
        try:
            await creator.frame(dict(jsonrpc='2.0', id='create', method='session.create', params=dict(
                source='gui', request_id=self.desktop_request_id, model='fixture', toolsets=[])))
            while True:
                frame = await asyncio.wait_for(replies.get(), timeout=15)
                if frame.get('id') == 'create':
                    assert 'result' in frame, frame
                    chat = frame['result']['session_id']
                    break
        finally:
            await creator.close()
        live = self.authority.sessions[chat]
        channel = self.channel
        channel.runner, channel.source = self.runner, live.source
        channel.entry = self.runner.session_store.get_or_create_session(live.source)
        self.reg = replace(self.reg, conversation_id=chat, operator_id=live.source.user_id)
        path = home / 'config.yaml'
        config = json.loads(path.read_text('utf-8'))
        settings = config['plugins']['entries']['symphony_pm']['settings']
        settings['operator'] = dict(platform='local', user_id=live.source.user_id, chat_id=chat,
                                    session_key=live.route)
        settings['desktop_plan_route'] = asdict(self.reg)
        path.write_text(json.dumps(config), encoding='utf-8')
        self.requests = []

        async def model(message, context_prompt, history, source, session_id, **kwargs):
            admission = viewer_admission_id.get()
            assert admission and viewer_turn.get() is live and session_id == channel.entry.session_id
            from plugins.symphony_pm.operator_commands import viewer_job_controls_held
            assert viewer_job_controls_held(channel.owner.scope)
            request_id = 'task-' + admission
            payload = submit_payload(request_id=request_id, goal=message, project_ref=channel.project_ref,
                                     raw_constraints=['Only the offline fixture launcher may run.'])
            response = _call(payload, session_id=session_id)
            self.requests.append(dict(admission_id=admission, request_id=request_id, outcome=response['outcome']))
            return dict(final_response=response['outcome'], messages=[], completed=True)

        patch.setattr(self.runner, '_run_agent', model)
        return dict(manifest, profile_id=self.authority.profile_id, operator_id=live.source.user_id,
                    conversation_id=chat, session_id=channel.entry.session_id)

    async def control(self, cmd):
        op = cmd['op']
        if op == 'init' and self.channel is None:
            return await self.init_task(cmd)
        if op == 'open' and cmd.get('channel') == 'ordinary':
            key = cmd['socket']
            assert key not in self.sockets
            self.sockets[key] = await self.ordinary(lambda frame: self.emit(dict(socket=key, frame=frame)))
            return True
        if op == 'counts':
            counters = await asyncio.to_thread(self.inspect)
            rows = list_session_admissions(self.authority.db, session_id=self.reg.conversation_id, pending_only=False)
            counters['admissions'] = [{k: row[k] for k in ('request_id', 'admission_id', 'status')} for row in rows]
            counters['requests'] = list(self.requests)
            return counters
        if op not in {'open', 'frame', 'close'}:
            raise ValueError('unknown task fixture operation')
        return await super().control(cmd)

    def inspect(self, *, wait_for_completion=True):
        counters = super().inspect(wait_for_completion=wait_for_completion)
        journal = self.channel.owner.journal
        audit = journal.path.with_name('typed-plan-audit.sqlite3')
        counters['plan_inputs'] = []
        with journal._transaction_lock:
            if audit.exists():
                with closing(sqlite3.connect(audit.as_uri() + '?mode=ro', uri=True)) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='voice_inputs'").fetchone():
                        counters['plan_inputs'] = [
                            dict(admission_id=admission, request_id=request, plan_digest=digest)
                            for admission, request, digest in db.execute(
                                'SELECT message_id,request_id,plan_digest FROM voice_inputs WHERE session_id=?',
                                (self.channel.entry.session_id,))]
            with closing(sqlite3.connect(journal.path.as_uri() + '?mode=ro', uri=True)) as db:
                counters['cancel_causes'], = db.execute('SELECT COUNT(*) FROM serving_cancel_causes').fetchone()
        counters['executor_cancels'] = sum(receiver.cancels for receiver in self.native.created)
        counters['attempts'] = [
            {field: getattr(receiver.binding.ticket, field) for field in ('project', 'job', 'revision', 'attempt')}
            for receiver in self.native.created]
        return counters


if __name__ == '__main__':
    asyncio.run(run(TaskPeer))
