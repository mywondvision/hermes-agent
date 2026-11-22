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
from gateway.platforms.event import MessageEvent, MessageType
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
    def __init__(self, platform=Platform.DISCORD):
        super().__init__()
        self.platform = platform
        self.chat = 'fixture-owner-room'
        self.last_message_id = 1000

    def message_id(self, supplied=None):
        self.last_message_id = max(self.last_message_id + 1, int(supplied or 0))
        return supplied or str(self.last_message_id)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if chat_id != self.chat:
            return SendResult(success=False, error='fixture chat does not exist')
        self.sent.append(content)
        return SendResult(success=True, message_id=self.message_id())


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
    def __init__(self, runner, adapter, source, entry, loop, launcher, owner, project_ref):
        self.runner, self.adapter, self.source, self.entry = runner, adapter, source, entry
        self.loop, self.launcher, self.owner = loop, launcher, owner
        self.project_ref = project_ref

    def _submit(self, action):
        req = submit_payload(request_id='qualification-'+action, goal='Record the '+action+' fixture decision.',
                             project_ref=self.project_ref, raw_constraints=['Only the in-process fixture launcher may run.'])
        tokens = set_session_vars(platform=self.source.platform.value, chat_id=self.source.chat_id, user_id=self.source.user_id,
                                  chat_type='dm', session_key=self.entry.session_key, session_id=self.entry.session_id)
        try:
            return _call(req, session_id=self.entry.session_id)['outcome']
        finally:
            clear_session_vars(tokens)

    def request(self, action):
        before = len(self.adapter.sent)
        status = self._submit(action)
        return status, '\n'.join(self.adapter.sent[before:])

    def deliver(self, text, *, source=None, message_type=MessageType.TEXT, timeout=20, message_id=None, with_raw=True):
        source = source or self.source
        message_id = self.adapter.message_id(message_id)
        raw = SimpleNamespace(id=message_id, content=text, author=SimpleNamespace(id=source.user_id, bot=False),
                              channel=SimpleNamespace(id=source.chat_id), webhook_id=None, attachments=[],
                              message_snapshots=[], edited_at=None)
        event = MessageEvent(text=text, source=source, message_type=message_type, message_id=message_id, raw_message=raw if with_raw else None)
        return asyncio.run_coroutine_threadsafe(self.runner._handle_message(event), self.loop).result(timeout)

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
def human_session(root, *, restart=False, project_ref="fixture", owner_guard=None, launcher=None,
                  repository=None, linux_factory=None, typed_enabled=True, desktop=False, desktop_request_id=None,
                  platform=Platform.DISCORD):
    """Local operator adapter and plugin manager for one isolated test profile."""
    home, repo = root/'profile', root/'project'
    home.mkdir(exist_ok=True)
    if repository is not None:
        repo = repository
    if restart or repository is not None:
        if not (repo/'.git').is_dir():
            raise RuntimeError('FIXTURE_BASELINE_MISSING')
    else:
        repo.mkdir()
        _git('init', '-q', '-b', 'fixture-base', cwd=repo)
        _git('commit', '--allow-empty', '-m', 'Fixture baseline', cwd=repo)
    adapter = LocalChat(platform)
    runner = _runner(adapter)
    # The command-choice fixture suppresses context binding. Exercise the real slash-command route.
    del runner._set_session_env
    runner.config.multiplex_profiles = False
    runner.adapters = {platform: adapter}
    platform = Platform.LOCAL if desktop else platform
    user_id, session_id = 'fixture-human', 'fixture-owner-session'
    if platform is Platform.TELEGRAM:
        adapter.chat = user_id = '4242'
    if desktop_request_id is not None:
        from gateway.owner_identity import principal_subject
        from hermes_state_keys import local_identity
        user_id = principal_subject(dict(provider='local', user_id='fixture-owner'))
        session_id = adapter.chat = local_identity(str(home.resolve()), user_id, desktop_request_id)
    source = SessionSource(platform=platform, user_id=user_id, chat_type='dm', chat_id=adapter.chat)
    key = runner._session_key_for_source(source)
    entry = SessionEntry(session_key=key, session_id=session_id, created_at=datetime.now(),
                         updated_at=datetime.now(), origin=source, platform=source.platform, chat_type='dm')
    runner.session_store.lookup_by_session_key.side_effect = lambda route: entry if route == key else None
    if desktop:
        from gateway.session_local import LocalSessionAdapter
        adapter.plan_events = []
        def publish(chat_id, payload, *, event_type):
            adapter.plan_events.append((chat_id, event_type, payload))
        live = SimpleNamespace(route=key, source=source, event_stream=SimpleNamespace(publish=publish))
        authority = SimpleNamespace(profile_id=str(home), sessions={source.chat_id: live}, runner=runner)
        local = LocalSessionAdapter(authority)
        local.register_source(source)
        local.policies[source.chat_id] = SimpleNamespace(source='gui')
        runner.session_authority = authority
        runner.adapters = {Platform.LOCAL: local}
    operator = dict(platform=platform.value, user_id=source.user_id, chat_id=source.chat_id, session_key=key)
    typed = dict(project_ref=project_ref, repository=str(repo), branch='fixture-base', ttl_seconds=900)
    settings = {'operator': operator}
    if typed_enabled:
        settings['typed_approval'] = typed
    (home/'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['symphony_pm'], 'entries': {
        'symphony_pm': {'settings': settings}}}}), encoding='utf-8')
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
        if launcher is None:
            launcher = FixtureLauncher(repo, root/'attempts'/'active'/'src')
        def guard(scope):
            return Guard(scope, incarnation='fixture-restart' if restart else 'fixture-first', predecessor_dead=restart)
        bootstrap = TransportBootstrap(owner_guard or guard, lambda _: Observer([time.time()]), lambda _: Native(),
                                      launcher, channel, 'serving-test', 'controller', ('issuer',))
        bindings = []
        start_errors = []
        def start(scope):
            try:
                binding = linux_factory(scope, channel) if linux_factory is not None else bootstrap(scope)
            except Exception as exc:
                start_errors.append(exc)
                raise
            bindings.append(binding)
            return binding
        stack.callback(host.stop)
        service = 'ingress-linux' if linux_factory is not None else 'ingress'
        host.configure(home, {('symphony_pm',service): ServingBootstrap(start)})
        host.start_profile(home)
        if start_errors:
            raise RuntimeError('fixture service failed to start') from start_errors[0]
        binding, = bindings
        yield QualificationSession(runner, adapter, source, entry, loop, launcher, binding.port.owner, project_ref)
