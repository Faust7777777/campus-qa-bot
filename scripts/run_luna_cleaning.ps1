param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,
    [int]$MaxBatches = 25,
    [string]$Model = "gpt-5.6-luna",
    [ValidateRange(1, 120)]
    [int]$BatchTimeoutMinutes = 20
)

$ErrorActionPreference = "Stop"
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$lockPath = Join-Path $workspacePath "state\runner.lock"
$statusPath = Join-Path $workspacePath "state\status.jsonl"
$runnerLog = Join-Path $workspacePath "logs\runner.log"
$codexLauncher = (Get-Command codex.cmd -ErrorAction Stop).Source
$codexScript = Join-Path (Split-Path -Parent $codexLauncher) "node_modules\@openai\codex\bin\codex.js"
if (-not (Test-Path -LiteralPath $codexScript)) {
    throw "Cannot resolve the Codex npm entry point from $codexLauncher"
}
$nodePath = (Get-Command node.exe -ErrorAction Stop).Source

function Invoke-LunaProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Prompt,
        [Parameter(Mandatory = $true)]
        [string]$FinalPath,
        [Parameter(Mandatory = $true)]
        [string]$LogPath,
        [bool]$EnableInternet = $false
    )

    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = $nodePath
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardInput = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true

    $arguments = [System.Collections.Generic.List[string]]::new()
    $arguments.Add($codexScript)
    foreach ($argument in @("-a", "never")) {
        $arguments.Add($argument)
    }
    if ($EnableInternet) {
        $arguments.Add("--search")
    }
    foreach ($argument in @(
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-m", $Model,
        "-s", "workspace-write",
        "-C", $workspacePath,
        "-o", $FinalPath,
        "-"
    )) {
        $arguments.Add($argument)
    }
    foreach ($argument in $arguments) {
        $processInfo.ArgumentList.Add($argument)
    }

    $process = [System.Diagnostics.Process]::Start($processInfo)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.StandardInput.Write($Prompt)
    $process.StandardInput.Close()

    $finished = $process.WaitForExit($BatchTimeoutMinutes * 60 * 1000)
    if (-not $finished) {
        $process.Kill($true)
        $process.WaitForExit()
    }
    $stdoutText = $stdout.GetAwaiter().GetResult()
    $stderrText = $stderr.GetAwaiter().GetResult()
    if ($stdoutText) {
        $stdoutText | Add-Content -LiteralPath $LogPath -Encoding utf8
    }
    if ($stderrText) {
        $stderrText | Add-Content -LiteralPath $LogPath -Encoding utf8
    }
    return [pscustomobject]@{
        ExitCode = if ($finished) { $process.ExitCode } else { 124 }
        TimedOut = -not $finished
    }
}

try {
    New-Item -ItemType File -Path $lockPath -ErrorAction Stop | Out-Null
} catch {
    throw "A Luna runner is already active or left a lock at $lockPath"
}

