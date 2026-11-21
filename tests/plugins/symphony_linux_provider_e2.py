"""Cold, fake-account Linux qualification. Imported only inside the opted-in native test."""
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import time
import tomllib
from types import SimpleNamespace
from uuid import uuid4

from plugins.symphony_pm.transport import linux_carrier as carrier
from plugins.symphony_pm.transport import linux_carrier_probe, linux_sandbox as sandbox
from plugins.symphony_pm.transport.linux_check_service import supervise
from plugins.symphony_pm.transport.linux_check_wire import CheckGroup
from plugins.symphony_pm.transport.linux_control import members, publish, strict_json
from plugins.symphony_pm.transport.linux_host import boot_identity, private_directory, process_identity, service_user_matches
from plugins.symphony_pm.transport.types import digest, sha
from tests.plugins.symphony_linux_provider_connector_e2 import connector, fake_connector
from tests.plugins.symphony_linux_responses_stub import responses_stub

ROOT = Path('/opt/hermes-symphony/e2/runtime')
INPUT = Path('/etc/hermes-symphony/provider-e2-input.json')
STATE = Path('/var/lib/hermes-symphony/provider-e2')
UNIT = 'hermes-symphony-provider-e2.service'
UNIT_PATH = '/system.slice/' + UNIT
GROUPS = Path('/sys/fs/cgroup')
CA = '/etc/symphony/e2-ca.pem'
CONNECTOR_SHA256 = '9c26e3a3577774b6e50fa9c3ee4daaf041f721faca1623de47bf7fa41cac2f8c'
Q = SimpleNamespace(executable='/usr/bin/codex', probe_python='/usr/bin/python3',
                    permission_profile='symphony-provider', command_environment=tuple(carrier.PROBE_ENV))
FILES = {'bwrap_sha256': Path('/usr/bin/bwrap'), 'codex_sha256': sandbox.in_image(ROOT, Q.executable),
         'python_sha256': sandbox.in_image(ROOT, Q.probe_python),
         'config_sha256': sandbox.in_image(ROOT, carrier.HOME + '/config.toml'),
         'catalog_sha256': sandbox.in_image(ROOT, carrier.CATALOG),
         'requirements_sha256': sandbox.in_image(ROOT, '/etc/codex/requirements.toml'),
         'connector_sha256': Path('/opt/hermes-symphony/e2/source/plugins/symphony_pm/transport/linux_responses_connector.py')}


def systemd_invocation():
    expected = {'Id': UNIT, 'ControlGroup': UNIT_PATH, 'ActiveState': 'active', 'PrivateNetwork': 'yes',
                'User': 'hermes-symphony', 'Group': 'hermes-symphony', 'KillMode': 'control-group',
                'Delegate': 'yes', 'NoNewPrivileges': 'yes', 'CapabilityBoundingSet': '', 'AmbientCapabilities': '',
                'TasksMax': '256', 'MemoryMax': '4294967296',
                'CPUQuotaPerSecUSec': '1s', 'RuntimeMaxUSec': '4min', 'NeedDaemonReload': 'no'}
    command = [str(sandbox.protected('/usr/bin/systemctl')), '--no-pager', '--no-ask-password',
               'show', '--all', '--property=' + ','.join((*expected, 'InvocationID')), UNIT]
    done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env={'LC_ALL': 'C'},
                          timeout=4, check=True)
    lines = done.stdout.decode('utf-8').splitlines()
    assert len(lines) == len(expected) + 1
    values = dict(line.split('=', 1) for line in lines)
    invocation = values.pop('InvocationID')
    assert values == expected and sha(invocation, 32)
    return invocation


def private_network(proc, host_net):
    net = os.readlink(proc/'ns/net')
    assert re.fullmatch(r'net:\[[0-9]+\]', net) and net != host_net
    interfaces = (proc/'net/dev').read_text(encoding='ascii').splitlines()[2:]
    assert [row.split(':')[0].strip() for row in interfaces] == ['lo']
    assert len((proc/'net/route').read_text(encoding='ascii').splitlines()) == 1
    ipv6 = proc/'net/ipv6_route'
    if ipv6.exists():
        assert all(line.split()[-1] == 'lo' for line in ipv6.read_text(encoding='ascii').splitlines())
    return net


