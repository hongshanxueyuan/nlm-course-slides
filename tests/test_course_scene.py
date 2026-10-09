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
        self.assertIn("第 1 页固定为封面页", standard_prompt)
        self.assertNotIn("来源文档说明：", standard_prompt)
        self.assertIn("中国出海企业员工", overseas_prompt)
        self.assertIn("【核心分页与结构约束】：", overseas_prompt)
        self.assertNotIn("MARKDOWN_PAGINATION_PROMPT", standard_prompt)
        self.assertNotIn("## 任务描述", standard_prompt)

    def test_scene_prompts_are_separate_from_html_pagination_rules(self) -> None:
        pagination_rules = common.load_html_pagination_rules()
        self.assertIn("正文分页结果**不得超过 19 页**", pagination_rules)
        self.assertIn("不生成开头页、结尾页、文件名页或额外封面页，避免冗余内容。", pagination_rules)
        for scene in (common.COURSE_SCENE_STANDARD, common.COURSE_SCENE_OVERSEAS):
            with self.subTest(scene=scene):
                prompt = common.FOCUS_PROMPT_TEMPLATES[scene]
                self.assertNotIn("## 任务描述", prompt)
                self.assertNotIn("正文分页结果**不得超过 19 页**", prompt)
                self.assertNotIn("MARKDOWN_PAGINATION_PROMPT", prompt)

    def test_scene_prompts_define_cover_without_disabling_other_page_constraints(self) -> None:
        for scene in (common.COURSE_SCENE_STANDARD, common.COURSE_SCENE_OVERSEAS):
            with self.subTest(scene=scene):
                prompt = common.FOCUS_PROMPT_TEMPLATES[scene]
                self.assertNotIn("严禁生成封面页", prompt)
                self.assertNotIn("取消封面页", prompt)
                self.assertNotIn("阶段 B", prompt)
                self.assertNotIn("canonical Markdown", prompt)
                self.assertNotIn("文稿第 1 页必须直接从来源文档第 1 个分页标识", prompt)
                self.assertIn(
                    "严禁将来源文档中两个或多个分页标识对应的内容合并为一页 PPT",
                    prompt,
                )
                self.assertIn("正文从第 2 页开始", prompt)
                for heading in (
                    "【生成要求】：",
                    "【核心分页与结构约束】：",
                    "【封面页要求】：",
                    "【内容与专业表达要求】：",
                    "【正文要求】：",
                    "【有声幻灯片要求】：",
                    "【视觉效果要求】：",
                    "【底版强制要求】：",
                    "【配图与图标要求】：",
                ):
                    self.assertIn(f"{heading}\n", prompt)
                self.assertIn(
                    "【封面页要求】：\n"
                    "- 第 1 页固定为封面页，只保留主标题，不要副标题，字体样式为黑体。\n"
                    "- 封面页不要出现“目标受众”“适合谁”“面向谁”等对象描述。\n"
                    "- 封面页不要出现课程简介、课程背景、价值说明、协同说明等补充文字。\n"
                    "- 封面页需要有与主题相关的现代企业扁平线性矢量插图或主视觉，与标题关系协调，美观。",
                    prompt,
                )
                self.assertIn("不得生成结尾页、致谢页、问答页", prompt)
                self.assertIn("严禁额外生成来源文档里没有的目录页与转折过渡页", prompt)
                self.assertIn("1:1 逐页强对应法则", prompt)
                self.assertIn("配音只朗读主标题，并沿用普通页面的音频处理", prompt)

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
