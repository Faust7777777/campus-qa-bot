param(
    [int[]]$WatchPids = @(13244, 61800, 48872),
    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$watch = @(
    @{ Pid = 13244; Workspace = "work\luna_workspace_v5" },
    @{ Pid = 61800; Workspace = "work\luna_workspace_v6_fetch" },
    @{ Pid = 48872; Workspace = "work\luna_workspace_v7_search_v2" }
) | Where-Object { $_.Pid -in $WatchPids }
$v8Workspace = Join-Path $repo "work\luna_workspace_v8_current_catalog"
$watcherLog = Join-Path $v8Workspace "logs\slot-watcher.log"
New-Item -ItemType Directory -Force -Path (Split-Path $watcherLog) | Out-Null

function Test-WorkspaceComplete {
    param([string]$Workspace)

    $manifestPath = Join-Path $Workspace "manifest.json"
    $statusPath = Join-Path $Workspace "state\status.jsonl"
    if (-not (Test-Path -LiteralPath $manifestPath) -or -not (Test-Path -LiteralPath $statusPath)) {
        return $false
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $latest = @{}
    Get-Content -LiteralPath $statusPath | ForEach-Object {
        if ($_.Trim()) {
            $record = $_ | ConvertFrom-Json
            $latest[$record.batch] = $record
        }
    }
    if ($latest.Count -ne [int]$manifest.batch_count) {
        return $false
    }
    return -not @($latest.Values | Where-Object { -not $_.passed }).Count
}

while ($true) {
    foreach ($item in $watch) {
        if (Get-Process -Id $item.Pid -ErrorAction SilentlyContinue) {
            continue
        }
        $workspace = Join-Path $repo $item.Workspace
        if (-not (Test-WorkspaceComplete -Workspace $workspace)) {
            continue
        }
        $lockPath = Join-Path $v8Workspace "state\runner.lock"
        if (Test-Path -LiteralPath $lockPath) {
            "[$((Get-Date).ToString('o'))] v8 already has a runner lock; watcher exits" |
                Add-Content -LiteralPath $watcherLog
            exit 0
        }
        $runner = Join-Path $repo "scripts\run_luna_cleaning.ps1"
        $process = Start-Process -FilePath "pwsh" -WindowStyle Hidden -PassThru -WorkingDirectory $repo `
            -ArgumentList @(
                "-NoProfile",
                "-File", $runner,
                "-Workspace", "work\luna_workspace_v8_current_catalog",
                "-MaxBatches", "34",
                "-Model", "gpt-5.6-luna",
                "-BatchTimeoutMinutes", "15"
            )
        "[$((Get-Date).ToString('o'))] started v8 PID=$($process.Id) after $($item.Workspace) completed" |
            Add-Content -LiteralPath $watcherLog
        exit 0
    }
    Start-Sleep -Seconds $PollSeconds
}
