"""Exercise a Telegram denial through the adapter event and the ordinary gateway command."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import sqlite3
from types import SimpleNamespace

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageType
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.symphony_pm.transport.types import digest
from tests.plugins.symphony_typed_human_support import human_session
from tests.plugins.test_symphony_pm_typed_approval import displayed


def _event(text, *, message_id=17, chat_id=4242, user_id=4242, **extra):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='4242:offline-fixture'))
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type='private', full_name='Fixture', title=None, is_forum=False),
        from_user=SimpleNamespace(id=user_id, is_bot=False, full_name='Fixture'),
        text=text, caption=None, entities=[], caption_entities=[], message_thread_id=None,
        is_topic_message=False, message_id=message_id, reply_to_message=None, quote=None,
        date=datetime.now(UTC), forum_topic_created=None, **extra,
    )
    return adapter._build_message_event(message, MessageType.COMMAND, update_id=100)


def _deliver(channel, event):
    return asyncio.run_coroutine_threadsafe(channel.runner._handle_message(event), channel.loop).result(15)


def _denials(channel):
    path = channel.owner.journal.path.with_name('typed-plan-audit.sqlite3')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return db.execute('SELECT session_id,request_id,message_id,plan_digest FROM telegram_denials').fetchall()


def test_telegram_denial_survives_owner_restart_without_a_permit(tmp_path):
    with human_session(tmp_path, platform=Platform.TELEGRAM) as channel:
        plan, command = displayed(channel, action='deny')
        assert '/symphony approve' not in '\n'.join(channel.adapter.sent)
        event = _event(command)
        assert _deliver(channel, event) == 'DENIED'
        assert _deliver(channel, event) == 'DENIED'
        expected = [(channel.entry.session_id, plan['request_id'], '17', digest(plan))]
        assert _denials(channel) == expected
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}

    with human_session(tmp_path, platform=Platform.TELEGRAM, restart=True) as channel:
        assert _deliver(channel, _event(command)) == 'DENIED'
        assert channel.request('deny') == ('WAITING_FOR_AUTHORITY', '')
        assert _denials(channel) == expected
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}


def test_telegram_refuses_approval_and_denials_without_exact_origin(tmp_path):
    with human_session(tmp_path, platform=Platform.TELEGRAM) as channel:
        _, command = displayed(channel, action='deny')
        original = _event(command)
        rejected = (
            (_event(command.replace('deny', 'approve')), 'ORIGINAL_TYPED_COMMAND_REQUIRED'),
            (_event(command[:-1] + ('0' if command[-1] != '0' else '1')), 'NO_MATCHING_TYPED_PLAN'),
            (_event(command, user_id=57), 'OWNER_SENDER_REQUIRED'),
            (_event(command, chat_id=57), 'OWNER_SENDER_REQUIRED'),
            (_event(command, edit_date=datetime.now(UTC)), 'ORIGINAL_TYPED_COMMAND_REQUIRED'),
            (_event(command, via_bot=SimpleNamespace(id=99)), 'ORIGINAL_TYPED_COMMAND_REQUIRED'),
            (_event(command, forward_origin=SimpleNamespace(type='user')), 'ORIGINAL_TYPED_COMMAND_REQUIRED'),
            (replace(original, message_type=MessageType.VOICE), 'ORIGINAL_TYPED_COMMAND_REQUIRED'),
            (replace(original, raw_message=None), 'ORIGINAL_TYPED_COMMAND_REQUIRED'),
        )
        for event, expected in rejected:
            assert _deliver(channel, event) == expected
        assert not _table_exists(channel)
        assert _deliver(channel, original) == 'DENIED'
        assert len(_denials(channel)) == 1
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}


def _table_exists(channel):
    path = channel.owner.journal.path.with_name('typed-plan-audit.sqlite3')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_denials'").fetchone() is not None
