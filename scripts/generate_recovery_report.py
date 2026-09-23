#!/usr/bin/env python3
"""
Scan a NotebookLM notebook and generate a recovery report for a course manifest.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from common import (
    configure_nlm_api_delay,
    ensure_authenticated,
    load_course_manifest,
    log_message,
    match_sections_to_notebook_state,
    read_json,
    sanitize_filename,
    utc_timestamp,
    write_json,
)


RECOVERY_SCENE_REPORT_NAMES = (
    "recovery-report.json",
    "finalize-report.json",
    "create-report.json",
    "upload-report.json",
    "section-list.json",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--notebook-id", required=True, help="Existing notebook ID or alias")
    parser.add_argument("--output-dir", help="Course output directory")
    parser.add_argument("--report-path", help="Optional recovery report path")
    parser.add_argument("--course-scene", help="Course scene override")
    parser.add_argument("--profile", help="NotebookLM profile")
    parser.add_argument("--api-delay-seconds", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _load_report_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = read_json(path)
    return payload if isinstance(payload, dict) else {}


def _resolve_recovery_course_scene(
    *,
    manifest_path: str,
    output_dir: Path,
    report_path: str | None,
    course_scene_override: str | None,
) -> str | None:
    if course_scene_override:
        return course_scene_override

    candidate_paths: list[Path] = []
    if report_path:
        candidate_paths.append(Path(report_path).resolve())
    candidate_paths.extend(output_dir / name for name in RECOVERY_SCENE_REPORT_NAMES)

    seen_paths: set[Path] = set()
    for path in candidate_paths:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        payload = _load_report_payload(path)
        raw_course_scene = payload.get("course_scene")
        if isinstance(raw_course_scene, str) and raw_course_scene.strip():
            log_message(f"[recovery-report] reuse course_scene from {path.name}: {raw_course_scene.strip()}")
            return raw_course_scene.strip()

    manifest = load_course_manifest(manifest_path)
    return manifest.course_scene


def main() -> int:
    args = build_parser().parse_args()
    if args.api_delay_seconds < 0:
        raise SystemExit("--api-delay-seconds must be at least 0")

    configure_nlm_api_delay(args.api_delay_seconds)
    initial_manifest = load_course_manifest(args.manifest)
    output_dir = Path(args.output_dir or sanitize_filename(initial_manifest.course_title)).resolve()
    resolved_course_scene = _resolve_recovery_course_scene(
        manifest_path=args.manifest,
        output_dir=output_dir,
        report_path=args.report_path,
        course_scene_override=args.course_scene,
    )
    manifest = load_course_manifest(args.manifest, course_scene=resolved_course_scene)
    output_dir.mkdir(parents=True, exist_ok=True)

    log_message(f"[recovery-report] loaded manifest: {manifest.source_path}")
    log_message(f"[recovery-report] output directory: {output_dir}")
    ensure_authenticated(profile=args.profile, dry_run=args.dry_run)
    log_message("[recovery-report] authentication check complete")

    states = match_sections_to_notebook_state(
        manifest,
        notebook_id=args.notebook_id,
        output_dir=output_dir,
        profile=args.profile,
        dry_run=args.dry_run,
    )
    counts: dict[str, int] = {}
    for state in states:
        counts[state.status] = counts.get(state.status, 0) + 1

    report = {
        "manifest": manifest.source_path,
        "course_title": manifest.course_title,
        "course_scene": manifest.course_scene,
        "output_dir": str(output_dir),
        "notebook_id": args.notebook_id,
        "generated_at": utc_timestamp(),
        "matching_strategy": "artifact_title_then_source_title_then_local_output_name",
        "api_delay_seconds": args.api_delay_seconds,
        "results": [asdict(state) for state in states],
        "status_counts": counts,
    }
    report_path = Path(args.report_path).resolve() if args.report_path else output_dir / "recovery-report.json"
    if not args.dry_run:
        write_json(report_path, report)
        log_message(f"[recovery-report] wrote report: {report_path}")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