def environment():
    record = strict_json(sandbox.protected(INPUT))
    assert set(record) == {'purpose', 'source', 'boot', 'host_net', 'image_sha256', *FILES}
    assert record['purpose'] == 'fake-only-provider-e2-v2' and record['source'] == carrier.SOURCE
    assert record['boot'] == boot_identity() and sha(record['image_sha256'], 64)
    assert service_user_matches('hermes-symphony', 'hermes-symphony')
    assert systemd_invocation() == os.environ['INVOCATION_ID']
    assert os.fsdecode(Path('/proc/self/cgroup').read_bytes()).strip() == '0::' + UNIT_PATH
    assert re.fullmatch(r'net:\[[0-9]+\]', record['host_net'])
    private_network(Path('/proc/self'), record['host_net'])
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text(encoding='utf-8').splitlines())
    assert status['NoNewPrivs'].strip() == '1' and all(int(status[k], 16) == 0 for k in ('CapEff', 'CapAmb', 'CapBnd'))
    assert Path(connector.__file__).resolve(strict=True) == FILES['connector_sha256']
    assert record['connector_sha256'] == CONNECTOR_SHA256
    sandbox.protected(ROOT, directory=True)
    sandbox.verify_image(ROOT, record['image_sha256'])
    assert os.statvfs(ROOT).f_flag & os.ST_RDONLY
    assert {key: sandbox.file_digest(path) for key, path in FILES.items()} == {key: record[key] for key in FILES}
    with FILES['codex_sha256'].open('rb') as src:
        assert src.read(4) == b'\x7fELF'
    home = sandbox.protected(sandbox.in_image(ROOT, carrier.HOME), directory=True)
    assert {item.name for item in home.iterdir()} == {'config.toml', '.symphony-qualification-marker'}
    sandbox.protected(home/'.symphony-qualification-marker')
    sandbox.protected(sandbox.in_image(ROOT, CA))
    carrier.verify_config(FILES['config_sha256'], Q.permission_profile)
    with FILES['requirements_sha256'].open('rb') as src:
        assert tomllib.load(src) == {'chatgpt_base_url': carrier.BASE}
    private_directory(STATE)
    return record


def synthetic_files(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    authority = ec.generate_private_key(ec.SECP256R1())
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ephemeral E2 CA')])
    localhost = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'loopback E2')])
    now = datetime.now(UTC)
    builder = x509.CertificateBuilder().issuer_name(issuer).not_valid_before(now - timedelta(hours=1)).not_valid_after(
        now + timedelta(days=1))
    issuer_id = x509.SubjectKeyIdentifier.from_public_key(authority.public_key())
    authority_id = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(issuer_id)
    ca = (builder.subject_name(issuer).public_key(authority.public_key()).serial_number(x509.random_serial_number())
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), True)
          .add_extension(issuer_id, False).add_extension(authority_id, False).sign(authority, hashes.SHA256()))
    leaf = (builder.subject_name(localhost).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(authority_id, False)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
                                                        x509.DNSName('chatgpt.com')]), False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True).sign(authority, hashes.SHA256()))
    cert, endpoint, private = directory/'ca.pem', directory/'server.pem', directory/'server.key'
    cert.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    endpoint.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(endpoint, private)
    private.unlink()
    def encode(value):
        return urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode('ascii')
    claims = {'https://api.openai.com/auth': {'chatgpt_plan_type': 'pro', 'chatgpt_account_id': 'fixture-account'}}
    token = '.'.join((encode({'alg': 'none', 'typ': 'JWT'}), encode(claims), 'c2lnbmF0dXJl'))
    auth = directory/'synthetic.json'
    auth.write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'id_token': token, 'access_token': 'fixture-token',
                    'refresh_token': 'fixture-refresh', 'account_id': 'fixture-account'},
                    'last_refresh': '2099-01-01T00:00:00Z'}), encoding='ascii')
    return tls, cert, auth


def guest(argv, work, cert, auth):
    spec = {'bwrap': str(FILES['bwrap_sha256']), 'runtime': str(ROOT), 'workspace': str(work)}
    wrap = sandbox.sandbox_command(spec, argv)
    wrap.insert(2, '--share-net')  # Already inside the measured private test unit with only loopback.
    overlays = ['--tmpfs', carrier.HOME, '--ro-bind', str(auth), carrier.HOME + '/auth.json',
                '--ro-bind', str(cert), CA, '--setenv', 'CODEX_CA_CERTIFICATE', CA]
    for name in ('config.toml', '.symphony-qualification-marker'):
        target = carrier.HOME + '/' + name
        overlays.extend(('--ro-bind', str(sandbox.in_image(ROOT, target)), target))
    split = wrap.index('--')
    return wrap[:split] + overlays + wrap[split:]


