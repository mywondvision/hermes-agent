"""Windows CR-8. A checked patch has one parent and never replaces the owner's index or files."""
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from threading import Event

import pytest

from hermes_platform.resolver import locate_command
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.test_symphony_pm_codex_exec import _git


@pytest.mark.platforms("windows")
def test_prepare_robot_commit_preserves_worktree_and_creates_no_branch(tmp_path):
    from plugins.symphony_pm.transport.robot_apply_git import check_candidate, prepare_commit

    repo = tmp_path / "project"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    tracked = repo / "value.txt"
    tracked.write_bytes(b"original\n")
    _git("add", "value.txt", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    tracked.write_bytes(b"staged\n")
    _git("add", "value.txt", cwd=repo)
    tracked.write_bytes(b"local\n")
    private = repo / "notes.txt"
    private.write_bytes(b"owner copy\n")
    index = repo / ".git" / "index"
    before = index.read_bytes()
    patch = b"diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+checked\n"
    digest = hashlib.sha256(patch).hexdigest()
    git = Path(locate_command("git").command[0])
    with pytest.raises(Refused, match="PATCH_DIGEST"):
        prepare_commit(git, repo, base, "job-1", patch, "0" * 64)
    commit = prepare_commit(git, repo, base, "job-1", patch, digest)
    assert _git("show", "-s", "--format=%P|%an|%ae", commit, cwd=repo) == f"{base}|Robot|robot@symphony.invalid"
    assert _git("show", f"{commit}:value.txt", cwd=repo) == "checked"
    assert check_candidate(git, repo, base, commit) == {
        "name": "git-diff-check", "source_sha": base, "candidate_sha": commit, "exit_code": 0,
    }
    whitespace = patch.replace(b"+checked\n", b"+checked  \n")
    other = prepare_commit(git, repo, base, "job-1", whitespace, hashlib.sha256(whitespace).hexdigest())
    assert check_candidate(git, repo, base, other)["exit_code"] == 2
    with pytest.raises(Refused, match="CHECK_PARENT"):
        check_candidate(git, repo, other, commit)
    assert _git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=repo) == "refs/heads/main"
    assert _git("rev-parse", "HEAD", cwd=repo) == base
    assert index.read_bytes() == before
    assert tracked.read_bytes() == b"local\n"
    assert private.read_bytes() == b"owner copy\n"


