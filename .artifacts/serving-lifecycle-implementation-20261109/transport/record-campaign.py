"""Record completed evidence only; never starts tests, runtime or native processes."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

out = Path(__file__).resolve().parent
root = out.parents[2]
result = json.loads((out/'campaign-result.json').read_text(encoding='utf-8'))
manifest = json.loads((out/'syntax-and-source-manifest.json').read_text(encoding='utf-8'))
plan = json.loads((out/'campaign-plan.json').read_text(encoding='utf-8'))
log = (out/'campaign.log').read_text(encoding='utf-8')
summary = re.search(r'Summary: (\d+) files, (\d+) tests passed, (\d+) failed.*? in ([\d.]+)s', log)
if summary is None:
    raise SystemExit('No canonical summary; preserve UNKNOWN')
files, passed, failed = map(int, summary.group(1, 2, 3))
mismatches = [item['path'] for item in manifest['files']
              if hashlib.sha256((root/item['path']).read_bytes()).hexdigest() != item['sha256']]
per_file = []
for path in plan['files']:
    row = re.search(re.escape(path.replace('/', '\\'))+r' \((\d+)\u2713, ([\d.]+)s\)', log)
    if row is None:
        raise SystemExit('Per-file receipt missing: '+path)
    per_file.append({'path': path, 'passed': int(row[1]), 'seconds': float(row[2])})
valid = result['exit'] == 0 and files == len(plan['files']) and failed == 0 and not mismatches
proof = {
    'utc': datetime.now(timezone.utc).isoformat(),
    'status': 'FIXTURE_CONTRACT_PASS_NATIVE_HOLD' if valid else 'HOLD',
    'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
    'files': files, 'passed': passed, 'failed': failed,
    'runner_seconds': float(summary[4]), 'command_seconds': result['seconds'],
    'per_file': per_file, 'source_files_verified': len(manifest['files']),
    'source_mismatches': mismatches, 'campaign_runs': 1, 'campaign_remaining': 0,
    'retries': 0, 'native_qualified': False, 'overall_cancel': 'HOLD',
    'voice_chain': 'NOT_ACCEPTED',
    'receipt_hashes': {name: hashlib.sha256((out/name).read_bytes()).hexdigest()
                       for name in ('campaign.log', 'campaign-result.json', 'campaign-attempt.json')},
}
(out/'campaign-proof.json').write_text(json.dumps(proof, indent=2)+'\n', encoding='utf-8')
plan.update(status='CONSUMED_FINISHED', campaign_remaining=0)
(out/'campaign-plan.json').write_text(json.dumps(plan, indent=2)+'\n', encoding='utf-8')
print(json.dumps(proof))