def cli_identity(group, fd, argv, host_net):
    path = '/' + str(group.relative_to(GROUPS))
    wire = b'\x00'.join(os.fsencode(a) for a in argv) + b'\x00'
    exe = FILES['codex_sha256'].stat()
    found = []
    for process in members(fd, path):
        proc = Path('/proc')/str(process.pid)
        try:
            if (proc/'cmdline').read_bytes() != wire or not os.path.samestat((proc/'exe').stat(), exe):
                continue
            ns = {kind: os.readlink(proc/'ns'/kind) for kind in ('mnt', 'net', 'pid')}
            assert private_network(proc, host_net) == ns['net']
            assert process_identity(process.pid) == process and process in members(fd, path)
            found.append({'process': asdict(process), 'namespaces': ns})
        except (FileNotFoundError, ProcessLookupError):
            continue
    assert len(found) == 1
    return found[0]


def execute(argv, work, cert, auth, prompt, timeout, directory, label):
    with (directory/(label + '.stdout')).open('xb') as out, (directory/(label + '.stderr')).open('xb') as err:
        run = subprocess.run(guest(argv, work, cert, auth), input=prompt.encode(), stdout=out, stderr=err,
                             cwd=work, env={}, timeout=timeout, check=False, close_fds=True)
    return run.returncode


@contextmanager
def local_endpoints(nonce, tls, cert, origin, namespaces, observed_scope, port):
    with responses_stub(nonce, tls=tls, client_namespaces=namespaces, port=0) as upstream:
        with fake_connector(upstream.origin, cert, observed_scope) as (forward, calls):
            with responses_stub(nonce, tls=tls, policy_origin=origin, forward_post=forward, port=port) as ingress:
                yield ingress, upstream, calls


def connector_scope(group, fd, host_net):
    proc = Path('/proc/thread-self')
    path = '/' + str(group.relative_to(GROUPS))
    assert os.fsdecode((proc/'cgroup').read_bytes()).strip() == '0::' + path
    net = private_network(proc, host_net)
    process = process_identity(os.getpid())
    assert process in members(fd, path) and os.path.samestat(os.fstat(fd), group.lstat())
    return {'net': net, 'cgroup': path, 'cgroup_inode': os.fstat(fd).st_ino, 'process': asdict(process)}


def verify_policy_order(ingress, upstream, expected_posts):
    assert ingress.first_policy_index is not None and ingress.policy_requests > 0
    assert ingress.request_methods[ingress.first_policy_index] == 'GET'
    assert ingress.request_methods.count('POST') == expected_posts
    assert upstream.request_methods == ['POST'] * expected_posts
    if expected_posts:
        assert ingress.first_policy_index < ingress.request_methods.index('POST')


