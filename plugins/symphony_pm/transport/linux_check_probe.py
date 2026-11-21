"""Fixed boundary checks inside the checker image immediately before exec of an approved tool."""
from string import Template

from .linux_sandbox import GUEST_SCRATCH

PROGRAM = Template(r'''
import json, os, sys
expected, hidden = json.loads(sys.argv[1]), json.loads(sys.argv[2])
def deny():
    raise SystemExit(124)
for kind, original in expected.items():
    if os.readlink('/proc/self/ns/' + kind) == original:
        deny()
if set(os.environ) != {'HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_ALL', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD'}:
    deny()
if not os.statvfs('/work').f_flag & os.ST_RDONLY or not os.statvfs('/').f_flag & os.ST_RDONLY:
    deny()
with open('/proc/net/dev', encoding='ascii') as f:
    if [line.split(':')[0].strip() for line in f.read().splitlines()[2:]] != ['lo']:
        deny()
for path in hidden:
    try:
        os.stat(path)
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        continue
    deny()
if any(os.path.lexists(p) for p in ('/usr/bin/git','/bin/git','/usr/lib/git-core','/usr/libexec/git-core','/work/.git')):
    deny()
with open('$guest_scratch/check-scratch', 'xb') as f:
    f.write(b'x')
os.unlink('$guest_scratch/check-scratch')
os.execve(sys.argv[3], sys.argv[3:], dict(os.environ))
''').substitute(guest_scratch=GUEST_SCRATCH)
