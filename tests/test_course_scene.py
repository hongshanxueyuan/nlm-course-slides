from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import common  # noqa: E402
import create_slides_from_sources  # noqa: E402
import generate_course_slides  # noqa: E402
import generate_recovery_report  # noqa: E402
import generate_section_slides  # noqa: E402
import list_course_sections  # noqa: E402


class CourseSceneManifestTest(unittest.TestCase):
    def test_load_course_manifest_defaults_course_scene_to_standard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 测试小节",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            manifest = common.load_course_manifest(manifest_path)

        self.assertEqual("企业流程与智能化场景", manifest.course_scene)

    def test_build_focus_prompt_supports_course_scene_templates(self) -> None:
        standard_prompt = common.build_focus_prompt("1.1 测试小节", course_scene="企业流程与智能化场景")
        overseas_prompt = common.build_focus_prompt("1.1 测试小节", course_scene="企业出海场景")

        self.assertIn("流程与智能化中的企业领导者", standard_prompt)
        self.assertIn("请严格按来源文档中的分页标识逐页生成对应的演示文稿", standard_prompt)
        self.assertNotIn("来源文档说明：", standard_prompt)
        self.assertIn("中国出海企业员工", overseas_prompt)
        self.assertIn("【核心分页与结构约束】：", overseas_prompt)

    def test_normalize_course_scene_supports_legacy_aliases(self) -> None:
        self.assertEqual("企业流程与智能化场景", common.normalize_course_scene("标准"))
        self.assertEqual("企业流程与智能化场景", common.normalize_course_scene("企业流程与智能化"))
        self.assertEqual("企业出海场景", common.normalize_course_scene("出海"))
        self.assertEqual("企业出海场景", common.normalize_course_scene("企业出海"))


