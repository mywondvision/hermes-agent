"""Fixed systemd ExecStart target in the measured controller installation."""
from pathlib import Path
import sys


if __name__ == '__main__':
    if not sys.flags.isolated or not sys.flags.no_site or len(sys.argv) != 3:
        raise SystemExit(125)
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from plugins.symphony_pm.transport.linux_check_service import serve
    serve(sys.argv[1], sys.argv[2])
