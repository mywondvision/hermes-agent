"""Build and read back a qualification worker from the previously verified offline supply."""
import argparse
import hashlib
import importlib.util
import json
import mmap
from pathlib import Path
import runpy
import subprocess
import tarfile


STAND = Path(__file__).resolve().parent
CODEX_NAME = "release-inputs-5d5d7e6f/codex-package-x86_64-unknown-linux-musl.tar.gz"
CODEX_TAR_SHA = "b33cd426c9acab9b34c5a93200ba4fe83c8e614c18ce5ef52b2bf36408b8e18c"
CODEX_SHA = "167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9"
BASE_NAME = "checker-image-prep/inputs/ubuntu-base-22.04.5-base-amd64.tar.gz"
BASE_SHA = "242cd8898b33ea806ef5f13b1076ed7c76f9f989d18384452f7166692438ff1a"
POLICY = "etc/symphony/codex/config.toml"
FIXTURE = "usr/libexec/hermes-symphony/qualify.py"
PROBE = "usr/libexec/hermes-symphony/stand_probe.py"


def checked_tool(path, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("offline build tool changed: " + str(path))
    return path


def load_build_tools(supply):
    spec = importlib.util.spec_from_file_location("controller_build", checked_tool(
        supply / "controller-image-prep/build.py", "4ff11fd558654eb5a06207c09983c21bcb7446652bd2d433b8dbc34b9d2ec8d1"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def python_runtime(toolkit, tar, manifest):
    if toolkit.sha_file(toolkit.PYTHON_TAR) != toolkit.PYTHON_TAR_SHA:
        raise ValueError("verified CPython filesystem changed")
    with tarfile.open(toolkit.PYTHON_TAR) as source:
        for item in source:
            if not item.isfile():
                continue
            if (item.name == "usr/bin/python3" or item.name.startswith(("lib/", "lib64/"))
                    or (item.name.startswith("usr/lib/python3.14/") and "site-packages" not in item.name.split("/"))):
                toolkit.add(tar, manifest, item.name, source.extractfile(item).read(), item.mode)


def release_assets(toolkit, tar, manifest, supply):
    codex, base = supply / CODEX_NAME, supply / BASE_NAME
    if toolkit.sha_file(codex) != CODEX_TAR_SHA or toolkit.sha_file(base) != BASE_SHA:
        raise ValueError("pinned release input changed")
    names = {"bin/codex", "codex-package.json", "codex-resources/bwrap"}
    with tarfile.open(codex, "r|gz") as source:
        for item in source:
            if item.name in names:
                if not item.isfile():
                    raise ValueError("expected regular package member")
                names.remove(item.name)
                mode = 0o444 if item.name.endswith(".json") else 0o555
                dest = "usr/" + item.name
                if item.name == "bin/codex":
                    toolkit.add(tar, manifest, "usr/bin")
                    member = tarfile.TarInfo(dest)
                    member.size, member.mode = item.size, mode
                    with source.extractfile(item) as stream:
                        tar.addfile(member, stream)
                    manifest[dest] = {"type": "file", "mode": mode, "sha256": CODEX_SHA}
                else:
                    toolkit.add(tar, manifest, dest, source.extractfile(item).read(), mode)
    if names:
        raise ValueError("incomplete Codex distribution")
    with tarfile.open(base) as source:
        # Non-interactive shell discovery may use /bin/sh or /bin/bash.
        sources = {m.name.removeprefix("./"): m for m in source}
        for origin, destination in (("usr/bin/dash", "bin/sh"), ("usr/bin/bash", "bin/bash"),
                                    ("usr/lib/x86_64-linux-gnu/libtinfo.so.6", "lib/x86_64-linux-gnu/libtinfo.so.6")):
            item = sources[origin]
            if item.issym():
                item = sources[str(Path(origin).parent.as_posix()) + "/" + item.linkname]
            if not item.isfile():
                raise ValueError(origin)
            toolkit.add(tar, manifest, destination, source.extractfile(item).read(), 0o555)


def verify_static_codex(archive, item, elf):
    with archive.extractfile(item) as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != CODEX_SHA:
            raise ValueError("packaged Codex executable changed")
    with mmap.mmap(archive.fileobj.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
        with memoryview(mapping)[item.offset_data:item.offset_data + item.size] as binary:
            if elf(binary) != {"interpreter": None, "needed": []}:
                raise ValueError("expected the static Codex executable")


def verify_dependencies(bundle, supply):
    parser = runpy.run_path(str(checked_tool(
        supply / "checker-image-prep/build.py", "589095c1331b3190803132841752118e1c68abb3cc85e776eb4938dd36528021")))
    with tarfile.open(bundle) as archive:
        libraries = {}
        for item in archive:
            if not item.isfile():
                continue
            if item.name == "usr/bin/codex":
                verify_static_codex(archive, item, parser["elf"])
                continue
            with archive.extractfile(item) as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
                stream.seek(0)
                binary = stream.read()
            info = parser["elf"](binary)
            if info["interpreter"]:
                if not archive.getmember(info["interpreter"].lstrip("/")).isfile():
                    raise ValueError("missing ELF interpreter: " + item.name)
            needs = parser["version_table"](binary, 0x6ffffffe)
            for name in set(info["needed"]) | set(needs):
                if name not in libraries:
                    with archive.extractfile("lib/x86_64-linux-gnu/" + name) as stream:
                        libraries[name] = set(parser["version_table"](stream.read(), 0x6ffffffd))
                if set(needs.get(name, ())) - libraries[name]:
                    raise ValueError("unsatisfied versioned dependency: " + item.name + " -> " + name)


def build(supply, output):
    toolkit = load_build_tools(supply)
    tools = toolkit.verified_tools()
    output.mkdir()
    bundle = output / "worker.tar"
    image = output / "worker.squashfs"
    entries = {}
    with tarfile.open(bundle, "x", format=tarfile.GNU_FORMAT) as tar:
        for directory in ("dev", "proc", "tmp", "work", "usr/codex-path", "usr/lib/python3.14/lib-dynload"):
            toolkit.add(tar, entries, directory)
        python_runtime(toolkit, tar, entries)
        release_assets(toolkit, tar, entries, supply)
        toolkit.add(tar, entries, POLICY, (STAND / "policy.toml").read_bytes(), 0o444)
        toolkit.add(tar, entries, "etc/symphony/codex/.symphony-qualification-marker",
                    b"symphony-qualification-marker\n", 0o444)
        toolkit.add(tar, entries, FIXTURE, (STAND / "qualify.py").read_bytes(), 0o444)
        toolkit.add(tar, entries, PROBE, (STAND / "stand_probe.py").read_bytes(), 0o444)
    codex = entries["usr/bin/codex"]["sha256"]
    policy = entries[POLICY]["sha256"]
    if (codex != CODEX_SHA
            or policy != "b5ca627735987f47dafc24525f71114fdd04a9e62f234881d1176dad4965392d"):
        raise ValueError("CR pin mismatch")
    verify_dependencies(bundle, supply)
    with bundle.open("rb") as stream:
        subprocess.run([tools / "tar2sqfs.exe", "--quiet", "--no-skip", "--compressor", "gzip",
                        "--num-jobs", "1", "--defaults", "uid=0,gid=0,mode=0555,mtime=0", image],
                       stdin=stream, check=True)
    toolkit.verify_image(image, entries, tools)
    manifest = {
        "format": "symphony-qualification-worker/1", "cases": ["write", "descendants", "wait"],
        "fixture_sha256": entries[FIXTURE]["sha256"], "policy_sha256": policy,
        "image_sha256": toolkit.sha_file(image), "codex_sha256": codex,
        "python_sha256": entries["usr/bin/python3"]["sha256"],
        "probe_sha256": entries[PROBE]["sha256"],
    }
    data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    (output / "manifest.json").write_bytes(data)
    print(json.dumps({"manifest_sha256": hashlib.sha256(data).hexdigest(), **manifest,
                      "entries_verified": len(entries), "image_bytes": image.stat().st_size}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("supply", type=Path, help="verified offline supply directory from the checkpoint")
    parser.add_argument("output", type=Path, help="new build directory")
    args = parser.parse_args()
    build(args.supply.resolve(strict=True), args.output.resolve())