class CourseSceneWorkflowTest(unittest.TestCase):
    def test_recovery_report_reuses_course_scene_from_prior_stage_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            output_dir = root / "out"
            upload_report_path = output_dir / "upload-report.json"
            recovery_report_path = output_dir / "recovery-report.json"
            output_dir.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 测试小节",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            upload_report_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "course_scene": "企业出海场景",
                        "results": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            captured: dict[str, str] = {}

            def fake_match_sections(manifest, **_kwargs):
                captured["course_scene"] = manifest.course_scene
                return []

            argv = [
                "generate_recovery_report.py",
                str(manifest_path),
                "--notebook-id",
                "notebook-1",
                "--output-dir",
                str(output_dir),
                "--report-path",
                str(recovery_report_path),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("generate_recovery_report.ensure_authenticated"),
                patch(
                    "generate_recovery_report.match_sections_to_notebook_state",
                    side_effect=fake_match_sections,
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = generate_recovery_report.main()

            self.assertEqual(0, exit_code)
            payload = json.loads(recovery_report_path.read_text(encoding="utf-8"))

        self.assertEqual("企业出海场景", captured["course_scene"])
        self.assertEqual("企业出海场景", payload["course_scene"])

    def test_stage_b_records_course_scene_from_cli_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            output_dir = root / "out"
            report_path = output_dir / "section-list.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 测试小节",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            argv = [
                "list_course_sections.py",
                str(manifest_path),
                "--output-dir",
                str(output_dir),
                "--report-path",
                str(report_path),
                "--course-scene",
                "企业出海场景",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                exit_code = list_course_sections.main()

            self.assertEqual(0, exit_code)
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual("企业出海场景", payload["course_scene"])

    def test_stage_d_uses_course_scene_from_upload_report_when_focus_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            upload_report_path = root / "upload-report.json"
            report_path = root / "create-report.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 测试小节",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            upload_report_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "notebook_id": "notebook-1",
                        "course_scene": "企业出海场景",
                        "results": [
                            {
                                "section_id": "1",
                                "status": "uploaded",
                                "source_id": "source-1",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            captured: dict[str, str] = {}

            def fake_create_slide_deck(_notebook_id, *, focus, **_kwargs):
                captured["focus"] = focus
                return "artifact-1"

            argv = [
                "create_slides_from_sources.py",
                str(manifest_path),
                "--upload-report",
                str(upload_report_path),
                "--report-path",
                str(report_path),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("create_slides_from_sources.ensure_authenticated"),
                patch("create_slides_from_sources.create_slide_deck", side_effect=fake_create_slide_deck),
                patch("create_slides_from_sources.time.sleep"),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = create_slides_from_sources.main()

            self.assertEqual(0, exit_code)
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual("企业出海场景", payload["course_scene"])
        self.assertIn("中国出海企业员工", captured["focus"])

    def test_resume_report_propagates_course_scene_to_section_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            output_dir = root / "out"
            resume_report_path = root / "recovery-report.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 测试小节",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            resume_report_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "notebook_id": "notebook-1",
                        "course_scene": "企业出海场景",
                        "results": [
                            {
                                "section_id": "1",
                                "status": "source_only",
                                "resume_action": "create_from_existing_source",
                                "source_id": "source-1",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            captured: dict[str, list[str]] = {}

            def fake_run_section(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                captured["cmd"] = cmd
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "status": "create_requested",
                            "section_id": "1",
                            "section_title": "1.1 测试小节",
                            "artifact_id": "artifact-1",
                        },
                        ensure_ascii=False,
                    ),
                    stderr="",
                )

            argv = [
                "generate_course_slides.py",
                str(manifest_path),
                "--resume-from-report",
                str(resume_report_path),
                "--output-dir",
                str(output_dir),
                "--max-concurrency",
                "1",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("generate_course_slides.ensure_authenticated"),
                patch("generate_course_slides.run_section", side_effect=fake_run_section),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = generate_course_slides.main()

            self.assertEqual(0, exit_code)
            payload = json.loads((output_dir / "slide-generation-report.json").read_text(encoding="utf-8"))

        self.assertEqual("企业出海场景", payload["course_scene"])
        self.assertIn("--course-scene", captured["cmd"])
        self.assertEqual(
            "企业出海场景",
            captured["cmd"][captured["cmd"].index("--course-scene") + 1],
        )

    def test_resume_report_without_course_scene_defaults_to_standard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            output_dir = root / "out"
            resume_report_path = root / "legacy-recovery-report.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 测试小节",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            resume_report_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "notebook_id": "notebook-1",
                        "results": [
                            {
                                "section_id": "1",
                                "status": "source_only",
                                "resume_action": "create_from_existing_source",
                                "source_id": "source-1",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            captured: dict[str, list[str]] = {}

            def fake_run_section(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                captured["cmd"] = cmd
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "status": "create_requested",
                            "section_id": "1",
                            "section_title": "1.1 测试小节",
                            "artifact_id": "artifact-1",
                        },
                        ensure_ascii=False,
                    ),
                    stderr="",
                )

            argv = [
                "generate_course_slides.py",
                str(manifest_path),
                "--resume-from-report",
                str(resume_report_path),
                "--output-dir",
                str(output_dir),
                "--max-concurrency",
                "1",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("generate_course_slides.ensure_authenticated"),
                patch("generate_course_slides.run_section", side_effect=fake_run_section),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = generate_course_slides.main()

            self.assertEqual(0, exit_code)
            payload = json.loads((output_dir / "slide-generation-report.json").read_text(encoding="utf-8"))

        self.assertEqual("企业流程与智能化场景", payload["course_scene"])
        self.assertIn("--course-scene", captured["cmd"])
        self.assertEqual(
            "企业流程与智能化场景",
            captured["cmd"][captured["cmd"].index("--course-scene") + 1],
        )

    def test_single_section_explicit_focus_overrides_course_scene(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            argv = [
                "generate_section_slides.py",
                "--notebook-id",
                "notebook-1",
                "--output-dir",
                str(output_dir),
                "--title",
                "1.1 测试小节",
                "--content",
                "## 标题\n\n正文",
                "--course-scene",
                "企业出海场景",
                "--focus",
                "这是显式 focus",
            ]

            captured: dict[str, str] = {}

            def fake_add_text_source(_notebook_id, _title, _text, **_kwargs):
                return "source-1"

            def fake_create_slide_deck(_notebook_id, *, focus, **_kwargs):
                captured["focus"] = focus
                return "artifact-1"

            with (
                patch.object(sys, "argv", argv),
                patch("generate_section_slides.ensure_authenticated"),
                patch("generate_section_slides.add_text_source", side_effect=fake_add_text_source),
                patch("generate_section_slides.create_slide_deck", side_effect=fake_create_slide_deck),
                patch("generate_section_slides.wait_for_artifact"),
                patch("generate_section_slides.rename_artifact"),
                patch("generate_section_slides.download_slide_deck"),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = generate_section_slides.main()

        self.assertEqual(0, exit_code)
        self.assertEqual("这是显式 focus", captured["focus"])


if __name__ == "__main__":
    unittest.main()
