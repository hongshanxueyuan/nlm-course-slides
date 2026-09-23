#!/usr/bin/env python3
"""
Aggregate stage reports into a single course status summary report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import default_resource_title, load_course_manifest, read_json, sanitize_filename, utc_timestamp, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--output-dir", help="Course output directory")
    parser.add_argument("--upload-report", help="Stage-C upload-report.json")
    parser.add_argument("--create-report", help="Stage-D create-report.json")
    parser.add_argument("--finalize-report", help="Stage-E/F finalize-report.json")
    parser.add_argument("--report-path", help="Optional summary report output path")
    parser.add_argument("--course-scene", help="Course scene override")
    parser.add_argument("--failed-only", action="store_true", help="Only emit failed items in the summary output")
    return parser


def _load_results(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    payload = read_json(path)
    results: dict[str, dict[str, Any]] = {}
    for item in payload.get("results", []):
        if isinstance(item, dict) and isinstance(item.get("section_id"), str):
            results[item["section_id"]] = item
    return results


def _final_status(upload_item: dict[str, Any] | None, create_item: dict[str, Any] | None, finalize_item: dict[str, Any] | None) -> str:
    if finalize_item:
        if finalize_item.get("downloaded"):
            return "downloaded_local"
        if finalize_item.get("error") and finalize_item.get("artifact_status") == "failed_download":
            return "failed_download"
        if finalize_item.get("renamed"):
            return "renamed"
        if finalize_item.get("artifact_status") in {"completed", "completed_remote"}:
            return "completed_remote"
        if finalize_item.get("artifact_status") in {"running", "pending", "queued"}:
            return "running"
    if create_item:
        if create_item.get("status") == "failed_create":
            return "failed_create"
        if create_item.get("status") == "create_requested":
            return "create_requested"
    if upload_item:
        if upload_item.get("status") == "failed_upload":
            return "failed_upload"
        if upload_item.get("status") == "uploaded":
            return "uploaded"
    return "pending"


def _retry_from_stage(status: str) -> str | None:
    if status == "failed_upload":
        return "upload"
    if status == "failed_create":
        return "create"
    if status in {"running", "failed_download"}:
        return "finalize"
    return None


def main() -> int:
    args = build_parser().parse_args()
    upload_report_path = Path(args.upload_report).resolve() if args.upload_report else None
    create_report_path = Path(args.create_report).resolve() if args.create_report else None
    finalize_report_path = Path(args.finalize_report).resolve() if args.finalize_report else None
    upload_payload = read_json(upload_report_path) if upload_report_path and upload_report_path.exists() else {}
    create_payload = read_json(create_report_path) if create_report_path and create_report_path.exists() else {}
    finalize_payload = read_json(finalize_report_path) if finalize_report_path and finalize_report_path.exists() else {}
    manifest = load_course_manifest(
        args.manifest,
        course_scene=(
            args.course_scene
            or finalize_payload.get("course_scene")
            or create_payload.get("course_scene")
            or upload_payload.get("course_scene")
        ),
    )
    output_dir = Path(args.output_dir or sanitize_filename(manifest.course_title)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    upload_results = _load_results(upload_report_path if upload_report_path else output_dir / "upload-report.json")
    create_results = _load_results(create_report_path if create_report_path else output_dir / "create-report.json")
    finalize_results = _load_results(finalize_report_path if finalize_report_path else output_dir / "finalize-report.json")

    sections = []
    failed_items = []
    retryable_items = []
    status_counts: dict[str, int] = {}

    for section in manifest.sections:
        upload_item = upload_results.get(section.id)
        create_item = create_results.get(section.id)
        finalize_item = finalize_results.get(section.id)
        current_status = _final_status(upload_item, create_item, finalize_item)
        status_counts[current_status] = status_counts.get(current_status, 0) + 1

        entry = {
            "section_id": section.id,
            "section_title": section.title,
            "resource_title": section.resource_title or default_resource_title(section.id, section.title),
            "current_status": current_status,
            "source_id": upload_item.get("source_id") if upload_item else None,
            "artifact_id": create_item.get("artifact_id") if create_item else finalize_item.get("artifact_id") if finalize_item else None,
            "output_path": finalize_item.get("output_path") if finalize_item else None,
            "error": (finalize_item or create_item or upload_item or {}).get("error"),
        }
        sections.append(entry)

        retry_stage = _retry_from_stage(current_status)
        if current_status.startswith("failed"):
            failed_items.append(entry)
        if retry_stage:
            retryable_items.append(
                {
                    "section_id": section.id,
                    "section_title": section.title,
                    "current_status": current_status,
                    "retry_from_stage": retry_stage,
                }
            )

    payload = {
        "manifest": manifest.source_path,
        "course_title": manifest.course_title,
        "course_scene": manifest.course_scene,
        "output_dir": str(output_dir),
        "generated_at": utc_timestamp(),
        "status_counts": status_counts,
        "sections": failed_items if args.failed_only else sections,
        "failed_items": failed_items,
        "retryable_items": retryable_items,
    }
    report_path = Path(args.report_path).resolve() if args.report_path else output_dir / "course-status-summary.json"
    write_json(report_path, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
