"""TUI host shutdown composition; callers bind the owning profile at bootstrap."""
import contextlib
import logging

from hermes_cli.plugin_services import close_host_service_admission, stop_host_services

logger = logging.getLogger(__name__)


def shutdown_sessions(server):
    close_host_service_admission(server)
    try:
        for step in (server._flush_sessions_before_exit, server._release_gateway_wake_owner,
                     server._stop_turns_before_exit):
            with contextlib.suppress(Exception):
                step()
        with server._sessions_lock:
            sids = list(server._sessions)
        for sid in sids:
            try:
                server._close_session_by_id(sid, end_reason='tui_shutdown')
            except Exception as exc:  # health: allow BLE001 -- one failed session cannot skip other owned cleanup
                logger.warning('Session shutdown failed (%s)', type(exc).__name__)
    finally:
        failures = stop_host_services(server)
        if failures:
            logger.warning('Runtime service cleanup incomplete (%s)', ','.join(failures))
