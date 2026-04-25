# Restore From Backup

Every import creates a backup under:

```text
%USERPROFILE%\.codex\sync_backups\pre-import-YYYYMMDD-HHMMSS
```

To restore manually:

1. Close Codex.
2. Pick the backup folder.
3. Copy backed-up critical files back into `%USERPROFILE%\.codex`.
4. Restore `sessions` and `archived_sessions` from the backup if they were changed.
5. Reopen Codex and validate.

The import backup includes an `import_manifest.json` describing what was replaced and where it came from.

Recommended rule: never delete backup folders until you have opened Codex on both machines and confirmed the sidebar/project grouping.
