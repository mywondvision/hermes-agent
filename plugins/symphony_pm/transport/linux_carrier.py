"""Pinned builtin-provider arguments and the primary-service credential boundary."""
import json
import tomllib

from .linux_sandbox import ENVIRONMENT, GUEST_SCRATCH
from .types import Refused

PROFILE = 'chatgpt-responses-private-v1'
PROBE_ENV = {'CODEX_SANDBOX_NETWORK_DISABLED': '1'}
PORT = 18463
BASE = f'https://127.0.0.1:{PORT}/backend-api'
PATH = '/backend-api/codex/responses'
HOME = ENVIRONMENT['CODEX_HOME']
AUTH = '/etc/hermes-symphony/subscription/auth.json'
SOCKET = '/run/hermes-symphony-connector/responses.sock'
CATALOG = '/etc/symphony/models.json'
SOURCE = '064c6b8c737f5b41d171fdda80bd9ef10ad06eb3'


def verify_config(config, profile):
    with config.open('rb') as stream:
        body = tomllib.load(stream)
    permitted = {'model', 'model_reasoning_effort', 'model_catalog_json', 'default_permissions', 'permissions',
                 'approval_policy', 'web_search', 'features', 'shell_environment_policy'}
    features = body.get('features', {})
    profiles = body.get('permissions', {})
    shell = body.get('shell_environment_policy', {})
    worker = {'filesystem': {':root': 'read', '/work': 'write', GUEST_SCRATCH: 'write', HOME: 'deny'},
              'network': {'enabled': False}}
    if (not set(body) <= permitted or body.get('model_catalog_json') != CATALOG
            or shell != {'inherit': 'none'}
            or body.get('default_permissions') != profile or profiles != {profile: worker}
            or body.get('approval_policy') != 'never'
            or body.get('web_search') != 'disabled' or type(features) is not dict
            or any(v is not False for v in features.values())
            or any(features.get(k) is not False for k in ('network_proxy', 'hooks', 'plugins', 'enable_mcp_apps',
                                                          'enable_request_compression'))):
        raise Refused('LINUX_PROVIDER_COMMAND_POLICY')


def require_sealed_primary(q, evidence):
    if (getattr(q, 'environment_ref', None) == PROFILE) != (evidence is not None):
        raise Refused('LINUX_PROVIDER_QUALIFICATION_REQUIRED')
    if evidence is not None:
        refuse_primary_dispatch()


def refuse_primary_dispatch():
    # This service runs as the journal owner and consumes an owner-writable launch spec.
    # Neither a digest nor a native fixture receipt can confer a separate client principal.
    raise Refused('LINUX_PROVIDER_SEPARATE_PRINCIPAL_REQUIRED')


def options():
    return ('-c', 'model_provider="openai"', '-c', 'forced_login_method="chatgpt"',
            '-c', 'cli_auth_credentials_store="file"', '-c', 'openai_base_url="' + BASE + '/codex"',
            '-c', 'chatgpt_base_url="' + BASE + '"')


def primary_exec(q):
    profile = 'default_permissions=' + json.dumps(q.permission_profile)
    return (q.executable, 'exec', '--skip-git-repo-check', '--ephemeral', '--json', '--strict-config',
            '-C', '/work', '-c', profile, '-')


def commands(q, targets, challenge):
    from .linux_carrier_probe import PROGRAM
    switches = options()
    probe = (q.executable, *switches, 'sandbox', '-P', q.permission_profile, '-C', '/work', '--include-managed-config', '--',
             q.probe_python, '-I', '-B', '-c', PROGRAM, challenge, json.dumps(targets),
             json.dumps(PROBE_ENV, sort_keys=True))
    return (q.executable, *switches, *primary_exec(q)[1:]), probe
