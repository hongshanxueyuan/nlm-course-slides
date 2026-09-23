#!/usr/bin/env python3
"""
Generate slides for a single course section.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

from common import (
    Section,
    add_text_source,
    configure_nlm_api_delay,
    create_slide_deck,
    default_output_name,
    default_resource_title,
    download_slide_deck,
    ensure_authenticated,
    find_section,
    load_course_manifest,
    log_message,
    normalize_course_scene,
    rename_artifact,
    resolve_section_focus,
    sanitize_filename,
    wait_for_artifact,
    write_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook-id", required=True, help="NotebookLM notebook ID or alias")
    parser.add_argument("--output-dir", required=True, help="Directory for the generated PPTX")
    parser.add_argument("--manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--section-id", help="Section ID inside the manifest")
    parser.add_argument("--title", help="Section title when not using --manifest")
    parser.add_argument("--content-file", help="UTF-8 text/markdown file for section content")
    parser.add_argument("--content", help="Inline section content when not using --manifest")
    parser.add_argument("--focus", help="Custom focus prompt for slide generation")
    parser.add_argument("--course-scene", help="Course scene override")
    parser.add_argument("--resource-title", help="Override NotebookLM source title")
    parser.add_argument("--output-name", help="Override output PPTX filename")
    parser.add_argument("--source-id", help="Reuse an existing NotebookLM source ID")
    parser.add_argument("--artifact-id", help="Reuse an existing NotebookLM artifact ID")
    parser.add_argument("--profile", help="NotebookLM profile")
    parser.add_argument("--skip-auth-check", action="store_true")
    parser.add_argument("--language", default="zh_Hans")
    parser.add_argument("--deck-format", default="detailed_deck")
    parser.add_argument("--length", default="default")
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--source-wait-timeout", type=float, default=600.0)
    parser.add_argument("--api-delay-seconds", type=float, default=15.0)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--skip-rename", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def load_section(args: argparse.Namespace) -> tuple[Section, str | None]:
    if args.manifest:
        if not args.section_id:
            raise SystemExit("--section-id is required when --manifest is used")
        manifest = load_course_manifest(args.manifest, course_scene=args.course_scene)
        return find_section(manifest, args.section_id), manifest.course_scene

    if not args.title:
        raise SystemExit("--title is required when --manifest is not used")
    if not args.content and not args.content_file:
        raise SystemExit("Provide --content or --content-file when not using --manifest")

    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")

    return (
        Section(
            id="1",
            order=1,
            title=args.title,
            content=(content or "").strip(),
            slug=sanitize_filename(args.title),
            output_name=args.output_name or default_output_name("1", args.title),
        ),
        args.course_scene,
    )


def emit_result(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    args = build_parser().parse_args()
    if args.api_delay_seconds < 0:
        raise SystemExit("--api-delay-seconds must be at least 0")
    configure_nlm_api_delay(args.api_delay_seconds)
    section, course_scene = load_section(args)
    course_scene = normalize_course_scene(course_scene)
    output_dir = Path(args.output_dir).resolve()
    output_name = args.output_name or section.output_name
    output_path = output_dir / output_name
    metadata_path = output_path.with_suffix(".slide.json")
    resource_title = args.resource_title or section.resource_title or default_resource_title(section.id, section.title)
    artifact_title = Path(output_name).stem
    focus = resolve_section_focus(
        section,
        course_scene=course_scene,
        explicit_focus=args.focus,
    )
    source_id = None
    artifact_id = None
    current_step = "initialize"

    if args.download_only and not args.artifact_id:
        raise SystemExit("--download-only requires --artifact-id")
    if args.prepare_only and args.artifact_id:
        raise SystemExit("--prepare-only cannot be combined with --artifact-id")

    if args.skip_existing and output_path.exists():
        log_message(f"[section {section.id}] skip existing output: {output_path}")
        payload = {
            "status": "skipped",
            "reason": "output-exists",
            "section_id": section.id,
            "output_path": str(output_path),
        }
        emit_result(payload)
        return 0

    log_message(
        f"[section {section.id}] start: {section.title}"
    )
    try:
        current_step = "ensure_output_directory"
        output_dir.mkdir(parents=True, exist_ok=True)
        log_message(f"[section {section.id}] ensure output directory: {output_dir}")

        if args.skip_auth_check:
            log_message(f"[section {section.id}] skip authentication check")
        else:
            current_step = "ensure_authenticated"
            ensure_authenticated(profile=args.profile, dry_run=args.dry_run)
            log_message(f"[section {section.id}] authentication check complete")

        if args.source_id:
            source_id = args.source_id
            log_message(f"[section {section.id}] reuse source: {source_id}")
        elif not args.artifact_id:
            current_step = "add_text_source"
            log_message(f"[section {section.id}] upload source: {resource_title}")
            source_id = add_text_source(
                args.notebook_id,
                resource_title,
                section.content,
                profile=args.profile,
                dry_run=args.dry_run,
                wait_timeout=args.source_wait_timeout,
            )
            log_message(f"[section {section.id}] source ready: {source_id}")

        if not args.prepare_only:
            if args.artifact_id:
                artifact_id = args.artifact_id
                log_message(f"[section {section.id}] reuse artifact: {artifact_id}")
            elif not args.download_only:
                current_step = "create_slide_deck"
                log_message(f"[section {section.id}] create slide deck")
                if source_id is None:
                    raise RuntimeError("source_id was not prepared before slide creation")
                artifact_id = create_slide_deck(
                    args.notebook_id,
                    source_id=source_id,
                    focus=focus,
                    language=args.language,
                    deck_format=args.deck_format,
                    length=args.length,
                    profile=args.profile,
                    dry_run=args.dry_run,
                )
                log_message(f"[section {section.id}] slide deck started: {artifact_id}")

            if not args.download_only:
                current_step = "wait_for_artifact"
                log_message(f"[section {section.id}] wait for slide deck completion")
                if artifact_id is None:
                    raise RuntimeError("artifact_id was not prepared before artifact polling")
                wait_for_artifact(
                    args.notebook_id,
                    artifact_id,
                    profile=args.profile,
                    dry_run=args.dry_run,
                    timeout_seconds=args.timeout_seconds,
                    poll_interval=args.poll_interval,
                )
                log_message(f"[section {section.id}] slide deck completed: {artifact_id}")

            if not args.skip_rename:
                current_step = "rename_artifact"
                log_message(f"[section {section.id}] rename artifact: {artifact_title}")
                if artifact_id is None:
                    raise RuntimeError("artifact_id was not prepared before rename")
                rename_artifact(
                    artifact_id,
                    artifact_title,
                    profile=args.profile,
                    dry_run=args.dry_run,
                )

            current_step = "download_slide_deck"
            log_message(f"[section {section.id}] download slide deck: {output_path}")
            if artifact_id is None:
                raise RuntimeError("artifact_id was not prepared before download")
            download_slide_deck(
                args.notebook_id,
                artifact_id,
                output_path,
                file_format="pptx",
                dry_run=args.dry_run,
            )
        else:
            log_message(f"[section {section.id}] prepare-only mode, skip slide generation and download")

        metadata = {
            "status": "prepared"
            if args.prepare_only and not args.dry_run
            else "dry-run"
            if args.dry_run
            else "completed",
            "prepare_only": args.prepare_only,
            "download_only": args.download_only,
            "section_id": section.id,
            "section_title": section.title,
            "resource_title": resource_title,
            "course_scene": course_scene,
            "focus": focus,
            "notebook_id": args.notebook_id,
            "source_id": source_id,
            "artifact_id": artifact_id,
            "output_path": str(output_path),
            "output_name": output_name,
        }
        if not args.dry_run and not args.prepare_only:
            write_json(metadata_path, metadata)
            log_message(f"[section {section.id}] wrote sidecar metadata: {metadata_path}")
        log_message(f"[section {section.id}] done")
        emit_result(metadata)
        return 0
    except Exception as exc:
        log_message(f"[section {section.id}] failed during {current_step}: {exc}")
        failure = {
            "status": "failed",
            "prepare_only": args.prepare_only,
            "download_only": args.download_only,
            "failed_step": current_step,
            "error": str(exc),
            "section_id": section.id,
            "section_title": section.title,
            "resource_title": resource_title,
            "course_scene": course_scene,
            "focus": focus,
            "notebook_id": args.notebook_id,
            "source_id": source_id,
            "artifact_id": artifact_id,
            "output_path": str(output_path),
            "output_name": output_name,
        }
        emit_result(failure)
        return 1


if __name__ == "__main__":
    sys.exit(main())
