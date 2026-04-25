# Edge Cases Covered

## Same Chat Edited On Both Machines

The mediator compares rows by thread ID. If both packages contain the thread, the row with the newer `updated_at_ms` wins. If the timestamp delta is larger than the conflict threshold, the decision is written to `merge-report.json`.

## Clock Skew

Latest-wins depends on trustworthy system clocks. Keep both Windows machines synced to network time. If clocks drift, reports should be reviewed before import.

## Codex Running During Import

Import and repair commands refuse to write while `Codex.exe` or `codex.exe` is running. The orchestrator closes Codex before export/import stages.

## Partial Transfer

Package validation checks SQLite integrity, critical files, and referenced rollout file counts before import.

## OneDrive Latency

The orchestrator copies packages directly over SSH with `pscp`; it does not rely on OneDrive timing for the active transfer.

## Path Differences Between Machines

The merged package uses the local/source profile as the canonical profile. Import repair maps those paths to the target machine profile.

## Codex Long Path Prefixes

Codex may rewrite paths with `\\?\` prefixes. The repair and validation code treats that as a path representation issue, not a failure by itself.

## Sidebar Recency Corruption

Codex may rescan rollout JSONL files and rewrite recency if file mtimes look new. The repair chain resets rollout file mtimes from the thread timestamps and rebuilds `session_index.jsonl`.

## Deleted Or Archived Threads

The merger preserves archived flags from the winning thread row. Do not use this as a replacement for a full deleted-state conflict resolver without reviewing reports.

## Schema Changes

The tool uses the table columns present in the source database and validates SQLite integrity. Codex schema changes should be reviewed before trusting unattended imports after an app update.