@pytest.mark.platforms("windows")
def test_publish_recovers_same_oid_and_preserves_conflicting_or_symbolic_refs(tmp_path):
    from plugins.symphony_pm.transport.robot_apply_git import publish_prepared

    git = Path(locate_command("git").command[0])
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("commit", "--allow-empty", "-qm", "base", cwd=tmp_path)
    base = _git("rev-parse", "HEAD", cwd=tmp_path)
    _git("commit", "--allow-empty", "-qm", "prepared", cwd=tmp_path)
    prepared = _git("rev-parse", "HEAD", cwd=tmp_path)
    ref = "refs/heads/symphony/fixture"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "reference-transaction").write_bytes(b"#!/bin/sh\nexit 1\n")
    _git("config", "core.hooksPath", str(hooks), cwd=tmp_path)
    assert publish_prepared(git, tmp_path, "fixture", prepared) == ref
    assert publish_prepared(git, tmp_path, "fixture", prepared) == ref
    with pytest.raises(Refused, match="BRANCH_CONFLICT"):
        publish_prepared(git, tmp_path, "fixture", base)
    assert _git("rev-parse", ref, cwd=tmp_path) == prepared
    alias = "refs/heads/symphony/alias"
    _git("-c", "core.hooksPath=", "symbolic-ref", alias, ref, cwd=tmp_path)
    with pytest.raises(Refused, match="BRANCH_CONFLICT"):
        publish_prepared(git, tmp_path, "alias", prepared)
    assert _git("symbolic-ref", alias, cwd=tmp_path) == ref


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("written", [True, False])
def test_ref_timeout_reads_actual_oid_without_another_write(tmp_path, monkeypatch, written):
    from plugins.symphony_pm.transport.robot_apply_git import publish_prepared

    git = Path(locate_command("git").command[0])
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("commit", "--allow-empty", "-qm", "base", cwd=tmp_path)
    oid = _git("rev-parse", "HEAD", cwd=tmp_path)
    ref = "refs/heads/symphony/fixture"
    actual = subprocess.run
    writes = []
    def timeout(argv, *args, **kwargs):
        if "update-ref" in argv:
            writes.append(tuple(argv))
            if written:
                assert actual(argv, *args, **kwargs).returncode == 0
            raise subprocess.TimeoutExpired(argv, 5)
        return actual(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", timeout)
    if written:
        assert publish_prepared(git, tmp_path, "fixture", oid) == ref
        assert publish_prepared(git, tmp_path, "fixture", oid, readback_only=True) == ref
    else:
        with pytest.raises(Refused, match="ROBOT_REF_UPDATE_UNCONFIRMED"):
            publish_prepared(git, tmp_path, "fixture", oid)
        with pytest.raises(Refused, match="ROBOT_REF_ABSENT_ON_RECOVERY"):
            publish_prepared(git, tmp_path, "fixture", oid, readback_only=True)
    assert len(writes) == 1


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("failure", ["none", "shown", "delivery-lost", "session-changed", "setup-failed", "readback-lost"])
def test_owner_message_applies_checked_capture_and_reconciles_after_git_write(tmp_path, monkeypatch, failure):
    from gateway.platforms.event import MessageType
    from gateway.platforms.base import SendResult
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from plugins.symphony_pm.status_cli import read_launches
    from plugins.symphony_pm.transport import robot_apply_git
    from plugins.symphony_pm.transport.attempt_workspace import Result
    from plugins.symphony_pm.transport.codex_exec_check import check_patch
    from plugins.symphony_pm.transport.codex_exec_launcher import AttemptSettings
    from plugins.symphony_pm.transport.owner_check_profile import DIFF_CHECK, CheckCommand, check_plan_fingerprint
    from plugins.symphony_pm.transport.types import digest
    from tests.plugins.symphony_owner_check_support import owner_client
    from tests.plugins.symphony_typed_human_support import FixtureLauncher, human_session

    failed_check = failure in ("shown", "delivery-lost")
    patch = b"--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+checked" + (b"  " if failed_check else b"") + b"\n"
    patch_hash = hashlib.sha256(patch).hexdigest()
    project_test = CheckCommand("project-git", "python", ("-c",
        "import pathlib,subprocess; assert pathlib.Path('new.txt').read_text().strip()=='checked'; "
        "assert subprocess.check_output(['git','show','HEAD:new.txt']).strip()==b'checked'; "
        "assert not subprocess.check_output(['git','remote']).strip(); "
        "assert not pathlib.Path('.git/objects/info/alternates').exists()"), True)
    plan = (CheckCommand("lint", "ruff", ("check",)), project_test, DIFF_CHECK)
    second_ready, second_finished = Event(), Event()
    second_jobs = []

    def launch(launcher, ticket, request, runner):
        launcher.workspace.mkdir(parents=True)
        runner(["fixture", "exec"], cwd=ticket.plan.workspace, timeout=1)
        owner = runner.owner
        if request.request_id.endswith("-second"):
            second_jobs.append(ticket.job)
            second_ready.set()
            assert second_finished.wait(20)
            return dict(exit_code=0)
        owner.observer.stopped = True
        assert owner.finish_client_job(owner.token, ticket)[0]
        directory = owner.journal.path.parent / "attempts" / digest(ticket.attempt)[:12]
        directory.mkdir(parents=True)
        path = directory / "saved.patch"
        path.write_bytes(patch)
        profile = launcher.settings.owner_check
        check = dict(attempt=ticket.attempt, source_sha=ticket.plan.source_sha, patch_sha256=patch_hash, apply=0,
                     commands={"lint": 0}, deferred=[project_test.name, DIFF_CHECK.name], ok=False, reason="CHECK_GIT_TESTS_DEFERRED",
                     revisions=profile.revisions(), plan_sha256=check_plan_fingerprint(profile, plan))
        launcher.calls.append((ticket, path))
        launcher.called.set()
        return dict(exit_code=0, stop=None, check=check, patch=dict(sha256=patch_hash, paths=["new.txt"], path=str(path)))

    monkeypatch.setattr(FixtureLauncher, "__call__", launch)
    monkeypatch.setattr(FixtureLauncher, "job_coverage_for", lambda self, guard: True, raising=False)
    with human_session(tmp_path) as channel:
        launcher = channel.launcher
        attempts = tmp_path / "attempts"
        launcher.settings = AttemptSettings(str(launcher.repo), str(attempts), "fixture", lambda _: plan, {})
        recorded = Event()
        journal = channel.owner.journal
        original_record = journal.record_result
        def record_result(*args, **kwargs):
            result = original_record(*args, **kwargs)
            recorded.set()
            return result
        monkeypatch.setattr(journal, "record_result", record_result)
        real_run = subprocess.run
        with owner_client(SimpleNamespace(attempts=attempts, launcher=launcher), plan=lambda _: plan) as (_, profile):
            # This stand runs only the fixture's fixed Python check in its exported Git repository.
            sandbox_calls = []
            project_in_client, release_client = Event(), Event()
            def client(argv, *args, **kwargs):
                if list(argv[:2]) == [str(profile.client), "sandbox"]:
                    sandbox_calls.append(tuple(argv))
                    if failure == "none" and list(argv[-2:]) == ["rev-parse", "HEAD"]:
                        project_in_client.set()
                        assert release_client.wait(15)
                    argv = argv[argv.index("--") + 1:]
                return real_run(argv, *args, **kwargs)
            monkeypatch.setattr(subprocess, "run", client)
            marker = tmp_path / "filter-invoked"
            filter_script = tmp_path / "checkout_filter.py"
            filter_script.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).touch()\n"
                                     "sys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
            (launcher.repo/".git/info/attributes").write_text("new.txt filter=sentinel\n")
            cmd = " ".join(f'"{Path(item).as_posix()}"' for item in (sys.executable, filter_script, marker))
            _git("config", "filter.sentinel.smudge", cmd, cwd=launcher.repo)
            blob = _git("hash-object", "-w", str(filter_script), cwd=launcher.repo)
            _git("cat-file", "--filters", "--path=new.txt", blob, cwd=launcher.repo)
            assert marker.is_file()
            marker.unlink()
            check_calls = []
            checks = robot_apply_git.run_checks
            def counted(*args):
                check_calls.append(tuple(command.name for command in args[4]))
                return checks(*args)
            monkeypatch.setattr(robot_apply_git, "run_checks", counted)
            original = launcher.typed_plan()
            monkeypatch.setattr(launcher, "typed_plan", lambda: dict(original, check_policy_sha256=check_plan_fingerprint(profile, plan)))
            state, shown = channel.request("approve")
            assert state == "WAITING_FOR_AUTHORITY"
            approve, = re.findall(r"/symphony approve [a-f0-9]+", shown)
            assert channel.deliver(approve).startswith("APPROVED")
            assert channel.retry("approve") == "SUBMITTED"
            ticket, path = launcher.calls[0]
            job = ticket.job
            assert recorded.wait(10), "The checked capture was not committed"
            command = f"/symphony apply {job} {patch_hash}"
            ref = f"refs/heads/symphony/{job}"
            assert channel.deliver(command) == "ROBOT_PROVEN_STOP_REQUIRED"
            launcher.workspace.parent.rename(attempts / ticket.attempt)
            journal.workspace_settled(channel.owner.token, ticket)
            assert channel.deliver(command, message_type=MessageType.VOICE) == "ORIGINAL_TYPED_COMMAND_REQUIRED"
            assert channel.deliver(command, source=replace(channel.source, user_id="stranger")) == "OWNER_SENDER_REQUIRED"
            assert not any(obs["kind"] == "APPLY" for row in read_launches(journal.path) for obs in row["observations"])
            path.write_bytes(patch + b"changed")
            assert channel.deliver(command) == "ROBOT_PATCH_DIGEST"
            path.write_bytes(patch)
            assert channel.deliver(command+" --accept-failed-git-checks") == "ROBOT_PRESENTED_FAILURE_AND_FULL_DIGEST_REQUIRED"
            assert check_calls == [] and sandbox_calls == []
            if failure == "setup-failed":
                def no_export(*args):
                    raise Refused("FIXTURE_EXPORT_UNAVAILABLE")
                monkeypatch.setattr(robot_apply_git, "_export_commit", no_export)
                assert channel.deliver(command) == "ROBOT_CHECK_INCOMPLETE"
                assert not profile.active.exists() and not sandbox_calls
                assert not any(obs["kind"] == "APPLY" for row in read_launches(journal.path) for obs in row["observations"])
                assert _git("for-each-ref", "--format=%(refname)", ref, cwd=launcher.repo) == ""
                return
            if failure == "session-changed":
                publish = robot_apply_git.publish_prepared
                def rotated(*args, **kwargs):
                    channel.entry.session_id = "changed-before-ref"
                    return publish(*args, **kwargs)
                monkeypatch.setattr(robot_apply_git, "publish_prepared", rotated)
                assert channel.deliver(command) == "TYPED_OWNER_ROUTE_REQUIRED"
                assert _git("for-each-ref", "--format=%(refname)", ref, cwd=launcher.repo) == ""
                assert len(check_calls) == 1 and not marker.exists()
                return
            if failure == "delivery-lost":
                send = channel.adapter.send
                presentations = []
                async def lost(chat_id, content, *args, **kwargs):
                    if "Git check failed" in content:
                        presentations.append(content)
                        await send(chat_id, content, *args, **kwargs)
                        return SendResult(success=False, error="fixture delivery unknown")
                    return await send(chat_id, content, *args, **kwargs)
                monkeypatch.setattr(channel.adapter, "send", lost)
                assert channel.deliver(command) == "TYPED_PLAN_DELIVERY_FAILED"
                assert json.loads(channel.deliver(command))["state"] == "CHECK_FAILED"
                assert channel.deliver(command+" --accept-failed-git-checks") == "ROBOT_PRESENTED_FAILURE_AND_FULL_DIGEST_REQUIRED"
                assert len(check_calls) == 1 and len(sandbox_calls) == 2 and not marker.exists()
                assert _git("for-each-ref", "--format=%(refname)", ref, cwd=launcher.repo) == ""
                assert len(presentations) == 1
                monkeypatch.setattr(channel.adapter, "send", send)
                channel.entry.session_id = "fresh-owner-session"
                redisplay = command + " --show-failed-git-checks"
                write = journal.append_observation
                def lost_receipt(token, ticket, body, **kwargs):
                    if body.get("state") == "CHECK_FAILED_SHOWN":
                        raise Refused("FIXTURE_LOST_PRESENTATION_RECEIPT")
                    return write(token, ticket, body, **kwargs)
                with monkeypatch.context() as cut:
                    cut.setattr(journal, "append_observation", lost_receipt)
                    assert channel.deliver(redisplay, message_id="1500") == "FIXTURE_LOST_PRESENTATION_RECEIPT"
                before = len(channel.adapter.sent)
                assert channel.deliver(redisplay, message_id="1500") == "ROBOT_NEW_PRESENTATION_REQUEST_REQUIRED"
                assert len(channel.adapter.sent) == before
                failed = json.loads(channel.deliver(redisplay))
                assert failed["state"] == "CHECK_FAILED_SHOWN"
                assert failed["presentation"]["session_id"] == "fresh-owner-session"
                assert _git("for-each-ref", "--format=%(refname)", ref, cwd=launcher.repo) == ""
                assert len(check_calls) == 1 and len(sandbox_calls) == 2
                command += " --accept-failed-git-checks"
            elif failed_check:
                failed = json.loads(channel.deliver(command))
                assert failed["state"] == "CHECK_FAILED_SHOWN" and failed["git_checks"][-1]["exit_code"] == 2
                assert any("git-diff-check (exit 2)" in message for message in channel.adapter.sent)
                assert _git("for-each-ref", "--format=%(refname)", ref, cwd=launcher.repo) == ""
                command += " --accept-failed-git-checks"
                assert channel.deliver(command, message_id="1") == "ROBOT_PRESENTED_FAILURE_AND_FULL_DIGEST_REQUIRED"
            append = journal.append_observation
            def lose_final(token, ticket, body, **kwargs):
                if body.get("state") == "APPLIED":
                    raise Refused("FIXTURE_INTERRUPTED_AFTER_REF")
                return append(token, ticket, body, **kwargs)
            with monkeypatch.context() as cut:
                cut.setattr(journal, "append_observation", lose_final)
                if failure == "none":
                    channel.owner.observer.stopped = False
                    state, shown = channel.request("second")
                    assert state == "WAITING_FOR_AUTHORITY"
                    approve_second, = re.findall(r"/symphony approve [a-f0-9]+", shown)
                    assert channel.deliver(approve_second).startswith("APPROVED")
                    assert channel.retry("second") == "SUBMITTED" and second_ready.wait(5)
                    entered, release = Event(), Event()
                    def slow_check(*args):
                        entered.set()
                        assert release.wait(15)
                        return counted(*args)
                    cut.setattr(robot_apply_git, "run_checks", slow_check)
                    with ThreadPoolExecutor(max_workers=2) as caller:
                        future = caller.submit(copy_context().run, channel.deliver, command)
                        try:
                            assert entered.wait(10)
                            assert "view" in json.loads(channel.deliver(f"/symphony status {job}", timeout=5))
                            stop = json.loads(channel.deliver(f"/symphony stop {second_jobs[0]}", timeout=5))
                            assert channel.owner.native.created[-1].cancels == 1, stop
                        finally:
                            release.set()
                            channel.owner.observer.stopped = True
                            second_finished.set()
                        assert project_in_client.wait(10)
                        assert (profile.active / "src/.git").is_dir()
                        directory = tmp_path / "concurrent-check"
                        directory.mkdir()
                        ordinary = CheckCommand("read", "python", ("-c", "from pathlib import Path; assert Path('new.txt').is_file()"))
                        settings = replace(launcher.settings, checks=lambda _: (ordinary,), owner_check=replace(profile),
                                           check_environment=os.environ)
                        waiting = Event()
                        def ordinary_check():
                            waiting.set()
                            return check_patch(settings, str(profile.git), directory, Result(path, patch_hash, ("new.txt",), True),
                                               ticket.plan.source_sha, "fixture-other-attempt")
                        concurrent = caller.submit(ordinary_check)
                        try:
                            assert waiting.wait(5)
                        finally:
                            release_client.set()
                        assert future.result() == "FIXTURE_INTERRUPTED_AFTER_REF"
                        assert concurrent.result()["commands"] == {"read": 0}
                elif failure == "readback-lost":
                    updates = []
                    def lose_read(argv, *args, **kwargs):
                        if "update-ref" in argv and ref in argv:
                            updates.append(tuple(argv))
                            assert client(argv, *args, **kwargs).returncode == 0
                            raise subprocess.TimeoutExpired(argv, 5)
                        if updates and ref in argv and "symbolic-ref" in argv:
                            raise OSError("fixture readback unavailable")
                        return client(argv, *args, **kwargs)
                    cut.setattr(subprocess, "run", lose_read)
                    assert channel.deliver(command) == "ROBOT_REF_OUTCOME_UNKNOWN"
                    assert channel.deliver(command) == "ROBOT_REF_OUTCOME_UNKNOWN"
                    assert json.loads(channel.deliver(f"/symphony status {job} --saved"))["apply"]["state"] == "PUBLISHING"
                    assert len(updates) == 1
                else:
                    assert channel.deliver(command) == "FIXTURE_INTERRUPTED_AFTER_REF"
            assert check_calls == [(project_test.name, DIFF_CHECK.name)] and len(sandbox_calls) == (3 if failure == "none" else 2)
            assert not marker.exists()
            oid = _git("rev-parse", ref, cwd=launcher.repo)
            path.unlink()  # Published Git objects survive retention of the attempt archive.
            journal.append_observation(channel.owner.token, ticket, {
                "kind": "CAPTURE", "source_sha": ticket.plan.source_sha,
                "patch": {"sha256": "1" * 64, "paths": ["another.txt"]},
            })
    with human_session(tmp_path, restart=True) as recovered:
        assert not hasattr(recovered.launcher, "settings")
        with ThreadPoolExecutor(max_workers=2) as callers:
            calls = [callers.submit(copy_context().run, recovered.deliver, command) for _ in range(2)]
            replies = [call.result() for call in calls]
        finished = json.loads(replies[0])
        assert json.loads(replies[1]) == finished
        assert finished["commit"] == oid and finished["state"] == "APPLIED"
        assert finished["accepted_failed_git_checks"] is failed_check
        assert json.loads(recovered.deliver(f"/symphony status {job} --saved"))["apply"] == finished
        rows = read_launches(recovered.owner.journal.path)
        states = [obs["body"]["state"] for row in rows for obs in row["observations"] if obs["kind"] == "APPLY"]
        failures = ["CHECK_FAILED", "CHECK_FAILED_SHOWN"] if failed_check else []
        if failure == "delivery-lost":
            failures = ["CHECK_FAILED", "CHECK_FAILED", "CHECK_FAILED", "CHECK_FAILED_SHOWN"]
        assert states == ["CHECKING", *failures, "PUBLISHING", "APPLIED"]
