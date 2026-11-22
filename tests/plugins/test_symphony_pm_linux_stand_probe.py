"""Offline Restricted socket outcomes through the test-stand probe and the real precheck gate."""
import errno
import hashlib
from io import StringIO
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from plugins.symphony_pm.transport import linux_native, linux_probe, linux_sandbox
from plugins.symphony_pm.transport.linux_launcher import LinuxLauncher
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_qualification import harness, stand_probe
from tests.plugins.test_symphony_pm_linux_native_contract import identity


def test_ordinary_launcher_uses_original_probe(tmp_path):
    q = SimpleNamespace(executable="/usr/bin/codex", probe_python="/usr/bin/python3", permission_profile="ordinary")
    launcher = SimpleNamespace(qualification=q, settings=SimpleNamespace(exec_timeout=30, probe_timeout=2))
    request = SimpleNamespace(goal="write", raw_constraints=("use stand",))
    call = LinuxLauncher._call(launcher, None, request, SimpleNamespace(src=tmp_path), {"port": 1234})
    assert call.probe_argv[-3] == linux_probe.PROGRAM
    assert hashlib.sha256(linux_probe.PROGRAM.encode()).hexdigest() == (
        "9fcfca9cbfac0ec12c2a6c62146715e3519e4810c72156fa0faff3e74959b36a")
    assert call.argv[1] == "exec" and call.probe_argv[-3] != harness.PROGRAM


@pytest.mark.parametrize(("stage", "error", "change", "expected"), [
    ("socket", errno.EPERM, "", True), ("connect", errno.EPERM, "", True),
    ("connected", None, "", False), ("socket", errno.EACCES, "", False),
    ("connect", errno.ECONNREFUSED, "", False), ("connect", errno.ETIMEDOUT, "", False),
    ("socket", errno.EPERM, "missing", False), ("connect", errno.EPERM, "invalid_json", False),
    ("connect", errno.EPERM, "challenge", False), ("socket", errno.EPERM, "environment", False),
    ("connect", errno.EPERM, "namespace", False), ("socket", errno.EPERM, "interfaces", False),
    ("socket", errno.EPERM, "failed_check", False), ("socket", errno.EPERM, "expired", False),
])
def test_stand_network_result_controls_second_command(tmp_path, monkeypatch, capsys, stage, error, change, expected):
    client = MagicMock()
    factory = Mock(return_value=client)
    if stage == "socket":
        factory.side_effect = OSError(error, "synthetic socket result")
    elif stage == "connect":
        client.connect.side_effect = OSError(error, "synthetic connect result")
    monkeypatch.setattr(stand_probe, "socket", SimpleNamespace(socket=factory, AF_INET=2, SOCK_STREAM=1))
    monkeypatch.setattr(stand_probe, "os", SimpleNamespace(
        O_RDONLY=os.O_RDONLY, O_WRONLY=os.O_WRONLY, environ={"CODEX_SANDBOX_NETWORK_DISABLED": "1"},
        readlink=lambda path: "net:[42]"))
    monkeypatch.setattr(stand_probe, "denied", lambda path, flags: True)
    monkeypatch.setattr(stand_probe, "writable", lambda path, challenge: True)
    monkeypatch.setattr(stand_probe, "open", lambda *a, **kw: StringIO("headers\nheaders\n lo: 0\n"), raising=False)
    hidden = {key: "/" + key for key in ("deny_control", "deny_journal", "deny_neighbor",
                                         "deny_source", "deny_credentials", "deny_owner_processes")}
    report = stand_probe.report("challenge", {"port": 1234, "hidden": hidden})
    assert set(report["checks"]) == linux_sandbox.BOUNDARY_CHECKS
    assert report["checks"]["deny_network"] is (error == errno.EPERM)
    assert capsys.readouterr().err.strip() == (stage if error is None else f"{stage} {error}")
    factory.assert_called_once_with(2, 1)
    if stage == "socket":
        client.connect.assert_not_called()
    else:
        client.connect.assert_called_once_with(("127.0.0.1", 1234))
    if change == "challenge":
        report["challenge"] = "old"
    elif change == "environment":
        report["environment"].append("LANG")
    elif change == "namespace":
        report["network_namespace"] = "net:[12]"
    elif change == "interfaces":
        report["interfaces"].append("eth0")
    elif change == "failed_check":
        report["checks"]["deny_control"] = False
    if change != "missing":
        (tmp_path / "probe-output").write_text("{" if change == "invalid_json" else json.dumps(report), encoding="utf-8")
    (tmp_path / "probe-exit").write_text("0", encoding="ascii")

    binding = identity()
    q = SimpleNamespace(command_environment=("CODEX_SANDBOX_NETWORK_DISABLED",), fingerprint="stand", verify=Mock())
    call = SimpleNamespace(prompt="", probe_timeout=1, challenge="challenge", argv=("fixed",))
    receiver = linux_native.LinuxReceiver(SimpleNamespace(directory=lambda _: tmp_path), binding,
                                         SimpleNamespace(qualification=q), call=call)
    monkeypatch.setattr(receiver, "check_binding", Mock())
    monkeypatch.setattr(linux_native, "owner_network_namespace", lambda: "net:[12]")
    monkeypatch.setattr(linux_native, "wait_message", lambda *args: 0)
    gates = []
    monkeypatch.setattr(linux_native, "publish", lambda directory, name, value: gates.append(name))
    def authorize(action):
        if change == "expired" and "probe" in gates:
            raise Refused("PERMIT_EXPIRED")
        action()
    receiver.resume("", authorize=authorize)
    if expected:
        assert receiver.wait_result(["fixed"], 1).returncode == 0
        assert gates == ["probe", "probe-receipt", "exec"]
    else:
        failure = {"missing": FileNotFoundError, "invalid_json": json.JSONDecodeError}.get(change, Refused)
        with pytest.raises(failure):
            receiver.wait_result(["fixed"], 1)
        assert gates == (["probe", "probe-receipt"] if change == "expired" else ["probe"])
