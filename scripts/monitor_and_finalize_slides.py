#!/usr/bin/env python3
"""
Monitor created slide artifacts, rename them, and optionally download them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import (
    download_slide_deck,
    ensure_authenticated,
    find_section,
    load_course_manifest,
    read_json,
    rename_artifact,
    sanitize_filename,
    utc_timestamp,
    wait_for_artifact,
    write_json,
)


def _default_postprocess_image_path() -> str:
    return str((Path(__file__).resolve().parent.parent / "assets" / "logo.png").resolve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--create-report", required=True, help="Stage-D create-report.json")
    parser.add_argument("--retry-failed-from-report", help="Retry only unfinished or failed items from a previous finalize report")
    parser.add_argument("--section-id", action="append", dest="section_ids", help="Only finalize the specified section ID")
    parser.add_argument("--notebook-id", help="Existing notebook ID or alias")
    parser.add_argument("--output-dir", help="Course output directory")
    parser.add_argument("--report-path", help="Optional finalize report output path")
    parser.add_argument("--course-scene", help="Course scene override")
    parser.add_argument("--profile", help="NotebookLM profile")
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--download", action="store_true", help="Download completed slide decks to local PPTX files")
    parser.add_argument(
        "--skip-local-postprocess",
        action="store_true",
        help="When downloading, skip the local PPT post-processing defaults and save the raw deck directly.",
    )
    parser.add_argument(
        "--postprocess-image-path",
        default=_default_postprocess_image_path(),
        help="Local logo image used by the default PPT post-processing workflow.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _load_retry_filter(path: str | None) -> set[str]:
    if not path:
        return set()
    payload = read_json(Path(path).resolve())
    selected = set()
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        section_id = item.get("section_id")
        if not isinstance(section_id, str):
            continue
        if item.get("artifact_status") in {"running", "pending", "queued"} or item.get("error"):
            selected.add(section_id)
    return selected


def _watermark_output_path(raw_output_path: Path) -> Path:
    return raw_output_path.with_name(f"{raw_output_path.stem}_水印版{raw_output_path.suffix}")


def _markdown_output_path(slide_output_path: Path) -> Path:
    return slide_output_path.with_suffix(".md")


def _section_markdown_body(section: object) -> str:
    content = str(getattr(section, "content", "") or "").strip()
    if content:
        return content
    page_content = getattr(section, "page_content", None)
    if isinstance(page_content, list):
        parts = [str(item).strip() for item in page_content if str(item).strip()]
        if parts:
            return "\n\n".join(parts).strip()
    return ""


def _write_section_markdown(section: object, target_path: Path, *, dry_run: bool) -> bool:
    markdown_parts = []
    clean_title = str(getattr(section, "title", "") or "").strip()
    clean_content = _section_markdown_body(section)
    if clean_title:
        markdown_parts.append(f"# {clean_title}")
    if clean_content:
        if markdown_parts:
            markdown_parts.append("")
        markdown_parts.append(clean_content)
    markdown_text = "\n".join(markdown_parts).strip()
    if not markdown_text:
        return False
    markdown_text = f"{markdown_text}\n"
    if dry_run:
        return True
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(markdown_text, encoding="utf-8")
    return True


def _find_codex_node_modules() -> Path:
    env_value = os.environ.get("CODEX_NODE_MODULES") or os.environ.get("NODE_PATH")
    candidates: list[Path] = []
    if env_value:
        for item in env_value.split(os.pathsep):
            if item.strip():
                candidates.append(Path(item.strip()))

    home = Path.home()
    candidates.append(
        home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "node_modules"
    )
    runtime_root = home / ".cache" / "codex-runtimes"
    if runtime_root.exists():
        for child in runtime_root.iterdir():
            candidates.append(child / "dependencies" / "node" / "node_modules")

    for candidate in candidates:
        if (candidate / "@oai" / "artifact-tool" / "dist" / "artifact_tool.mjs").exists():
            return candidate
    raise RuntimeError("Could not locate a node_modules directory containing @oai/artifact-tool.")


def _run_local_postprocess(
    raw_output_path: Path,
    final_output_path: Path,
    *,
    image_path: str,
    dry_run: bool,
) -> dict[str, object]:
    script_path = Path(__file__).with_name("postprocess_downloaded_pptx.mjs")
    node_bin = shutil.which("node")
    if not node_bin:
        raise RuntimeError("Could not find `node` in PATH for local PPT post-processing.")

    command = [
        node_bin,
        str(script_path),
        "--input",
        str(raw_output_path),
        "--output",
        str(final_output_path),
        "--image-path",
        str(Path(image_path).resolve()),
    ]
    if dry_run:
        return {
            "postprocess_command": command,
            "output_path": str(final_output_path),
            "slide_count": None,
            "color_counts": {},
            "non_white_slides": [],
            "dry_run": True,
        }

    env = os.environ.copy()
    env["CODEX_NODE_MODULES"] = str(_find_codex_node_modules())
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
    stdout_text = completed.stdout.strip()
    json_candidates = [stdout_text] if stdout_text else []
    if "\n{" in stdout_text:
        json_candidates.append(stdout_text[stdout_text.rfind("\n{") + 1 :])
    if "{" in stdout_text:
        json_candidates.append(stdout_text[stdout_text.find("{") :])

    for candidate in json_candidates:
        try:
            payload = json.loads(candidate)
            if completed.returncode == 0 or final_output_path.exists():
                return payload
        except json.JSONDecodeError:
            continue

    if completed.returncode != 0:
        raise RuntimeError(
            "Local PPT post-processing failed.\n"
            f"Command: {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Local PPT post-processing did not return valid JSON.\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        ) from exc


def main() -> int:
    args = build_parser().parse_args()
    create_report = read_json(Path(args.create_report).resolve())
    manifest = load_course_manifest(
        args.manifest,
        course_scene=args.course_scene or create_report.get("course_scene"),
    )
    output_dir = Path(args.output_dir or sanitize_filename(manifest.course_title)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    notebook_id = args.notebook_id or str(create_report.get("notebook_id") or "").strip()
    if not notebook_id:
        raise SystemExit("Notebook ID is required via --notebook-id or create-report.json")

    requested_ids = set(args.section_ids or [])
    requested_ids.update(_load_retry_filter(args.retry_failed_from_report))
    ensure_authenticated(profile=args.profile, dry_run=args.dry_run)

    results = []
    failures = 0
    for item in create_report.get("results", []):
        if not isinstance(item, dict):
            continue
        section_id = item.get("section_id")
        if not isinstance(section_id, str):
            continue
        if requested_ids and section_id not in requested_ids:
            continue

        section = find_section(manifest, section_id)
        artifact_id = item.get("artifact_id")
        if item.get("status") != "create_requested" or not isinstance(artifact_id, str) or not artifact_id.strip():
            results.append(
                {
                    "section_id": section.id,
                    "artifact_id": artifact_id if isinstance(artifact_id, str) else None,
                    "artifact_status": "skipped",
                    "renamed": False,
                    "downloaded": False,
                    "output_path": None,
                    "markdown_exported": False,
                    "markdown_output_path": None,
                    "postprocess_result": None,
                    "error": item.get("error"),
                }
            )
            continue

        output_path = output_dir / section.output_name
        final_output_path = _watermark_output_path(output_path) if args.download and not args.skip_local_postprocess else output_path
        markdown_output_path = _markdown_output_path(final_output_path)
        renamed = False
        downloaded = False
        markdown_exported = False
        artifact_status = "running"
        error = None
        postprocess_result: dict[str, object] | None = None
        try:
            artifact = wait_for_artifact(
                notebook_id,
                artifact_id,
                profile=args.profile,
                dry_run=args.dry_run,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
            artifact_status = str(artifact.get("status") or "completed").lower()
            rename_artifact(artifact_id, output_path.stem, profile=args.profile, dry_run=args.dry_run)
            renamed = True
            if args.download:
                if args.skip_local_postprocess:
                    download_slide_deck(notebook_id, artifact_id, output_path, dry_run=args.dry_run)
                else:
                    with tempfile.TemporaryDirectory(prefix="nlm-course-slides-download-") as temp_dir:
                        raw_output_path = Path(temp_dir) / section.output_name
                        download_slide_deck(notebook_id, artifact_id, raw_output_path, dry_run=args.dry_run)
                        postprocess_result = _run_local_postprocess(
                            raw_output_path,
                            final_output_path,
                            image_path=args.postprocess_image_path,
                            dry_run=args.dry_run,
                        )
                markdown_exported = _write_section_markdown(section, markdown_output_path, dry_run=args.dry_run)
                downloaded = True
                artifact_status = "downloaded_local"
            else:
                artifact_status = "completed"
        except TimeoutError:
            artifact_status = "running"
        except Exception as exc:
            failures += 1
            error = str(exc)
            if renamed and not downloaded:
                artifact_status = "failed_download"
            elif "download" in str(exc).lower():
                artifact_status = "failed_download"
            else:
                artifact_status = "failed"

        results.append(
            {
                "section_id": section.id,
                "artifact_id": artifact_id,
                "artifact_status": artifact_status,
                "renamed": renamed,
                "downloaded": downloaded,
                "output_path": str(final_output_path) if downloaded else None,
                "markdown_exported": markdown_exported,
                "markdown_output_path": str(markdown_output_path) if downloaded else None,
                "postprocess_result": postprocess_result,
                "error": error,
            }
        )

    payload = {
        "manifest": manifest.source_path,
        "course_title": manifest.course_title,
        "course_scene": manifest.course_scene,
        "output_dir": str(output_dir),
        "notebook_id": notebook_id,
        "generated_at": utc_timestamp(),
        "poll_interval": args.poll_interval,
        "timeout_seconds": args.timeout_seconds,
        "download": args.download,
        "failures": failures,
        "results": results,
    }
    notebook_url = create_report.get("notebook_url")
    if notebook_url:
        payload["notebook_url"] = str(notebook_url)
    if "share_result" in create_report:
        payload["share_result"] = create_report.get("share_result")
    report_path = Path(args.report_path).resolve() if args.report_path else output_dir / "finalize-report.json"
    write_json(report_path, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
