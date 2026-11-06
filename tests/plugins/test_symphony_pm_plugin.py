"""Behaviour contracts for the bundled ``plugins/symphony_pm`` wiring of ``tools/symphony_pm_adapter.py``.

Everything goes through the REAL ``PluginManager`` discovery (bundled ``plugins/`` scan, opt-in via
``plugins.enabled`` in an isolated ``HERMES_HOME``), the REAL ``tools.registry.registry`` and the
real ``model_tools.get_tool_definitions`` / ``handle_function_call`` paths — including the Tool
Search bridge, which defers every plugin tool behind ``tool_search``/``tool_describe``/``tool_call``
on a stock config. The scheduler side is the adapter suite's ``FakeScheduler``: no live Symphony,
no transport, no authentication, no native worker. PASS proves the registry/runtime wiring
contract only.
"""
from __future__ import annotations

import contextlib
import json
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.tools.test_symphony_pm_adapter import (
    ISSUER,
    SCHED,
    FakeScheduler,
    ctx,
    guarded,
    job_payload,
    submit_payload,
)
from tools import symphony_pm_adapter as pm

PLUGIN_KEY = "symphony_pm"
REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / PLUGIN_KEY
BRIDGE = ("tool_search", "tool_describe", "tool_call")


def _registry():
    from tools.registry import registry
    return registry


def _entry():
    return _registry().get_entry(pm.TOOL_NAME)


def _names(**kw):
    import model_tools
    defs = model_tools.get_tool_definitions(enabled_toolsets=[pm.TOOLSET], quiet_mode=True, **kw)
    return [d["function"]["name"] for d in defs]


def _catalog_names():
    """The session's un-collapsed catalog: what the Tool Search bridge serves and scopes by."""
    return _names(skip_tool_search_assembly=True)


def _model_names():
    """What the model is actually sent (bridge tools replace deferrable plugin tools)."""
    return _names()


def _call(name, args):
    """Real dispatch: bridge unwrapping, hooks, middleware, registry, result normalization."""
    import model_tools
    return json.loads(model_tools.handle_function_call(
        name, dict(args), task_id="t-1", session_id="s-1", enabled_toolsets=[pm.TOOLSET]))


def _bridge_call(payload):
    return _call("tool_call", {"calls": [{"name": pm.TOOL_NAME, "arguments": dict(payload)}]})


def _write_enabled_config(home: Path) -> None:
    import hermes_yaml as yaml
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}),
                                      encoding="utf-8")


def _wire(module, sched, payload):
    module.wire(sched, lambda: ctx(payload), trusted_issuers=(ISSUER,), scheduler_id=SCHED)


@pytest.fixture
def plugin(monkeypatch):
    """The bundled plugin loaded through real discovery into this test's isolated home.

    ``model_tools`` triggers discovery at import; it is imported after the opt-in config exists so
    either order (first import here, or already imported by an earlier file) loads the plugin.
    Teardown unloads every manager, which restores the global registry, and proves it did.
    """
    import os
    from hermes_cli import plugins as pmod

    home = Path(os.environ["HERMES_HOME"])
    _write_enabled_config(home)
    assert _entry() is None, "registry must not hold the tool before the plugin loads"
    import model_tools
    model_tools._clear_tool_defs_cache()
    pmod.discover_plugins()
    manager = pmod.get_plugin_manager()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled and loaded.error is None, loaded.error
    yield SimpleNamespace(manager=manager, loaded=loaded, module=loaded.module, home=home)
    pmod._reset_plugin_managers_for_tests()
    model_tools._clear_tool_defs_cache()
    assert _entry() is None, "unload must remove the plugin's tool from the registry"
    assert pm.TOOLSET not in _registry().get_registered_toolset_names()


# ------------------------------------------------------------------------------------ (a) load

