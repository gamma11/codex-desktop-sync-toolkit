#!/usr/bin/env python3
"""Export and guarded-import Codex Desktop state packages.

Designed for a laptop-authoritative workflow:
- laptop runs: export --source-name laptop
- desktop runs: validate-latest, dry-run-import, import --apply

The import command refuses to run while Codex/Codex.exe is running and always
creates a local pre-import backup before replacing critical state files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


CRITICAL_PATTERNS = (
    "state_5.sqlite",
    "state_5.sqlite-shm",
    "state_5.sqlite-wal",
    "session_index.jsonl",
    ".codex-global-state.json",
)

THREAD_RELATED_TABLES = (
    ("stage1_outputs", "thread_id"),
    ("thread_dynamic_tools", "thread_id"),
    ("thread_spawn_edges", "child_thread_id"),
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_from_ms(value) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except Exception:
        return None


def short_title(value: str | None, limit: int = 120) -> str:
    if not value:
        return ""
    title = str(value).splitlines()[0]
    return title if len(title) <= limit else title[: limit - 3] + "..."


def codex_dir() -> Path:
    userprofile = os.environ.get("USERPROFILE")
    if not userprofile:
        raise SystemExit("USERPROFILE is not set.")
    return Path(userprofile) / ".codex"


def onedrive_root() -> Path:
    for key in ("OneDriveConsumer", "OneDrive"):
        value = os.environ.get(key)
        if value:
            return Path(value)
    raise SystemExit("Neither OneDriveConsumer nor OneDrive is set.")


def state_sync_root() -> Path:
    return onedrive_root() / "Codex" / "StateSync"


def package_parent(source_name: str) -> Path:
    return state_sync_root() / f"source-{source_name}"


def is_codex_running() -> bool:
    for image_name in ("Codex.exe", "codex.exe"):
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
                text=True,
                capture_output=True,
                check=False,
            )
        except Exception:
            continue
        if image_name.lower() in proc.stdout.lower():
            return True
    return False


def copy_matching_files(src_dir: Path, dest_dir: Path) -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in CRITICAL_PATTERNS:
        src = src_dir / name
        if src.exists():
            shutil.copy2(src, dest_dir / name)
            copied.append(name)
    return copied


def copy_referenced_session_files(source_codex: Path, package: Path) -> dict:
    db_path = package / "state_5.sqlite"
    if not db_path.exists():
        return {"copied": 0, "missing": 0, "missing_examples": []}

    files_root = package / "profile_files"
    copied = 0
    missing: list[str] = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        paths = [row[0] for row in con.execute("SELECT DISTINCT rollout_path FROM threads")]
    finally:
        con.close()

    for raw in paths:
        if not raw:
            continue
        src_text = raw[4:] if raw.startswith("\\\\?\\") else raw
        src = Path(src_text)
        if not src.exists():
            missing.append(raw)
            continue
        try:
            rel = src.relative_to(source_codex)
        except ValueError:
            missing.append(raw)
            continue
        dest = files_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return {"copied": copied, "missing": len(missing), "missing_examples": missing[:10]}


def copy_profile_files_from_package(package: Path, target_codex: Path) -> dict:
    files_root = package / "profile_files"
    if not files_root.exists():
        return {"available": False, "copied": 0}

    copied = 0
    for src in files_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(files_root)
        dest = target_codex / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return {"available": True, "copied": copied}


def backup_profile_file_roots(target_codex: Path, package: Path, backup: Path) -> list[str]:
    files_root = package / "profile_files"
    if not files_root.exists():
        return []

    backed_up: list[str] = []
    backup_root = backup / "profile_files_before"
    for src_root in files_root.iterdir():
        target_root = target_codex / src_root.name
        if not target_root.exists():
            continue
        dest = backup_root / src_root.name
        if target_root.is_dir():
            shutil.copytree(target_root, dest, dirs_exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_root, dest)
        backed_up.append(src_root.name)
    return backed_up


def replace_profile_file_roots(target_codex: Path, package: Path) -> list[str]:
    files_root = package / "profile_files"
    if not files_root.exists():
        return []

    replaced: list[str] = []
    for src_root in files_root.iterdir():
        target_root = target_codex / src_root.name
        if not target_root.exists():
            continue
        if target_root.is_dir():
            shutil.rmtree(target_root)
        else:
            target_root.unlink()
        replaced.append(str(target_root))
    return replaced


def integrity_check(db_path: Path) -> str:
    if not db_path.exists():
        return "missing"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()


def thread_count(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        return int(con.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
    finally:
        con.close()


def latest_package(source_name: str) -> Path:
    parent = package_parent(source_name)
    if not parent.exists():
        raise SystemExit(f"No package folder exists yet: {parent}")
    packages = sorted(
        [p for p in parent.iterdir() if p.is_dir() and (p / "manifest.json").exists()],
        key=lambda p: p.name,
    )
    if not packages:
        raise SystemExit(f"No packages found under: {parent}")
    return packages[-1]


def read_manifest(package: Path) -> dict:
    manifest_path = package / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Missing manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def package_report(package: Path) -> dict:
    manifest = read_manifest(package)
    db = package / "state_5.sqlite"
    profile_files_count = (
        sum(1 for p in (package / "profile_files").rglob("*") if p.is_file())
        if (package / "profile_files").exists()
        else 0
    )
    expected_profile_files_count = None
    session_files = manifest.get("session_files")
    if isinstance(session_files, dict) and isinstance(session_files.get("copied"), int):
        expected_profile_files_count = session_files["copied"]
    files = {
        name: {
            "exists": (package / name).exists(),
            "bytes": (package / name).stat().st_size if (package / name).exists() else None,
        }
        for name in CRITICAL_PATTERNS
    }
    return {
        "package": str(package),
        "manifest": manifest,
        "files": files,
        "profile_files_count": profile_files_count,
        "profile_files_expected_count": expected_profile_files_count,
        "profile_files_complete": (
            expected_profile_files_count is None
            or profile_files_count >= expected_profile_files_count
        ),
        "sqlite_integrity": integrity_check(db),
        "thread_count": thread_count(db),
    }


def sqlite_table_names(db_path: Path) -> set[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        return {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()


def sqlite_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")]


def row_updated_ms(row: sqlite3.Row) -> int:
    for key in ("updated_at_ms", "updated_at"):
        try:
            value = row[key]
        except (KeyError, IndexError):
            value = None
        if value is None:
            continue
        try:
            multiplier = 1000 if key == "updated_at" else 1
            return int(value) * multiplier
        except (TypeError, ValueError):
            continue
    return 0


def remap_row_values(
    row: sqlite3.Row,
    columns: list[str],
    source_profile: str,
    target_profile: str,
) -> list:
    values = []
    for col in columns:
        value = row[col]
        if isinstance(value, str) and source_profile and target_profile:
            mapped, _changed = remap_path_text(value, source_profile, target_profile)
            if mapped == value:
                mapped, _count = remap_raw_text(value, source_profile, target_profile)
            values.append(mapped)
        else:
            values.append(value)
    return values


def copy_related_rows_for_thread(
    target_con: sqlite3.Connection,
    source_con: sqlite3.Connection,
    table: str,
    thread_column: str,
    thread_id: str,
    source_profile: str,
    target_profile: str,
) -> int:
    columns = sqlite_columns(source_con, table)
    if not columns:
        return 0

    placeholders = ",".join("?" for _ in columns)
    quoted_columns = ",".join(f'"{col}"' for col in columns)
    rows = source_con.execute(
        f'SELECT {quoted_columns} FROM "{table}" WHERE "{thread_column}" = ?',
        (thread_id,),
    ).fetchall()
    target_con.execute(f'DELETE FROM "{table}" WHERE "{thread_column}" = ?', (thread_id,))
    for row in rows:
        values = remap_row_values(row, columns, source_profile, target_profile)
        target_con.execute(
            f'INSERT OR REPLACE INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
            values,
        )
    return len(rows)


def copy_profile_file_with_profile_remap(
    source_package: Path,
    target_package: Path,
    rollout_path: str,
    source_profile: str,
    target_profile: str,
) -> str | None:
    source_profile_clean = without_long_prefix(source_profile)
    target_profile_clean = without_long_prefix(target_profile)
    source_codex_clean = str(Path(source_profile_clean) / ".codex")
    source_rollout = Path(without_long_prefix(str(rollout_path)))
    try:
        rel = source_rollout.relative_to(source_codex_clean)
    except ValueError:
        return None

    source_file = source_package / "profile_files" / rel
    if not source_file.exists():
        return None

    target_file = target_package / "profile_files" / rel
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if source_file.suffix.lower() == ".jsonl" and source_profile_clean.lower() != target_profile_clean.lower():
        text = source_file.read_text(encoding="utf-8", errors="replace")
        text, _count = remap_raw_text(text, source_profile_clean, target_profile_clean)
        target_file.write_text(text, encoding="utf-8")
        stat = source_file.stat()
        os.utime(target_file, (stat.st_atime, stat.st_mtime))
    else:
        shutil.copy2(source_file, target_file)
    return str(target_file)


def merge_packages(args: argparse.Namespace) -> int:
    left = Path(args.left_package) if args.left_package else latest_package(args.left_source)
    right = Path(args.right_package) if args.right_package else latest_package(args.right_source)
    left_manifest = read_manifest(left)
    right_manifest = read_manifest(right)
    left_profile = str(left_manifest.get("user_profile") or "")
    right_profile = str(right_manifest.get("user_profile") or "")
    canonical_profile = str(args.canonical_profile or left_profile)
    if not canonical_profile:
        raise SystemExit("Could not determine canonical profile for merged package.")

    left_db = left / "state_5.sqlite"
    right_db = right / "state_5.sqlite"
    if integrity_check(left_db) != "ok":
        raise SystemExit(f"Left package SQLite integrity failed: {left}")
    if integrity_check(right_db) != "ok":
        raise SystemExit(f"Right package SQLite integrity failed: {right}")

    stamp = now_stamp()
    package = package_parent(args.output_source) / stamp
    package.mkdir(parents=True, exist_ok=False)
    shutil.copy2(left_db, package / "state_5.sqlite")
    for name in (".codex-global-state.json", "session_index.jsonl"):
        if (left / name).exists():
            shutil.copy2(left / name, package / name)

    copied_profile_files = 0
    profile_copy_errors: list[dict] = []
    conflicts: list[dict] = []
    selected_by_source = {args.left_source: 0, args.right_source: 0}

    target_con = sqlite3.connect(package / "state_5.sqlite", timeout=15)
    target_con.row_factory = sqlite3.Row
    left_con = sqlite3.connect(f"file:{left_db}?mode=ro", uri=True, timeout=15)
    left_con.row_factory = sqlite3.Row
    right_con = sqlite3.connect(f"file:{right_db}?mode=ro", uri=True, timeout=15)
    right_con.row_factory = sqlite3.Row
    try:
        left_threads = {
            str(row["id"]): row
            for row in left_con.execute("SELECT * FROM threads")
        }
        right_threads = {
            str(row["id"]): row
            for row in right_con.execute("SELECT * FROM threads")
        }
        target_columns = sqlite_columns(target_con, "threads")
        table_names = sqlite_table_names(package / "state_5.sqlite")
        all_ids = sorted(set(left_threads) | set(right_threads))

        for thread_id in all_ids:
            left_row = left_threads.get(thread_id)
            right_row = right_threads.get(thread_id)
            if left_row is not None and right_row is not None:
                left_updated = row_updated_ms(left_row)
                right_updated = row_updated_ms(right_row)
                if right_updated > left_updated:
                    winner_row = right_row
                    winner_con = right_con
                    winner_package = right
                    winner_source = args.right_source
                    winner_profile = right_profile
                    loser_source = args.left_source
                else:
                    winner_row = left_row
                    winner_con = left_con
                    winner_package = left
                    winner_source = args.left_source
                    winner_profile = left_profile
                    loser_source = args.right_source
                if abs(left_updated - right_updated) > args.conflict_threshold_ms:
                    conflicts.append(
                        {
                            "thread_id": thread_id,
                            "title": short_title(winner_row["title"]),
                            "winner": winner_source,
                            "loser": loser_source,
                            "left_updated_ms": left_updated,
                            "right_updated_ms": right_updated,
                        }
                    )
            elif right_row is not None:
                winner_row = right_row
                winner_con = right_con
                winner_package = right
                winner_source = args.right_source
                winner_profile = right_profile
            else:
                winner_row = left_row
                winner_con = left_con
                winner_package = left
                winner_source = args.left_source
                winner_profile = left_profile

            selected_by_source[winner_source] += 1
            values = remap_row_values(winner_row, target_columns, winner_profile, canonical_profile)
            quoted_columns = ",".join(f'"{col}"' for col in target_columns)
            placeholders = ",".join("?" for _ in target_columns)
            target_con.execute('DELETE FROM "threads" WHERE "id" = ?', (thread_id,))
            target_con.execute(
                f'INSERT OR REPLACE INTO "threads" ({quoted_columns}) VALUES ({placeholders})',
                values,
            )
            for table, thread_column in THREAD_RELATED_TABLES:
                if table in table_names:
                    copy_related_rows_for_thread(
                        target_con,
                        winner_con,
                        table,
                        thread_column,
                        thread_id,
                        winner_profile,
                        canonical_profile,
                    )
            copied = copy_profile_file_with_profile_remap(
                winner_package,
                package,
                str(winner_row["rollout_path"]),
                winner_profile,
                canonical_profile,
            )
            if copied:
                copied_profile_files += 1
            else:
                profile_copy_errors.append(
                    {
                        "thread_id": thread_id,
                        "source": winner_source,
                        "rollout_path": str(winner_row["rollout_path"]),
                    }
                )
        target_con.commit()
    finally:
        target_con.close()
        left_con.close()
        right_con.close()

    manifest = {
        "created_at_utc": utc_now(),
        "source_name": args.output_source,
        "computer_name": socket.gethostname(),
        "user_profile": canonical_profile,
        "codex_dir": str(Path(canonical_profile) / ".codex"),
        "package": str(package),
        "merged_from": [
            {"source_name": args.left_source, "package": str(left), "user_profile": left_profile},
            {"source_name": args.right_source, "package": str(right), "user_profile": right_profile},
        ],
        "merge_policy": "thread latest-updated-ms wins; ties prefer left source",
        "copied_files": [name for name in CRITICAL_PATTERNS if (package / name).exists()],
        "missing_files": [name for name in CRITICAL_PATTERNS if not (package / name).exists()],
        "session_files": {
            "copied": copied_profile_files,
            "missing": len(profile_copy_errors),
            "missing_examples": profile_copy_errors[:10],
        },
        "selected_by_source": selected_by_source,
        "conflicts_resolved_latest_wins": conflicts,
        "sqlite_integrity": integrity_check(package / "state_5.sqlite"),
        "thread_count": thread_count(package / "state_5.sqlite"),
    }
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (package / "merge-report.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(package_report(package), indent=2))
    return 0


def strip_long_prefix(value: str) -> tuple[str, str]:
    if value.startswith("\\\\?\\"):
        return "\\\\?\\", value[4:]
    return "", value


def without_long_prefix(value: str) -> str:
    return value[4:] if isinstance(value, str) and value.startswith("\\\\?\\") else value


def strip_long_prefix_text(text: str) -> tuple[str, int]:
    """Remove Windows long-path prefixes in plain and JSON-escaped text."""
    replacements = (
        ("\\\\\\\\?\\\\", ""),
        ("\\\\?\\", ""),
    )
    changed = 0
    new_text = text
    for old, new in replacements:
        new_text, count = new_text.replace(old, new), new_text.count(old)
        changed += count
    return new_text, changed


def with_long_prefix(value: str) -> str:
    if not isinstance(value, str) or value.startswith("\\\\?\\"):
        return value
    if re.match(r"^[A-Za-z]:\\", value):
        return "\\\\?\\" + value
    return value


def remap_path_text(value: str, source_profile: str, target_profile: str) -> tuple[str, bool]:
    """Map source-machine user-rooted paths to this machine's user profile."""
    if not isinstance(value, str) or not source_profile:
        return value, False

    prefix, inner = strip_long_prefix(value)
    source_profile = without_long_prefix(source_profile)
    source_variants = {
        source_profile,
        source_profile.lower(),
        source_profile.upper(),
    }

    for source in sorted(source_variants, key=len, reverse=True):
        if inner.lower().startswith(source.lower()):
            mapped = target_profile + inner[len(source) :]
            mapped_value = prefix + mapped
            return mapped_value, mapped_value != value

    return value, False


