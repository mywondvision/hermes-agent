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


def read_launches(path):
    """Launch rows as dicts, or None when there is no journal. Opened read-only, closed before returning."""
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT project,job,attempt,state,binding,result FROM serving_launches '
                          'ORDER BY rowid').fetchall()
    finally:
        db.close()
    launches = []
    for row in rows:
        result = json.loads(row['result']) if row['result'] else None
        launches.append({'project': row['project'], 'job': row['job'], 'attempt': row['attempt'],
                         'state': row['state'], 'bound': row['binding'] is not None, 'result': result})
    return launches


def summary(launch):
    result = launch['result'] or {}
    patch, check = result.get('patch') or {}, result.get('check')
    return {'job': launch['job'], 'attempt': launch['attempt'], 'state': launch['state'],
            'bound': launch['bound'], 'recorded': launch['result'] is not None,
            'stop': result.get('stop'), 'exit_code': result.get('exit_code'),
            'precheck': (result.get('precheck') or {}).get('verdict'), 'inexec_probe': result.get('inexec_probe'),
            'patch_path': patch.get('path'), 'patch_sha256': patch.get('sha256'),
            'patch_refused': patch.get('refused'), 'paths': patch.get('paths'),
            'check_ok': None if check is None else check.get('ok'),
            'workspace': (result.get('workspace') or {}).get('path'), 'usage': result.get('usage')}


def _setup(parser):
    commands = parser.add_subparsers(dest='symphony_command')
    status = commands.add_parser('status', help='Show launches recorded in the Symphony journal (read-only)')
    status.add_argument('--namespace', default=DEFAULT_NAMESPACE)
    status.add_argument('--json', action='store_true', help='Print JSON')


def _handle(args):
    if getattr(args, 'symphony_command', None) != 'status':
        print('usage: hermes symphony status [--namespace NS] [--json]')
        return 2
    from hermes_constants import get_hermes_home
    path = journal_path(get_hermes_home(), args.namespace)
    launches = read_launches(path)
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
        for key in ('stop', 'exit_code', 'precheck', 'inexec_probe', 'patch_path', 'patch_sha256', 'patch_refused',
                    'paths', 'check_ok', 'workspace', 'usage'):
            if row[key] is not None:
                print(f'  {key}: {row[key]}')
    return 0


def register(ctx):
    register_cli = getattr(ctx, 'register_cli_command', None)
    if register_cli is not None:
        register_cli(name='symphony', help='Symphony PM journal (read-only status)', setup_fn=_setup,
                     handler_fn=_handle, description='Read what the Symphony journal recorded; never writes.')