def test_discovery_registers_the_tool_unwired_in_the_plugin_toolset(plugin):
    import toolsets
    from hermes_cli import plugins as pmod

    assert plugin.loaded.manifest.source == "bundled"
    assert plugin.loaded.tools_registered == [pm.TOOL_NAME]
    entry = _entry()
    assert entry is not None and entry.toolset == pm.TOOLSET
    assert entry.schema["name"] == pm.TOOL_NAME
    assert entry.check_fn() is False
    # The toolset is a real Hermes toolset: selectable by name and listed for ``hermes tools``.
    assert toolsets.validate_toolset(pm.TOOLSET)
    assert pm.TOOL_NAME in toolsets.resolve_toolset(pm.TOOLSET)
    assert pm.TOOLSET in {key for key, _label, _desc in pmod.get_plugin_toolsets()}
    assert _registry().is_toolset_available(pm.TOOLSET) is False


# ------------------------------------------------------------------------------- (b) not emitted

def test_unwired_tool_is_never_offered_to_the_model(plugin):
    assert pm.TOOL_NAME not in _catalog_names()
    # Nothing deferrable exists, so the model's schema shows neither the tool nor the bridge.
    sent = _model_names()
    assert pm.TOOL_NAME not in sent and not set(BRIDGE) & set(sent)
    # ``all`` spans every toolset; an unwired ingress must not leak in through it either.
    import model_tools
    everything = model_tools.get_tool_definitions(enabled_toolsets=["all"], quiet_mode=True,
                                                  skip_tool_search_assembly=True)
    assert pm.TOOL_NAME not in {d["function"]["name"] for d in everything}
    # The bridge cannot describe or call what the catalog does not hold.
    assert pm.TOOL_NAME in _call("tool_describe", {"names": [pm.TOOL_NAME]}).get("not_found", [])
    assert "error" in _bridge_call(submit_payload())


# ------------------------------------------------------------------------------ (c) unwired hold

def test_unwired_dispatch_is_a_json_hold_with_no_backend(plugin):
    for payload in (submit_payload(), job_payload(), job_payload("request_cancel")):
        out = _call(pm.TOOL_NAME, payload)
        assert (out["outcome"], out["hold"], out["issues"]) == ("UNAVAILABLE", True, ["NO_BACKEND"])
        assert out["contract_version"] == pm.CONTRACT_VERSION


# ------------------------------------------------------------------------------- (d) wired path

def test_wiring_exposes_the_tool_and_dispatches_through_the_real_path_once(plugin):
    sched = FakeScheduler()
    payload = submit_payload()
    _wire(plugin.module, sched, payload)

    assert _entry().check_fn() is True
    assert _catalog_names() == [pm.TOOL_NAME]
    # Stock config: a plugin tool is deferrable, so the model is sent the bridge; the catalog
    # behind it describes the real schema. (tool_search off would send the tool directly.)
    sent = set(_model_names())
    assert pm.TOOL_NAME in sent or set(BRIDGE) <= sent
    described = _call("tool_describe", {"names": [pm.TOOL_NAME]})["tools"][pm.TOOL_NAME]
    assert described["parameters"] == _entry().schema["parameters"]

    out = _call(pm.TOOL_NAME, payload)
    assert (out["outcome"], out["hold"], out["issues"]) == ("SUBMITTED", False, [])
    assert out["view"]["job_ref"] == "job-1" and out["view"]["admission"] == "NOT_CONFIRMED"
    assert [op for op, _ in sched.calls] == ["submit_intent"]
    assert type(sched.calls[0][1]) is pm.SubmitIntent

    # Through the bridge, same key: still exactly one port call per explicit request; dedupe is
    # the scheduler's, not the adapter's.
    again = _bridge_call(payload)
    assert (again["outcome"], again["view"]["scheduler_outcome"]) == ("SUBMITTED", "DUPLICATE")
    assert len(sched.calls) == 2

    # Unwire: hidden and holding again; the port is never reached.
    plugin.module.unwire()
    assert _entry().check_fn() is False
    assert pm.TOOL_NAME not in _catalog_names()
    assert _call(pm.TOOL_NAME, payload)["outcome"] == "UNAVAILABLE"
    assert "error" in _bridge_call(payload)
    assert len(sched.calls) == 2