def remap_json_paths(obj, source_profile: str, target_profile: str):
    changes = 0
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            new_key, key_changed = remap_path_text(key, source_profile, target_profile)
            new_value, value_changes = remap_json_paths(value, source_profile, target_profile)
            result[new_key] = new_value
            changes += value_changes + int(key_changed)
        return result, changes
    if isinstance(obj, list):
        result = []
        for item in obj:
            new_item, item_changes = remap_json_paths(item, source_profile, target_profile)
            result.append(new_item)
            changes += item_changes
        return result, changes
    if isinstance(obj, str):
        new_value, changed = remap_path_text(obj, source_profile, target_profile)
        return new_value, int(changed)
    return obj, 0


def replace_case_insensitive(text: str, old: str, new: str) -> tuple[str, int]:
    return re.subn(re.escape(old), lambda _match: new, text, flags=re.IGNORECASE)


def remap_raw_text(text: str, source_profile: str, target_profile: str) -> tuple[str, int]:
    replacements = [
        (source_profile, target_profile),
        (source_profile.replace("\\", "\\\\"), target_profile.replace("\\", "\\\\")),
        (source_profile.replace("\\", "/"), target_profile.replace("\\", "/")),
    ]
    changed = 0
    new_text = text
    for old, new in replacements:
        new_text, count = replace_case_insensitive(new_text, old, new)
        if count:
            changed += count
    return new_text, changed


