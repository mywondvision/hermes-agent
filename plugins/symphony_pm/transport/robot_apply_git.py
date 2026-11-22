"""Build a candidate commit in the configured repository without touching its index or refs."""
import hashlib
from contextlib import nullcontext
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from .attempt_workspace import EMPTY_TREE, GIT_TIMEOUT, PATCH_LIMIT, _clean_environment, require_patch_policy
from .types import Refused, identifier, sha
from .owner_check_profile import DIFF_CHECK, PROFILE


def _run(git, cwd, environment, *arguments, data=None):
    result = subprocess.run(
        [git, *arguments], cwd=cwd, env=environment, input=data, capture_output=True, timeout=GIT_TIMEOUT, check=False,
    )
    if result.returncode:
        raise Refused("ROBOT_GIT_FAILED")
    return result.stdout


def check_candidate(git, repository, source_sha, commit):
    """Run the owner's CR-8 A3 diff check on exact OIDs using a private Git directory."""
    if not sha(source_sha, 40) or not sha(commit, 40):
        raise Refused("ROBOT_CHECK_IDENTITY")
    env = _clean_environment(GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1")
    with TemporaryDirectory(prefix="symphony-diff-check-") as temporary:
        root = Path(temporary)
        private = root / "checker.git"
        _run(git, root, env, "init", "--bare", "--template=", "-q", str(private))
        objects = _run(git, repository, env, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        env.update(GIT_DIR=str(private), GIT_OBJECT_DIRECTORY=objects.decode("utf-8").rstrip("\r\n"))
        parent = _run(git, root, env, "show", "--no-patch", "--format=%P", commit).decode("ascii").strip()
        if parent != source_sha:
            raise Refused("ROBOT_CHECK_PARENT")
        run = subprocess.run([git, "diff", "--no-ext-diff", "--no-textconv", "--check", source_sha, commit, "--"],
                             cwd=root, env=env, capture_output=True, timeout=GIT_TIMEOUT, check=False)
        if run.returncode not in (0, 2):
            raise Refused("ROBOT_CHECK_UNAVAILABLE")
        return {"name": "git-diff-check", "source_sha": source_sha, "candidate_sha": commit,
                "exit_code": run.returncode}


def _export_commit(git, repository, source_sha, commit, root):
    """Copy Git objects into an independent clone. A project test gets no alternates or origin URL."""
    env = _clean_environment(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1")
    clone = root / "src"
    clone.mkdir()
    _run(git, clone, env, "init", "--template=", "-q")
    objects = _run(git, repository, env, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    reader = dict(env, GIT_DIR=str(clone/".git"), GIT_OBJECT_DIRECTORY=objects.decode().rstrip("\r\n"))
    # The candidate and its source are sufficient for the approved check, including a shallow source.
    parent = _run(git, clone, reader, "show", "--no-patch", "--format=%P", commit).strip()
    if parent != source_sha.encode("ascii"):
        raise Refused("ROBOT_CHECK_PARENT")
    (clone / ".git" / "shallow").write_bytes(parent + b"\n")
    with (root/"objects.pack").open("x+b") as pack:
        subprocess.run([git, "pack-objects", "--stdout", "--revs"], env=reader, cwd=clone,
                       input=(commit+"\n").encode("ascii"), stdout=pack, check=True, timeout=GIT_TIMEOUT)
        pack.seek(0)
        subprocess.run([git, "index-pack", "--stdin"], env=env, cwd=clone, stdin=pack, stdout=subprocess.DEVNULL,
                       check=True, timeout=GIT_TIMEOUT)
    (root/"objects.pack").unlink()
    _run(git, clone, env, "update-ref", "--no-deref", "HEAD", commit)
    _run(git, clone, dict(env, GIT_ATTR_SOURCE=EMPTY_TREE), "-c", "core.autocrlf=false",
         "-c", "core.symlinks=false", "-c", "core.longpaths=true", "read-tree", "--reset", "-u", commit)
    return clone


def _project_check(profile, repository, source_sha, commit, command, timeout, authorize, starting):
    """The qualified client runs only the fixed tool and argv already bound in the CHECK plan."""
    profile.active.mkdir()  # The qualified workspace has one writer. A previous uncertain call retains it.
    settled = True
    try:
        (profile.active/"temp").mkdir()
        clone = _export_commit(profile.git, repository, source_sha, commit, profile.active)
        program = {"python": profile.python, "ruff": profile.ruff}[command.tool]
        wrapper = ("import os,subprocess,sys; os.chdir(sys.argv[1]); "
                   "os.environ['PATH']=sys.argv[2]+os.pathsep+os.environ.get('PATH',''); "
                   "os.environ.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT='0'); ")
        argv = [str(profile.client), "sandbox", "-C", str(profile.anchor), "-P", PROFILE, "--", str(profile.python),
                "-I", "-B", "-c"]
        args = [str(clone), str(profile.git.parent)]
        environment = profile.environment(os.environ)
        authorize()
        # A missing Git runtime or inaccessible private clone is a setup failure, not an overridable test result.
        settled = False
        probe = subprocess.run([*argv, wrapper+"raise SystemExit(subprocess.call(sys.argv[3:]))", *args,
                                str(profile.git), "rev-parse", "HEAD"], cwd=profile.anchor, env=environment,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
        settled = True
        if probe.returncode or probe.stdout.strip() != commit.encode("ascii"):
            raise Refused("ROBOT_GIT_TEST_ENVIRONMENT_UNAVAILABLE")
        authorize()
        report = ("print('ROBOT_CHECK_EXIT',subprocess.call(sys.argv[3:],stdout=subprocess.DEVNULL,"
                  "stderr=subprocess.DEVNULL),flush=True)")
        starting()
        settled = False
        result = subprocess.run([*argv, wrapper+report, *args, str(program), *command.arguments],
                                cwd=profile.anchor, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, check=False)
        match = re.fullmatch(rb'ROBOT_CHECK_EXIT (-?\d+)\s*', result.stdout)
        if result.returncode or not match:
            raise Refused("ROBOT_CHECK_OUTCOME_UNKNOWN")
        settled = True
        return int(match[1])
    except subprocess.TimeoutExpired:
        settled = False
        raise
    finally:
        if settled:
            profile.active.rename(profile.root/("checked-apply-"+uuid4().hex))


def run_checks(profile, repository, source_sha, commit, commands, timeout, authorize, starting, project_check=None):
    results = []
    for command in commands:
        authorize()
        if command == DIFF_CHECK:
            starting()
            results.append(check_candidate(profile.git, repository, source_sha, commit))
        elif project_check is not None:
            results.append(project_check(command, starting, tuple(results)))
        else:
            code = _project_check(profile, repository, source_sha, commit, command, timeout, authorize, starting)
            results.append(dict(name=command.name, source_sha=source_sha, candidate_sha=commit, exit_code=code))
    return results


def prepare_commit(git, repository, source_sha, job, patch, patch_sha256):
    if not sha(source_sha, 40) or not identifier(job):
        raise Refused("ROBOT_TICKET_IDENTITY")
    if not isinstance(patch, bytes) or not patch or len(patch) > PATCH_LIMIT:
        raise Refused("ROBOT_PATCH_SIZE")
    if hashlib.sha256(patch).hexdigest() != patch_sha256:
        raise Refused("ROBOT_PATCH_DIGEST")

    env = _clean_environment(GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1")
    objects = Path(_run(git, repository, env, "rev-parse", "--path-format=absolute",
                        "--git-path", "objects").decode("utf-8").rstrip("\r\n"))
    if not objects.is_absolute() or not objects.is_dir():
        raise Refused("ROBOT_OBJECT_DIRECTORY")
    with TemporaryDirectory(prefix="symphony-apply-") as temporary:
        root = Path(temporary)
        private_git = root / "private.git"
        _run(git, root, env, "init", "--bare", "--template=", "-q", str(private_git))
        env.update(GIT_DIR=str(private_git), GIT_INDEX_FILE=str(root / "index"),
                   GIT_OBJECT_DIRECTORY=str(objects), GIT_ATTR_SOURCE=EMPTY_TREE,
                   GIT_AUTHOR_NAME="Robot", GIT_COMMITTER_NAME="Robot",
                   GIT_AUTHOR_EMAIL="robot@symphony.invalid", GIT_COMMITTER_EMAIL="robot@symphony.invalid")
        if _run(git, root, env, "cat-file", "-t", source_sha).strip() != b"commit":
            raise Refused("ROBOT_SOURCE_COMMIT")
        _run(git, root, env, "read-tree", source_sha)
        _run(git, root, env, "apply", "--cached", "--binary", "--whitespace=nowarn", "-", data=patch)
        names = _run(git, root, env, "diff", "--cached", "--no-ext-diff", "--no-renames", "--name-only",
                     "-z", source_sha, "--")
        if not names:
            raise Refused("ROBOT_EMPTY_PATCH")
        require_patch_policy(patch, names.decode("utf-8").rstrip("\0").split("\0"))
        tree = _run(git, root, env, "write-tree").decode("ascii").strip()
        message = f"Apply Symphony job {job}\n\nPatch SHA-256: {patch_sha256}\n"
        return _run(git, root, env, "commit-tree", tree, "-p", source_sha,
                    data=message.encode("utf-8")).decode("ascii").strip()


def publish_prepared(git, repository, job, commit, *, readback_only=False, publication_guard=nullcontext):
    """The caller has durably recorded this exact OID and authorized publication."""
    try:
        return _publish_ref(git, repository, job, commit, readback_only, publication_guard)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("ROBOT_REF_OUTCOME_UNKNOWN") from exc
    except Refused as exc:
        if str(exc) in ('ROBOT_GIT_FAILED', 'ROBOT_REF_READBACK'):
            raise Refused("ROBOT_REF_OUTCOME_UNKNOWN") from exc
        raise


def require_absent_ref(git, repository, job):
    """Prove the archive decision leaves no corresponding direct or symbolic Git ref."""
    if not identifier(job):
        raise Refused('ROBOT_TICKET_IDENTITY')
    ref = f'refs/heads/symphony/{job}'
    env = _clean_environment(GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1')
    for args, absent_codes in ((('symbolic-ref', '-q', ref), (1, 128)),
                               (('show-ref', '--verify', '--quiet', ref), (1,))):
        result = subprocess.run([git, *args], cwd=repository, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=GIT_TIMEOUT, check=False)
        if result.returncode not in absent_codes:
            raise Refused('ROBOT_ARCHIVE_REF_UNCONFIRMED')


def _publish_ref(git, repository, job, commit, readback_only, publication_guard):
    if not identifier(job) or not sha(commit, 40):
        raise Refused("ROBOT_TICKET_IDENTITY")
    ref = f"refs/heads/symphony/{job}"
    env = _clean_environment(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    with TemporaryDirectory(prefix="symphony-no-hooks-") as empty:
        options = ("-c", f"core.hooksPath={empty}", "-c", "core.fsmonitor=false")
        _run(git, repository, env, *options, "check-ref-format", ref)
        worktrees = _run(git, repository, env, *options, "worktree", "list", "--porcelain", "-z")
        if not readback_only and f"branch {ref}".encode("ascii") in worktrees.split(b"\0"):
            raise Refused("ROBOT_BRANCH_CHECKED_OUT")
        # A symbolic ref must never count as a recovered direct ref or be followed by the write.
        def read_ref():
            symbolic_ref = subprocess.run([git, *options, "symbolic-ref", "-q", ref],
                                          cwd=repository, env=env, capture_output=True, timeout=GIT_TIMEOUT, check=False)
            if symbolic_ref.returncode == 0:
                raise Refused("ROBOT_BRANCH_CONFLICT")
            if symbolic_ref.returncode not in (1, 128):
                raise Refused("ROBOT_REF_READBACK")
            raw = _run(git, repository, env, *options, "for-each-ref",
                       "--format=%(refname)%00%(objectname)%00%(symref)", "--", ref)
            for line in raw.splitlines():
                name, oid, symbolic = line.split(b"\0")
                if name == ref.encode("ascii"):
                    if symbolic or oid.decode("ascii") != commit:
                        raise Refused("ROBOT_BRANCH_CONFLICT")
                    return True
            return False

        if not read_ref():
            if readback_only:
                raise Refused("ROBOT_REF_ABSENT_ON_RECOVERY")
            # Even if a concurrent writer created the ref since read_ref(), Git's old-value
            # check prevents replacement. Read the ref after any uncertain update.
            try:
                with publication_guard():
                    result = subprocess.run([git, *options, "update-ref", "--no-deref", ref, commit, "0" * 40],
                                            cwd=repository, env=env, capture_output=True, timeout=5, check=False)
            except subprocess.TimeoutExpired as exc:
                if read_ref():
                    return ref
                raise Refused("ROBOT_REF_UPDATE_UNCONFIRMED") from exc
            if not read_ref():
                raise Refused("ROBOT_REF_UPDATE_UNCONFIRMED" if result.returncode else "ROBOT_REF_READBACK")
    return ref