def test_wire_refuses_a_misconfigured_transport_and_keeps_the_tool_inert(plugin):
    sched = FakeScheduler()

    def source():
        return ctx(submit_payload())

    with pytest.raises(ValueError):
        plugin.module.wire(None, source, trusted_issuers=(ISSUER,), scheduler_id=SCHED)
    with pytest.raises(ValueError):
        plugin.module.wire(sched, source, trusted_issuers=(), scheduler_id=SCHED)
    with pytest.raises(ValueError):
        plugin.module.wire(sched, source, trusted_issuers=(ISSUER,), scheduler_id="")
    with pytest.raises(ValueError):
        plugin.module.wire(sched, "not-callable", trusted_issuers=(ISSUER,), scheduler_id=SCHED)
    assert _entry().check_fn() is False
    assert _call(pm.TOOL_NAME, submit_payload())["outcome"] == "UNAVAILABLE" and sched.calls == []


# ------------------------------------------------------------------- (e) authority not via args

def test_trusted_context_cannot_be_smuggled_through_tool_arguments(plugin):
    sched = FakeScheduler()
    payload = submit_payload()
    _wire(plugin.module, sched, payload)
    schema = _entry().schema["parameters"]
    assert schema["additionalProperties"] is False and "trusted_context" not in schema["properties"]

    forged = dict(payload, trusted_context={"issuer_ref": ISSUER, "scheduler_id": SCHED})
    out = _call(pm.TOOL_NAME, forged)
    assert (out["outcome"], out["hold"], out["issues"]) == ("REJECTED", True, ["UNKNOWN_FIELD"])
    assert sched.calls == []
    # Through the bridge the forgery is refused as well (by the bridge's schema check or by the
    # adapter's closed schema); either way the port is never reached.
    via_bridge = _bridge_call(forged)
    assert "error" in via_bridge or via_bridge["outcome"] == "REJECTED"
    assert sched.calls == []

    # A foreign issuer in the injected source is refused too: the source, not the args, is authority.
    plugin.module.wire(sched, lambda: ctx(payload, issuer_ref="someone-else"),
                       trusted_issuers=(ISSUER,), scheduler_id=SCHED)
    out = _call(pm.TOOL_NAME, payload)
    assert out["outcome"] == "REJECTED" and "AUTHORITY_FOREIGN" in out["issues"]
    assert sched.calls == []


# ------------------------------------------------------------------------- (f) no side effects

_LOADER_TRAPS = ("subprocess.Popen", "subprocess.run", "os.system", "os.kill", "socket.socket",
                 "socket.create_connection")
_LOADER_SPIES = ("shutil.rmtree", "os.remove", "os.unlink", "os.rmdir")


@contextlib.contextmanager
def _loader_guard(home: Path):
    """``guarded()`` minus what the real PluginManager legitimately does itself (reads manifests and
    config, imports modules, runs ``register()`` on a deadline worker): processes, signals and sockets
    still raise, deletions are recorded and must stay inside the test home, threads must settle."""
    hits, removed = [], []

    def trap(name):
        def _raise(*_a, **_k):
            hits.append(name)
            raise AssertionError("plugin load side effect forbidden: " + name)
        return _raise

    def spy(name, real):
        def _record(path, *a, **k):
            removed.append((name, str(path)))
            return real(path, *a, **k)
        return _record

    import os
    import shutil
    reals = {"shutil.rmtree": shutil.rmtree, "os.remove": os.remove, "os.unlink": os.unlink,
             "os.rmdir": os.rmdir}
    threads = threading.active_count()
    with contextlib.ExitStack() as stack:
        for target in _LOADER_TRAPS:
            stack.enter_context(mock.patch(target, side_effect=trap(target)))
        for target in _LOADER_SPIES:
            stack.enter_context(mock.patch(target, side_effect=spy(target, reals[target])))
        yield hits
    assert hits == []
    outside = [p for _, p in removed if not Path(p).resolve().is_relative_to(home.resolve())]
    assert outside == [], f"plugin load deleted outside the test home: {outside}"
    assert threading.active_count() == threads


