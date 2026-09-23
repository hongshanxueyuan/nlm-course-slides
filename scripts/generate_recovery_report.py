#!/usr/bin/env python3
"""
Scan a NotebookLM notebook and generate a recovery report for a course manifest.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from common import (
    configure_nlm_api_delay,
    ensure_authenticated,
    load_course_manifest,
    log_message,
    match_sections_to_notebook_state,
    sanitize_filename,
    utc_timestamp,
    write_json,
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


def main() -> int:
    args = build_parser().parse_args()
    if args.api_delay_seconds < 0:
        raise SystemExit("--api-delay-seconds must be at least 0")

    configure_nlm_api_delay(args.api_delay_seconds)
    manifest = load_course_manifest(args.manifest, course_scene=args.course_scene)
    output_dir = Path(args.output_dir or sanitize_filename(manifest.course_title)).resolve()
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
