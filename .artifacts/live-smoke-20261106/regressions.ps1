$ErrorActionPreference = 'Stop'
Set-Location 'D:/AI/worktrees/hermes-pm-ingress-20261031'
$env:HERMES_PYTHON = 'D:/AI/envs/hermes-pm-ingress-test-20261106/Scripts/python.exe'
$env:HERMES_HOME = "$PWD/.artifacts/live-smoke-20261106/regression-home"
$env:HERMES_RUNTIME_DIR = "$PWD/.artifacts/test-runtime"
$env:TEMP = "$PWD/.artifacts/live-smoke-20261106/temp"
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HERMES_TEST_FILE_RETRIES = '0'
$env:__HERMES_ACTIVATED = $null
$env:__HERMES_TESTS_REEXEC = $null
New-Item -ItemType Directory -Force -Path $env:HERMES_HOME,$env:TEMP | Out-Null
& 'C:/Program Files/Git/bin/bash.exe' scripts/run_tests.sh tests/tools/test_registry.py tests/tools/test_toolsets.py -j 1 --file-retries 0 -v *> .artifacts/live-smoke-20261106/regressions.txt
$testExit = $LASTEXITCODE
Get-Content .artifacts/live-smoke-20261106/regressions.txt -Tail 14
exit $testExit
