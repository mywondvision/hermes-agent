"""Shutdown closes service admission before sessions, then drains owned callbacks."""
import threading
from types import SimpleNamespace

from tui_gateway.plugin_service_lifecycle import shutdown_sessions


def _owner(events, *, failing_session=False):
    class Host:
        def close_admission(self):
            events.append('admission-closed')

        def stop(self, **_kwargs):
            events.append('services-stopped')
            return ()

    def close(sid, **_kwargs):
        events.append('session-'+sid)
        if failing_session and sid == 'first':
            raise RuntimeError('session close failed')

    return SimpleNamespace(
        _plugin_service_host=Host(), _sessions_lock=threading.RLock(),
        _sessions={'first': {}, 'second': {}}, _close_session_by_id=close,
        _flush_sessions_before_exit=lambda: events.append('flushed'),
        _release_gateway_wake_owner=lambda: None, _stop_turns_before_exit=lambda: None)


def test_shutdown_closes_admission_before_sessions_and_services_last():
    events = []
    shutdown_sessions(_owner(events))
    assert events == ['admission-closed', 'flushed', 'session-first', 'session-second', 'services-stopped']


def test_failed_session_cannot_skip_remaining_sessions_or_service_cleanup():
    events = []
    shutdown_sessions(_owner(events, failing_session=True))
    assert events == ['admission-closed', 'flushed', 'session-first', 'session-second', 'services-stopped']
