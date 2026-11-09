"""SQLite state/intent contracts, never OS death or native cancellation proof."""
import json
import sqlite3

import pytest

from plugins.symphony_pm.transport.journal import Journal
from plugins.symphony_pm.transport.schema import initialize
from plugins.symphony_pm.transport.types import Refused
from tests.fixtures.symphony_pm_ingress.owner import Owner as LegacyOwner
from tests.plugins.symphony_serving_support import approve, rig
from tests.tools.test_symphony_pm_adapter import submit_payload


def test_lost_submit_reply_reuses_durable_ticket_after_connection_reopen(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        ticket, state, fresh = r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
        reopened = Journal(tmp_path, r.scope.namespace)
        same, replay_state, again = reopened.reserve_launch(r.owner.token, request, context, r.now[0])
        assert same == ticket and state == replay_state == 'LAUNCH_INTENT' and fresh and not again
        with sqlite3.connect(reopened.path) as db:
            assert db.execute('SELECT used FROM serving_permits').fetchone() == (1,)
    finally:
        r.owner.close(0)


def test_unknown_unbound_intent_blocks_another_request_and_new_attempt(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
        other, other_context, _ = approve(r, payload=submit_payload(request_id='request-2'))
        with pytest.raises(Refused, match='UNSETTLED_PRIOR_LAUNCH'):
            r.owner.journal.reserve_launch(r.owner.token, other, other_context, r.now[0])
        assert r.native.created == []
    finally:
        r.owner.close(0)


def test_controller_rotation_fences_old_token_without_lease_takeover(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        old = r.owner.token
        new = r.owner.rotate_controller('controller-2')
        assert new.epoch == old.epoch+1
        with pytest.raises(Refused, match='WRITER_FENCED'):
            r.owner.journal.reserve_launch(old, request, context, r.now[0])
        with pytest.raises(Refused, match='PREDECESSOR_LIVE_OR_UNKNOWN'):
            rig(tmp_path, incarnation='another-incarnation')
        assert r.native.created == []
    finally:
        r.owner.close(0)


def test_legacy_migration_preserves_spent_effect_and_quarantines(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    legacy = LegacyOwner(path)
    legacy.claim('legacy-controller', 1)
    legacy.db.execute("INSERT INTO effects VALUES(?,?,'DISPATCH_INTENT',1,2,?,NULL,NULL)",
                      ('legacy-effect', json.dumps({'opaque_legacy_target': True}), 'legacy-fence'))
    legacy.close()
    with sqlite3.connect(path, isolation_level=None) as db:
        before = list(db.execute('SELECT * FROM effects'))
        with pytest.raises(Refused, match='EXPLICIT_LEGACY_MIGRATION'):
            initialize(db)
        initialize(db, migrate_legacy=True)
        assert list(db.execute('SELECT * FROM effects')) == before
        assert db.execute('SELECT reason FROM serving_recovery_hold WHERE namespace=?', ('*',)).fetchone()
        assert db.execute('SELECT COUNT(*) FROM serving_launches').fetchone() == (0,)


def test_foreign_database_is_not_adopted(tmp_path):
    with sqlite3.connect(tmp_path/'foreign.sqlite3', isolation_level=None) as db:
        db.execute('CREATE TABLE customer_data(secret TEXT)')
        with pytest.raises(Refused):
            initialize(db, migrate_legacy=True)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='customer_data'").fetchone()


def test_version_marker_without_serving_tables_is_refused(tmp_path):
    with sqlite3.connect(tmp_path/'incomplete.sqlite3', isolation_level=None) as db:
        db.execute('CREATE TABLE serving_schema(version INTEGER NOT NULL)')
        db.execute('INSERT INTO serving_schema VALUES(1)')
        with pytest.raises(Refused, match='SERVING_SCHEMA_MISMATCH'):
            initialize(db)
