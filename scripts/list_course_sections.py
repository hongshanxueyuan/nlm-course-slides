#!/usr/bin/env python3
"""
Generate the stage-B section list report from a course manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import (
    build_section_list_entry,
    load_course_manifest,
    materialize_markdown_artifacts,
    sanitize_filename,
    utc_timestamp,
    write_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--output-dir", help="Course output directory")
    parser.add_argument("--report-path", help="Optional section list output path")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = load_course_manifest(args.manifest)
    output_dir = Path(args.output_dir or sanitize_filename(manifest.course_title)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    materialize_markdown_artifacts(manifest, output_dir=output_dir)

    sections = [build_section_list_entry(section) for section in manifest.sections]
    payload = {
        "manifest": manifest.source_path,
        "course_title": manifest.course_title,
        "output_dir": str(output_dir),
        "generated_at": utc_timestamp(),
        "total_sections": len(sections),
        "sections": sections,
    }
    report_path = Path(args.report_path).resolve() if args.report_path else output_dir / "section-list.json"
    write_json(report_path, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
