param(
    [string]$SourceName = "merged",
    [string]$PackagePath = "",
    [switch]$NoClose
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

function Invoke-Py {
    py @args
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE`: py $args"
    }
}

function Invoke-SyncCommand {
    param(
        [string]$Command,
        [string[]]$ExtraArgs = @(),
        [switch]$Apply
    )
    $args = @(".\codex_state_sync.py", $Command)
    if ($Command -in @("validate-latest", "dry-run-import", "import", "repair-paths", "repair-thread-recency")) {
        $args += @("--source-name", $SourceName)
        if ($PackagePath) {
            $args += @("--package", $PackagePath)
        }
    }
    $args += $ExtraArgs
    if ($Apply) {
        $args += "--apply"
    }
    Invoke-Py @args
}

if (-not $NoClose) {
    Write-Host "Closing Codex processes..."
    Get-Process Codex,codex -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 3

    $remaining = Get-Process Codex,codex -ErrorAction SilentlyContinue
    if ($remaining) {
        $remaining | Select-Object ProcessName,Id,Path | Format-Table -AutoSize
        throw "Stop failed. Close remaining Codex processes manually."
    }
}

Write-Host "Validating latest $SourceName package..."
Invoke-SyncCommand -Command "validate-latest"

Write-Host "Importing $SourceName package with pre-import backup..."
Invoke-SyncCommand -Command "import" -ExtraArgs @("--replace-profile-files") -Apply

Write-Host "Repairing imported profile paths..."
Invoke-SyncCommand -Command "repair-paths" -Apply

Write-Host "Repairing imported thread visibility flags..."
Invoke-SyncCommand -Command "repair-user-event-flags" -Apply

Write-Host "Aligning sidebar project roots..."
Invoke-SyncCommand -Command "repair-project-roots" -Apply

Write-Host "Populating per-thread workspace root hints..."
Invoke-SyncCommand -Command "repair-workspace-hints" -Apply

Write-Host "Restoring merged thread recency..."
Invoke-SyncCommand -Command "repair-thread-recency" -Apply

Write-Host "Restoring rollout file mtimes..."
Invoke-SyncCommand -Command "repair-rollout-file-mtimes" -Apply

Write-Host "Sorting session index by recency..."
Invoke-SyncCommand -Command "repair-session-index-recency" -Apply

Write-Host "Verifying repair..."
$verifyJson = (Invoke-SyncCommand -Command "repair-paths") -join [Environment]::NewLine
Write-Host $verifyJson
$verify = $verifyJson | ConvertFrom-Json

$verifyEventsJson = (Invoke-SyncCommand -Command "repair-user-event-flags") -join [Environment]::NewLine
Write-Host $verifyEventsJson
$verifyEvents = $verifyEventsJson | ConvertFrom-Json

$verifyRootsJson = (Invoke-SyncCommand -Command "repair-project-roots") -join [Environment]::NewLine
Write-Host $verifyRootsJson
$verifyRoots = $verifyRootsJson | ConvertFrom-Json

$verifyHintsJson = (Invoke-SyncCommand -Command "repair-workspace-hints") -join [Environment]::NewLine
Write-Host $verifyHintsJson
$verifyHints = $verifyHintsJson | ConvertFrom-Json

$verifyRecencyJson = (Invoke-SyncCommand -Command "repair-thread-recency") -join [Environment]::NewLine
Write-Host $verifyRecencyJson
$verifyRecency = $verifyRecencyJson | ConvertFrom-Json

$verifyMtimesJson = (Invoke-SyncCommand -Command "repair-rollout-file-mtimes") -join [Environment]::NewLine
Write-Host $verifyMtimesJson
$verifyMtimes = $verifyMtimesJson | ConvertFrom-Json

$verifyIndexJson = (Invoke-SyncCommand -Command "repair-session-index-recency") -join [Environment]::NewLine
Write-Host $verifyIndexJson
$verifyIndex = $verifyIndexJson | ConvertFrom-Json

$pending = @(
    $verify.sqlite_rows_to_update,
    $verify.sqlite_rollout_paths_to_update,
    $verify.global_state_changes,
    $verify.session_index_changes,
    $verify.session_files_to_update,
    $verify.session_file_path_replacements,
    $verifyEvents.threads_to_mark_has_user_event,
    $verifyRoots.project_root_changes,
    $verifyHints.thread_workspace_hint_changes,
    $verifyRecency.thread_timestamp_changes,
    $verifyMtimes.rollout_file_mtime_changes,
    $verifyIndex.session_index_recency_changes
) | Measure-Object -Sum

if ($pending.Sum -ne 0) {
    throw "Verification still shows pending updates. Do not reopen Codex yet."
}

Write-Host "Merged import complete."
