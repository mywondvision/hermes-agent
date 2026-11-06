"""Read-only S4 inventory. No recording, model loading, installation, or network."""
import ctypes
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    started = time.perf_counter()
    packages = {}
    for name in ("hermes-agent", "sounddevice", "numpy", "faster-whisper", "ctranslate2", "openai", "PyYAML", "pytest"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    git = lambda *args: subprocess.run(["git", "--no-optional-locks", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10)
    status = git("status", "--short").stdout
    relevant = ["hermes_cli/voice.py", "hermes_cli/cli_voice_mixin.py", "tools/voice_mode.py", "tools/transcription_tools.py", "tools/transcription_cloud.py", "tools/transcription_command.py", "ui-tui/src/app/createGatewayEventHandler.ts"]
    foreign = ["tools/symphony_pm_adapter.py", "plugins/symphony_pm/__init__.py", "tests/tools/test_symphony_pm_adapter.py", "tests/tools/test_symphony_pm_adapter_cancel_s2.py", "tests/plugins/test_symphony_pm_plugin.py", "docs/change-requests/CR-hermes-symphony-ingress-offline-20261106.md"]
    paths = relevant + foreign
    hashes = {name: digest(ROOT / name) for name in paths if (ROOT / name).is_file()}
    class WaveCaps(ctypes.Structure):
        _fields_ = [("manufacturer", ctypes.c_ushort), ("product", ctypes.c_ushort), ("version", ctypes.c_uint), ("name", ctypes.c_wchar * 32), ("formats", ctypes.c_uint), ("channels", ctypes.c_ushort), ("reserved", ctypes.c_ushort)]
    devices = []
    winmm = ctypes.WinDLL("winmm")
    for index in range(winmm.waveInGetNumDevs()):
        caps = WaveCaps()
        result = winmm.waveInGetDevCapsW(ctypes.c_size_t(index), ctypes.byref(caps), ctypes.sizeof(caps))
        devices.append({"index": index, "result": result, "name": caps.name, "channels": caps.channels})
    user_home = Path.home()
    cache_roots = [user_home / ".cache/huggingface/hub", user_home / ".cache/whisper", Path("C:/codex-cache/huggingface/hub"), user_home / ".cache/openwhispr", user_home / "AppData/Roaming/open-whispr/models"]
    caches = {}
    for path in cache_roots:
        if path.is_dir():
            caches[str(path)] = [p.name for p in path.iterdir()]
        else:
            caches[str(path)] = None
    result = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "root": str(ROOT), "head": git("rev-parse", "HEAD").stdout.strip(), "branch": git("branch", "--show-current").stdout.strip(), "status": status, "executable": sys.executable, "python": sys.version, "packages": packages, "devices_enumeration_only": devices, "cache_roots_bounded": caches, "hashes_before": hashes, "microphone_opened": False, "audio_transmitted": False, "elapsed_seconds": round(time.perf_counter() - started, 3)}
    (OUT / "inventory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