SESSION_PATH_KEYS = {
    "cwd",
    "path",
    "workspace_root",
    "workspaceRoot",
    "working_dir",
    "workingDirectory",
}


def remap_session_path_fields(obj, source_profile: str, target_profile: str, key: str | None = None):
    changes = 0
    if isinstance(obj, dict):
        updated = {}
        for item_key, item_value in obj.items():
            new_value, value_changes = remap_session_path_fields(
                item_value,
                source_profile,
                target_profile,
                str(item_key),
            )
            updated[item_key] = new_value
            changes += value_changes
        return updated, changes
    if isinstance(obj, list):
        updated = []
        for item in obj:
            new_item, item_changes = remap_session_path_fields(item, source_profile, target_profile, key)
            updated.append(new_item)
            changes += item_changes
        return updated, changes
    if isinstance(obj, str) and key:
        key_normalized = key.replace("-", "_")
        is_path_key = (
            key_normalized in SESSION_PATH_KEYS
            or key_normalized.endswith("_path")
            or key_normalized.endswith("_paths")
        )
        if is_path_key:
            mapped, changed = remap_path_text(obj, source_profile, target_profile)
            return mapped, int(changed)
    return obj, 0


def repair_session_files(
    target: Path,
    source_profile: str,
    target_profile: str,
    backup: Path | None,
    apply: bool,
) -> tuple[int, int, list[str], Path | None]:
    roots = [target / "sessions", target / "archived_sessions"]
    files_to_update: list[tuple[Path, str, int]] = []
    examples: list[str] = []

    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            changed_lines = []
            changes = 0
            for line in lines:
                if not line.strip():
                    changed_lines.append(line)
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    changed_lines.append(line)
                    continue
                remapped, item_changes = remap_session_path_fields(item, source_profile, target_profile)
                if item_changes:
                    changed_lines.append(json.dumps(remapped, separators=(",", ":"), ensure_ascii=False))
                    changes += item_changes
                else:
                    changed_lines.append(line)
            if changes:
                new_text = "\n".join(changed_lines) + ("\n" if lines else "")
                files_to_update.append((path, new_text, changes))
                if len(examples) < 10:
                    examples.append(str(path))

    if apply and files_to_update:
        if backup is None:
            backup = backup_for_repair(target)
        backup_root = backup / "session_files_before"
        for path, new_text, _changes in files_to_update:
            for root in roots:
                try:
                    rel = path.relative_to(root)
                    backup_path = backup_root / root.name / rel
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup_path)
                    break
                except ValueError:
                    continue
            path.write_text(new_text, encoding="utf-8")

    return len(files_to_update), sum(item[2] for item in files_to_update), examples, backup