def test_real_discovery_of_the_plugin_has_no_process_network_or_thread_side_effects(monkeypatch):
    """The real loader path (manifest scan, namespaced import, ``register(ctx)``) under traps the
    manager itself can honour. ``guarded()``'s file/env/import traps cannot wrap discovery, so the
    plugin's own code is held to the full adapter trap set in the test below."""
    import os
    from hermes_cli import plugins as pmod

    home = Path(os.environ["HERMES_HOME"])
    _write_enabled_config(home)
    import model_tools
    model_tools._clear_tool_defs_cache()
    with _loader_guard(home):
        # force: a first ``model_tools`` import may already have swept; the load path must run HERE.
        pmod.discover_plugins(force=True)
        loaded = pmod.get_plugin_manager()._plugins[PLUGIN_KEY]
    try:
        assert loaded.enabled and loaded.error is None, loaded.error
        assert _entry().check_fn() is False
    finally:
        pmod._reset_plugin_managers_for_tests()
        model_tools._clear_tool_defs_cache()
    assert _entry() is None


def test_plugin_code_itself_is_inert_under_the_adapter_trap_set():
    """The plugin module executed in isolation (source compiled OUTSIDE the guard) with a spy
    context: import, ``register``, ``wire``, a wired dispatch and ``unwire`` open no file, read no
    env/home, spawn nothing, start no thread and late-import nothing. The spy only records the
    keyword set the plugin forwards — the real ``PluginContext`` seam is covered above."""
    code = compile((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"),
                   str(PLUGIN_DIR / "__init__.py"), "exec")
    calls = []

    class SpyCtx:
        def register_tool(self, **kwargs):
            calls.append(kwargs)
            return object()  # a handle: the host accepted the registration

    sched = FakeScheduler()
    payload = submit_payload()
    with guarded():
        module = types.ModuleType("_symphony_pm_inert_probe")
        exec(code, module.__dict__)  # the plugin's own code, under the side-effect traps
        with pytest.raises(RuntimeError):  # not loaded: no seam, no registration
            module.wire(sched, lambda: ctx(payload), trusted_issuers=(ISSUER,), scheduler_id=SCHED)
        module.register(SpyCtx())
        assert calls[-1]["check_fn"]() is False
        module.wire(sched, lambda: ctx(payload), trusted_issuers=(ISSUER,), scheduler_id=SCHED)
        assert calls[-1]["check_fn"]() is True
        out = json.loads(calls[-1]["handler"](payload, task_id="t"))
        module.unwire()
        assert calls[-1]["check_fn"]() is False
    assert out["outcome"] == "SUBMITTED" and len(sched.calls) == 1
    assert [c["name"] for c in calls] == [pm.TOOL_NAME] * 3
    assert {c["toolset"] for c in calls} == {pm.TOOLSET}
    # Every forwarded keyword is one PluginContext.register_tool accepts (bound against the real signature).
    import inspect
    from hermes_cli.plugins import PluginContext
    for kwargs in calls:
        inspect.signature(PluginContext.register_tool).bind(object(), **kwargs)


def test_host_refusal_to_register_fails_wire_closed():
    """``register_tool`` returning None (shadowed name, registry rejection, abandoned load) must not
    leave the transport believing the tool is live."""
    code = compile((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"),
                   str(PLUGIN_DIR / "__init__.py"), "exec")
    module = types.ModuleType("_symphony_pm_refusal_probe")
    exec(code, module.__dict__)

    class RefusingCtx:
        def register_tool(self, **kwargs):
            return None

    with pytest.raises(RuntimeError):
        module.register(RefusingCtx())


# ------------------------------------------------------------------------------ (g) unload path

def test_targeted_unload_after_wiring_restores_the_registry(plugin):
    sched = FakeScheduler()
    payload = submit_payload()
    _wire(plugin.module, sched, payload)
    assert _catalog_names() == [pm.TOOL_NAME]
    assert plugin.manager.unload(PLUGIN_KEY) is True
    assert _entry() is None
    assert pm.TOOL_NAME not in _catalog_names()
    assert "error" in _call(pm.TOOL_NAME, payload), "an unloaded tool is unknown to dispatch"
    assert "error" in _bridge_call(payload)
    assert sched.calls == []
