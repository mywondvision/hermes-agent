param([switch]$Baseline,[switch]$EvidenceFix,[switch]$RuntimeFix,[switch]$CleanupFix)
$ErrorActionPreference = 'Stop'
Set-Location 'D:/AI/worktrees/hermes-pm-ingress-20261031'
$env:HERMES_PYTHON = 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe'
$env:HERMES_HOME = "$PWD/.artifacts/cancel-correction-20261106/test-home"
$env:HERMES_RUNTIME_DIR = "$PWD/.artifacts/test-runtime"
$env:TEMP = "$PWD/.artifacts/cancel-correction-20261106/temp"
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HERMES_TEST_FILE_RETRIES = '0'
$env:__HERMES_ACTIVATED = $null
$env:__HERMES_TESTS_REEXEC = $null
New-Item -ItemType Directory -Force -Path $env:HERMES_HOME,$env:TEMP | Out-Null
if ($Baseline) {
    & 'C:/Program Files/Git/bin/bash.exe' scripts/run_tests.sh .artifacts/cancel-correction-20261106/baseline-probe/tests/plugins/test_symphony_smoke_cancel.py -k test_windows_receiver_refuses_unticketed_cancel -j 1 --file-retries 0 -v *> .artifacts/cancel-correction-20261106/baseline-red.txt
    $testExit = $LASTEXITCODE
    Get-Content .artifacts/cancel-correction-20261106/baseline-red.txt -Tail 24
} elseif ($EvidenceFix) {
    & 'C:/Program Files/Git/bin/bash.exe' scripts/run_tests.sh tests/plugins/test_symphony_smoke_evidence.py -k 'test_broken_cross_file_identity_or_lifecycle_rejected_after_resealing and request and not cancel_ledger_request' -j 1 --file-retries 0 -v *> .artifacts/cancel-correction-20261106/evidence-fix.txt
    $testExit = $LASTEXITCODE
    Get-Content .artifacts/cancel-correction-20261106/evidence-fix.txt -Tail 18
} elseif ($RuntimeFix) {
    & 'C:/Program Files/Git/bin/bash.exe' scripts/run_tests.sh tests/plugins/test_symphony_smoke_cancel.py -k test_real_runner_timeout_routes_to_ticket_and_keeps_both_wait_limits -j 1 --file-retries 0 -v *> .artifacts/cancel-correction-20261106/runtime-fix.txt
    $testExit = $LASTEXITCODE
    Get-Content .artifacts/cancel-correction-20261106/runtime-fix.txt -Tail 18
} elseif ($CleanupFix) {
    & 'C:/Program Files/Git/bin/bash.exe' scripts/run_tests.sh tests/plugins/test_symphony_smoke_cancel.py -j 1 --file-retries 0 -v *> .artifacts/cancel-correction-20261106/cleanup-fix.txt
    $testExit = $LASTEXITCODE
    Get-Content .artifacts/cancel-correction-20261106/cleanup-fix.txt -Tail 18
} else {
    & 'C:/Program Files/Git/bin/bash.exe' scripts/run_tests.sh tests/plugins/test_symphony_smoke_cancel.py tests/plugins/test_symphony_smoke_evidence.py -j 1 --file-retries 0 -v *> .artifacts/cancel-correction-20261106/targeted.txt
    $testExit = $LASTEXITCODE
    Get-Content .artifacts/cancel-correction-20261106/targeted.txt -Tail 24
}
exit $testExit
