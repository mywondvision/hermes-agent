"""``hermes symphony status``: what the journal recorded about launches. Reads only.

The journal is opened read-only and nothing is created when it is absent: the command must never
take a write lock a live owner needs, initialise a schema or leave a file behind. What it prints is
the owner's own record (state, patch path and SHA-256, the check's outcome), not a trusted status
of the external run.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

DEFAULT_NAMESPACE = 'symphony_pm:ingress'


def journal_path(home, namespace):
    from .transport.types import digest
    return Path(home)/'plugin-data'/'symphony_pm'/digest(namespace)[:32]/'journal.sqlite3'


def read_launches(path, *, namespace=None):
    """Launch rows as dicts, or None when there is no journal. Opened read-only, closed before returning."""
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
    try:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        versions = [r[0] for r in db.execute('SELECT version FROM serving_schema')]
        if versions not in ([1], [2]):
            raise ValueError('UNSUPPORTED_SCHEMA_VERSION')
        observations = {}
        columns = 'namespace,project,job,ticket,attempt,state,binding,result'
        if versions == [2]:
            columns += ',stop_proof,proof_source,hold_reason,owner_identity,ownership_contract,host_id,boot_id'
            for obs in db.execute('SELECT attempt,seq,kind,body FROM serving_observations ORDER BY attempt,seq'):
                observations.setdefault(obs['attempt'], []).append({
                    'seq': obs['seq'], 'kind': obs['kind'], 'body': json.loads(obs['body'])})
        query = 'SELECT ' + columns + ' FROM serving_launches'
        if namespace is None:
            rows = db.execute(query + ' ORDER BY rowid').fetchall()
        else:
            rows = db.execute(query + ' WHERE namespace=? ORDER BY rowid', (namespace,)).fetchall()
        settled = {}
        if versions == [2]:
            from .transport.linux_identity import CONTRACT
            from .transport.linux_journal import saved_stop
            for row in rows:
                if row['namespace'] == 'symphony_pm:ingress-linux':
                    settled[row['attempt']] = (saved_stop(db, row) or
                        row['state'] == 'NOT_STARTED' and row['binding'] is None and row['ownership_contract'] == CONTRACT)
    finally:
        db.close()
    launches = []
    for row in rows:
        result = json.loads(row['result']) if row['result'] else None
        launch = {'namespace': row['namespace'], 'revision': json.loads(row['ticket']).get('revision'),
                  'project': row['project'], 'job': row['job'], 'attempt': row['attempt'],
                  'state': row['state'], 'bound': row['binding'] is not None, 'result': result}
        if versions == [2]:
            launch.update({field: row[field] for field in ('stop_proof', 'proof_source', 'hold_reason', 'owner_identity')})
            if row['namespace'] == 'symphony_pm:ingress-linux':
                launch['containment_settled'] = settled[row['attempt']]
            launch['observations'] = observations.get(row['attempt'], [])
        launches.append(launch)
    return launches


def summary(launch):
    result = launch['result'] or {}
    patch, check = result.get('patch') or {}, result.get('check')
    return {**{key: launch[key] for key in ('stop_proof', 'proof_source', 'hold_reason', 'observations') if key in launch},
            'job': launch['job'], 'attempt': launch['attempt'], 'state': launch['state'],
            'view': next((obs['body']['view'] for obs in reversed(launch.get('observations', ()))
                          if obs['kind'] == 'STATUS' and 'view' in obs['body']), None),
            'bound': launch['bound'], 'recorded': launch['result'] is not None,
            'not_started_reason': next((obs['body']['reason'] for obs in launch.get('observations', ())
                                        if obs['kind'] == 'NOT_STARTED'), None),
            'stop': result.get('stop'), 'exit_code': result.get('exit_code'),
            'precheck': (result.get('precheck') or {}).get('verdict'), 'inexec_probe': result.get('inexec_probe'),
            'patch_path': patch.get('path'), 'patch_sha256': patch.get('sha256'),
            'patch_refused': patch.get('refused'), 'paths': patch.get('paths'),
            'check_ok': None if check is None else check.get('ok'),
            'workspace': (result.get('workspace') or {}).get('path'), 'usage': result.get('usage')}


def _setup(parser):
    commands = parser.add_subparsers(dest='symphony_command')
    for name, help_text in [('status', 'Show saved launch observations'), ('doctor', 'Inspect qualification and storage')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--namespace', default=DEFAULT_NAMESPACE)
        command.add_argument('--json', action='store_true', help='Print JSON')


def _handle(args):
    if getattr(args, 'symphony_command', None) not in ('status', 'doctor'):
        print('usage: hermes symphony {status,doctor} [--namespace NS] [--json]')
        return 2
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    path = journal_path(home, args.namespace)
    try:
        launches = read_launches(path, namespace=args.namespace)
    except (OSError, sqlite3.Error, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) == 'UNSUPPORTED_SCHEMA_VERSION' else 'JOURNAL_UNREADABLE'
        print(json.dumps({'journal': path.is_file(), 'reasons': [reason]}) if args.json else reason)
        return 1
    if args.symphony_command == 'doctor':
        from .doctor import doctor
        report = doctor(home, args.namespace, path, launches)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            for field, value in report.items():
                print(f'{field}: {value}')
        return 0
    rows = [] if launches is None else [summary(launch) for launch in launches]
    if args.json:
        print(json.dumps({'journal': launches is not None, 'launches': rows}, ensure_ascii=False, indent=1))
        return 0
    if launches is None:
        print('No Symphony journal for namespace', args.namespace)
        return 0
    if not rows:
        print('No launches recorded.')
    for row in rows:
        print(f"{row['job']}  {row['state']}  attempt={row['attempt']}")
        for key in ('stop_proof', 'proof_source', 'hold_reason', 'not_started_reason', 'stop', 'exit_code', 'precheck', 'inexec_probe',
                    'patch_path', 'patch_sha256', 'patch_refused', 'paths', 'check_ok', 'workspace', 'usage',
                    'view', 'observations'):
            if row.get(key) is not None:
                print(f'  {key}: {row[key]}')
    return 0


def register(ctx):
    register_cli = getattr(ctx, 'register_cli_command', None)
    if register_cli is not None:
        register_cli(name='symphony', help='Symphony PM journal (read-only status)', setup_fn=_setup,
                     handler_fn=_handle, description='Read what the Symphony journal recorded; never writes.')