try {
    $batches = Get-ChildItem -LiteralPath (Join-Path $workspacePath "inputs") -Filter "batch_*.jsonl" |
        Sort-Object Name |
        Select-Object -First $MaxBatches
    foreach ($batch in $batches) {
        $outputPath = Join-Path $workspacePath ("outputs\" + $batch.Name)
        $reportPath = Join-Path $workspacePath ("reports\" + $batch.BaseName + ".json")
        $finalPath = Join-Path $workspacePath ("state\" + $batch.BaseName + ".final.txt")
        $batchLog = Join-Path $workspacePath ("logs\" + $batch.BaseName + ".log")

        if ((Test-Path -LiteralPath $outputPath) -and (Test-Path -LiteralPath $reportPath)) {
            $existing = Get-Content -Raw -LiteralPath $reportPath | ConvertFrom-Json
            if ($existing.passed) {
                continue
            }
        }

        $relativeInput = "inputs/$($batch.Name)"
        $relativeOutput = "outputs/$($batch.Name)"
        $batchActions = @(
            Get-Content -LiteralPath $batch.FullName -Encoding utf8 |
                ForEach-Object { ($_ | ConvertFrom-Json).action } |
                Sort-Object -Unique
        )
        if ($batchActions.Count -ne 1) {
            throw "Batch must contain exactly one action: $($batch.Name)"
        }
        $batchAction = $batchActions[0]
        if ($batchAction -eq "verify_refresh_and_extract") {
            $enableInternet = $false
            $prompt = "Follow CLEAN_PROTOCOL.md exactly. Process every item in $relativeInput and write only $relativeOutput. This is an offline transformation of captured seed_description snapshots. Do not access the network, URLs, browsers, or external tools. Preserve one output line per input source_id and update the output after each item. Validate JSONL, SHA-256 hashes, and exact evidence substrings before finishing."
        } else {
            $enableInternet = $true
            $prompt = "Follow WORKER_PROTOCOL.md exactly. Process every item in $relativeInput and write only $relativeOutput. The batch action is $batchAction. Use only the route defined for that action, avoid captcha-based web pages, preserve one output line per input source_id, and validate JSONL, hashes, and evidence substrings before finishing."
        }

        $started = Get-Date
        "[$($started.ToString('o'))] START $($batch.Name)" | Add-Content -LiteralPath $runnerLog
        $run = Invoke-LunaProcess -Prompt $prompt -FinalPath $finalPath -LogPath $batchLog `
            -EnableInternet $enableInternet
        $exitCode = $run.ExitCode

        if ($exitCode -eq 0 -and (Test-Path -LiteralPath $outputPath)) {
            & campus-qa-kb luna-validate --batch $batch.FullName --output $outputPath --report $reportPath `
                *>> $batchLog
            $validationExit = $LASTEXITCODE
        } else {
            $validationExit = 1
        }

        if ($validationExit -ne 0 -and (Test-Path -LiteralPath $outputPath)) {
            $repairPrompt = "The deterministic validator rejected $relativeOutput. Read the applicable protocol, $relativeInput, $relativeOutput, and reports/$($batch.BaseName).json. Repair only that output file: contract fields, missing or duplicate source IDs, content hashes, exact evidence substrings, and cardinality. Do not add unsupported facts or access anything outside this workspace. On Windows, write the JSONL with PowerShell or Python file I/O; do not call apply_patch for workspace data files because the Windows sandbox may reject it. Preserve one output line per input source_id, including unresolved/search_failed lines when evidence cannot be verified. Validate it again before finishing."
            $repairRun = Invoke-LunaProcess -Prompt $repairPrompt -FinalPath $finalPath `
                -LogPath $batchLog -EnableInternet $false
            if ($repairRun.ExitCode -eq 0) {
                & campus-qa-kb luna-validate --batch $batch.FullName --output $outputPath --report $reportPath `
                    *>> $batchLog
                $validationExit = $LASTEXITCODE
            }
        }

        $finished = Get-Date
        $record = [ordered]@{
            batch = $batch.Name
            action = $batchAction
            started_at = $started.ToString("o")
            finished_at = $finished.ToString("o")
            elapsed_seconds = [math]::Round(($finished - $started).TotalSeconds, 2)
            codex_exit = $exitCode
            timed_out = $run.TimedOut
            validation_exit = $validationExit
            passed = ($validationExit -eq 0)
        }
        ($record | ConvertTo-Json -Compress) | Add-Content -LiteralPath $statusPath
        "[$($finished.ToString('o'))] END $($batch.Name) passed=$($record.passed)" |
            Add-Content -LiteralPath $runnerLog
    }
} catch {
    "[$((Get-Date).ToString('o'))] RUNNER_ERROR $($_.Exception.Message)" | Add-Content -LiteralPath $runnerLog
    throw
} finally {
    if (Test-Path -LiteralPath $lockPath) {
        Remove-Item -LiteralPath $lockPath -Force
    }
}
