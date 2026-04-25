#!/usr/bin/env python3
"""Validate imported Codex Desktop state and model sidebar project grouping."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_TARGET_PROJECTS: tuple[str, ...] = ()


def without_long_prefix(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return value[4:] if value.startswith("\\\\?\\") else value


def norm_path(value: str | None) -> str:
    return without_long_prefix(value).rstrip("\\/")


def path_parts(value: str | None) -> list[str]:
    path = norm_path(value).replace("/", "\\")
    return [part for part in path.split("\\") if part]


def basename(value: str | None) -> str:
    parts = path_parts(value)
    return parts[-1] if parts else ""


def iso_from_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except Exception:
        return None


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_threads(db_path: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, title, cwd, rollout_path, has_user_event, archived,
                   created_at, created_at_ms, updated_at, updated_at_ms,
                   first_user_message
            FROM threads
            ORDER BY COALESCE(updated_at_ms, 0) DESC, COALESCE(created_at_ms, 0) DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def integrity(db_path: Path) -> str:
    if not db_path.exists():
        return "missing"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()


def choose_root(thread: dict, hints: dict, roots: list[str]) -> str:
    hinted = hints.get(str(thread["id"]))
    if isinstance(hinted, str) and hinted:
        return norm_path(hinted)
    cwd = norm_path(thread.get("cwd"))
    matches = [
        norm_path(root)
        for root in roots
        if cwd.lower() == norm_path(root).lower()
        or cwd.lower().startswith((norm_path(root) + "\\").lower())
    ]
    if matches:
        return max(matches, key=len)
    return cwd


def project_for_root(root: str) -> str:
    return basename(root) or "(projectless)"


def title_of(thread: dict) -> str:
    title = str(thread.get("title") or "").strip()
    if title:
        return title.splitlines()[0][:120]
    msg = str(thread.get("first_user_message") or "").strip()
    return msg.splitlines()[0][:120] if msg else str(thread.get("id"))


def load_package_info(state_sync_root: Path) -> dict:
    source = state_sync_root / "source-laptop"
    packages = sorted([p for p in source.iterdir() if p.is_dir()]) if source.exists() else []
    if not packages:
        return {"latest_package": None}
    latest = packages[-1]
    manifest = latest / "manifest.json"
    return {
        "latest_package": str(latest),
        "manifest": read_json(manifest) if manifest.exists() else None,
    }


def build_report(args: argparse.Namespace) -> dict:
    codex_dir = Path(args.codex_dir).expanduser()
    state_sync_root = Path(args.state_sync_root).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    db_path = codex_dir / "state_5.sqlite"
    global_path = codex_dir / ".codex-global-state.json"
    session_index = codex_dir / "session_index.jsonl"

    global_state = read_json(global_path)
    saved_roots = [
        norm_path(root)
        for key in ("electron-saved-workspace-roots", "active-workspace-roots", "project-order")
        for root in global_state.get(key, [])
        if isinstance(root, str)
    ]
    seen_roots = []
    for root in saved_roots:
        if root and root.lower() not in {r.lower() for r in seen_roots}:
            seen_roots.append(root)

    hints = global_state.get("thread-workspace-root-hints", {})
    if not isinstance(hints, dict):
        hints = {}

    all_threads = read_threads(db_path) if db_path.exists() else []
    visible = [
        thread
        for thread in all_threads
        if int(thread.get("has_user_event") or 0) == 1 and int(thread.get("archived") or 0) == 0
    ]

    project_groups: dict[str, dict] = {}
    recent_sidebar = []
    target_project_reports = {}
    missing_rollout_paths = []

    for rank, thread in enumerate(visible, start=1):
        root = choose_root(thread, hints, seen_roots)
        project = project_for_root(root)
        item = {
            "rank": rank,
            "id": str(thread["id"]),
            "title": title_of(thread),
            "cwd": norm_path(thread.get("cwd")),
            "workspace_root": root,
            "rollout_path": norm_path(thread.get("rollout_path")),
            "updated_at_ms": thread.get("updated_at_ms"),
            "updated_at_iso": iso_from_ms(thread.get("updated_at_ms")),
        }
        project_groups.setdefault(
            project,
            {"project": project, "root": root, "visible_count": 0, "threads": []},
        )
        project_groups[project]["visible_count"] += 1
        if len(project_groups[project]["threads"]) < args.per_project_limit:
            project_groups[project]["threads"].append(item)
        if len(recent_sidebar) < args.recent_limit:
            recent_sidebar.append(item | {"project": project})
        rollout = item["rollout_path"]
        if rollout and not Path(rollout).exists():
            missing_rollout_paths.append(item)

    saved_root_names = {basename(root).lower(): root for root in seen_roots}
    target_projects = tuple(args.target_project or DEFAULT_TARGET_PROJECTS)
    for project in target_projects:
        project_threads = [
            item for item in (recent_sidebar + [
                t
                for group in project_groups.values()
                for t in group["threads"]
            ])
            if project.lower() in [part.lower() for part in path_parts(item.get("workspace_root"))]
            or project.lower() in [part.lower() for part in path_parts(item.get("cwd"))]
        ]
        all_project_visible = [
            thread
            for thread in visible
            if project.lower() in [part.lower() for part in path_parts(choose_root(thread, hints, seen_roots))]
            or project.lower() in [part.lower() for part in path_parts(thread.get("cwd"))]
        ]
        ranks = [
            index + 1
            for index, thread in enumerate(visible)
            if thread in all_project_visible
        ]
        root_present = project.lower() in saved_root_names or any(
            project.lower() in [part.lower() for part in path_parts(root)] for root in seen_roots
        )
        target_project_reports[project] = {
            "saved_project_root_present": root_present,
            "visible_thread_count": len(all_project_visible),
            "best_recent_rank": min(ranks) if ranks else None,
            "within_recent_limit": bool(ranks and min(ranks) <= args.recent_limit),
            "sample_threads": project_threads[:10],
        }

    state_pass = (
        db_path.exists()
        and integrity(db_path) == "ok"
        and all(
            item["saved_project_root_present"]
            and item["visible_thread_count"] > 0
            and item["within_recent_limit"]
            for item in target_project_reports.values()
        )
    )

    screenshot_path = output_dir / "codex-sidebar.png"
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "codex_dir": str(codex_dir),
        "state_sync_root": str(state_sync_root),
        "package": load_package_info(state_sync_root),
        "files": {
            "sqlite": {"path": str(db_path), "exists": db_path.exists(), "integrity": integrity(db_path)},
            "global_state": {"path": str(global_path), "exists": global_path.exists()},
            "session_index": {"path": str(session_index), "exists": session_index.exists()},
        },
        "counts": {
            "threads_total": len(all_threads),
            "threads_visible_unarchived": len(visible),
            "saved_roots": len(seen_roots),
            "workspace_hints": len(hints),
            "missing_rollout_paths": len(missing_rollout_paths),
        },
        "target_projects": target_project_reports,
        "simulated_sidebar": {
            "recent_limit": args.recent_limit,
            "recent_threads": recent_sidebar,
            "project_groups": sorted(project_groups.values(), key=lambda item: item["project"].lower()),
        },
        "screenshot": {
            "path": str(screenshot_path),
            "exists": screenshot_path.exists(),
            "bytes": screenshot_path.stat().st_size if screenshot_path.exists() else 0,
        },
        "pass": {
            "state": state_pass,
            "screenshot_exists": screenshot_path.exists() and screenshot_path.stat().st_size > 10000
            if screenshot_path.exists()
            else False,
            "overall_without_visual_ocr": False,
        },
        "notes": [
            "Long-path \\\\?\\ prefixes are normalized before grouping.",
            "Visual pass still requires inspecting codex-sidebar.png for rendered sidebar content.",
        ],
    }
    report["pass"]["overall_without_visual_ocr"] = bool(
        report["pass"]["state"] and report["pass"]["screenshot_exists"]
    )
    return report


def write_grouping_text(report: dict, path: Path) -> None:
    lines = []
    lines.append(f"Generated: {report['generated_at_utc']}")
    lines.append(f"State pass: {report['pass']['state']}")
    lines.append("")
    lines.append("Target projects:")
    for name, item in report["target_projects"].items():
        lines.append(
            f"- {name}: root={item['saved_project_root_present']} "
            f"visible={item['visible_thread_count']} best_rank={item['best_recent_rank']} "
            f"within_recent={item['within_recent_limit']}"
        )
        for thread in item["sample_threads"][:5]:
            lines.append(f"  [{thread['rank']}] {thread['title']} ({thread['workspace_root']})")
    lines.append("")
    lines.append("Simulated recent sidebar:")
    for thread in report["simulated_sidebar"]["recent_threads"]:
        lines.append(f"[{thread['rank']}] {thread['project']} - {thread['title']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-dir", default=str(Path(os.environ["USERPROFILE"]) / ".codex"))
    parser.add_argument(
        "--state-sync-root",
        default=str(Path(os.environ.get("OneDriveConsumer") or os.environ["OneDrive"]) / "Codex" / "StateSync"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(os.environ.get("OneDriveConsumer") or os.environ["OneDrive"]) / "Codex" / "StateSync" / "desktop-ui-checks"),
    )
    parser.add_argument("--recent-limit", type=int, default=80)
    parser.add_argument("--per-project-limit", type=int, default=20)
    parser.add_argument(
        "--target-project",
        action="append",
        default=[],
        help="Project folder basename to require in validation. Repeat for multiple projects.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args)
    report_path = output_dir / "codex-state-report.json"
    grouping_path = output_dir / "codex-sidebar-simulated.txt"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_grouping_text(report, grouping_path)
    print(json.dumps({"report": str(report_path), "grouping": str(grouping_path), "pass": report["pass"]}, indent=2))
    return 0 if report["pass"]["overall_without_visual_ocr"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
