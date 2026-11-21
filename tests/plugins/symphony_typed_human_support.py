"""Local typed-approval stand. A synthetic direct-message adapter is its only output channel."""
import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli import plugins
from hermes_cli.approval_transport import RegisteredApprovalTransport
from hermes_cli.plugin_services import ServiceHost
from hermes_cli.plugins_loader import _plugin_home_scope
from plugins.symphony_pm.contracts import ServingBootstrap
from plugins.symphony_pm.transport.bootstrap import TransportBootstrap
from tests.gateway.test_plugin_command_choice import _TextAdapter, _runner
from tests.plugins.symphony_serving_support import Guard, Native, Observer
from tests.plugins.test_symphony_pm_adapter import submit_payload
from tests.plugins.test_symphony_pm_codex_exec import _git
from tests.plugins.test_symphony_pm_serving import _call


class LocalChat(_TextAdapter):
    def __init__(self):
        super().__init__()
        self.platform = Platform.DISCORD
        self.chat = 'fixture-owner-room'

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if chat_id != self.chat:
            return SendResult(success=False, error='fixture chat does not exist')
        self.sent.append(content)
        return SendResult(success=True, message_id=str(len(self.sent)))


class FixtureLauncher:
    def __init__(self, repo, workspace):
        self.repo, self.workspace = repo, workspace
        self.calls = []
        self.called = threading.Event()

    def typed_plan(self):
        return dict(repository=str(self.repo), workspace=str(self.workspace), model='fixture-no-model',
                    effort='fixture-only', environment_ref='fixture-environment', checks=['fixture-check'],
                    configured_model='fixture-no-model', configured_effort='fixture-only', exec_timeout=30,
                    executor_sha256='a'*64, config_sha256='b'*64)

    def __call__(self, ticket, request, run):
        self.calls.append((ticket.attempt, request.request_id))
        self.called.set()


class QualificationSession:
    def __init__(self, runner, adapter, source, entry, loop, launcher, owner):
        self.runner, self.adapter, self.source, self.entry = runner, adapter, source, entry
        self.loop, self.launcher, self.owner = loop, launcher, owner
        self.number = 0

    def _submit(self, action):
        req = submit_payload(request_id='qualification-'+action, goal='Record the '+action+' fixture decision.',
                             project_ref='fixture', raw_constraints=['Only the in-process fixture launcher may run.'])
        tokens = set_session_vars(platform='discord', chat_id=self.source.chat_id, user_id=self.source.user_id,
                                  chat_type='dm', session_key=self.entry.session_key, session_id=self.entry.session_id)
        try:
            return _call(req, session_id=self.entry.session_id)['outcome']
        finally:
            clear_session_vars(tokens)

    def request(self, action):
        before = len(self.adapter.sent)
        status = self._submit(action)
        return status, '\n'.join(self.adapter.sent[before:])

    def deliver(self, text):
        self.number += 1
        message_id = str(1000 + self.number)
        raw = SimpleNamespace(id=message_id, content=text, author=SimpleNamespace(id=self.source.user_id, bot=False),
                              channel=SimpleNamespace(id=self.source.chat_id), webhook_id=None, attachments=[],
                              message_snapshots=[], edited_at=None)
        event = MessageEvent(text=text, source=self.source, message_id=message_id, raw_message=raw)
        return asyncio.run_coroutine_threadsafe(self.runner._handle_message(event), self.loop).result(20)

    def retry(self, action):
        outcome = self._submit(action)
        if outcome == 'SUBMITTED' and not self.launcher.called.wait(5):
            raise RuntimeError('FIXTURE_LAUNCHER_NOT_OBSERVED')
        return outcome

    def counts(self):
        with sqlite3.connect(self.owner.journal.path.as_uri()+'?mode=ro', uri=True) as db:
            permits, = db.execute('SELECT COUNT(*) FROM serving_permits').fetchone()
            reserved, = db.execute('SELECT COUNT(*) FROM serving_launches').fetchone()
        return {'permits': permits, 'reservations': reserved, 'launcher_calls': len(self.launcher.calls)}


@contextmanager
def human_session(root, *, restart=False):
    """Each call gets its own config, Git repo, journal and plugin manager, with fake OS ports."""
    home, repo = root/'profile', root/'project'
    home.mkdir(exist_ok=True)
    if restart:
        if not (repo/'.git').is_dir():
            raise RuntimeError('FIXTURE_BASELINE_MISSING')
    else:
        repo.mkdir()
        _git('init', '-q', '-b', 'fixture-base', cwd=repo)
        _git('commit', '--allow-empty', '-m', 'Fixture baseline', cwd=repo)
    adapter = LocalChat()
    runner = _runner(adapter)
    runner.config.multiplex_profiles = False
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, user_id='fixture-human', chat_type='dm', chat_id=adapter.chat)
    key = runner._session_key_for_source(source)
    entry = SessionEntry(session_key=key, session_id='fixture-owner-session', created_at=datetime.now(),
                         updated_at=datetime.now(), origin=source, platform=source.platform, chat_type='dm')
    runner.session_store.lookup_by_session_key.side_effect = lambda route: entry if route == key else None
    operator = dict(platform='discord', user_id=source.user_id, chat_id=source.chat_id, session_key=key)
    typed = dict(project_ref='fixture', repository=str(repo), branch='fixture-base', ttl_seconds=900)
    (home/'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['symphony_pm'], 'entries': {
        'symphony_pm': {'settings': {'operator': operator, 'typed_approval': typed}}}}}), encoding='utf-8')
    loop = asyncio.new_event_loop()
    runner._running, runner._gateway_loop = True, loop
    thread = threading.Thread(target=loop.run_forever, name='fixture-only-gateway-loop', daemon=True)
    thread.start()
    host = ServiceHost()
    with ExitStack() as stack:
        stack.callback(loop.close)
        stack.callback(thread.join, 5)
        stack.callback(loop.call_soon_threadsafe, loop.stop)
        stack.enter_context(_plugin_home_scope(home))
        stack.enter_context(patch('gateway.run._gateway_runner_ref', return_value=runner))
        stack.enter_context(patch.object(runner, '_served_profile_for_home',
            side_effect=lambda target: 'fixture' if Path(target).resolve() == home.resolve() else None))
        plugins._reset_plugin_managers_for_tests()
        stack.callback(plugins._reset_plugin_managers_for_tests)
        plugins.discover_plugins()
        plugin = plugins.get_plugin_manager()._plugins['symphony_pm']
        if not plugin.enabled or plugin.error:
            raise RuntimeError('FIXTURE_PLUGIN_DISCOVERY')
        def unused(*args):
            raise RuntimeError('LEGACY_APPROVAL_ROUTE_USED')
        channel = RegisteredApprovalTransport('fixture', unused, 'test', str(home))
        launcher = FixtureLauncher(repo, root/'attempts'/'active'/'src')
        def guard(scope):
            return Guard(scope, incarnation='fixture-restart' if restart else 'fixture-first', predecessor_dead=restart)
        bootstrap = TransportBootstrap(guard, lambda _: Observer([time.time()]), lambda _: Native(),
                                      launcher, channel, 'serving-test', 'controller', ('issuer',))
        bindings = []
        def start(scope):
            binding = bootstrap(scope)
            bindings.append(binding)
            return binding
        stack.callback(host.stop)
        host.configure(home, {('symphony_pm','ingress'): ServingBootstrap(start)})
        host.start_profile(home)
        binding, = bindings
        yield QualificationSession(runner, adapter, source, entry, loop, launcher, binding.port.owner)