def infer_source_profile(args: argparse.Namespace) -> str:
    if args.source_profile:
        return args.source_profile
    package = Path(args.package) if args.package else latest_package(args.source_name)
    manifest = read_manifest(package)
    source_profile = manifest.get("user_profile")
    if not source_profile:
        raise SystemExit("Could not infer source profile from package manifest. Pass --source-profile.")
    return str(source_profile)


def backup_for_repair(target: Path) -> Path:
    backups_root = target / "sync_backups"
    backups_root.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    backup = backups_root / f"pre-path-repair-{stamp}"
    suffix = 1
    while backup.exists():
        backup = backups_root / f"pre-path-repair-{stamp}-{suffix}"
        suffix += 1
    backup.mkdir(parents=True, exist_ok=False)
    copied = copy_matching_files(target, backup)
    (backup / "repair_backup_manifest.json").write_text(
        json.dumps({"created_at_utc": utc_now(), "copied_files": copied}, indent=2) + "\n",
        encoding="utf-8",
    )
    return backup


def repair_paths(args: argparse.Namespace) -> int:
    target = codex_dir()
    source_profile = infer_source_profile(args)
    target_profile = os.environ.get("USERPROFILE")
    if not target_profile:
        raise SystemExit("USERPROFILE is not set.")

    if args.apply and is_codex_running():
        raise SystemExit("Refusing path repair because Codex.exe/codex.exe appears to be running. Close Codex first.")

    db_path = target / "state_5.sqlite"
    json_path = target / ".codex-global-state.json"
    session_path = target / "session_index.jsonl"

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "source_profile": source_profile,
        "target_profile": target_profile,
        "codex_dir": str(target),
        "sqlite_rows_to_update": 0,
        "sqlite_rollout_paths_to_update": 0,
        "sqlite_examples": [],
        "sqlite_rollout_examples": [],
        "global_state_changes": 0,
        "session_index_changes": 0,
        "session_files_to_update": 0,
        "session_file_path_replacements": 0,
        "session_file_examples": [],
        "backup": None,
    }

    if not db_path.exists():
        raise SystemExit(f"Missing SQLite state: {db_path}")

    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        rows = con.execute("SELECT id, cwd, rollout_path FROM threads").fetchall()
        updates = []
        rollout_updates = []
        for thread_id, cwd, rollout_path in rows:
            mapped, changed = remap_path_text(cwd, source_profile, target_profile)
            if changed:
                updates.append((mapped, thread_id, cwd))
            mapped_rollout, rollout_changed = remap_path_text(rollout_path, source_profile, target_profile)
            if rollout_changed:
                rollout_updates.append((mapped_rollout, thread_id, rollout_path))
        report["sqlite_rows_to_update"] = len(updates)
        report["sqlite_rollout_paths_to_update"] = len(rollout_updates)
        report["sqlite_examples"] = [
            {"from": old, "to": new}
            for new, _thread_id, old in updates[:10]
        ]
        report["sqlite_rollout_examples"] = [
            {"from": old, "to": new}
            for new, _thread_id, old in rollout_updates[:10]
        ]
        if args.apply and (updates or rollout_updates):
            backup = backup_for_repair(target)
            report["backup"] = str(backup)
            if updates:
                con.executemany("UPDATE threads SET cwd = ? WHERE id = ?", [(new, tid) for new, tid, _old in updates])
            if rollout_updates:
                con.executemany(
                    "UPDATE threads SET rollout_path = ? WHERE id = ?",
                    [(new, tid) for new, tid, _old in rollout_updates],
                )
            con.commit()
        elif not args.apply:
            con.rollback()
    finally:
        con.close()

    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        remapped, changes = remap_json_paths(data, source_profile, target_profile)
        report["global_state_changes"] = changes
        if args.apply and changes:
            if not report["backup"]:
                report["backup"] = str(backup_for_repair(target))
            json_path.write_text(json.dumps(remapped, separators=(",", ":")), encoding="utf-8")

    if session_path.exists():
        lines = session_path.read_text(encoding="utf-8").splitlines()
        changed_lines = []
        changes = 0
        for line in lines:
            mapped, changed = remap_path_text(line, source_profile, target_profile)
            changed_lines.append(mapped)
            changes += int(changed)
        report["session_index_changes"] = changes
        if args.apply and changes:
            if not report["backup"]:
                report["backup"] = str(backup_for_repair(target))
            session_path.write_text("\n".join(changed_lines) + ("\n" if lines else ""), encoding="utf-8")

    files_count, replacements, examples, backup = repair_session_files(
        target,
        source_profile,
        target_profile,
        Path(report["backup"]) if report["backup"] else None,
        args.apply,
    )
    report["session_files_to_update"] = files_count
    report["session_file_path_replacements"] = replacements
    report["session_file_examples"] = examples
    if backup is not None:
        report["backup"] = str(backup)

    print(json.dumps(report, indent=2))
    return 0


