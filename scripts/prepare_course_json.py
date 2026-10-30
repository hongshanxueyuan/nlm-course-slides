#!/usr/bin/env python3
"""
Save course JSON returned by get_runtime_course_content into a local course directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import log_message, sanitize_filename, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-stdin",
        action="store_true",
        required=True,
        help="Read the complete get_runtime_course_content JSON response from stdin",
    )
    parser.add_argument("--course-name", required=True, help="Course name used to create the local course directory")
    parser.add_argument("--output-root", default=".", help="Root directory where the course directory should be created")
    parser.add_argument("--filename", help="Target JSON filename inside the course directory")
    parser.add_argument("--report-path", help="Optional course JSON report output path")
    parser.add_argument("--force", action="store_true", help="Overwrite the target JSON file if it already exists")
    return parser


def read_course_json() -> bytes:
    raw_json = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid UTF-8 JSON received on stdin: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Course JSON returned by get_runtime_course_content must be a JSON object")
    return raw_json


def main() -> int:
    args = build_parser().parse_args()
    raw_json = read_course_json()

    course_dir = Path(args.output_root).resolve() / sanitize_filename(args.course_name, fallback="course")
    course_dir.mkdir(parents=True, exist_ok=True)

    default_name = f"{sanitize_filename(args.course_name, fallback='course')}.json"
    target_file = course_dir / (args.filename or default_name)

    if target_file.exists() and not args.force:
        raise SystemExit(f"Target file already exists: {target_file}. Use --force to overwrite.")

    log_message(f"[course-json] save get_runtime_course_content JSON to {target_file}")
    target_file.write_bytes(raw_json)

    report_path = Path(args.report_path).resolve() if args.report_path else course_dir / "course-json-report.json"
    report = {
        "status": "ready",
        "source_kind": "insights_mcp_server:get_runtime_course_content",
        "course_name": args.course_name,
        "course_dir": str(course_dir),
        "json_path": str(target_file),
        "report_path": str(report_path),
    }
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
