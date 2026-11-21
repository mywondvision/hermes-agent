"""Versioned serving schema, retaining the historical owner tables without importing fixtures."""
from .types import Refused

VERSION = 2
_LEGACY = {'writers', 'operations', 'jobs', 'effects'}
_COLUMNS = {
    'writers': ['namespace', 'owner', 'epoch', 'expires'],
    'operations': ['namespace', 'project', 'request_id', 'binding', 'job', 'revision', 'effect_key'],
    'jobs': ['namespace', 'project', 'job', 'revision', 'contract', 'target'],
    'effects': ['effect_key', 'target', 'state', 'dispatch_epoch', 'cancel_at', 'fence_ref', 'proof', 'proof_epoch'],
}
_V1_COLUMNS = {
    **_COLUMNS,
    'serving_schema': ['version'],
    'serving_owner': ['namespace', 'identity', 'controller', 'epoch', 'observer_generation', 'observer_healthy'],
    'serving_permits': ['ref', 'body', 'binding', 'identity_binding', 'approval_id', 'approval_digest', 'used'],
    'serving_launches': ['namespace', 'project', 'job', 'attempt', 'ticket', 'state', 'binding', 'result'],
    'serving_cancel_causes': ['effect_key', 'request_id', 'cause', 'detail', 'observed_at'],
    'serving_readbacks': ['effect_key', 'writer_epoch', 'observer_generation', 'observed_at', 'body'],
    'serving_recovery_hold': ['namespace', 'reason'],
    'serving_members': ['attempt', 'pid', 'creation'],
}
_LAUNCH_FIELDS = ['stop_proof', 'proof_source', 'hold_reason', 'owner_identity', 'ownership_contract', 'host_id',
                  'boot_id']
_CURRENT_COLUMNS = {
    **_V1_COLUMNS,
    'serving_owner': [*_V1_COLUMNS['serving_owner'], 'host_id', 'workspace_guarded'],
    'serving_launches': [*_V1_COLUMNS['serving_launches'], *_LAUNCH_FIELDS],
    'serving_observations': ['attempt', 'seq', 'kind', 'body'],
}
_OBSERVATIONS = ('CREATE TABLE serving_observations(attempt TEXT NOT NULL,seq INTEGER NOT NULL,'
                 'kind TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(attempt,seq))')
_PRESERVED_LEGACY = {'launches', 'cancel_causes', 'cancel_readbacks'}

DDL = """
CREATE TABLE IF NOT EXISTS writers(namespace TEXT PRIMARY KEY,owner TEXT NOT NULL,epoch INTEGER NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operations(namespace TEXT,project TEXT,request_id TEXT,binding TEXT NOT NULL,job TEXT NOT NULL,revision TEXT NOT NULL,effect_key TEXT,PRIMARY KEY(namespace,project,request_id));
CREATE TABLE IF NOT EXISTS jobs(namespace TEXT,project TEXT,job TEXT,revision TEXT NOT NULL,contract TEXT NOT NULL,target TEXT,PRIMARY KEY(namespace,project,job));
CREATE TABLE IF NOT EXISTS effects(effect_key TEXT PRIMARY KEY,target TEXT NOT NULL,state TEXT NOT NULL,dispatch_epoch INTEGER NOT NULL,cancel_at REAL NOT NULL,fence_ref TEXT NOT NULL,proof TEXT,proof_epoch INTEGER);
CREATE TABLE IF NOT EXISTS serving_schema(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS serving_owner(namespace TEXT PRIMARY KEY,identity TEXT NOT NULL,controller TEXT NOT NULL,epoch INTEGER NOT NULL,observer_generation INTEGER NOT NULL,observer_healthy INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS serving_permits(ref TEXT PRIMARY KEY,body TEXT NOT NULL,binding TEXT NOT NULL,identity_binding TEXT NOT NULL,approval_id TEXT NOT NULL,approval_digest TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS serving_permits_by_identity ON serving_permits(identity_binding);
CREATE TABLE IF NOT EXISTS serving_launches(namespace TEXT,project TEXT,job TEXT,attempt TEXT UNIQUE NOT NULL,ticket TEXT NOT NULL,state TEXT NOT NULL,binding TEXT,result TEXT,PRIMARY KEY(namespace,project,job));
CREATE TABLE IF NOT EXISTS serving_cancel_causes(effect_key TEXT,request_id TEXT,cause TEXT,detail TEXT,observed_at REAL,PRIMARY KEY(effect_key,request_id,cause));
CREATE TABLE IF NOT EXISTS serving_readbacks(effect_key TEXT PRIMARY KEY,writer_epoch INTEGER NOT NULL,observer_generation INTEGER NOT NULL,observed_at REAL NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS serving_recovery_hold(namespace TEXT PRIMARY KEY,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS serving_members(attempt TEXT,pid INTEGER,creation TEXT,PRIMARY KEY(attempt,pid,creation));
"""


