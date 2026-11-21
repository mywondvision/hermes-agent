"""Read saved attempt pairs and prune only the archives in a human-approved proposal."""
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time

from .doctor import atomic_record
from .status_cli import journal_path, read_launches
from .transport.recovery import PROVEN_STOP
from .transport.types import Refused, digest, finite, sha

DAY = 86400
WEEK = 7 * DAY


def _plain_info(path):
    info = path.lstat()
    if (not info.st_ino or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
        raise Refused('ARCHIVE_FILE_IDENTITY')
    return info


def _stamp(info):
    return (info.st_dev, info.st_ino, getattr(info, 'st_birthtime_ns', None),
            info.st_mode, info.st_size, info.st_mtime_ns)


def _scan(root, *, contents):
    """Preorder manifest. New names cannot enter the later deletion walk."""
    pending = [root]
    entries = {}
    size = 0
    while pending:
        path = pending.pop()
        info = _plain_info(path)
        stamp = _stamp(info)
        content = None
        if stat.S_ISDIR(info.st_mode):
            pending.extend(sorted(path.iterdir(), reverse=True))
        else:
            size += info.st_size
            if contents:
                with path.open('rb') as stream:
                    content = hashlib.file_digest(stream, 'sha256').hexdigest()
                if _stamp(_plain_info(path)) != stamp:
                    raise Refused('ARCHIVE_CHANGED')
        entries[path.relative_to(root).as_posix()] = (stamp, content)
    return entries, size


def _inspect(parent, root, row, *, contents):
    from .transport.recapture import archive_path, directory_identity
    if (not sha(row.get('owner_identity'), 64) or parent.name != digest(row['namespace'])[:32]):
        raise Refused('ARCHIVE_PROFILE_BINDING')
    attempt = row['attempt']
    private = parent/'attempts'/digest(attempt)[:12]
    workspace = archive_path(root, attempt)
    if any(not path.is_absolute() or path.resolve(strict=True) != path for path in (workspace, private)):
        raise Refused('ARCHIVE_PATH_REDIRECTED')
    if workspace.is_relative_to(private) or private.is_relative_to(workspace):
        raise Refused('ARCHIVE_ROOTS_OVERLAP')
    directory_identity(private)
    receipt = private/'workspace-id.json'
    if not stat.S_ISREG(_plain_info(receipt).st_mode):
        raise Refused('ARCHIVE_RECEIPT')
    if json.loads(receipt.read_bytes()) != directory_identity(workspace):
        raise Refused('ARCHIVE_RECEIPT')
    trees = {}
    size = 0
    for path in (workspace, private):
        entries, count = _scan(path, contents=contents)
        trees[path] = entries
        size += count
    return trees, size


_UNREADABLE = (OSError, ValueError, Refused)


def archive_inventory(journal_parent, attempts_root, launches):
    """Count bytes under journal-backed archive pairs, including protected pairs."""
    archives = []
    for row in launches:
        try:
            _, size = _inspect(Path(journal_parent), Path(attempts_root), row, contents=False)
        except _UNREADABLE:
            continue
        archives.append({'attempt': row['attempt'], 'bytes': size})
    return archives


def _settled(row):
    return not row.get('hold_reason') and (
        row['state'] in ('NOT_STARTED', 'RELEASED_BY_OWNER') or
        row['state'] == 'STOPPED' and row.get('stop_proof') in PROVEN_STOP and bool(row.get('proof_source')))


def _snapshot(owner, row, days):
    if (row['namespace'] != owner.scope.namespace or row['attempt'] in owner._preparing or not _settled(row)):
        return None
    trees, size = _inspect(owner.journal.path.parent, Path(owner.launcher.settings.attempts_root), row,
                           contents=True)
    # Capture and recapture write immutable results in the private tree. Until CR-8 supplies an
    # applied receipt, every nonempty patch is unapplied, including one left before a journal append.
    private = next(reversed(trees))
    cutoff = owner.clock() - days * DAY
    for name, (stamp, _) in trees[private].items():
        if name.endswith('.patch') and stamp[4] and stamp[5] / 1e9 > cutoff:
            return None
    # A missing patch referenced by a result is an unresolved discrepancy. Never derive a target
    # to read or remove from a saved path.
    records = [row.get('result') or {}, *(o['body'] for o in row.get('observations', ()) if o['kind'] == 'CAPTURE')]
    for record in records:
        patch = record.get('patch') or {}
        if patch.get('paths'):
            path = Path(patch.get('path') or '')
            if not path.is_relative_to(private) or path.relative_to(private).as_posix() not in trees[private]:
                return None
    signature = digest([row, [(str(p), entries) for p, entries in trees.items()]])
    return {'attempt': row['attempt'], 'bytes': size, 'signature': signature}, trees


def _candidate(owner, row, days):
    try:
        return _snapshot(owner, row, days)
    except _UNREADABLE:
        return None


def _read(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}


def _collect(owner, days):
    candidates = []
    for row in read_launches(owner.journal.path) or ():
        if not owner.scope.active:
            raise Refused('WRITER_FENCED')
        snapshot = _candidate(owner, row, days)
        if snapshot:
            candidates.append(snapshot[0])
    return candidates


def _propose(owner, path, binding, candidates):
    now = owner.clock()
    proposal = _read(path)
    if (proposal.get('binding') != binding or proposal.get('candidates') != candidates
            or not now < proposal['expires_at'] or _receipt_path(path, proposal['code']).exists()):
        proposal = {'binding': binding, 'code': secrets.token_hex(8), 'expires_at': now + WEEK,
                    'candidates': candidates}
        atomic_record(path, proposal)
    return {'code': proposal['code'], 'expires_at': proposal['expires_at'],
            'archives': [{'attempt': c['attempt'], 'bytes': c['bytes']} for c in candidates]}


def _remove(trees, authorize):
    for root, entries in trees.items():
        # Only unlink the measured names. In particular a late directory entry makes rmdir fail.
        for name, (stamp, _) in reversed(entries.items()):
            path = root/name
            if path.resolve(strict=True) != path:
                raise Refused('ARCHIVE_PATH_REDIRECTED')
            current = _stamp(_plain_info(path))
            if stat.S_ISDIR(stamp[3]):
                # Removing children changes their parent's size and timestamp.
                if current[:4] != stamp[:4]:
                    raise Refused('ARCHIVE_CHANGED')
                authorize()
                path.rmdir()
            else:
                if current != stamp:
                    raise Refused('ARCHIVE_CHANGED')
                if getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_READONLY:
                    # Git stores loose objects read-only on Windows. This is a verified single-link
                    # regular file in a claimed directory.
                    authorize()
                    path.chmod(stat.S_IWRITE)
                authorize()
                path.unlink()


def _claim_and_remove(trees, locations, authorize):
    """Rename both trees on their original volumes, then compare the complete pair again."""
    staged = {}
    holders = []
    outcome = 'skipped'
    try:
        for root in trees:
            holder = Path(locations[str(root)])
            holder.mkdir(mode=0o700)
            holders.append(holder)
            if root.resolve(strict=True) != root or _stamp(_plain_info(root)) != trees[root]['.'][0]:
                raise Refused('ARCHIVE_CHANGED')
            authorize()
            staged[root] = root.rename(holder/'a')
        for root, claimed in staged.items():
            if claimed.resolve(strict=True) != claimed or _scan(claimed, contents=True)[0] != trees[root]:
                raise Refused('ARCHIVE_CHANGED')
        authorize()
        outcome = 'incomplete'
        _remove({claimed: trees[root] for root, claimed in staged.items()}, authorize)
        outcome = 'removed'
    except _UNREADABLE:
        pass  # Restore any surviving directories. Never overwrite an arrival at the old name.
    finally:
        for root, claimed in staged.items():
            if os.path.lexists(claimed):
                try:
                    if os.path.lexists(root):
                        raise Refused('ARCHIVE_NAME_OCCUPIED')
                    claimed.rename(root)
                except _UNREADABLE:
                    outcome = 'incomplete'
        for holder in holders:
            try:
                holder.rmdir()
            except OSError:
                outcome = 'incomplete'
    return outcome


def _receipt_path(path, code):
    if not sha(code, 16):
        raise Refused('PRUNE_CODE_INVALID')
    return path.with_name(f'prune-receipt-{code}.json')


def _approve(owner, days, path, binding, code):
    from functools import partial
    authorize = partial(owner._check, owner.token)
    receipt_path = _receipt_path(path, code)
    receipt = _read(receipt_path)
    if receipt.get('binding') == binding:
        return receipt['result']
    proposal = _read(path)
    if proposal.get('binding') != binding or not secrets.compare_digest(code, proposal['code']):
        raise Refused('PRUNE_CODE_INVALID')
    if owner.clock() >= proposal['expires_at']:
        raise Refused('PRUNE_CODE_EXPIRED')
    candidates = proposal['candidates']
    rows = {row['attempt']: row for row in read_launches(owner.journal.path) or ()}
    result = {'removed': [], 'skipped': [], 'incomplete': [c['attempt'] for c in candidates]}
    # Spend the code before the first unlink. A crash or an I/O error cannot replay any deletion.
    receipt = {'binding': binding, 'result': result, 'staging': {}}
    atomic_record(receipt_path, receipt)
    for candidate in candidates:
        authorize()
        attempt = candidate['attempt']
        snapshot = _candidate(owner, rows[attempt], days) if attempt in rows else None
        if not snapshot or snapshot[0] != candidate:
            outcome = 'skipped'
        else:
            trees = snapshot[1]
            locations = {str(root): str(root.with_name('.p-' + secrets.token_hex(8))) for root in trees}
            # Save recovery coordinates before the first rename, including on different volumes.
            receipt['staging'][attempt] = locations
            atomic_record(receipt_path, receipt)
            outcome = _claim_and_remove(trees, locations, authorize)
        if outcome != 'incomplete':
            result['incomplete'].remove(attempt)
            result[outcome].append(attempt)
            receipt['staging'].pop(attempt, None)
        atomic_record(receipt_path, receipt)
    return result


def linux_retention_unavailable(owner):
    from .transport.linux_identity import CONTRACT
    return (owner.scope.namespace == 'symphony_pm:ingress-linux'
            or getattr(owner.journal, 'backend', None) == CONTRACT
            or getattr(getattr(owner, 'native', None), 'ownership_contract', None) == CONTRACT)


def prune(owner, config, code=None):
    if linux_retention_unavailable(owner):
        raise Refused('LINUX_PRUNE_UNAVAILABLE')
    with owner.lock:
        owner._check(owner.token)
        journal = owner.journal.path
        if journal != journal_path(owner.scope.profile_home, owner.scope.namespace):
            raise Refused('ARCHIVE_PROFILE_BINDING')
        days = config.get('patch_retention_days', 30)
        if not finite(days) or not finite(days * DAY) or days < 0:
            raise Refused('PATCH_RETENTION_DAYS')
        binding = digest([owner.scope.profile_home, owner.scope.namespace])
        path = journal.with_name('prune-proposal.json')
        if code is not None:
            return _approve(owner, days, path, binding, code)
    # A weekly read can hash several large workspaces. The supervisor must still accept STOP.
    # A racing recapture simply produces a proposal that cannot pass the approval-time comparison.
    candidates = _collect(owner, days)
    with owner.lock:
        owner._check(owner.token)
        return _propose(owner, path, binding, candidates)


def _weekly_config(ctx, owner):
    from hermes_cli.plugins_loader import _plugin_home_scope
    from .operator_commands import operator_config
    with _plugin_home_scope(Path(owner.scope.profile_home)):
        config = operator_config(ctx)
        if (not all(isinstance(config.get(k), str) and config[k].strip()
                    for k in ('platform', 'user_id', 'chat_id', 'session_key'))
                or not ctx._gateway_injection_allowed()):
            return None
        return config


def _weekly_tick(ctx, owner):
    path = owner.journal.path.with_name('prune-weekly.json')
    with owner.lock:
        owner._check(owner.token)
        now = owner.clock()
        due = _read(path).get('due', 0)
        if now < due:
            return due - now
    config = _weekly_config(ctx, owner)
    if config is None:
        return 3600
    proposal = prune(owner, config)
    with owner.lock:
        owner._check(owner.token)
        now = owner.clock()
        due = _read(path).get('due', 0)
        if now < due:
            return due - now
        if _read(path.with_name('prune-proposal.json')).get('code') != proposal['code']:
            return 1
        archives = proposal['archives']
        lines = [f"Symphony archives: {len(archives)} ({sum(a['bytes'] for a in archives)} bytes)."]
        lines.extend(f"{a['attempt']}  {a['bytes']} bytes" for a in archives)
        if archives:
            lines.append('/symphony prune approve ' + proposal['code'])
        if _weekly_config(ctx, owner) != config:
            return 1
        # The host also returns False when it loses an acknowledgment after scheduling.
        # Spend the slot before crossing that boundary; local policy refusals above spend nothing.
        atomic_record(path, {'due': now + WEEK})
        if not ctx.inject_message('\n'.join(lines), session_key=config['session_key']):
            logging.getLogger(__name__).warning('Symphony archive notice acceptance unconfirmed')
        return WEEK


def start_weekly(ctx, owner, operator_config):
    from .transport.run_supervisor import profile_thread
    if (not isinstance(operator_config.get('session_key'), str) or not operator_config['session_key'].strip()
            or not callable(getattr(ctx, 'inject_message', None))
            or getattr(getattr(ctx, '_manager', None), '_cli_ref', None) is not None):
        return
    stop = threading.Event()

    def run():
        while not stop.is_set():
            delay = 1  # The host opens admission after the service factory returns.
            if owner.scope.active:
                try:
                    delay = _weekly_tick(ctx, owner)
                except (OSError, ValueError, Refused, sqlite3.Error) as exc:
                    logging.getLogger(__name__).warning('Symphony archive notice unavailable (%s)', type(exc).__name__)
                    delay = 3600
            stop.wait(min(WEEK, max(1, delay)))

    thread = profile_thread(owner.scope.profile_home, run, 'symphony-archive-notices')

    def close(deadline):
        stop.set()
        if thread.ident is not None:
            thread.join(max(0, deadline - time.monotonic()))
            if thread.is_alive():
                raise Refused('ARCHIVE_WORKER_CLEANUP_PENDING')

    owner.scope.on_close(close)
    thread.start()
