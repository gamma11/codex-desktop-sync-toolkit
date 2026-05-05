# Codex Desktop State Sync

Windows tooling for backing up, exporting, merging, importing, repairing, and validating Codex Desktop state across two PCs.

This is for people who use Codex Desktop on more than one Windows machine and need chats to appear under the right Projects after moving state between machines.

## Status

This has only been validated on one two-PC Windows setup. It works for that setup, but Codex Desktop state is not a stable public sync API, so you should expect to tweak paths, validation rules, and repair behavior for your own situation. Codex can help adapt the scripts to your machines.

Recommended rollout:

1. Start with one-way sync from your known-good machine to a test target.
2. Validate SQLite state, rollout files, project grouping, and the rendered Codex sidebar.
3. Keep the scheduled task disabled until one-way restore and validation are repeatable.
4. Enable two-way sync only after you understand the reports and backups.

## What It Does

- Creates timestamped state packages from each PC.
- Creates a pre-import backup before writing to either `.codex` profile.
- Mediates two-way sync by thread ID.
- Resolves same-thread edits with latest `updated_at_ms` wins.
- Preserves the losing package in the source package history and records conflict decisions in `merge-report.json`.
- Repairs common imported-state problems:
  - user profile path differences
  - project root/sidebar grouping
  - `has_user_event` visibility flags
  - workspace root hints
  - thread recency
  - rollout JSONL mtimes
  - `session_index.jsonl` ordering
- Validates SQLite state and simulated sidebar/project grouping.
- Optionally launches Codex on the target desktop and captures a sidebar screenshot.

## Requirements

- Windows on both machines.
- Codex Desktop installed on both machines.
- Python available as `py`.
- PowerShell 5+.
- PuTTY tools installed on the orchestrating PC:
  - `plink.exe`
  - `pscp.exe`
- SSH enabled on the remote PC.
- OneDrive available on both machines, or equivalent paths passed with script parameters.

## Safety Model

The import scripts refuse to write while Codex is running and always create a pre-import backup under:

```text
%USERPROFILE%\.codex\sync_backups
```

The recommended workflow is:

```text
backup/export both -> transfer packages -> mediator merge -> import merged state to both -> repair -> validate
```

## Install

Copy the `scripts` folder to both machines. A common location is:

```text
%OneDriveConsumer%\Codex\Python_scripts\codex_workspace_sync_setup
```

From the orchestrating PC, register the daily scheduled task:

```powershell
$env:CODEX_DESKTOP_SSH_PASSWORD = Read-Host "Remote SSH password"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Register-CodexTwoWaySyncSchedule.ps1 `
  -DesktopHost "192.0.2.10" `
  -DesktopUser "YOUR_REMOTE_USER" `
  -DesktopHostKey "SHA256:YOUR_PINNED_HOST_KEY" `
  -DesktopPassword $env:CODEX_DESKTOP_SSH_PASSWORD
```

The password is stored using Windows `Export-Clixml`, encrypted for the current Windows user on the current PC.

## Run A Non-Destructive Test

This exports both sides, transfers packages, runs the mediator, and validates the merged package without importing it into live Codex state:

```powershell
$env:CODEX_DESKTOP_SSH_PASSWORD = Read-Host "Remote SSH password"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Invoke-CodexTwoWaySync.ps1 `
  -Mode ExportMergeOnly `
  -DesktopHost "192.0.2.10" `
  -DesktopUser "YOUR_REMOTE_USER" `
  -DesktopHostKey "SHA256:YOUR_PINNED_HOST_KEY" `
  -DesktopPassword $env:CODEX_DESKTOP_SSH_PASSWORD `
  -NoCloseLocalForTest
```

## Nightly Schedule

The default schedule starts at 03:00 and the orchestrator offsets stages internally:

- 03:00 deploy scripts and export local PC
- 03:10 export remote PC
- 03:15 transfer packages
- 03:20 merge
- 03:25 transfer merged package
- 03:30 import/repair local PC
- 03:45 import/repair/validate remote PC

Each run writes a report under:

```text
%OneDriveConsumer%\Codex\StateSync\two-way-runs
```

## Important Notes

- This is not an official OpenAI project.
- Codex Desktop internal state can change between releases. The scripts validate the current schema before importing, but review reports after app updates.
- If both machines edit the same chat, latest timestamp wins. The losing state remains in the source package backups.
- If system clocks drift, latest-wins conflict resolution becomes less reliable. Keep both PCs time-synced.
- Do not enable unattended two-way sync until a one-way sync has been tested and visually validated on your machines.

## License

MIT
