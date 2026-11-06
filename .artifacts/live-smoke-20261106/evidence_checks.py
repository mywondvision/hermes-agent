"""Validate immutable published evidence, including nonempty sets and cross-file bindings."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import subprocess

PUBLISHED_SHA = "ebcc60d2644df20bf2d8ca70735b29d8da3ee78e"
PUBLICATION_HASH = "a154947f3b6f10f88f7b3765d496400801d68e42baf1d2048b8c08be3055cb74"
EVIDENCE_HASH = "5e8f09df4654d0620bf9817273ce09060bf06f1d0c5bb7fab2d9f92d7f1f969a"
PREFIX = ".artifacts/live-smoke-20261106/"
RUN = PREFIX + "approved-retry-1/"


class InvalidEvidence(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise InvalidEvidence(code)


def relative(name):
    require(isinstance(name, str), "PATH_TYPE")
    name = name.replace("\\", "/")
    require(name and not name.startswith("/") and ":" not in name and "\x00" not in name, "PATH_ROOT")
    require(all(p not in ("", ".", "..") for p in name.split("/")), "PATH_COMPONENT")
    return name


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class GitPacket:
    def __init__(self, root, commit=PUBLISHED_SHA):
        require(re.fullmatch("[0-9a-f]{40}", commit), "COMMIT_SHA")
        self.root, self.commit, self.cache = Path(root), commit, {}

    def read(self, name):
        name = relative(name)
        if name not in self.cache:
            self.cache[name] = subprocess.check_output(["git", "show", self.commit + ":" + name], cwd=self.root)
        return self.cache[name]


class DirectoryPacket:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def read(self, name):
        path = self.root / relative(name)
        require(path.resolve().is_relative_to(self.root), "PATH_ESCAPE")
        return path.read_bytes()


class EvidenceVerifier:
    def __init__(self, packet, *, publication_hash=PUBLICATION_HASH, evidence_hash=EVIDENCE_HASH):
        self.packet, self.publication_hash, self.evidence_hash = packet, publication_hash, evidence_hash

    def obj(self, name):
        return json.loads(self.packet.read(name))

    def nonempty(self, value, label, kind=list):
        require(isinstance(value, kind) and bool(value), "EMPTY_" + label)
        return value

    def lines(self, name):
        return [json.loads(line) for line in self.packet.read(name).decode().splitlines() if line]

    def hashes(self, path, pin, prefix=""):
        raw = self.packet.read(path)
        require(hashlib.sha256(raw).hexdigest() == pin, "MANIFEST_PIN:" + path)
        manifest = json.loads(raw)
        entries = manifest if prefix else manifest.get("included")
        self.nonempty(entries, "HASH_MANIFEST", dict)
        normalized = {relative(name): sha for name, sha in entries.items()}
        require(len(normalized) == len(entries), "DUPLICATE_HASH_PATH")
        for name, sha in normalized.items():
            require(isinstance(sha, str) and re.fullmatch("[0-9a-f]{64}", sha), "HASH_SHAPE")
            require(hashlib.sha256(self.packet.read(prefix + name)).hexdigest() == sha, "HASH:" + prefix + name)
        return normalized

    def manifests(self):
        publication = self.hashes(PREFIX + "PUBLICATION_MANIFEST.json", self.publication_hash)
        artifacts = self.hashes(RUN + "artifact-hashes.json", self.evidence_hash, RUN)
        required = {"events.jsonl", "first-verdict.json", "cancel-verdict.json", "ledger-readback.json",
                    "workspace-1/input.json", "attempt-1/result.json", "attempt-1/native-target.json",
                    "attempt-2/native-target.json", "attempt-1/terminal-observer.json",
                    "attempt-2/running-observer.json", "attempt-2/terminal-observer.json",
                    "attempt-1/stdout.jsonl", "attempt-2/stdout.jsonl"}
        require(required <= set(artifacts), "INCOMPLETE_EVIDENCE_MANIFEST")
        require({RUN + name for name in artifacts} <= set(publication), "PUBLICATION_COVERAGE")
        return len(publication), len(artifacts)

    def observer(self, number, label, target, terminal):
        folder = RUN + f"attempt-{number}/"
        record = self.obj(folder + label + ".json")
        command = record["command"]
        require(len(command) == 4 and PureWindowsPath(command[2]).name == "winjob.py"
                and PureWindowsPath(command[3]).name == "observer-target.json", "OBSERVER_COMMAND")
        expected = self.obj(folder + "observer-target.json")
        require(expected["job_object"] == "Local\\HermesSmoke-" + target["attempt"], "OBSERVER_JOB_BINDING")
        want = self.nonempty(expected["known_processes"], "EXPECTED_PROCESSES")
        proof = record["proof"]
        known = self.nonempty(proof["known_processes"], "KNOWN_PROCESSES")
        expected_pids, actual_pids = {p["pid"]: p["start_ticks"] for p in want}, {p["pid"]: p for p in known}
        require(len(expected_pids) == len(want) and len(actual_pids) == len(known)
                and set(expected_pids) == set(actual_pids), "PID_SET_BINDING")
        require(expected_pids.get(int(target["process"])) == target["start"], "ROOT_PID_START_BINDING")
        for pid, p in actual_pids.items():
            require(p["start_ticks"] == expected_pids[pid], "PROCESS_START_BINDING")
            if p.get("absent") is True:
                require(p.get("error") == 87, "ABSENT_PROCESS_ERROR")
            else:
                require(p.get("actual_start_ticks") == p["start_ticks"] and p.get("identity_matches") is True,
                        "PROCESS_IDENTITY")
                if terminal:
                    require(p.get("signaled") is True, "PROCESS_NOT_TERMINAL")
        require(type(proof["active"]) is int and proof["active"] == len(proof["pids"])
                and proof["total"] >= proof["active"], "JOB_PID_COUNT")
        if terminal:
            require(proof["active"] == 0 and proof["pids"] == [] and proof["tree_stopped"] is True, "TREE_NOT_TERMINAL")
        else:
            self.nonempty(proof["pids"], "RUNNING_PIDS")
            members = self.nonempty(proof["members"], "RUNNING_MEMBERS")
            require({p["pid"] for p in members} == set(proof["pids"])
                    and int(target["process"]) in proof["pids"] and proof["tree_stopped"] is False, "ROOT_NOT_RUNNING")
        return proof

    def worker(self, number, verdict, starts, responses, ledger):
        folder = RUN + f"attempt-{number}/"
        target, binding = self.obj(folder + "native-target.json"), self.obj(folder + "binding.json")
        require(target == verdict["target"] and target["job"] == verdict["job"]
                and target["attempt"] == verdict["attempt"], "JOB_ATTEMPT_BINDING")
        launch = [e for e in starts if e["attempt"] == target["attempt"]]
        require(len(launch) == 1, "NATIVE_START_CARDINALITY")
        event = launch[0]
        require(event["job"] == target["job"] and str(event["pid"]) == target["process"]
                and event["start_ticks"] == target["start"] and event["job_object"] == binding["job_object"], "START_EVENT_BINDING")
        for key, value in target.items():
            require(binding[key] == value or key == "session" and binding[key] == target["attempt"], "INITIAL_BINDING:" + key)
        require(str(binding["pid"]) == target["process"] and binding["start_ticks"] == target["start"], "BINDING_PID_START")
        submits = [e for e in responses if e["payload"]["operation"] == "submit_intent"
                   and e["result"].get("view", {}).get("job_ref") == target["job"]]
        require(len(submits) == 2 and submits[0]["result"]["view"]["scheduler_outcome"] == "SUBMITTED"
                and submits[1]["result"]["view"]["scheduler_outcome"] == "DUPLICATE", "SUBMIT_DEDUPE")
        payload = submits[0]["payload"]
        require(payload["project_ref"] == target["project"] and digest(payload) == target["request_digest"], "REQUEST_DIGEST_BINDING")
        for event in submits:
            out = event["result"]
            require(event["payload"] == payload and out["request_id"] == payload["request_id"]
                    and out["view"]["job_revision"] == target["revision"], "SUBMIT_REQUEST_BINDING")
            require(out["payload_digest"] == digest({k: v for k, v in payload.items() if k != "request_id"}), "PAYLOAD_DIGEST")
        operations = [row for row in ledger["operations"] if row["request_id"] == payload["request_id"]]
        require(len(operations) == 1 and operations[0]["job"] == target["job"]
                and operations[0]["revision"] == target["revision"] and operations[0]["project"] == target["project"], "LEDGER_REQUEST_BINDING")
        jobs = [row for row in ledger["jobs"] if row["job"] == target["job"]]
        require(len(jobs) == 1 and json.loads(jobs[0]["target"]) == target, "LEDGER_TARGET_BINDING")
        launches = [row for row in ledger["launches"] if row["attempt"] == target["attempt"]]
        require(len(launches) == 1 and launches[0]["job"] == target["job"], "LEDGER_ATTEMPT_BINDING")
        native_binding = json.loads(launches[0]["binding"])
        require(str(native_binding["pid"]) == target["process"] and native_binding["start_ticks"] == target["start"], "LEDGER_PROCESS_BINDING")
        native = self.lines(folder + "stdout.jsonl")
        threads = [e for e in native if e["type"] == "thread.started"]
        require(len(threads) == 1 and threads[0]["thread_id"] == target["session"], "NATIVE_SESSION_BINDING")
        require(sum(e["type"] == "turn.started" for e in native) == 1
                and sum(e["type"] == "turn.completed" for e in native) == (1 if number == 1 else 0), "TURN_CARDINALITY")
        driver = self.obj(folder + "driver-result.json")
        require(driver["runId"] == target["attempt"] and driver["timedOut"] is False
                and driver["returncode"] == (0 if number == 1 else 123), "DRIVER_RESULT_BINDING")
        command = self.obj(folder + "command.json")
        require(hashlib.sha256(self.packet.read(folder + "prompt.txt")).hexdigest() == command["stdin_sha256"], "PROMPT_HASH")
        argv = command["argv"]
        require(argv[argv.index("--sandbox") + 1] == "read-only" and "gpt-6.1-sol" in argv
                and "model_reasoning_effort=high" in argv, "READONLY_MODEL_BINDING")
        return target, launch[0]

    def cancellation(self, cancel, target, responses, calls, ledger):
        require(len(calls) == 1, "CANCEL_DISPATCH_CARDINALITY")
        effect = ledger["effects"][0]
        key = digest([target[k] for k in ("namespace", "project", "job", "revision", "attempt")] + ["cancel"])
        require(effect["effect_key"] == calls[0]["effect_key"] == key and json.loads(effect["target"]) == target, "EFFECT_BINDING")
        require(effect["state"] == "CANCEL_REQUESTED" and calls[0]["attempt"] == target["attempt"]
                and str(calls[0]["pid"]) == target["process"] and calls[0]["start"] == target["start"]
                and calls[0]["session"] == target["session"], "CANCEL_NATIVE_BINDING")
        deliveries = [e for e in responses if e["payload"]["operation"] == "request_cancel"]
        require(len(deliveries) == 3, "CANCEL_DELIVERY_CARDINALITY")
        for event, label in zip(deliveries, ("lost_reply", "replay", "new_id_replay")):
            p, out = event["payload"], event["result"]
            require(out == cancel[label] and p["request_id"] == out["request_id"] and p["job_ref"] == target["job"]
                    and p["expected_job_revision"] == target["revision"] and p["project_ref"] == target["project"], "CANCEL_REQUEST_BINDING")
            require(out["payload_digest"] == digest({k: v for k, v in p.items() if k != "request_id"}), "CANCEL_PAYLOAD_DIGEST")
            operations = [row for row in ledger["operations"] if row["request_id"] == p["request_id"]]
            require(len(operations) == 1 and operations[0]["job"] == target["job"]
                    and operations[0]["revision"] == target["revision"]
                    and operations[0]["effect_key"] == key, "CANCEL_LEDGER_REQUEST_BINDING")
            require(out["hold"] is True, "CANCEL_HOLD")
        require(deliveries[0]["payload"] == deliveries[1]["payload"] and deliveries[2]["payload"]["request_id"]
                != deliveries[1]["payload"]["request_id"], "REDELIVERY_IDENTITIES")
        require(cancel["lost_reply"]["outcome"] == "UNKNOWN" and cancel["lost_reply"]["issues"] == ["RESPONSE_LOST"], "LOST_REPLY")
        require(all(cancel[k]["view"]["scheduler_outcome"] == "ALREADY_REQUESTED" for k in ("replay", "new_id_replay")), "CANCEL_REDISPATCH")
        require(all(cancel[k]["view"]["reservation"] == "POSSIBLY_CONSUMED_HELD" for k in ("replay", "new_id_replay")), "RESERVATION_RELEASED")
        return effect

    def verify(self):
        published_count, hashed_count = self.manifests()
        first, cancel, ledger = (self.obj(RUN + name) for name in ("first-verdict.json", "cancel-verdict.json", "ledger-readback.json"))
        for label in ("operations", "jobs", "launches", "effects"):
            self.nonempty(ledger[label], "LEDGER_" + label.upper())
        require(len(ledger["launches"]) == 3 and len(ledger["effects"]) == 1, "LEDGER_CARDINALITY")
        require(len({r["attempt"] for r in ledger["launches"]}) == 3
                and len({r["job"] for r in ledger["launches"]}) == 3, "DUPLICATE_LEDGER_ID")
        require({r["state"] for r in ledger["launches"]} == {
            "FAILED_CONFIG_HOLD", "TERMINAL_SUCCESS", "TERMINAL_LOCAL_CANCEL_HOLD"}, "LEDGER_TERMINAL_STATES")
        events = self.nonempty(self.lines(RUN + "events.jsonl"), "EVENTS")
        starts, calls, responses = ([e for e in events if e["event"] == name] for name in
                                    ("NATIVE_STARTED", "NATIVE_CANCEL_DISPATCH", "PM_RESPONSE"))
        require(len(starts) == 2, "START_CARDINALITY")
        one, start_one = self.worker(1, first, starts, responses, ledger)
        two, start_two = self.worker(2, cancel, starts, responses, ledger)
        source, result = self.obj(RUN + "workspace-1/input.json"), self.obj(RUN + "attempt-1/result.json")
        require(result == first["result"] == {"marker": source["marker"], "value": source["value"], "sum": sum(source["numbers"])}, "RESULT_BINDING")
        verified = [e for e in events if e["event"] == "FIRST_VERIFIED"]
        require(len(verified) == 1 and start_one["at"] < verified[0]["at"] < start_two["at"]
                and verified[0]["attempt"] == one["attempt"] and verified[0]["job"] == one["job"], "FIRST_BEFORE_SECOND")
        initial = self.observer(1, "terminal-observer", one, True)
        require(start_one["at"] < initial["observed_at"] < verified[0]["at"], "FIRST_STOP_TIME")
        running, terminal = self.observer(2, "running-observer", two, False), self.observer(2, "terminal-observer", two, True)
        effect = self.cancellation(cancel, two, responses, calls, ledger)
        require(start_two["at"] < running["observed_at"] <= effect["cancel_at"] <= calls[0]["at"] < terminal["observed_at"], "CANCEL_TIME")
        handles = self.nonempty(cancel["retained_handles_signaled"], "HANDLES", dict)
        require(all(v is True for v in handles.values()) and set(handles) == {str(p["pid"]) for p in terminal["known_processes"]}, "HANDLE_SET_BINDING")
        require(first["status"] == "PASS" and cancel["status"] == "HOLD" and
                cancel["billing_settled"] == cancel["external_effects_settled"] == "UNKNOWN", "SETTLEMENT_HOLD")
        for name, expected in (("offline.txt", "90 tests passed, 0 failed"), ("regressions.txt", "54 tests passed, 0 failed")):
            raw = self.packet.read(PREFIX + name)
            text = raw.decode("utf-16" if raw.startswith(b"\xff\xfe") else "utf-8-sig")
            require(expected in text, "OFFLINE_LOG:" + name)
        return {"recorded_evidence_consistent": True, "publication_files": published_count,
                "hashed_evidence_files": hashed_count, "evidence_commit": PUBLISHED_SHA,
                "native_cancel_calls": 1, "overall_native_attempts": 3, "cancel_acceptance": "HOLD",
                "controller_restart": "NOT_PROVEN", "recovery": "database reopened; SAME LivePort and OS handles",
                "scope": "immutable recorded artifact consistency, not a new live execution"}
