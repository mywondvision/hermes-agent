"""Fixed boundary checks inside the checker image immediately before exec of an approved tool."""
from string import Template

from .linux_sandbox import GUEST_SCRATCH

_PROBE = Template(r'''
import json, os, sys
expected, hidden = json.loads(sys.argv[1]), json.loads(sys.argv[2])
def deny():
    raise SystemExit(124)
for kind, original in expected.items():
    if os.readlink('/proc/self/ns/' + kind) == original:
        deny()
if set(os.environ) != {'HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_ALL', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD'$extra_names}:
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
$check_git
with open('$guest_scratch/check-scratch', 'xb') as f:
    f.write(b'x')
os.unlink('$guest_scratch/check-scratch')
$launch
''')

PROGRAM = _PROBE.substitute(guest_scratch=GUEST_SCRATCH, extra_names='', check_git="""
if any(os.path.lexists(p) for p in ('/usr/bin/git','/bin/git','/usr/lib/git-core','/usr/libexec/git-core','/work/.git')):
    deny()
""", launch='os.execve(sys.argv[3], sys.argv[3:], dict(os.environ))')

GIT_ENV = {'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': ''}
GIT_PROGRAM = _PROBE.substitute(guest_scratch=GUEST_SCRATCH, extra_names=', ' + ', '.join(map(repr, GIT_ENV)),
    check_git="""
import subprocess
def git(*args):
    return subprocess.check_output(['/usr/bin/git', *args], stderr=subprocess.DEVNULL).strip()
if (os.getcwd() != '/work' or git('rev-parse', '--absolute-git-dir') != b'/work/.git'
        or git('rev-parse', 'HEAD') != sys.argv[3].encode('ascii')
        or git('rev-parse', 'HEAD^') != sys.argv[4].encode('ascii') or git('remote')
        or os.path.lexists('/work/.git/objects/info/alternates')):
    deny()
""", launch="""
# Only the trusted probe can reach this point before project code starts.
os.write(1, b'G')
with open(os.devnull, 'wb') as sink:
    os.dup2(sink.fileno(), 1)
try:
    os.execve(sys.argv[5], sys.argv[5:], dict(os.environ))
except OSError:
    deny()
""")