def repair_user_event_flags(args: argparse.Namespace) -> int:
    target = codex_dir()
    db_path = target / "state_5.sqlite"
    if not db_path.exists():
        raise SystemExit(f"Missing SQLite state: {db_path}")
    if args.apply and is_codex_running():
        raise SystemExit(
            "Refusing user-event repair because Codex.exe/codex.exe appears to be running. Close Codex first."
        )

    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        rows = con.execute(
            """
            SELECT id, title, has_user_event
            FROM threads
            WHERE COALESCE(first_user_message, '') <> ''
            """
        ).fetchall()
        updates = [(thread_id, title) for thread_id, title, flag in rows if int(flag or 0) == 0]
        report = {
            "mode": "apply" if args.apply else "dry-run",
            "codex_dir": str(target),
            "threads_with_first_user_message": len(rows),
            "threads_to_mark_has_user_event": len(updates),
            "examples": [{"id": thread_id, "title": title} for thread_id, title in updates[:10]],
            "backup": None,
        }
        if args.apply and updates:
            backup = backup_for_repair(target)
            report["backup"] = str(backup)
            con.executemany(
                "UPDATE threads SET has_user_event = 1 WHERE id = ?",
                [(thread_id,) for thread_id, _title in updates],
            )
            con.commit()
    finally:
        con.close()

    print(json.dumps(report, indent=2))
    return 0


def visible_thread_cwds(target: Path) -> set[str]:
    db_path = target / "state_5.sqlite"
    if not db_path.exists():
        raise SystemExit(f"Missing SQLite state: {db_path}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        return {
            str(row[0])
            for row in con.execute(
                """
                SELECT DISTINCT cwd
                FROM threads
                WHERE has_user_event = 1
                  AND archived = 0
                  AND COALESCE(cwd, '') <> ''
                """
            )
        }
    finally:
        con.close()


def root_has_visible_threads(root: str, cwds: set[str], *, long_prefix: bool | None = None) -> bool:
    root_norm = without_long_prefix(root).rstrip("\\/")
    for cwd in cwds:
        if long_prefix is True and not cwd.startswith("\\\\?\\"):
            continue
        if long_prefix is False and cwd.startswith("\\\\?\\"):
            continue
        cwd_norm = without_long_prefix(cwd).rstrip("\\/")
        if cwd_norm.lower() == root_norm.lower() or cwd_norm.lower().startswith((root_norm + "\\").lower()):
            return True
    return False


def align_project_root_list(values: list, cwds: set[str]) -> tuple[list, list[dict]]:
    aligned = []
    changes = []
    for value in values:
        if isinstance(value, str) and value.startswith("\\\\?\\"):
            normal = without_long_prefix(value)
            if normal != value and (Path(normal).exists() or root_has_visible_threads(value, cwds)):
                aligned.append(normal)
                changes.append({"from": value, "to": normal})
                continue
        aligned.append(value)
    return aligned, changes