def exchange(directory, nonce, group, fd, record, origin):
    work = directory/'work'
    work.mkdir(mode=0o700)
    tls, cert, auth = synthetic_files(directory)
    hidden = {}
    for key in ('deny_control', 'deny_journal', 'deny_source', 'deny_neighbor', 'deny_credentials'):
        marker = directory/key
        marker.write_text(nonce, encoding='ascii')
        hidden[key] = str(marker)
    hidden['deny_owner_processes'] = f'/proc/{os.getpid()}/root' + hidden['deny_control']
    argv, probe = carrier.commands(Q, {'hidden': hidden, 'port': carrier.PORT}, nonce)
    identities = []
    def namespaces():
        measured = cli_identity(group, fd, argv, record['host_net'])
        assert measured['namespaces']['net'] == os.readlink('/proc/self/ns/net')
        identities.append(measured)
        return measured['namespaces']
    def observed_scope():
        return connector_scope(group, fd, record['host_net'])
    with local_endpoints(nonce, tls, cert, origin, namespaces, observed_scope, carrier.PORT) as (ingress, upstream, calls):
        assert ingress.origin + '/backend-api' == carrier.BASE
        assert execute(probe, work, cert, auth, '', 20, directory, 'probe') == 0
        report = strict_json(directory/'probe.stdout')
        binding = SimpleNamespace(unit=UNIT, invocation_id=os.environ['INVOCATION_ID'])
        scope = {'unit': UNIT, 'invocation': binding.invocation_id, 'net': os.readlink('/proc/self/ns/net')}
        precheck = linux_carrier_probe.verify(report, scope, binding, Q, nonce, record['host_net'])
        code = execute(argv, work, cert, auth, 'Run only the fixture tool once. ' + nonce, 55, directory, 'exec')
    verify_policy_order(ingress, upstream, 2 if origin == 'NO_CONSTRAINT' else 0)
    assert ingress.responses == 0
    result = {'origin': origin, 'precheck': precheck, 'policy_requests': ingress.policy_requests,
              'ingress_methods': ingress.request_methods, 'first_policy_index': ingress.first_policy_index,
              'upstream_methods': upstream.request_methods,
              'exit': code, 'responses': upstream.responses, 'connector_posts': len(calls), 'connector': calls}
    if origin == 'NO_CONSTRAINT':
        assert code == 0 and len(identities) == 1 and len(calls) == upstream.responses == 2
        assert all(call['net'] == scope['net'] for call in calls)
        receipt = upstream.receipt()
        assert receipt['tool_output_source'] == 'linux-cli'
        result.update(exchange=receipt, client=identities[0])
    else:
        assert code != 0 and upstream.responses == 0 and not calls
        log = (directory/'exec.stdout').read_bytes() + (directory/'exec.stderr').read_bytes()
        assert b'required ChatGPT backend conflicts with workspace routing' in log
    auth.unlink()
    return result


def payload(directory, group, fd, record, nonce):
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 << 20, 4 << 20))
    assert os.fsdecode(Path('/proc/self/cgroup').read_bytes()).strip() == '0::/' + str(group.relative_to(GROUPS))
    private_network(Path('/proc/self'), record['host_net'])
    observations = []
    for number, origin in enumerate(('NO_CONSTRAINT', 'https://conflict.invalid')):
        segment = directory/str(number)
        segment.mkdir(mode=0o700)
        observations.append(exchange(segment, nonce + str(number), group, fd, record, origin))
    publish(directory, 'observations.json', observations)


def run():
    if not __debug__:
        raise RuntimeError('E2 requires active assertions')
    from plugins.symphony_pm.transport.linux_host import require_fork
    record = environment()
    nonce = uuid4().hex
    directory, group = STATE/nonce, GROUPS/UNIT_PATH.lstrip('/')/('payload-' + nonce)
    directory.mkdir(mode=0o700)
    group.mkdir()
    fd = os.open(group, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
    try:
        assert (group/'cgroup.kill').is_file()
        reader, writer = os.pipe()
        pid = require_fork()()
        if pid == 0:
            os.close(writer)
            code = 1
            try:
                if os.read(reader, 1) == b'1':
                    os.close(reader)
                    payload(directory, group, fd, record, nonce)
                    code = 0
            except BaseException as error:
                publish(directory, 'failure.json', {'class': type(error).__name__})
            finally:
                os._exit(code)
        os.close(reader)
        try:
            (group/'cgroup.procs').write_text(str(pid), encoding='ascii')
            root = process_identity(pid)
            path = '/' + str(group.relative_to(GROUPS))
            assert root and members(fd, path) == (root,)
            stat = os.fstat(fd)
            now = time.monotonic()
            request = {'started': now, 'expires': now + 165, 'nonce': nonce, 'inputs': digest(record)}
            bound = CheckGroup(nonce, 'cold-fake-only', 1, digest(record), boot_identity(), UNIT,
                               os.environ['INVOCATION_ID'], digest(request), path, stat.st_dev, stat.st_ino, root)
            publish(directory, 'binding.json', {'binding': asdict(bound), 'request': request})
            os.write(writer, b'1')
        finally:
            os.close(writer)
        supervise(directory, group, fd, bound, request, 'started')
        stop = strict_json(directory/'stop')
        assert stop['binding'] == digest(asdict(bound)) and stop['returncode'] == 0 and not stop['timed_out']
        assert stop['sealed'] and stop['populated'] == 0 and not members(fd, path)
        assert os.path.samestat(os.fstat(fd), group.lstat())
        group.rmdir()
        publish(directory, 'cold-e2.json', {'inputs': record, 'binding': digest(asdict(bound)),
                'stop': digest(stop), 'observations': strict_json(directory/'observations.json')})
    finally:
        os.close(fd)
    return directory
