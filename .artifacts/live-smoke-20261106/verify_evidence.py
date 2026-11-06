"""Read-only verification pinned to the published evidence commit; no model calls."""
import argparse
import json
from pathlib import Path

from evidence_checks import DirectoryPacket, EvidenceVerifier, GitPacket


def verify(root=None):
    packet = DirectoryPacket(root) if root is not None else GitPacket(Path(__file__).resolve().parents[2])
    result = EvidenceVerifier(packet).verify()
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Published repository export, checked against pinned manifests")
    verify(parser.parse_args().root)