def _install_v2(db):
    db.execute('ALTER TABLE serving_owner ADD COLUMN host_id TEXT')
    db.execute('ALTER TABLE serving_owner ADD COLUMN workspace_guarded INTEGER NOT NULL DEFAULT 0')
    for field in _LAUNCH_FIELDS:
        db.execute(f'ALTER TABLE serving_launches ADD COLUMN {field} TEXT')
    db.execute(_OBSERVATIONS)


def initialize(db, *, migrate_legacy=False, migrate_v1=False):
    """Explicit legacy migration preserves all rows and quarantines every legacy namespace.

    A database with unrelated tables or a newer schema is refused. Migration never
    manufactures process identities, proof, permits or permission to resume.
    """
    db.execute('BEGIN IMMEDIATE')
    try:
        _initialize(db, migrate_legacy, migrate_v1)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _initialize(db, migrate_legacy, migrate_v1):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables and 'serving_schema' not in tables:
        if not migrate_legacy or not _LEGACY.issubset(tables):
            raise Refused('EXPLICIT_LEGACY_MIGRATION_REQUIRED')
        if tables - (_LEGACY | _PRESERVED_LEGACY):
            raise Refused('FOREIGN_DATABASE')
        for name, expected in _COLUMNS.items():
            if [r[1] for r in db.execute(f'PRAGMA table_info({name})')] != expected:
                raise Refused('LEGACY_SCHEMA_MISMATCH')
    elif 'serving_schema' in tables:
        versions = [tuple(r) for r in db.execute('SELECT version FROM serving_schema')]
        if versions not in ([(1,)], [(VERSION,)]):
            raise Refused('UNSUPPORTED_SCHEMA_VERSION')
        columns = _V1_COLUMNS if versions == [(1,)] else _CURRENT_COLUMNS
        if not columns.keys() <= tables or tables - (columns.keys() | _PRESERVED_LEGACY):
            raise Refused('SERVING_SCHEMA_MISMATCH')
        for name, expected in columns.items():
            if [r[1] for r in db.execute(f'PRAGMA table_info({name})')] != expected:
                raise Refused('SERVING_SCHEMA_MISMATCH')
        if versions == [(1,)]:
            if not migrate_v1:
                raise Refused('EXPLICIT_V1_MIGRATION_REQUIRED')
            _install_v2(db)
            db.execute('UPDATE serving_schema SET version=?', (VERSION,))
        return
    for statement in DDL.split(';'):
        if statement.strip():
            db.execute(statement)
    _install_v2(db)
    db.execute('INSERT INTO serving_schema VALUES(?)', (VERSION,))
    if tables:
        namespaces = {r[0] for r in db.execute('SELECT namespace FROM writers UNION SELECT namespace FROM jobs')}
        for namespace in namespaces:
            db.execute('INSERT INTO serving_recovery_hold VALUES(?,?)', (namespace, 'LEGACY_NATIVE_IDENTITY_UNKNOWN'))
        if any(db.execute(f'SELECT 1 FROM {name} LIMIT 1').fetchone() for name in tables):
            db.execute('INSERT INTO serving_recovery_hold VALUES(?,?)', ('*', 'LEGACY_STATE_REQUIRES_RECONCILIATION'))
