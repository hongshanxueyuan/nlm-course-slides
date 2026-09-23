#!/usr/bin/env python3
"""
Generate or resume slide decks for every section in a course manifest.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import (
    configure_nlm_api_delay,
    create_notebook,
    ensure_authenticated,
    load_course_manifest,
    log_message,
    read_json,
    sanitize_filename,
    write_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Course manifest (.json/.yaml/.md)")
    parser.add_argument("--resume-from-report", help="Recovery report JSON created by generate_recovery_report.py")
    parser.add_argument("--notebook-id", help="Existing notebook ID or alias")
    parser.add_argument("--notebook-title", help="Notebook title when creating a new notebook")
    parser.add_argument("--output-dir", help="Course output directory")
    parser.add_argument("--course-scene", help="Course scene override")
    parser.add_argument("--profile", help="NotebookLM profile")
    parser.add_argument("--language", default="zh_Hans")
    parser.add_argument("--deck-format", default="detailed_deck")
    parser.add_argument("--length", default="default")
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--source-wait-timeout", type=float, default=600.0)
    parser.add_argument("--api-delay-seconds", type=float, default=15.0)
    parser.add_argument("--max-concurrency", type=int, default=10)
    parser.add_argument("--max-sections", type=int, help="Process at most this many sections after manifest filtering")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def parse_json_from_stdout(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if candidate.startswith("{"):
            return json.loads(candidate)
    raise RuntimeError(f"Could not find JSON payload in output:\n{stdout}")


def run_section(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=None,
        check=False,
    )


def process_completed_section(
    completed: subprocess.CompletedProcess[str],
    *,
    section_id: str,
    section_title: str,
) -> tuple[dict[str, object], bool]:
    stdout = completed.stdout.strip()
    if stdout:
        parsed = parse_json_from_stdout(stdout)
        status = parsed.get("status")
        return parsed, status == "failed" or completed.returncode != 0
    return (
        {
            "status": "failed",
            "section_id": section_id,
            "section_title": section_title,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        True,
    )


def build_normal_section_command(
    args: argparse.Namespace,
    *,
    section_id: str,
    notebook_id: str,
    course_dir: Path,
    section_script: Path,
) -> list[str]:
    cmd = [
        sys.executable,
        str(section_script),
        "--manifest",
        str(Path(args.manifest).resolve()),
        "--section-id",
        section_id,
        "--notebook-id",
        notebook_id,
        "--output-dir",
        str(course_dir),
        "--language",
        args.language,
        "--deck-format",
        args.deck_format,
        "--length",
        args.length,
        "--poll-interval",
        str(args.poll_interval),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--source-wait-timeout",
        str(args.source_wait_timeout),
        "--api-delay-seconds",
        str(args.api_delay_seconds),
    ]
    if args.course_scene:
        cmd.extend(["--course-scene", args.course_scene])
    if args.profile:
        cmd.extend(["--profile", args.profile])
    cmd.append("--skip-auth-check")
    if args.skip_existing:
        cmd.append("--skip-existing")
    if args.prepare_only:
        cmd.append("--prepare-only")
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def build_resume_section_command(
    args: argparse.Namespace,
    *,
    section: Any,
    state: dict[str, Any],
    notebook_id: str,
    course_dir: Path,
    section_script: Path,
) -> list[str] | None:
    action = state.get("resume_action")
    if action in {None, "skip", "manual_review"}:
        return None
    cmd = build_normal_section_command(
        args,
        section_id=section.id,
        notebook_id=notebook_id,
        course_dir=course_dir,
        section_script=section_script,
    )
    if action == "create_from_existing_source" and state.get("source_id"):
        cmd.extend(["--source-id", str(state["source_id"])])
        return cmd
    if action == "wait_and_finalize" and state.get("artifact_id"):
        cmd.extend(["--artifact-id", str(state["artifact_id"])])
        return cmd
    if action == "download_only" and state.get("artifact_id"):
        cmd.extend(["--artifact-id", str(state["artifact_id"]), "--download-only"])
        return cmd
    if action == "upload_and_create":
        return cmd
    return None


def load_resume_states(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    payload = read_json(Path(path).resolve())
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError(f"Invalid recovery report: {path}")
    states: dict[str, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        section_id = item.get("section_id")
        if isinstance(section_id, str):
            states[section_id] = item
    return states


def build_skip_result(section: Any, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "skipped",
        "section_id": section.id,
        "section_title": section.title,
        "resume_action": state.get("resume_action"),
        "resume_status": state.get("status"),
        "source_id": state.get("source_id"),
        "artifact_id": state.get("artifact_id"),
        "notes": state.get("notes", []),
    }


def main() -> int:
    args = build_parser().parse_args()
    if args.max_concurrency < 1:
        raise SystemExit("--max-concurrency must be at least 1")
    if args.api_delay_seconds < 0:
        raise SystemExit("--api-delay-seconds must be at least 0")
    if args.max_sections is not None and args.max_sections < 1:
        raise SystemExit("--max-sections must be at least 1")

    configure_nlm_api_delay(args.api_delay_seconds)
    resume_states = load_resume_states(args.resume_from_report)
    resume_report_payload = read_json(Path(args.resume_from_report).resolve()) if args.resume_from_report else None
    resume_report_notebook_id = None
    resolved_course_scene = args.course_scene
    if isinstance(resume_report_payload, dict):
        raw_notebook_id = resume_report_payload.get("notebook_id")
        if isinstance(raw_notebook_id, str) and raw_notebook_id.strip():
            resume_report_notebook_id = raw_notebook_id.strip()
        raw_course_scene = resume_report_payload.get("course_scene")
        if not resolved_course_scene and isinstance(raw_course_scene, str) and raw_course_scene.strip():
            resolved_course_scene = raw_course_scene.strip()

    manifest = load_course_manifest(args.manifest, course_scene=resolved_course_scene)
    if args.max_sections is not None:
        manifest.sections = manifest.sections[: args.max_sections]
    if not manifest.sections:
        raise SystemExit("No sections selected for processing")

    args.course_scene = manifest.course_scene
    course_dir = Path(args.output_dir or sanitize_filename(manifest.course_title)).resolve()
    course_dir.mkdir(parents=True, exist_ok=True)
    log_message(f"[course] loaded manifest: {manifest.source_path}")
    log_message(f"[course] sections to process: {len(manifest.sections)}")
    log_message(
        f"[course] output directory: {course_dir} | max_concurrency={args.max_concurrency} | "
        f"max_sections={args.max_sections or 'all'} | prepare_only={args.prepare_only} | "
        f"api_delay_seconds={args.api_delay_seconds} | resume_from_report={bool(args.resume_from_report)} | "
        f"course_scene={manifest.course_scene}"
    )

    ensure_authenticated(profile=args.profile, dry_run=args.dry_run)
    log_message("[course] authentication check complete")
    notebook_id = args.notebook_id or resume_report_notebook_id
    notebook_id = notebook_id or create_notebook(
        args.notebook_title or manifest.course_title,
        profile=args.profile,
        dry_run=args.dry_run,
    )
    if args.notebook_id or args.resume_from_report:
        log_message(f"[course] reuse notebook: {notebook_id}")
    else:
        log_message(f"[course] created notebook: {notebook_id}")

    section_script = Path(__file__).with_name("generate_section_slides.py")
    results_by_index: dict[int, dict[str, object]] = {}
    failures = 0
    stop_submitting = False

    tasks: list[tuple[int, Any, list[str] | None, dict[str, Any] | None]] = []
    for index, section in enumerate(manifest.sections):
        state = resume_states.get(section.id)
        if state:
            cmd = build_resume_section_command(
                args,
                section=section,
                state=state,
                notebook_id=notebook_id,
                course_dir=course_dir,
                section_script=section_script,
            )
            tasks.append((index, section, cmd, state))
        else:
            cmd = build_normal_section_command(
                args,
                section_id=section.id,
                notebook_id=notebook_id,
                course_dir=course_dir,
                section_script=section_script,
            )
            tasks.append((index, section, cmd, None))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_concurrency) as executor:
        in_flight: dict[concurrent.futures.Future[subprocess.CompletedProcess[str]], tuple[int, Any]] = {}
        next_index = 0

        def submit_until_full() -> None:
            nonlocal next_index
            while not stop_submitting and next_index < len(tasks) and len(in_flight) < args.max_concurrency:
                index, section, cmd, state = tasks[next_index]
                if cmd is None:
                    results_by_index[index] = build_skip_result(section, state or {})
                    log_message(
                        f"[course] skip section {section.id} | action={state.get('resume_action') if state else 'skip'}"
                    )
                    next_index += 1
                    continue
                future = executor.submit(run_section, cmd)
                in_flight[future] = (index, section)
                log_message(
                    f"[course] submitted section {section.id} ({section.title}) | in_flight={len(in_flight)}"
                )
                next_index += 1

        submit_until_full()
        while in_flight:
            done, _ = concurrent.futures.wait(in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                index, section = in_flight.pop(future)
                result, failed = process_completed_section(
                    future.result(),
                    section_id=section.id,
                    section_title=section.title,
                )
                results_by_index[index] = result
                status = result.get("status", "unknown")
                log_message(
                    f"[course] finished section {section.id} with status={status} | completed={len(results_by_index)}/{len(manifest.sections)}"
                )
                if failed:
                    failures += 1
                    log_message(f"[course] section {section.id} failed")
                    if not args.continue_on_error:
                        stop_submitting = True
                        log_message("[course] stop submitting new sections after failure")
            submit_until_full()

    if stop_submitting and not args.continue_on_error:
        for index, section in enumerate(manifest.sections):
            if index in results_by_index:
                continue
            results_by_index[index] = {
                "status": "not-started",
                "section_id": section.id,
                "section_title": section.title,
                "reason": "stopped-after-earlier-failure",
            }

    results = [results_by_index[index] for index in sorted(results_by_index)]
    report = {
        "manifest": manifest.source_path,
        "course_title": manifest.course_title,
        "course_scene": manifest.course_scene,
        "output_dir": str(course_dir),
        "notebook_id": notebook_id,
        "max_concurrency": args.max_concurrency,
        "api_delay_seconds": args.api_delay_seconds,
        "max_sections": args.max_sections,
        "prepare_only": args.prepare_only,
        "resume_from_report": str(Path(args.resume_from_report).resolve()) if args.resume_from_report else None,
        "total_sections": len(manifest.sections),
        "failures": failures,
        "results": results,
    }
    report_path = course_dir / "slide-generation-report.json"
    if not args.dry_run:
        write_json(report_path, report)
        log_message(f"[course] wrote batch report: {report_path}")
    log_message(f"[course] run complete | failures={failures}")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
