[CmdletBinding()]
param(
    [string]$Worktree = (Get-Location).Path,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CheckpointRef
)

$ErrorActionPreference = 'Stop'
$worktreePath = (Resolve-Path -LiteralPath $Worktree).ProviderPath
$root = git -C $worktreePath rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0) { throw 'Worktree must be an existing Git checkout.' }
$root = (Resolve-Path -LiteralPath $root).ProviderPath
$commit = git -C $root rev-parse --verify --end-of-options ("${CheckpointRef}^{commit}")
if ($LASTEXITCODE -ne 0) { throw 'Fetch the published checkpoint ref before launching.' }
$handoff = "${commit}:docs/handoffs/symphony-checkpoint-20261121.md"
$type = git -C $root cat-file -t $handoff
if ($LASTEXITCODE -ne 0 -or $type -ne 'blob') { throw 'The checkpoint has no handoff document.' }
$null = Get-Command codex -ErrorAction Stop
$shell = (Get-Command powershell.exe -CommandType Application).Source

# A role guard spans the interactive CLI's lifetime, including across launcher retries.
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToUpperInvariant()))
} finally {
    $sha.Dispose()
}
$guardPrefix = 'Local\HermesSymphony-' + [BitConverter]::ToString($digest).Replace('-', '')
$launchTag = 'Symphony-' + [Guid]::NewGuid().ToString('N')
$roleScope = [ordered]@{
    coordinator = 'Coordinate the two existing roles. Do not create replacement workers or a second controller.'
    windows = 'Own only the Windows work listed for the Windows session. Do not dispatch another Windows worker or act as coordinator.'
    linux = 'Own only the Linux work listed for the Linux session. Do not dispatch another Linux worker or act as coordinator.'
}
$worker = @'
$ErrorActionPreference = 'Stop'
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$Host.UI.RawUI.WindowTitle = "Symphony - $($p.role)"
$guard = [Threading.Mutex]::new($false, $p.guard)
$acquired = $false
try {
    try { $acquired = $guard.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { Write-Host "The $($p.role) session already has an open launcher."; return }
    Set-Location -LiteralPath $p.root
    & codex -C $p.root $p.prompt
    if ($LASTEXITCODE -ne 0) { throw "Codex exited with code $LASTEXITCODE." }
} catch {
    Write-Host $_ -ForegroundColor Red
    $null = Read-Host 'Press Enter to close'
} finally {
    if ($acquired) { $guard.ReleaseMutex() }
    $guard.Dispose()
}
'@

foreach ($role in $roleScope.Keys) {
    $prompt = @(
        "$launchTag role=$role. You are the $role session for the Hermes Symphony checkpoint. Reply in Russian."
        "First read the published handoff with git show $handoff and verify its resume preconditions against current evidence."
        $roleScope[$role]
        "The owner requests exactly one Codex goal per role. Inspect get_goal in this fresh thread. If absent, call create_goal once with the objective and boundaries for your role from the handoff. Reuse a matching active goal. If an existing goal differs, is terminal or needs owner resumption, ask the owner. Verify with get_goal."
        "Report your role, actual fresh thread ID, goal ID and status to the owner. State explicitly when an ID is unavailable. Never substitute an old ID, start a replacement session or duplicate a goal."
        "If peer lookup is available, find this launch's other roles through read-only thread listing and verify the exact launch tag and role in each first user prompt. The coordinator records the three confirmed receipts. If peer lookup is unavailable, ask the owner to relay the two worker receipts to the coordinator."
        "Before acting, reconcile existing controllers, in-flight operations and leases. Preserve all CR approvals and holds from the handoff. Do not start duplicate controllers, acquire a new lease, make cross-role writes or retry an operation with an unknown outcome."
        "Stay interactive. Ask the owner directly for outstanding approvals, login or user presence and wait for the answer. Continue only within the checkpoint's authorized scope."
    ) -join ' '
    $payload = @{ role = $role; root = $root; guard = "$guardPrefix-$role"; prompt = $prompt } | ConvertTo-Json -Compress
    $encodedPayload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
    $command = $worker.Replace('__PAYLOAD__', $encodedPayload)
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Start-Process -FilePath $shell -WorkingDirectory $root -WindowStyle Normal -ArgumentList @('-NoLogo', '-NoProfile', '-EncodedCommand', $encodedCommand)
}
