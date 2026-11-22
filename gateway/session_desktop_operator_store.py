"""Durable, private OPERATOR ownership alongside the canonical admission ledger."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing

from hermes_state_runtime import RuntimeStoreError


async def routing_index(authority):
    store = authority.runner.session_store
    db = await asyncio.to_thread(lambda: store._routing_db)
    if db is None:
        raise RuntimeStoreError('storage_unavailable')
    return store._routing_scope(), db if db is not authority.db else None


def routing_session_id(conn, scope, route, *, separate_db=None):
    sql = "SELECT json_extract(entry_json,'$.session_id') FROM gateway_routing WHERE scope=? AND session_key=?"
    try:
        if separate_db is None:
            row = conn.execute(sql, (scope, route)).fetchone()
        else:
            # Multiplexed routing lives in the launch database. A tracked read-only connection
            # cannot acquire that database's Python writer lock while this transaction is held.
            from hermes_state import _READ_BUSY_TIMEOUT_S

            with closing(separate_db._connect_read_only(timeout=_READ_BUSY_TIMEOUT_S)) as reader:
                row = reader.execute(sql, (scope, route)).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeStoreError('storage_unavailable') from exc
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def binding_key(session_id, submission_id):
    identity = json.dumps([session_id, submission_id], ensure_ascii=False, separators=(',', ':'))
    return 'gateway.desktop_operator.v1.' + hashlib.sha256(identity.encode('utf-8')).hexdigest()


def read_record(conn, key):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def write_record(conn, key, value):
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, json.dumps(value, separators=(',', ':'))))


def deny():
    raise RuntimeStoreError('permission_denied')


def check_binding(record, principal, generation, connection, *, recover=False):
    if (not record or record['principal'] != principal or record['generation'] != generation
            or (not recover and record['connection'] != connection)):
        deny()
    return record


def admit(conn, key, principal, generation, connection, admission_id, session_id, submission_id, *, replay):
    record = read_record(conn, key)
    if replay:
        # A delivered first submit can be replayed with a fresh nonce on the original socket.
        # The same initial generation can never regain control on a recovered socket.
        check_binding(record, principal, 1 if generation == 0 else generation, connection)
        if record['admission_id'] != admission_id:
            deny()
        return
    if generation or record or conn.execute(
        'SELECT 1 FROM session_admissions WHERE target_session_id=? AND request_id=? LIMIT 1',
        (session_id, submission_id),
    ).fetchone():
        deny()
    write_record(conn, key, dict(principal=principal, generation=1, connection=connection,
                                admission_id=admission_id))
