#!/usr/bin/env python3
"""
Create NotebookLM slide artifacts serially from uploaded section sources.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from common import (
    configure_nlm_api_delay,
    create_slide_deck,
    ensure_authenticated,
    find_section,
    load_course_manifest,
    read_json,
    resolve_section_focus,
    sanitize_filename,
    utc_timestamp,
    write_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--upload-report", required=True, help="Stage-C upload-report.json")
    parser.add_argument("--retry-failed-from-report", help="Retry only failed creates from a previous create report")
    parser.add_argument("--section-id", action="append", dest="section_ids", help="Only create slides for the specified section ID")
    parser.add_argument("--notebook-id", help="Existing notebook ID or alias")
    parser.add_argument("--output-dir", help="Course output directory")
    parser.add_argument("--report-path", help="Optional create report output path")
    parser.add_argument("--course-scene", help="Course scene override")
    parser.add_argument("--profile", help="NotebookLM profile")
    parser.add_argument("--language", default="zh_Hans")
    parser.add_argument("--deck-format", default="detailed_deck")
    parser.add_argument("--length", default="default")
    parser.add_argument("--api-delay-seconds", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _load_retry_filter(path: str | None) -> set[str]:
    if not path:
        return set()
    payload = read_json(Path(path).resolve())
    selected = set()
    for item in payload.get("results", []):
        if isinstance(item, dict) and item.get("status") == "failed_create" and isinstance(item.get("section_id"), str):
            selected.add(item["section_id"])
    return selected


def main() -> int:
    args = build_parser().parse_args()
    if args.api_delay_seconds < 0:
        raise SystemExit("--api-delay-seconds must be at least 0")

    configure_nlm_api_delay(args.api_delay_seconds)
    upload_report = read_json(Path(args.upload_report).resolve())
    manifest = load_course_manifest(
        args.manifest,
        course_scene=args.course_scene or upload_report.get("course_scene"),
    )
    output_dir = Path(args.output_dir or sanitize_filename(manifest.course_title)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    notebook_id = args.notebook_id or str(upload_report.get("notebook_id") or "").strip()
    if not notebook_id:
        raise SystemExit("Notebook ID is required via --notebook-id or upload-report.json")

    requested_ids = set(args.section_ids or [])
    requested_ids.update(_load_retry_filter(args.retry_failed_from_report))
    ensure_authenticated(profile=args.profile, dry_run=args.dry_run)

    queued_items = []
    for item in upload_report.get("results", []):
        if not isinstance(item, dict):
            continue
        section_id = item.get("section_id")
        if not isinstance(section_id, str):
            continue
        if requested_ids and section_id not in requested_ids:
            continue
        queued_items.append(item)

    eligible_create_count = sum(
        1
        for item in queued_items
        if item.get("status") == "uploaded" and isinstance(item.get("source_id"), str) and str(item.get("source_id")).strip()
    )

    results = []
    failures = 0
    created_count = 0
    for item in queued_items:
        if not isinstance(item, dict):
            continue
        section_id = item.get("section_id")
        if not isinstance(section_id, str):
            continue

        section = find_section(manifest, section_id)
        source_id = item.get("source_id")
        if item.get("status") != "uploaded" or not isinstance(source_id, str) or not source_id.strip():
            results.append(
                {
                    "section_id": section.id,
                    "section_title": section.title,
                    "source_id": source_id if isinstance(source_id, str) else None,
                    "artifact_id": None,
                    "status": "skipped",
                    "error": item.get("error"),
                }
            )
            continue

        try:
            artifact_id = create_slide_deck(
                notebook_id,
                source_id=source_id,
                focus=resolve_section_focus(
                    section,
                    course_scene=manifest.course_scene,
                ),
                language=args.language,
                deck_format=args.deck_format,
                length=args.length,
                profile=args.profile,
                dry_run=args.dry_run,
            )
            created_count += 1
            results.append(
                {
                    "section_id": section.id,
                    "section_title": section.title,
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "status": "create_requested",
                    "error": None,
                }
            )
        except Exception as exc:
            failures += 1
            results.append(
                {
                    "section_id": section.id,
                    "section_title": section.title,
                    "source_id": source_id,
                    "artifact_id": None,
                    "status": "failed_create",
                    "error": str(exc),
                }
            )
        if created_count + failures < eligible_create_count:
            time.sleep(args.api_delay_seconds)

    payload = {
        "manifest": manifest.source_path,
        "course_title": manifest.course_title,
        "course_scene": manifest.course_scene,
        "output_dir": str(output_dir),
        "notebook_id": notebook_id,
        "generated_at": utc_timestamp(),
        "api_delay_seconds": args.api_delay_seconds,
        "failures": failures,
        "results": results,
    }
    report_path = Path(args.report_path).resolve() if args.report_path else output_dir / "create-report.json"
    write_json(report_path, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
