param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,
    [Parameter(Mandatory = $true)]
    [string]$CollectedOutput,
    [Parameter(Mandatory = $true)]
    [string]$ReviewedOutput,
    [Parameter(Mandatory = $true)]
    [string]$ReviewReport,
    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspacePath = Join-Path $repo $Workspace
$logPath = Join-Path $workspacePath "logs\postprocess-watcher.log"
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null

function Test-WorkspaceComplete {
    $manifestPath = Join-Path $workspacePath "manifest.json"
    $statusPath = Join-Path $workspacePath "state\status.jsonl"
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

while (-not (Test-WorkspaceComplete)) {
    Start-Sleep -Seconds $PollSeconds
}

$collectedPath = Join-Path $repo $CollectedOutput
$reviewedPath = Join-Path $repo $ReviewedOutput
$reportPath = Join-Path $repo $ReviewReport
try {
    if (-not (Test-Path -LiteralPath $collectedPath)) {
        & campus-qa-kb luna-collect --workspace $workspacePath --output $collectedPath
        if ($LASTEXITCODE -ne 0) {
            throw "luna-collect failed with exit code $LASTEXITCODE"
        }
    }
    $reviewedExists = Test-Path -LiteralPath $reviewedPath
    $reportExists = Test-Path -LiteralPath $reportPath
    if ($reviewedExists -xor $reportExists) {
        throw "review output/report presence mismatch; refusing implicit overwrite"
    }
    if (-not $reviewedExists) {
        & campus-qa-kb review --input $collectedPath --output $reviewedPath --report $reportPath
        if ($LASTEXITCODE -ne 0) {
            throw "review failed with exit code $LASTEXITCODE"
        }
    }
    "[$((Get-Date).ToString('o'))] strict collect and pending review completed" |
        Add-Content -LiteralPath $logPath
} catch {
    "[$((Get-Date).ToString('o'))] postprocess failed: $($_.Exception.Message)" |
        Add-Content -LiteralPath $logPath
    throw
}
