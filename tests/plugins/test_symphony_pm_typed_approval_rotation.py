"""The profile's gateway routing index decides which conversation can use the owner DM."""
from contextlib import contextmanager
from dataclasses import replace
import asyncio
from types import SimpleNamespace

from gateway.session import SessionStore
from tests.plugins.symphony_typed_human_support import human_session


def test_compression_repoint_then_real_new_command_can_publish_again(tmp_path, monkeypatch, request):
    import hermes_state
    with human_session(tmp_path) as channel:
        runner = channel.runner
        monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
        store = SessionStore(tmp_path/'profile'/'sessions', runner.config)
        request.addfinalizer(store.close_all_db_handles)
        runner.session_store = store
        runner._session_model_overrides, runner._pending_model_notes, runner._background_tasks = {}, {}, set()
        monkeypatch.setattr(runner, '_read_user_config', lambda: {'approvals': {'destructive_slash_confirm': False}})
        channel.entry = store.get_or_create_session(channel.source)
        original = replace(channel.entry)
        key = original.session_key
        _, shown = channel.request('original')
        assert '/symphony approve' in shown
        stale = next(line for line in shown.splitlines() if line.startswith('/symphony approve '))
        child = 'compressed-conversation'
        db = store._db_for_key(key)
        db.create_session(original.session_id, source='discord')
        db.append_message(original.session_id, 'user', 'fixture request')
        watermark = db.get_active_message_watermark(original.session_id)
        handoff = [{'role': 'assistant', 'content': 'fixture summary'}]
        db.publish_compression_child(parent_session_id=original.session_id, child_session_id=child,
                                     source='discord', messages=handoff, watermark=watermark,
                                     watermark_ceiling=watermark, require_compression_lease=False)
        asyncio.run_coroutine_threadsafe(runner._persist_manual_compression(
            SimpleNamespace(session_id=child), channel.entry, channel.source, handoff),
            channel.loop).result(10)
        compressed = store.lookup_by_session_key(key)
        assert compressed and compressed.session_id != original.session_id
        # No Symphony call sees the intermediate id: /new reports B -> C while Symphony remembers A.
        channel.deliver('/new')
        current = store.lookup_by_session_key(key)
        assert current.session_id not in (original.session_id, compressed.session_id)
        channel.entry = current
        state, fresh = channel.request('current')
        assert state == 'WAITING_FOR_AUTHORITY' and '/symphony approve ' in fresh
        assert channel.deliver(stale) == 'NO_MATCHING_TYPED_PLAN'
        channel.entry = original
        assert channel.request('late-worker') == ('WAITING_FOR_AUTHORITY', '')
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}


def test_model_retry_cannot_reserve_after_route_moves_during_insert(tmp_path, monkeypatch):
    with human_session(tmp_path) as channel:
        _, shown = channel.request('original')
        decision = next(line for line in shown.splitlines() if line.startswith('/symphony approve '))
        assert 'APPROVED' in channel.deliver(decision)
        journal = channel.owner.journal
        original = journal._connection
        inserted = []
        @contextmanager
        def repoint_during_reserve():
            with original() as db:
                def repoint(statement):
                    if statement.startswith('INSERT INTO serving_launches'):
                        channel.entry.session_id = 'auto-reset-id'
                        inserted.append(True)
                db.set_trace_callback(repoint)
                yield db
        with monkeypatch.context() as m:
            m.setattr(journal, '_connection', repoint_during_reserve)
            assert channel.retry('original') != 'SUBMITTED'
        assert inserted == [True]
        assert channel.counts() == {'permits': 1, 'reservations': 0, 'launcher_calls': 0}
        assert '/symphony approve ' in channel.request('new')[1]
        assert channel.deliver(decision) == 'NO_MATCHING_TYPED_PLAN'


def test_unknown_or_foreign_owner_dm_cannot_publish(tmp_path, monkeypatch):
    with human_session(tmp_path) as channel:
        lookup = channel.runner.session_store.lookup_by_session_key
        for origin in (None, replace(channel.source, user_id='foreign')):
            with monkeypatch.context() as m:
                m.setattr(lookup, 'side_effect', lambda _: replace(channel.entry, origin=origin))
                assert channel.request('unbound') == ('WAITING_FOR_AUTHORITY', '')
        with monkeypatch.context() as m:
            m.setattr(lookup, 'side_effect', lambda _: None)
            assert channel.request('unbound') == ('WAITING_FOR_AUTHORITY', '')
        with monkeypatch.context() as m:
            m.setattr(channel.runner, '_served_profile_for_home', lambda _: None)
            assert channel.request('unbound') == ('WAITING_FOR_AUTHORITY', '')
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}
