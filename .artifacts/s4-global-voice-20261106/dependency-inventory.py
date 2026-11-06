"""Offline planning only: derive target wheel candidates from the existing lock."""
import collections
import hashlib
import json
from pathlib import Path
import tomllib
from urllib.parse import unquote, urlparse

from packaging.markers import Marker, default_environment
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
by_name = collections.defaultdict(list)
for package in lock["package"]:
    by_name[package["name"]].append(package)
environment = default_environment()
environment["extra"] = ""
tag_order = {tag: index for index, tag in enumerate(sys_tags())}
pending = [("hermes-agent", frozenset({"audio-io", "stt-whisper", "web"}))]
seen = set()
selected = {}
unknown = []
while pending:
    name, extras = pending.pop()
    if (name, extras) in seen:
        continue
    seen.add((name, extras))
    candidates = by_name[name]
    matching = [p for p in candidates if not p.get("resolution-markers") or any(Marker(m).evaluate(environment) for m in p["resolution-markers"])]
    if len(matching) != 1:
        unknown.append({"name": name, "reason": "ambiguous lock candidates", "count": len(matching)})
        continue
    package = matching[0]
    dependencies = list(package.get("dependencies", []))
    for extra in extras:
        dependencies.extend(package.get("optional-dependencies", {}).get(extra, []))
    for dependency in dependencies:
        if dependency.get("marker") and not Marker(dependency["marker"]).evaluate(environment):
            continue
        pending.append((dependency["name"], frozenset(dependency.get("extra", []))))
    wheels = []
    for wheel in package.get("wheels", []):
        try:
            tags = parse_wheel_filename(unquote(Path(urlparse(wheel["url"]).path).name))[3]
        except ValueError:
            continue
        ranks = [tag_order[tag] for tag in tags if tag in tag_order]
        if ranks:
            wheels.append((min(ranks), wheel))
    if wheels:
        artifact = min(wheels, key=lambda item: item[0])[1]
        selected[name] = {"version": package["version"], **artifact}
    elif name != "hermes-agent":
        unknown.append({"name": name, "version": package["version"], "reason": "no target wheel; build is not authorized", "sdist": package.get("sdist")})

result = {
    "scope": "static target wheel closure estimate; not PM resolver or installation proof",
    "target_environment": environment,
    "extras": ["audio-io", "stt-whisper", "web"],
    "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
    "wheel_count": len(selected),
    "selected_wheel_download_bytes": sum(p.get("size", 0) for p in selected.values()),
    "unknown": unknown,
    "wheels": dict(sorted(selected.items())),
}
(OUT / "python-artifacts.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps({k: v for k, v in result.items() if k not in {"wheels", "target_environment"}}, indent=2))