def repair_project_roots(args: argparse.Namespace) -> int:
    target = codex_dir()
    if args.apply and is_codex_running():
        raise SystemExit(
            "Refusing project-root repair because Codex.exe/codex.exe appears to be running. Close Codex first."
        )

    json_path = target / ".codex-global-state.json"
    if not json_path.exists():
        raise SystemExit(f"Missing global state: {json_path}")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    cwds = visible_thread_cwds(target)
    keys = ("project-order", "electron-saved-workspace-roots", "active-workspace-roots")
    changes_by_key = {}
    total_changes = 0
    updated = dict(data)

    for key in keys:
        values = data.get(key)
        if not isinstance(values, list):
            continue
        aligned, changes = align_project_root_list(values, cwds)
        if changes:
            updated[key] = aligned
            changes_by_key[key] = changes
            total_changes += len(changes)

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "codex_dir": str(target),
        "visible_thread_cwds": len(cwds),
        "project_root_changes": total_changes,
        "changes": changes_by_key,
        "backup": None,
    }

    if args.apply and total_changes:
        backup = backup_for_repair(target)
        report["backup"] = str(backup)
        json_path.write_text(json.dumps(updated, separators=(",", ":")), encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0


def read_session_meta_cwd(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        payload = json.loads(first).get("payload", {})
        cwd = payload.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None
    except Exception:
        return None


def visible_threads_with_roots(target: Path) -> list[tuple[str, str]]:
    db_path = target / "state_5.sqlite"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        rows = con.execute(
            """
            SELECT id, cwd, rollout_path
            FROM threads
            WHERE has_user_event = 1
              AND archived = 0
              AND COALESCE(id, '') <> ''
            """
        ).fetchall()
    finally:
        con.close()

    result = []
    for thread_id, cwd, rollout_path in rows:
        root = read_session_meta_cwd(str(rollout_path)) or without_long_prefix(str(cwd or ""))
        if root:
            result.append((str(thread_id), without_long_prefix(root)))
    return result


def choose_workspace_root(cwd: str, roots: list[str]) -> str:
    cwd_norm = without_long_prefix(cwd).rstrip("\\/")
    matches = [
        root.rstrip("\\/")
        for root in roots
        if cwd_norm.lower() == root.rstrip("\\/").lower()
        or cwd_norm.lower().startswith((root.rstrip("\\/") + "\\").lower())
    ]
    if matches:
        return max(matches, key=len)
    return cwd_norm


def repair_thread_workspace_hints(args: argparse.Namespace) -> int:
    target = codex_dir()
    if args.apply and is_codex_running():
        raise SystemExit(
            "Refusing workspace-hint repair because Codex.exe/codex.exe appears to be running. Close Codex first."
        )

    json_path = target / ".codex-global-state.json"
    if not json_path.exists():
        raise SystemExit(f"Missing global state: {json_path}")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    roots = [
        without_long_prefix(str(root))
        for root in data.get("electron-saved-workspace-roots", [])
        if isinstance(root, str)
    ]
    projectless = set(data.get("projectless-thread-ids", []))
    hints = dict(data.get("thread-workspace-root-hints", {}))
    changes = []

    for thread_id, cwd in visible_threads_with_roots(target):
        if thread_id in projectless:
            continue
        root = choose_workspace_root(cwd, roots)
        if hints.get(thread_id) != root:
            changes.append({"id": thread_id, "from": hints.get(thread_id), "to": root})
            hints[thread_id] = root

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "codex_dir": str(target),
        "thread_workspace_hint_changes": len(changes),
        "examples": changes[:20],
        "backup": None,
    }

    if args.apply and changes:
        backup = backup_for_repair(target)
        report["backup"] = str(backup)
        updated = dict(data)
        updated["thread-workspace-root-hints"] = hints
        json_path.write_text(json.dumps(updated, separators=(",", ":")), encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0


def repair_thread_recency(args: argparse.Namespace) -> int:
    target = codex_dir()
    if args.apply and is_codex_running():
        raise SystemExit(
            "Refusing thread-recency repair because Codex.exe/codex.exe appears to be running. Close Codex first."
        )

    package = Path(args.package) if args.package else latest_package(args.source_name)
    source_db = package / "state_5.sqlite"
    target_db = target / "state_5.sqlite"
    if not source_db.exists():
        raise SystemExit(f"Missing source SQLite state: {source_db}")
    if not target_db.exists():
        raise SystemExit(f"Missing target SQLite state: {target_db}")

    source_con = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=15)
    try:
        source_rows = {
            str(row[0]): row[1:]
            for row in source_con.execute(
                """
                SELECT id, created_at, created_at_ms, updated_at, updated_at_ms
                FROM threads
                """
            )
        }
    finally:
        source_con.close()

    updates = []
    target_con = sqlite3.connect(str(target_db), timeout=15)
    try:
        target_rows = target_con.execute(
            """
            SELECT id, title, created_at, created_at_ms, updated_at, updated_at_ms
            FROM threads
            """
        ).fetchall()
        for thread_id, title, created_at, created_at_ms, updated_at, updated_at_ms in target_rows:
            source_values = source_rows.get(str(thread_id))
            if source_values is None:
                continue
            source_created_at, source_created_at_ms, source_updated_at, source_updated_at_ms = source_values
            target_values = (created_at, created_at_ms, updated_at, updated_at_ms)
            if target_values != source_values:
                updates.append(
                    {
                        "id": str(thread_id),
                        "title": short_title(title),
                        "from": {
                            "created_at": created_at,
                            "created_at_ms": created_at_ms,
                            "updated_at": updated_at,
                            "updated_at_ms": updated_at_ms,
                            "updated_at_iso": iso_from_ms(updated_at_ms),
                        },
                        "to": {
                            "created_at": source_created_at,
                            "created_at_ms": source_created_at_ms,
                            "updated_at": source_updated_at,
                            "updated_at_ms": source_updated_at_ms,
                            "updated_at_iso": iso_from_ms(source_updated_at_ms),
                        },
                        "values": (
                            source_created_at,
                            source_created_at_ms,
                            source_updated_at,
                            source_updated_at_ms,
                            str(thread_id),
                        ),
                    }
                )

        report = {
            "mode": "apply" if args.apply else "dry-run",
            "source_package": str(package),
            "codex_dir": str(target),
            "source_threads": len(source_rows),
            "target_threads": len(target_rows),
            "matching_threads": sum(1 for row in target_rows if str(row[0]) in source_rows),
            "thread_timestamp_changes": len(updates),
            "examples": [
                {
                    "id": update["id"],
                    "title": update["title"],
                    "from": update["from"],
                    "to": update["to"],
                }
                for update in updates[:20]
            ],
            "backup": None,
        }

        if args.apply and updates:
            backup = backup_for_repair(target)
            report["backup"] = str(backup)
            target_con.executemany(
                """
                UPDATE threads
                SET created_at = ?,
                    created_at_ms = ?,
                    updated_at = ?,
                    updated_at_ms = ?
                WHERE id = ?
                """,
                [update["values"] for update in updates],
            )
            target_con.commit()
        elif not args.apply:
            target_con.rollback()
    finally:
        target_con.close()

    print(json.dumps(report, indent=2))
    return 0


def repair_rollout_file_mtimes(args: argparse.Namespace) -> int:
    target = codex_dir()
    if args.apply and is_codex_running():
        raise SystemExit(
            "Refusing rollout file mtime repair because Codex.exe/codex.exe appears to be running. Close Codex first."
        )

    db_path = target / "state_5.sqlite"
    if not db_path.exists():
        raise SystemExit(f"Missing SQLite state: {db_path}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        rows = con.execute(
            """
            SELECT id, title, rollout_path, COALESCE(updated_at_ms, updated_at * 1000)
            FROM threads
            WHERE COALESCE(rollout_path, '') <> ''
            """
        ).fetchall()
    finally:
        con.close()

    updates = []
    missing = []
    for thread_id, title, rollout_path, updated_ms in rows:
        path = Path(without_long_prefix(str(rollout_path)))
        if not path.exists():
            missing.append(str(rollout_path))
            continue
        desired = int(updated_ms) / 1000
        current = path.stat().st_mtime
        if abs(current - desired) > 1:
            updates.append((path, desired, str(thread_id), short_title(title)))

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "codex_dir": str(target),
        "rollout_files_checked": len(rows),
        "rollout_file_mtime_changes": len(updates),
        "missing_rollout_files": len(missing),
        "missing_examples": missing[:10],
        "examples": [
            {"id": thread_id, "title": title, "path": str(path)}
            for path, _desired, thread_id, title in updates[:20]
        ],
    }

    if args.apply:
        for path, desired, _thread_id, _title in updates:
            os.utime(path, (desired, desired))

    print(json.dumps(report, indent=2))
    return 0


