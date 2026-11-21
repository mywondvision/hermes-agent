"""Offline diagnostics for the exact qualification last installed by this profile's host."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .transport.types import sha


def atomic_record(path, data):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        staging = Path(stream.name)
        try:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            staging.unlink(missing_ok=True)
            raise
    try:
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def record_qualification(owner):
    q = getattr(owner.launcher, 'qualification', None)
    settings = getattr(owner.launcher, 'settings', None)
    path = owner.journal.path.with_name('qualification.json')
    if owner.scope.namespace == 'symphony_pm:ingress-linux':
        from .transport.linux_identity import CONTRACT
        from .transport.linux_sandbox import LinuxQualification, in_image
        if not isinstance(q, LinuxQualification) or settings is None:
            return
        atomic_record(path, {
            'namespace': q.namespace, 'profile_home': q.profile_home, 'backend': CONTRACT,
            'fingerprint': q.fingerprint, 'evidence_sha256': q.evidence_sha256,
            'attempts_root': str(settings.attempts_root), 'permission_profile': q.permission_profile,
            'artifacts': {'executable': [str(in_image(q.runtime, q.executable)), q.hashes[4]],
                          'config': [str(in_image(q.runtime, '/etc/symphony/codex/config.toml')), q.hashes[6]]}})
        return
    if q is None or settings is None or not hasattr(q, 'config_sha256'):
        path.unlink(missing_ok=True)
        return
    atomic_record(path, {
        'profile_home': owner.scope.profile_home, 'namespace': owner.scope.namespace,
        'attempts_root': str(settings.attempts_root), 'permission_profile': q.permission_profile,
        'job_coverage': owner._coverage_verified(),
        'artifacts': {'executable': [q.executable, q.executable_sha256],
                      'config': [str(Path(q.codex_home)/'config.toml'), q.config_sha256]}})


def _artifacts_checked(items):
    checks = {}
    for name in ('config', 'executable'):
        target, expected = items[name]
        ok = False
        if sha(expected, 64):
            try:
                with Path(target).open('rb') as stream:
                    ok = hashlib.file_digest(stream, 'sha256').hexdigest() == expected
            except OSError:
                pass
        checks[name] = ok
    return checks


def doctor(home, namespace, journal, launches):
    from hermes_cli.plugin_request_binding import canonical_home
    from .transport.recovery import PROVEN_STOP
    disk_path = next(p for p in (home, *home.parents) if p.is_dir())
    try:
        import psutil
        ram = psutil.virtual_memory().available
    except (ImportError, OSError):
        ram = None
    reasons = [] if launches is not None else ['JOURNAL_ABSENT']
    rows = launches or ()
    linux = namespace == 'symphony_pm:ingress-linux'
    if linux:
        unsettled = any(row.get('containment_settled') is not True for row in rows)
        reasons.append('LINUX_LIVE_ADMISSION_UNOBSERVED')
    else:
        unsettled = any(row['state'] not in ('NOT_STARTED', 'RELEASED_BY_OWNER') and
                        not (row['state'] == 'STOPPED' and row.get('stop_proof') in PROVEN_STOP
                             and row.get('proof_source')) for row in rows)
    if unsettled:
        reasons.append('UNSETTLED_PRIOR_LAUNCH_HOLD')
    if any(row.get('hold_reason') == 'WORKSPACE_HELD' for row in rows):
        reasons.append('WORKSPACE_HELD')
    report = {'journal': launches is not None, 'free_disk_bytes': shutil.disk_usage(disk_path).free,
              'free_ram_bytes': ram, 'active': None, 'archive_count': 0, 'archive_bytes': 0,
              'qualification': None, 'owner_liveness': 'NOT_OBSERVED', 'reasons': reasons}
    if linux:
        report.update(archive_count=None, archive_bytes=None)
    try:
        record = json.loads(journal.with_name('qualification.json').read_text(encoding='utf-8'))
        if record['profile_home'] != canonical_home(home) or record['namespace'] != namespace:
            raise ValueError
        root = Path(record['attempts_root'])
        if not root.is_absolute():
            raise ValueError
        checks = _artifacts_checked(record['artifacts'])
        qualification = {'permission_profile': record['permission_profile'], 'checks': checks}
        if linux:
            from .transport.linux_identity import CONTRACT
            if (record['backend'] != CONTRACT or not sha(record['fingerprint'], 64)
                    or not sha(record['evidence_sha256'], 64)):
                raise ValueError
            qualification.update(backend=CONTRACT, fingerprint=record['fingerprint'],
                                 evidence_sha256=record['evidence_sha256'])
        else:
            qualification['job_coverage'] = record['job_coverage']
            if not record['job_coverage']:
                reasons.append('JOB_COVERAGE_UNQUALIFIED')
        report['qualification'] = qualification
        reasons.extend(name.upper() + '_CHANGED_OR_MISSING' for name, ok in checks.items() if not ok)
        report['active'] = os.path.lexists(root/'active')
        if report['active']:
            reasons.append('ACTIVE_OCCUPIED')
        if linux:
            # Inventory would descend through Linux mount points not covered by the Windows archive walk.
            return report
        from .retention import archive_inventory
        inventory = archive_inventory(journal.parent, root, launches or [])
        report.update(archive_count=len(inventory), archive_bytes=sum(i['bytes'] for i in inventory))
    except FileNotFoundError:
        reasons.append('QUALIFICATION_UNRECORDED')
    except (OSError, ValueError, KeyError, TypeError):
        reasons.append('QUALIFICATION_UNREADABLE')
    return report
