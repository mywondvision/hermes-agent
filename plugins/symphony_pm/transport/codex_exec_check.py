"""Owner-side patch capture and validation in a fresh checkout of the source."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import uuid

from . import attempt_workspace as ws
from .owner_check_profile import OwnerCheckProfile, PROFILE, check_plan_fingerprint
from .types import Refused


def clean_artifacts(src):
    """Interpreter, pytest and PowerShell caches are by-products of running things, never part of a patch."""
    removed = []
    stray = src/'Microsoft'/'Windows'/'PowerShell'
    if stray.is_dir():
        shutil.rmtree(stray)
        removed.append('Microsoft/Windows/PowerShell')
        for parent in (src/'Microsoft'/'Windows', src/'Microsoft'):
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
    for name in ('__pycache__', '.pytest_cache'):
        for path in sorted(src.rglob(name), reverse=True):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed.append(path.relative_to(src).as_posix())
    return removed


def capture_result(settings, git, capture, workspace, data, source_sha, *, verify=True):
    cleaned = clean_artifacts(workspace.src)
    try:
        result = capture.result()
    except Refused as exc:
        return {'cleaned': cleaned, 'patch': {'refused': str(exc)}}
    patch = {'path': str(result.patch), 'sha256': result.sha256, 'paths': list(result.paths)}
    if not verify:
        return {'cleaned': cleaned, 'patch': patch, 'check': None, 'check_skipped': 'ATTEMPT_INTERRUPTED'}
    check = check_patch(settings, git, data, result, source_sha, workspace.attempt) if result.paths else None
    return {'cleaned': cleaned, 'patch': patch, 'check': check}


# The client's working directory stays owner-controlled for every command. Only the sandboxed
# interpreter enters the patched tree. Its own imports start in isolated mode.
_RUN_IN_COPY = ('import os,subprocess,sys; os.chdir(sys.argv[1]); '
                'raise SystemExit(subprocess.call(sys.argv[2:]))')


def _run_commands(settings, profile, src, data, paths, environment, outcome):
    commands = tuple(settings.checks(paths))
    outcome['plan_sha256'] = check_plan_fingerprint(profile, commands)
    outcome['deferred'] = [c.name for c in commands if c.requires_git]
    for command in commands:
        if command.requires_git:
            continue
        program = profile.python if command.tool == 'python' else profile.ruff
        argv = [str(profile.client), 'sandbox', '-C', str(profile.anchor), '-P', PROFILE, '--',
                str(profile.python), '-I', '-B', '-c', _RUN_IN_COPY, str(src), str(program), *command.arguments]
        with (data/f'check-{command.name}.log').open('xb') as log:
            done = subprocess.run(argv, cwd=profile.anchor, env=environment, stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=settings.check_timeout, check=False)
        outcome['commands'][command.name] = done.returncode
        if done.returncode:
            raise Refused('CHECK_COMMAND_FAILED')
    if not outcome['commands']:
        raise Refused('CHECK_NO_COMMANDS_EXECUTED')
    if outcome['deferred']:
        raise Refused('CHECK_GIT_TESTS_DEFERRED')


def check_patch(settings, git, data, result, source_sha, attempt):
    """Check the exact capture in a fresh MXC-writable tree; all failures retain the original patch."""
    outcome = {'attempt': attempt, 'patch_sha256': result.sha256, 'source_sha': source_sha, 'apply': None, 'commands': {},
               'deferred': [], 'ok': False}
    profile = settings.owner_check
    try:
        if not isinstance(profile, OwnerCheckProfile):
            raise Refused('CHECK_PROFILE_MISSING')
        outside = [Path(p).resolve() for p in (settings.repository, settings.attempts_root, data)]
        with profile.qualified(git, outside=outside):
            outcome['revisions'] = profile.revisions()
            environment = profile.environment(settings.check_environment)
            patch = result.patch.read_bytes()
            if not result.baseline_matches_source or hashlib.sha256(patch).hexdigest() != result.sha256:
                raise Refused('CHECK_PATCH_CHANGED')
            ws.require_patch_policy(patch, result.paths)
            profile.active.mkdir()
            observed = True
            try:
                src = profile.active/'src'
                src.mkdir()
                (profile.active/'temp').mkdir()
                ws.export(git, settings.repository, source_sha, src)
                applied = subprocess.run([str(profile.git), 'apply', '--binary', '-'], cwd=src, input=patch,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                                         timeout=ws.GIT_TIMEOUT,
                                         env=ws._clean_environment(GIT_CEILING_DIRECTORIES=str(profile.active)))
                outcome['apply'] = applied.returncode
                if applied.returncode:
                    raise Refused('CHECK_APPLY_FAILED')
                _run_commands(settings, profile, src, data, tuple(result.paths), environment, outcome)
            except subprocess.TimeoutExpired:
                observed = False
                raise
            finally:
                # A failed rename leaves active occupied, so a later check cannot reuse uncertain state.
                if observed:
                    profile.active.rename(profile.root/('checked-'+uuid.uuid4().hex))
        outcome['ok'] = True
    except Refused as exc:
        outcome['reason'] = str(exc)
    except subprocess.TimeoutExpired:
        outcome['reason'] = 'CHECK_TIMEOUT'
    except Exception as exc:  # health: allow BLE001 -- a capture needs a durable CHECK even on client/setup failure
        outcome['reason'] = 'CHECK_FAILED_'+type(exc).__name__
    return outcome