def repair_session_index_recency(args: argparse.Namespace) -> int:
    target = codex_dir()
    if args.apply and is_codex_running():
        raise SystemExit(
            "Refusing session-index recency repair because Codex.exe/codex.exe appears to be running. Close Codex first."
        )

    db_path = target / "state_5.sqlite"
    session_path = target / "session_index.jsonl"
    if not db_path.exists():
        raise SystemExit(f"Missing SQLite state: {db_path}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, title, COALESCE(updated_at_ms, updated_at * 1000) AS updated_ms
            FROM threads
            WHERE has_user_event = 1
              AND archived = 0
            ORDER BY COALESCE(updated_at_ms, updated_at * 1000, 0) DESC
            """
        ).fetchall()
    finally:
        con.close()

    desired_lines = []
    for row in rows:
        updated = iso_from_ms(row["updated_ms"])
        item = {
            "id": str(row["id"]),
            "thread_name": short_title(row["title"], limit=160) or str(row["id"]),
            "updated_at": updated.replace("+00:00", "Z") if updated else None,
        }
        desired_lines.append(json.dumps(item, separators=(",", ":"), ensure_ascii=False))
    desired_text = "\n".join(desired_lines) + ("\n" if desired_lines else "")
    current_text = session_path.read_text(encoding="utf-8") if session_path.exists() else ""

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "codex_dir": str(target),
        "session_index_threads": len(rows),
        "session_index_recency_changes": int(current_text != desired_text),
        "session_index_path": str(session_path),
    }

    if args.apply and current_text != desired_text:
        session_path.write_text(desired_text, encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0


def export_package(args: argparse.Namespace) -> int:
    source = codex_dir()
    if not source.exists():
        raise SystemExit(f"Codex profile does not exist: {source}")

    stamp = now_stamp()
    package = package_parent(args.source_name) / stamp
    package.mkdir(parents=True, exist_ok=False)

    copied = copy_matching_files(source, package)
    session_files = copy_referenced_session_files(source, package)
    manifest = {
        "created_at_utc": utc_now(),
        "source_name": args.source_name,
        "computer_name": socket.gethostname(),
        "user_profile": os.environ.get("USERPROFILE"),
        "codex_dir": str(source),
        "package": str(package),
        "copied_files": copied,
        "missing_files": [name for name in CRITICAL_PATTERNS if name not in copied],
        "session_files": session_files,
        "sqlite_integrity": integrity_check(package / "state_5.sqlite"),
        "thread_count": thread_count(package / "state_5.sqlite"),
    }
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(package_report(package), indent=2))
    return 0


def validate_latest(args: argparse.Namespace) -> int:
    package = Path(args.package) if args.package else latest_package(args.source_name)
    print(json.dumps(package_report(package), indent=2))
    return 0


def dry_run_import(args: argparse.Namespace) -> int:
    package = Path(args.package) if args.package else latest_package(args.source_name)
    report = package_report(package)
    with tempfile.TemporaryDirectory(prefix="codex-state-dryrun-") as temp:
        temp_path = Path(temp)
        copied = copy_matching_files(package, temp_path)
        report["dry_run_temp"] = str(temp_path)
        report["dry_run_copied"] = copied
        report["dry_run_integrity"] = integrity_check(temp_path / "state_5.sqlite")
        report["would_replace_in"] = str(codex_dir())
    print(json.dumps(report, indent=2))
    return 0


def import_package(args: argparse.Namespace) -> int:
    if not args.apply:
        raise SystemExit("Refusing import without --apply. Use dry-run-import first.")
    if is_codex_running():
        raise SystemExit("Refusing import because Codex.exe/codex.exe appears to be running. Close Codex first.")

    target = codex_dir()
    if not target.exists():
        raise SystemExit(f"Codex profile does not exist: {target}")

    package = Path(args.package) if args.package else latest_package(args.source_name)
    report = package_report(package)
    if report["sqlite_integrity"] != "ok":
        raise SystemExit(f"Refusing import: package SQLite integrity is {report['sqlite_integrity']!r}")
    if not (package / "state_5.sqlite").exists():
        raise SystemExit("Refusing import: package is missing state_5.sqlite")
    expected_profile_files = report.get("profile_files_expected_count")
    actual_profile_files = report.get("profile_files_count", 0)
    if expected_profile_files is not None and actual_profile_files < expected_profile_files:
        raise SystemExit(
            "Refusing import: laptop package profile_files are incomplete "
            f"({actual_profile_files}/{expected_profile_files}). Wait for OneDrive to finish syncing and retry."
        )

    backup = target / "sync_backups" / f"pre-import-{now_stamp()}"
    backup.mkdir(parents=True, exist_ok=False)
    live_copied = copy_matching_files(target, backup)
    if (target / "sessions").exists():
        shutil.copytree(target / "sessions", backup / "sessions", dirs_exist_ok=True)
    if (target / "archived_sessions").exists():
        shutil.copytree(target / "archived_sessions", backup / "archived_sessions", dirs_exist_ok=True)
    backed_up_profile_roots = backup_profile_file_roots(target, package, backup)
    replaced_profile_roots = replace_profile_file_roots(target, package) if args.replace_profile_files else []
    imported = copy_matching_files(package, target)
    profile_files = copy_profile_files_from_package(package, target)

    result = {
        "imported_from": str(package),
        "imported_to": str(target),
        "pre_import_backup": str(backup),
        "backed_up_live_files": live_copied,
        "backed_up_profile_roots": backed_up_profile_roots,
        "replaced_profile_roots": replaced_profile_roots,
        "imported_files": imported,
        "imported_profile_files": profile_files,
        "package_thread_count": report["thread_count"],
        "package_sqlite_integrity": report["sqlite_integrity"],
    }
    (backup / "import_manifest.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Desktop state export/import helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    export_p = sub.add_parser("export", help="Export this machine's critical Codex state to OneDrive.")
    export_p.add_argument("--source-name", default="laptop", help="Source name, e.g. laptop.")
    export_p.set_defaults(func=export_package)

    validate_p = sub.add_parser("validate-latest", help="Validate latest package from a source.")
    validate_p.add_argument("--source-name", default="laptop")
    validate_p.add_argument("--package", default=None, help="Specific package folder to validate.")
    validate_p.set_defaults(func=validate_latest)

    dry_p = sub.add_parser("dry-run-import", help="Simulate import by copying package to temp and checking it.")
    dry_p.add_argument("--source-name", default="laptop")
    dry_p.add_argument("--package", default=None)
    dry_p.set_defaults(func=dry_run_import)

    merge_p = sub.add_parser(
        "merge-packages",
        help="Merge two source packages into a canonical latest-wins package for two-way sync.",
    )
    merge_p.add_argument("--left-source", default="laptop")
    merge_p.add_argument("--right-source", default="desktop")
    merge_p.add_argument("--output-source", default="merged")
    merge_p.add_argument("--left-package", default=None)
    merge_p.add_argument("--right-package", default=None)
    merge_p.add_argument(
        "--canonical-profile",
        default=None,
        help="Canonical USERPROFILE for the merged package. Defaults to the left package profile.",
    )
    merge_p.add_argument(
        "--conflict-threshold-ms",
        type=int,
        default=1000,
        help="Only report same-thread latest-wins decisions as conflicts above this timestamp delta.",
    )
    merge_p.set_defaults(func=merge_packages)

    import_p = sub.add_parser("import", help="Import a package into this machine's live Codex profile.")
    import_p.add_argument("--source-name", default="laptop")
    import_p.add_argument("--package", default=None)
    import_p.add_argument("--apply", action="store_true", help="Required to replace live critical files.")
    import_p.add_argument(
        "--replace-profile-files",
        action="store_true",
        help="Replace local profile_files roots such as sessions/archived_sessions after backing them up.",
    )
    import_p.set_defaults(func=import_package)

    repair_p = sub.add_parser("repair-paths", help="Remap imported source-machine user paths to this machine.")
    repair_p.add_argument("--source-name", default="laptop")
    repair_p.add_argument("--package", default=None)
    repair_p.add_argument("--source-profile", default=None, help="Override source USERPROFILE path.")
    repair_p.add_argument("--apply", action="store_true", help="Required to modify live state files.")
    repair_p.set_defaults(func=repair_paths)

    events_p = sub.add_parser(
        "repair-user-event-flags",
        help="Mark imported threads with first user messages as visible user chats.",
    )
    events_p.add_argument("--apply", action="store_true", help="Required to modify live state files.")
    events_p.set_defaults(func=repair_user_event_flags)

    roots_p = sub.add_parser(
        "repair-project-roots",
        help="Align sidebar project roots with visible thread cwd path format.",
    )
    roots_p.add_argument("--apply", action="store_true", help="Required to modify live state files.")
    roots_p.set_defaults(func=repair_project_roots)

    hints_p = sub.add_parser(
        "repair-workspace-hints",
        help="Populate per-thread workspace root hints from session metadata.",
    )
    hints_p.add_argument("--apply", action="store_true", help="Required to modify live state files.")
    hints_p.set_defaults(func=repair_thread_workspace_hints)

    recency_p = sub.add_parser(
        "repair-thread-recency",
        help="Restore thread created/updated timestamps from the authoritative source package.",
    )
    recency_p.add_argument("--source-name", default="laptop")
    recency_p.add_argument("--package", default=None)
    recency_p.add_argument("--apply", action="store_true", help="Required to modify live state files.")
    recency_p.set_defaults(func=repair_thread_recency)

    mtimes_p = sub.add_parser(
        "repair-rollout-file-mtimes",
        help="Set rollout JSONL mtimes to thread updated_at so Codex startup does not rewrite recency.",
    )
    mtimes_p.add_argument("--apply", action="store_true", help="Required to modify rollout file mtimes.")
    mtimes_p.set_defaults(func=repair_rollout_file_mtimes)

    index_p = sub.add_parser(
        "repair-session-index-recency",
        help="Rewrite session_index.jsonl in visible-thread updated_at order.",
    )
    index_p.add_argument("--apply", action="store_true", help="Required to modify session_index.jsonl.")
    index_p.set_defaults(func=repair_session_index_recency)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
