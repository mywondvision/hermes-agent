# Continue Hermes PM ingress in the existing worktree

This handoff is for an interactive Claude Code session already opened in the user's existing Git worktree. Inspect that worktree and its current work before planning how to use the published unified candidate. Do not create a replacement checkout or assume the local branch is the published branch.

## Published reference

- Repository: https://github.com/mywondvision/hermes-agent
- Unified branch: `hermes-pm-ingress-unified-20261106`.
- Exact code commit: `43dc3391177a78cfa546d344d8297c7e4d78c005`.
- Parent: `7ee792f232cc508482c53f81f45bf25e0b1aaec2`.
- The earlier `hermes-pm-ingress-h1h2-candidate` branch was left unchanged.
- The unified commit changes exactly two existing files, 877 insertions and 310 deletions.
- Source commit: https://github.com/mywondvision/hermes-agent/commit/43dc3391177a78cfa546d344d8297c7e4d78c005

Exact final bytes:

| Path | SHA-256 | Git blob |
|---|---|---|
| tools/symphony_pm_adapter.py | 0d05bd472afad912ca206a8782156134c36deabc88b5e764eb98f2abb27c4556 | 21e16a27f4d0c4a6eb87f74d4a7c623e9a2a41e7 |
| tests/tools/test_symphony_pm_adapter.py | c4519a1b8fe62b9d227449b12e7f87121c5a3deba7fcd0e459ee086559a1213d | fc65b528ffc9bf900c2fff83dc7a384747d126ed |

The published files already contain the unified change. Do not stack the earlier candidate, hardening or replacement patches on top.

## First turn

1. Report the actual working directory, Git top level, registered worktree entry, branch, HEAD and relevant remote names. Avoid printing credentials or credential-bearing URLs.
2. Read the applicable AGENTS.md and CLAUDE.md instructions, including area instructions before editing. Inspect relevant local task documents and existing changes. Do not assume this worktree has the same layout as the public Hermes fork.
3. Show tracked and untracked work relevant to the task. Preserve all of it. A dirty worktree is expected to be reviewed, not cleaned.
4. Locate the adapter, tests, scheduler/context-provider interfaces, frozen-S2 inputs and prior integration material. Identify what is actually present and what is missing.
5. Compare local source with the exact published code commit. If the source is not locally available, explain the minimum read-only retrieval needed. Do not change the current branch or replace files to make identities match.
6. Present the smallest offline integration plan and the checks needed. Finish with a clear next decision and report in Russian.

The initial session is in plan mode. Do not clone a new project, add a worktree, switch branches, reset, stash, clean, cherry-pick or apply a patch automatically. Do not start another writer in the same worktree. Do not merge, push, deploy, activate the runtime, launch native workers or call paid APIs as part of this preflight. Ask before actions that need further authorization.

## Behavior to preserve

- Tool arguments cannot create authority. Trusted context is supplied separately from a closed-schema intent.
- One explicit request causes at most one scheduler-port invocation. Unknown outcomes do not cause an automatic retry.
- Receipt fields are checked before comparison, hashing, projection and serialization.
- The verifier uses independent snapshots of context, evidence and receipts. Caller or port mutation cannot alter the decision or original request binding.
- String subclasses normalize to ordinary strings without trusting overridden equality, hash, length or encode behavior.
- Non-finite and overflowing timestamps fail closed without escaping exceptions.
- Malformed trusted evidence causes an all-or-nothing refusal before invoking the port. This policy is intentional; do not silently replace it with best-effort filtering.
- Structurally valid but unmatched evidence remains non-authoritative without rejecting valid unrelated evidence.
- Successful projections remain JSON-serializable and preserve request identity and payload digest.
- Imports and tool registration remain inert until an explicitly supplied backend and context are available.

## Evidence and remaining gates

The exact unified files previously passed 35 of 35 focused offline tests under pytest 9.1.1 on Linux/Python 3.12.14, with no skips or failures. This was the complete adapter test module, not the complete Hermes suite.

That run used a reconstructed S2 fixture, not a historical frozen snapshot. It imported the real registry class for signature checks; scheduler/native behavior and registration dispatch used the existing fake ports. No live runtime was wired or started.

Publication rechecked source identity, patch application and remote file bytes. Repository-wide CI, supported-runtime execution and live integration were not established by that check.

The public repository requires Python >=3.14,<3.15 and its test wrapper:
- `scripts/run_tests.sh tests/tools/test_symphony_pm_adapter.py -q`
- `python scripts/check`
- Relevant wider groups and eventually `scripts/run_tests.sh` for the full suite.

Use those commands only after the actual checkout, isolated environment and required fixture are ready under the local repository instructions. Do not mutate a managed environment with raw pip or uv. Do not mistake the previous Python 3.12 gate for a Python 3.14 or repository-runner pass.

The adapter tests reference `.artifacts/frozen-s2/snapshot-manifest.json` and matching source files. These were not added to the two-file code commit. Check whether the current worktree already has the correct authorized inputs. If absent, name the missing artifact; do not fabricate it, stub it, skip its tests or publish it merely to make CI green.

Separate passed, failed, blocked and not-run checks. Include exact commands and file/line evidence in the final report. Do not claim deployment readiness from offline source-contract tests.
