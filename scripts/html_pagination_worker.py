#!/usr/bin/env python3
"""Prepare one HTML section pagination result for an external Codex subagent."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from common import (
    HTML_PAGINATION_MODEL,
    CourseManifest,
    HtmlPaginationCache,
    Section,
    _normalize_explicit_page_content,
    _resolve_section_block_type,
    _select_html_source_text,
    load_course_manifest,
    materialize_section_markdown,
    prepare_html_pagination_request,
    read_json,
    write_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument(
        "--output-dir",
        help="Worker result directory; defaults to .html-pagination-workers beside the manifest",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list-jobs", action="store_true", help="List HTML pagination jobs as JSON")
    mode.add_argument("--section-index", type=int, help="Run the 1-based section index from the manifest")
    return parser


def _output_dir(manifest_path: Path, configured_path: str | None) -> Path:
    if configured_path:
        return Path(configured_path).resolve()
    return (manifest_path.resolve().parent / ".html-pagination-workers").resolve()


def _manifest_hash(manifest: CourseManifest) -> str:
    return hashlib.sha256(Path(manifest.source_path).read_bytes()).hexdigest()


def _result_path(output_dir: Path, section_index: int) -> Path:
    return output_dir / f"section-{section_index:04d}.result.json"


def _cache_path(output_dir: Path, section_index: int) -> Path:
    return output_dir / f"section-{section_index:04d}.cache.json"


def _model_output_path(output_dir: Path, section_index: int) -> Path:
    return output_dir / f"section-{section_index:04d}.model.json"


def _failure_path(output_dir: Path, section_index: int) -> Path:
    return output_dir / f"section-{section_index:04d}.failure.json"


def _is_pagination_job(section: Section, manifest: CourseManifest) -> bool:
    return (
        section.page_content is None
        and _resolve_section_block_type(
            section,
            default_block_type=manifest.default_block_type,
        )
        == "html"
    )


def _request_hash(section: Section) -> str:
    return prepare_html_pagination_request(_select_html_source_text(section)).request_hash


def _validated_pages(
    payload: dict[str, Any] | None,
    *,
    manifest_hash: str,
    section_index: int,
    section: Section,
    request_hash: str,
) -> list[str] | None:
    if not payload or any(
        (
            payload.get("version") != 2,
            payload.get("manifest_sha256") != manifest_hash,
            payload.get("model") != HTML_PAGINATION_MODEL,
            payload.get("request_hash") != request_hash,
            payload.get("section_index") != section_index,
            payload.get("section_id") != section.id,
        )
    ):
        return None
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list):
        return None
    try:
        pages = _normalize_explicit_page_content(raw_pages)
    except RuntimeError:
        return None
    if payload.get("page_count") != len(pages):
        return None
    return pages


def _load_valid_pages(
    path: Path,
    *,
    manifest_hash: str,
    section_index: int,
    section: Section,
    request_hash: str,
) -> list[str] | None:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        model_hash = hashlib.sha256(
            _model_output_path(path.parent, section_index).read_bytes()
        ).hexdigest()
    except OSError:
        return None
    if payload.get("model_output_sha256") != model_hash:
        return None
    return _validated_pages(
        payload,
        manifest_hash=manifest_hash,
        section_index=section_index,
        section=section,
        request_hash=request_hash,
    )


def list_jobs(manifest_path: Path, output_dir: Path) -> dict[str, Any]:
    manifest = load_course_manifest(manifest_path)
    manifest_hash = _manifest_hash(manifest)
    jobs = []
    for section_index, section in enumerate(manifest.sections, start=1):
        if not _is_pagination_job(section, manifest):
            continue
        request_hash = _request_hash(section)
        result_path = _result_path(output_dir, section_index)
        cached_pages = _load_valid_pages(
            result_path,
            manifest_hash=manifest_hash,
            section_index=section_index,
            section=section,
            request_hash=request_hash,
        )
        jobs.append(
            {
                "section_index": section_index,
                "section_id": section.id,
                "section_title": section.title,
                "status": "cached" if cached_pages is not None else "pending",
                "result_path": str(result_path),
            }
        )
    return {
        "manifest_sha256": manifest_hash,
        "model": HTML_PAGINATION_MODEL,
        "output_dir": str(output_dir),
        "total_jobs": len(jobs),
        "jobs": jobs,
    }


def run_job(manifest_path: Path, output_dir: Path, section_index: int) -> dict[str, Any]:
    manifest = load_course_manifest(manifest_path)
    if section_index < 1 or section_index > len(manifest.sections):
        raise RuntimeError(f"section index must be between 1 and {len(manifest.sections)}")
    section = manifest.sections[section_index - 1]
    if not _is_pagination_job(section, manifest):
        raise RuntimeError(f"Section {section.id} does not need HTML pagination")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_hash = _manifest_hash(manifest)
    result_path = _result_path(output_dir, section_index)
    failure_path = _failure_path(output_dir, section_index)
    try:
        pagination_request = prepare_html_pagination_request(_select_html_source_text(section))
        request_hash = pagination_request.request_hash
        cached_pages = _load_valid_pages(
            result_path,
            manifest_hash=manifest_hash,
            section_index=section_index,
            section=section,
            request_hash=request_hash,
        )
        if cached_pages is not None:
            failure_path.unlink(missing_ok=True)
            return {
                "status": "cached",
                "section_index": section_index,
                "section_id": section.id,
                "result_path": str(result_path),
            }

        materialize_section_markdown(
            section,
            output_dir=output_dir,
            default_block_type=manifest.default_block_type,
            include_cover=False,
            write_artifact=False,
            pagination_cache=HtmlPaginationCache(_cache_path(output_dir, section_index)),
            model_output_path=_model_output_path(output_dir, section_index),
            pagination_request=pagination_request,
        )
        if section.page_content is None or section.page_count is None:
            raise RuntimeError(f"Section {section.id} did not produce paginated pages")
        write_json(
            result_path,
            {
                "version": 2,
                "model_output_sha256": hashlib.sha256(
                    _model_output_path(output_dir, section_index).read_bytes()
                ).hexdigest(),
                "manifest_sha256": manifest_hash,
                "model": pagination_request.model,
                "request_hash": request_hash,
                "section_index": section_index,
                "section_id": section.id,
                "page_count": section.page_count,
                "pages": section.page_content,
            },
        )
        failure_path.unlink(missing_ok=True)
    except Exception as exc:
        write_json(
            failure_path,
            {
                "status": "failed",
                "manifest_sha256": manifest_hash,
                "section_index": section_index,
                "section_id": section.id,
                "error": str(exc),
            },
        )
        raise

    return {
        "status": "complete",
        "section_index": section_index,
        "section_id": section.id,
        "page_count": section.page_count,
        "result_path": str(result_path),
    }


def apply_results(manifest: CourseManifest, output_dir: Path) -> None:
    manifest_hash = _manifest_hash(manifest)
    for section_index, section in enumerate(manifest.sections, start=1):
        if not _is_pagination_job(section, manifest):
            continue
        request_hash = _request_hash(section)
        result_path = _result_path(output_dir, section_index)
        pages = _load_valid_pages(
            result_path,
            manifest_hash=manifest_hash,
            section_index=section_index,
            section=section,
            request_hash=request_hash,
        )
        if pages is None:
            raise RuntimeError(
                f"Missing or stale HTML pagination result for section {section.id}; "
                "rerun its worker before generating the section list"
            )
        section.page_content = pages
        section.page_count = len(pages)
        section.model_pagination_result = True


def main() -> int:
    args = build_parser().parse_args()
    manifest_path = Path(args.manifest)
    output_dir = _output_dir(manifest_path, args.output_dir)
    if args.list_jobs:
        print(json.dumps(list_jobs(manifest_path, output_dir), ensure_ascii=True))
        return 0

    try:
        result = run_job(manifest_path, output_dir, args.section_index)
    except Exception as exc:
        print(f"HTML pagination worker failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
