"""systemd ExecStart target for a qualified, read-only installation (python -I -S)."""
from pathlib import Path
import sys


def main():
    if not sys.flags.isolated or not sys.flags.no_site or len(sys.argv) != 3:
        raise SystemExit(125)
    # This file and the complete source tree are on the checked controller image.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from plugins.symphony_pm.transport.linux_controller import serve
    serve(sys.argv[1], sys.argv[2])


if __name__ == '__main__':
    main()
