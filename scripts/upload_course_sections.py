#!/usr/bin/env python3
"""
Upload course sections to NotebookLM serially and write the stage-C report.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from common import (
    add_text_source,
    configure_nlm_api_delay,
    create_notebook,
    default_resource_title,
    ensure_authenticated,
    find_section,
    load_canonical_markdown_body,
    load_course_manifest,
    load_section_list_metadata_map,
    read_json,
    sanitize_filename,
    utc_timestamp,
    write_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--section-list", help="Stage-B section-list.json")
    parser.add_argument("--retry-failed-from-report", help="Retry only failed uploads from a previous upload report")
    parser.add_argument("--section-id", action="append", dest="section_ids", help="Only upload the specified section ID")
    parser.add_argument("--notebook-id", help="Existing notebook ID or alias")
    parser.add_argument("--notebook-title", help="Notebook title when creating a new notebook")
    parser.add_argument("--output-dir", help="Course output directory")
    parser.add_argument("--report-path", help="Optional upload report output path")
    parser.add_argument("--profile", help="NotebookLM profile")
    parser.add_argument("--source-wait-timeout", type=float, default=600.0)
    parser.add_argument("--api-delay-seconds", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _load_section_id_filter(args: argparse.Namespace) -> set[str] | None:
    selected: set[str] = set(args.section_ids or [])
    if args.section_list:
        payload = read_json(Path(args.section_list).resolve())
        for item in payload.get("sections", []):
            if isinstance(item, dict) and isinstance(item.get("section_id"), str):
                selected.add(item["section_id"])
    if args.retry_failed_from_report:
        payload = read_json(Path(args.retry_failed_from_report).resolve())
        for item in payload.get("results", []):
            if isinstance(item, dict) and item.get("status") == "failed_upload" and isinstance(item.get("section_id"), str):
                selected.add(item["section_id"])
    return selected or None


def main() -> int:
    args = build_parser().parse_args()
    if args.api_delay_seconds < 0:
        raise SystemExit("--api-delay-seconds must be at least 0")

    configure_nlm_api_delay(args.api_delay_seconds)
    manifest = load_course_manifest(args.manifest)
    output_dir = Path(args.output_dir or sanitize_filename(manifest.course_title)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    section_id_filter = _load_section_id_filter(args)
    section_metadata_by_id = (
        load_section_list_metadata_map(Path(args.section_list).resolve())
        if args.section_list
        else {}
    )
    sections = manifest.sections
    if section_id_filter:
        sections = [section for section in sections if section.id in section_id_filter]
    if not sections:
        raise SystemExit("No sections selected for upload")

    ensure_authenticated(profile=args.profile, dry_run=args.dry_run)
    notebook_id = args.notebook_id or create_notebook(
        args.notebook_title or manifest.course_title,
        profile=args.profile,
        dry_run=args.dry_run,
    )
    notebook_action = "reused" if args.notebook_id else "created"

    results = []
    failures = 0
    for index, section in enumerate(sections):
        section_metadata = section_metadata_by_id.get(section.id)
        resource_title = str(
            (section_metadata or {}).get("resource_title")
            or section.resource_title
            or default_resource_title(section.id, section.title)
        ).strip()
        try:
            md_name, block_type, markdown_body = load_canonical_markdown_body(
                section,
                output_dir=output_dir,
                metadata=section_metadata,
                default_block_type=manifest.default_block_type,
            )
            source_id = add_text_source(
                notebook_id,
                resource_title,
                markdown_body,
                profile=args.profile,
                dry_run=args.dry_run,
                wait_timeout=args.source_wait_timeout,
            )
            results.append(
                {
                    "section_id": section.id,
                    "section_title": section.title,
                    "resource_title": resource_title,
                    "md_name": md_name,
                    "block_type": block_type,
                    "status": "uploaded",
                    "source_id": source_id,
                    "error": None,
                }
            )
        except Exception as exc:
            failures += 1
            results.append(
                {
                    "section_id": section.id,
                    "section_title": section.title,
                    "resource_title": resource_title,
                    "md_name": str((section_metadata or {}).get("md_name") or section.md_name or ""),
                    "block_type": str((section_metadata or {}).get("block_type") or section.block_type or manifest.default_block_type),
                    "status": "failed_upload",
                    "source_id": None,
                    "error": str(exc),
                }
            )
        if index < len(sections) - 1:
            time.sleep(args.api_delay_seconds)

    payload = {
        "manifest": manifest.source_path,
        "course_title": manifest.course_title,
        "output_dir": str(output_dir),
        "notebook_id": notebook_id,
        "notebook_title": args.notebook_title or manifest.course_title,
        "notebook_action": notebook_action,
        "generated_at": utc_timestamp(),
        "api_delay_seconds": args.api_delay_seconds,
        "total_sections": len(sections),
        "failures": failures,
        "results": results,
    }
    report_path = Path(args.report_path).resolve() if args.report_path else output_dir / "upload-report.json"
    write_json(report_path, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
