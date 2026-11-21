"""SQLite state/intent contracts, never OS death or native cancellation proof."""
import json
import sqlite3

import pytest

from plugins.symphony_pm.transport.journal import Journal
from plugins.symphony_pm.transport.schema import initialize
from plugins.symphony_pm.transport.types import Refused
from tests.fixtures.symphony_pm_ingress.owner import Owner as LegacyOwner
from tests.plugins.symphony_serving_support import approve, rig
from tests.plugins.test_symphony_pm_adapter import submit_payload


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


def test_missing_writer_cannot_append_an_observation_or_release_a_hold(tmp_path, monkeypatch):
    from tests.plugins.symphony_serving_support import Native
    monkeypatch.setattr(Native, 'ownership_contract', None)
    first = rig(tmp_path)
    request, context, _ = approve(first)
    ticket, _ = first.owner.submit(request, context)
    first.owner.close(0)
    r = rig(tmp_path, incarnation='replacement', predecessor_dead=True)
    try:
        journal, token = r.owner.journal, r.owner.token
        prior_seq = journal.next_observation_seq(token, ticket)
        with pytest.raises(Refused, match='WRITER_FENCED'):
            journal.append_observation(None, ticket, {'kind': 'CAPTURE'})
        with pytest.raises(Refused, match='WRITER_FENCED'):
            journal.release_by_owner(None, ticket, 'fixture release')
        assert journal.next_observation_seq(token, ticket) == prior_seq
        assert journal.read_launch(token, ticket.project, ticket.job, ticket.revision)[1]['state'] == 'HELD_UNKNOWN'
    finally:
        r.owner.close(0)


def test_reconcile_record_is_refused_for_a_bound_launch(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        ticket, _ = r.owner.submit(request, context)
        with pytest.raises(Refused, match='RECONCILE_TARGET'):
            r.owner.journal.record_reconcile(r.owner.token, ticket, r.owner.generation, r.now[0], {})
        assert r.owner.journal.reconcile_record(r.owner.token, ticket) is None
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


def test_v1_requires_explicit_atomic_migration_and_remains_readable(tmp_path):
    from plugins.symphony_pm.status_cli import read_launches
    from plugins.symphony_pm.transport.schema import DDL
    path = tmp_path/'v1.sqlite3'
    with sqlite3.connect(path, isolation_level=None) as db:
        db.executescript(DDL)
        db.execute('INSERT INTO serving_schema VALUES(1)')
        db.execute("INSERT INTO serving_launches VALUES('ns','project','job','attempt','{}','CREATE_INTENT',NULL,?)",
                   (json.dumps({'exit_code': 3}),))
        old = tuple(db.execute('SELECT * FROM serving_launches').fetchone())
        assert read_launches(path)[0]['state'] == 'CREATE_INTENT'
        with pytest.raises(Refused, match='EXPLICIT_V1_MIGRATION_REQUIRED'):
            initialize(db)
        for _ in range(2):
            initialize(db, migrate_v1=True)
            assert db.execute('SELECT version FROM serving_schema').fetchone() == (2,)
            assert db.execute('SELECT * FROM serving_launches').fetchone()[:len(old)] == old
            assert db.execute('SELECT stop_proof,hold_reason FROM serving_launches').fetchone() == (None,None)
            assert read_launches(path)[0]['observations'] == []
        db.execute('UPDATE serving_schema SET version=9')
        with pytest.raises(Refused, match='UNSUPPORTED_SCHEMA_VERSION'):
            initialize(db, migrate_v1=True)
        assert db.execute('SELECT version FROM serving_schema').fetchone() == (9,)


def test_workspace_remains_in_admission_even_after_proven_stop(tmp_path, monkeypatch):
    from plugins.symphony_pm.transport.lifecycle import LifecycleOwner
    from tests.plugins.symphony_serving_support import Guard, Native, Observer
    r = rig(tmp_path)
    request, context, _ = approve(r)
    ticket, _ = r.owner.submit(request, context)
    launch = r.owner.launcher
    r.owner.close(0)
    monkeypatch.setattr(launch, 'workspace_clear', lambda: False, raising=False)
    monkeypatch.setattr(launch, 'settle_workspace', lambda t: False, raising=False)
    r.owner = LifecycleOwner(r.scope, Guard(r.scope, 'replacement', True), Observer(r.now), Native(), launch,
                             scheduler_id='serving-test', controller='successor', clock=lambda:r.now[0])
    try:
        row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)[1]
        assert (row['state'],row['hold_reason']) == ('STOPPED','WORKSPACE_HELD')
        request, context, _ = approve(r, payload=submit_payload(request_id='fresh'))
        with pytest.raises(Refused, match='WORKSPACE_HELD'):
            r.owner.submit(request, context)
    finally:
        r.owner.close(0)
