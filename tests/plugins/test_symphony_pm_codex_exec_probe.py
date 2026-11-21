"""The qualification boundary uses a complete command-environment and OS denial report."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.transport.codex_exec_probe import _ProbeWatch, precheck, probe_ok


def report(mode='precheck'):
    names = ['SystemDrive', 'SystemRoot', 'TEMP', 'TMP']
    steps = [{'name': 'env', 'ok': True, 'names': names, 'name_count': len(names)}]
    for name in ('read-auth', 'list-codex-home', 'read-outside', 'write-outside', 'loopback'):
        steps.append({'name': name, 'ok': False, 'errno': 13})
    return {'mode': mode, 'steps': steps}


def test_complete_allowlisted_names_and_denials_are_accepted():
    assert probe_ok(report())


@pytest.mark.parametrize(('names', 'count'), [
    (['SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP', 'UNRELATED_ENDPOINT'], 5),
    ([], 0), (['SYSTEMROOT', 'TEMP', 'TMP'], 3),
    (['SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP'], 5),
    (['SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP', 'Temp'], 5),
    (['SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP', 5], 5),
    (['SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP'], True),
])
def test_extras_or_incomplete_environment_refuse(names, count):
    data = report()
    data['steps'][0].update(names=names, name_count=count)
    assert not probe_ok(data)


def test_absent_names_or_unproven_denial_refuse():
    data = report()
    del data['steps'][0]['names']
    assert not probe_ok(data)
    data = report()
    data['steps'][1]['errno'] = 2  # missing canary does not establish an access denial
    assert not probe_ok(data)
    data['steps'][1].update(errno='malformed', winerror=5)
    assert not probe_ok(data)
    data['steps'][1].update(errno=13, winerror=32)
    assert not probe_ok(data)
    data['steps'][1].update(errno=13, winerror=10013)
    assert not probe_ok(data)
    data['steps'][1].update(errno=13, winerror=5)
    data['steps'][-1].update(errno=13, winerror=10013)
    assert probe_ok(data)
    data = report()
    data['steps'].append(dict(data['steps'][1]))
    assert not probe_ok(data)


@pytest.mark.parametrize('malformed', [None, {}, [], {'steps': [None]}, {'steps': [{}]}])
def test_malformed_reports_are_refused(malformed):
    assert not probe_ok(malformed)


@pytest.mark.parametrize(('exit_code', 'timed_out'), [(1, False), (None, True), (0, True)])
def test_failed_precheck_cannot_admit_a_well_formed_report(tmp_path, exit_code, timed_out):
    def run(_argv, _cwd, _timeout, _streams):
        (tmp_path/'precheck-report.json').write_text(json.dumps(report()), encoding='utf-8')
        return exit_code, timed_out
    q = SimpleNamespace(held_executable=nullcontext, executable='fixture', permission_profile='fixture')
    settings = SimpleNamespace(command_prefix=(), precheck_timeout=1)
    workspace = SimpleNamespace(path=tmp_path, src=tmp_path)
    observed = precheck(q, settings, SimpleNamespace(run_unbound=run), tmp_path, workspace, [])
    assert observed['verdict'] != 'PROBE_OK'


@pytest.mark.parametrize('content', ['{', 'null', json.dumps(report()), '{"steps":[],"steps":[]}'])
def test_published_invalid_inexec_report_stops_the_attempt(tmp_path, content):
    path = tmp_path/'inexec-report.json'
    path.write_text(content, encoding='utf-8')
    stops = []
    watch = _ProbeWatch(path, lambda: stops.append('stop'))
    assert watch._judge()
    assert watch.verdict == 'PROBE_VIOLATION' and stops == ['stop']
