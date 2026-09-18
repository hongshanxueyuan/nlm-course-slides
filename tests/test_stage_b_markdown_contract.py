from __future__ import annotations

import io
import json
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
import list_course_sections  # noqa: E402
import upload_course_sections  # noqa: E402


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PURE_HTML_MANIFEST = (
    WORKSPACE_ROOT
    / "test"
    / "_files_dir"
    / "course-v1_FIRAx+211579+20260624.markdown.json"
)
MIXED_IMAGESGALLERY_MANIFEST = Path(
    "D:/work/ai建课/20260814/企业流程智能化-LTC流程智能化/fira_course-v1_FIRAx_1040045_20260807.json"
)


class StageBMarkdownContractTest(unittest.TestCase):
    def _run_stage_b(self, manifest_path: Path) -> tuple[dict, Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        output_dir = Path(tempdir.name)
        report_path = output_dir / "section-list.json"
        argv = [
            "list_course_sections.py",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--report-path",
            str(report_path),
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            exit_code = list_course_sections.main()
        self.assertEqual(0, exit_code)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        return payload, output_dir

    def _assert_marker_alignment(self, md_body: str, expected_count: int) -> None:
        matches = list(common.PAGE_MARKER_RE.finditer(md_body))
        self.assertEqual(expected_count, len(matches))
        self.assertEqual(list(range(1, expected_count + 1)), [int(match.group(1)) for match in matches])

    def _find_section(self, payload: dict, section_title: str) -> dict:
        for section in payload["sections"]:
            if section["section_title"] == section_title:
                return section
        raise AssertionError(f"Could not find section '{section_title}' in payload")

    def test_stage_b_uses_imagesgallery_route_for_mixed_section(self) -> None:
        payload, snapshot_dir = self._run_stage_b(MIXED_IMAGESGALLERY_MANIFEST)
        section = self._find_section(
            payload,
            "3.4 销售机会阶段：从个人作战转向智能协同作战",
        )

        self.assertEqual("imagesgallery", section["block_type"])
        self.assertEqual(
            Path(section["output_name"]).stem,
            Path(section["md_name"]).stem,
        )
        self.assertEqual(section["page_count"], len(section["page_content"]))

        md_body = (snapshot_dir / section["md_name"]).read_text(encoding="utf-8")
        self.assertTrue(md_body.startswith("- 第 1 页\n"))
        self._assert_marker_alignment(md_body, section["page_count"])
        self.assertIn("# 销售机会阶段：从个人作战转向智能协同作战", md_body)
        self.assertNotIn("下面是围绕所学知识点的训战互动", md_body)
        self.assertNotIn(
            "# 3.4 销售机会阶段：从个人作战转向智能协同作战\n\n- 第 1 页",
            md_body,
        )

    def test_stage_b_uses_markdown_rendered_html_route(self) -> None:
        payload, snapshot_dir = self._run_stage_b(PURE_HTML_MANIFEST)
        section = self._find_section(
            payload,
            "1.2 博弈规则——【法国】合规宽严度与生存策略",
        )

        self.assertEqual("html", section["block_type"])
        self.assertEqual(section["page_count"], len(section["page_content"]))
        self.assertLessEqual(section["page_count"], 20)
        self.assertTrue(section["page_content"][0].startswith("## 博弈规则——【法国】合规宽严度与生存策略"))
        self.assertTrue(section["page_content"][-1].startswith("## 本节要点"))
        for page in section["page_content"]:
            self.assertIsNone(common.PAGE_MARKER_RE.search(page))
            self.assertNotIn("<style", page)
            self.assertNotIn("<p>", page)
            self.assertNotIn("<h2>", page)

        md_body = (snapshot_dir / section["md_name"]).read_text(encoding="utf-8")
        self.assertTrue(md_body.startswith("- 第 1 页\n"))
        self._assert_marker_alignment(md_body, section["page_count"])
        self.assertNotIn("<style", md_body)
        self.assertNotIn("<p>", md_body)
        self.assertNotIn("<h2>", md_body)

    def test_stage_b_rejects_mismatched_explicit_md_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 错误示例",
                                "output_name": "1.1 错误示例.pptx",
                                "md_name": "别的名字.md",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                common.load_course_manifest(manifest_path)

    def test_stage_b_rejects_raw_html_in_html_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 原始 HTML",
                                "content": "<h2>标题</h2><p>正文</p>",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            manifest = common.load_course_manifest(manifest_path)
            with self.assertRaisesRegex(RuntimeError, "raw HTML tags"):
                common.materialize_markdown_artifacts(manifest, output_dir=root / "out")

    def test_stage_b_respects_explicit_json_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 显式元数据",
                                "output_name": "1.1 显式元数据.pptx",
                                "md_name": "1.1 显式元数据.md",
                                "block_type": "html",
                                "page_content": [
                                    "## 第一页\n\n第一页正文。",
                                    "## 第二页\n\n第二页正文。",
                                ],
                                "page_count": 2,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload, output_dir = self._run_stage_b(manifest_path)
            section = payload["sections"][0]

            self.assertEqual("1.1 显式元数据.md", section["md_name"])
            self.assertEqual("html", section["block_type"])
            self.assertEqual(
                ["## 第一页\n\n第一页正文。", "## 第二页\n\n第二页正文。"],
                section["page_content"],
            )
            self.assertEqual(2, section["page_count"])

            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")
            self.assertEqual(
                "- 第 1 页\n\n## 第一页\n\n第一页正文。\n\n- 第 2 页\n\n## 第二页\n\n第二页正文。\n",
                md_body,
            )

    def test_stage_b_derives_html_metadata_for_markdown_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "course.md"
            manifest_path.write_text(
                "# 测试课程\n\n## Markdown 小节\n\n这是正文。\n\n- 要点一\n",
                encoding="utf-8",
            )

            payload, output_dir = self._run_stage_b(manifest_path)
            section = payload["sections"][0]

            self.assertEqual("html", section["block_type"])
            self.assertEqual(
                Path(section["output_name"]).stem,
                Path(section["md_name"]).stem,
            )
            self.assertEqual(section["page_count"], len(section["page_content"]))
            self.assertGreaterEqual(section["page_count"], 1)

            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")
            self.assertTrue(md_body.startswith("- 第 1 页\n"))
            self.assertIn("这是正文。", md_body)

    def test_parse_canonical_markdown_requires_sequential_page_markers(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must be sequential"):
            common.parse_canonical_markdown_pages(
                "- 第 1 页\n\n第一页\n\n- 第 3 页\n\n第三页\n",
                section_title="1.1 测试小节",
                output_stem="1.1 测试小节",
            )

    def test_html_pagination_uses_rules_prompt_for_model_path(self) -> None:
        markdown = "## 标题\n\n第一页正文。"

        def fake_model(prompt: str) -> list[str]:
            self.assertIn("## 任务描述", prompt)
            self.assertIn("不要根据文件名、`output_name`、`md_name` 或 section 标题再额外生成一个包裹性的文档总标题。", prompt)
            self.assertIn("最终总页数**不得超过 20 页**", prompt)
            self.assertIn(markdown, prompt)
            self.assertNotIn("{text}", prompt)
            return ["## 标题\n\n第一页正文。"]

        with patch("common._run_html_pagination_model", side_effect=fake_model):
            pages = common.paginate_markdown_content(markdown)

        self.assertEqual(["## 标题\n\n第一页正文。"], pages)


class StageCMarkdownUploadTest(unittest.TestCase):
    def _build_simple_stage_b_outputs(self) -> tuple[Path, Path, Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
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
                            "content": "## 原始标题\n\n原始内容。",
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
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            exit_code = list_course_sections.main()
        self.assertEqual(0, exit_code)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        section = payload["sections"][0]
        md_path = output_dir / section["md_name"]
        return manifest_path, report_path, md_path

    def _build_fixture_stage_b_outputs(
        self,
        manifest_path: Path,
        section_title: str,
    ) -> tuple[Path, Path, Path, dict]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        output_dir = Path(tempdir.name)
        report_path = output_dir / "section-list.json"
        argv = [
            "list_course_sections.py",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--report-path",
            str(report_path),
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            exit_code = list_course_sections.main()
        self.assertEqual(0, exit_code)

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        for section in payload["sections"]:
            if section["section_title"] == section_title:
                single_section_report_path = output_dir / "single-section-list.json"
                single_section_report_path.write_text(
                    json.dumps(
                        {
                            "manifest": payload["manifest"],
                            "course_title": payload["course_title"],
                            "generated_at": payload["generated_at"],
                            "sections": [section],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                return manifest_path, single_section_report_path, output_dir / section["md_name"], section
        raise AssertionError(f"Could not find section '{section_title}' in payload")

    def _assert_fixture_stage_c_uploads_canonical_markdown(
        self,
        manifest_path: Path,
        section_title: str,
        expected_block_type: str,
    ) -> None:
        manifest_path, report_path, md_path, section = self._build_fixture_stage_b_outputs(
            manifest_path,
            section_title,
        )
        expected_body = md_path.read_text(encoding="utf-8")
        captured: dict[str, str] = {}

        def fake_add_text_source(_notebook_id, title, text, **_kwargs):
            captured["title"] = title
            captured["text"] = text
            return "source-1"

        argv = [
            "upload_course_sections.py",
            str(manifest_path),
            "--section-list",
            str(report_path),
            "--output-dir",
            str(md_path.parent),
            "--report-path",
            str(md_path.parent / "upload-report.json"),
            "--notebook-id",
            "notebook-1",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("upload_course_sections.ensure_authenticated"),
            patch("upload_course_sections.add_text_source", side_effect=fake_add_text_source),
            patch("upload_course_sections.time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = upload_course_sections.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(expected_body, captured["text"])
        report = json.loads((md_path.parent / "upload-report.json").read_text(encoding="utf-8"))
        self.assertEqual("uploaded", report["results"][0]["status"])
        self.assertEqual(md_path.name, report["results"][0]["md_name"])
        self.assertEqual(expected_block_type, report["results"][0]["block_type"])
        self.assertEqual(section["resource_title"], report["results"][0]["resource_title"])

    def test_stage_c_uploads_canonical_markdown_body(self) -> None:
        manifest_path, report_path, md_path = self._build_simple_stage_b_outputs()
        sentinel_body = "- 第 1 页\n\n## 替换后的正文\n\n这是 stage C 应该上传的文件正文。\n"
        md_path.write_text(sentinel_body, encoding="utf-8")

        captured: dict[str, str] = {}

        def fake_add_text_source(_notebook_id, title, text, **_kwargs):
            captured["title"] = title
            captured["text"] = text
            return "source-1"

        argv = [
            "upload_course_sections.py",
            str(manifest_path),
            "--section-list",
            str(report_path),
            "--output-dir",
            str(md_path.parent),
            "--report-path",
            str(md_path.parent / "upload-report.json"),
            "--notebook-id",
            "notebook-1",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("upload_course_sections.ensure_authenticated"),
            patch("upload_course_sections.add_text_source", side_effect=fake_add_text_source),
            patch("upload_course_sections.time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = upload_course_sections.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(sentinel_body, captured["text"])
        report = json.loads((md_path.parent / "upload-report.json").read_text(encoding="utf-8"))
        self.assertEqual("uploaded", report["results"][0]["status"])
        self.assertEqual(md_path.name, report["results"][0]["md_name"])

    def test_stage_c_uploads_canonical_markdown_for_html_fixture(self) -> None:
        self._assert_fixture_stage_c_uploads_canonical_markdown(
            PURE_HTML_MANIFEST,
            "1.2 博弈规则——【法国】合规宽严度与生存策略",
            "html",
        )

    def test_stage_c_uploads_canonical_markdown_for_imagesgallery_fixture(self) -> None:
        self._assert_fixture_stage_c_uploads_canonical_markdown(
            MIXED_IMAGESGALLERY_MANIFEST,
            "3.4 销售机会阶段：从个人作战转向智能协同作战",
            "imagesgallery",
        )

    def test_stage_c_fails_fast_when_canonical_markdown_is_missing(self) -> None:
        manifest_path, report_path, md_path = self._build_simple_stage_b_outputs()
        md_path.unlink()

        argv = [
            "upload_course_sections.py",
            str(manifest_path),
            "--section-list",
            str(report_path),
            "--output-dir",
            str(md_path.parent),
            "--report-path",
            str(md_path.parent / "upload-report.json"),
            "--notebook-id",
            "notebook-1",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("upload_course_sections.ensure_authenticated"),
            patch("upload_course_sections.add_text_source") as add_text_source_mock,
            patch("upload_course_sections.time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = upload_course_sections.main()

        self.assertEqual(1, exit_code)
        add_text_source_mock.assert_not_called()
        report = json.loads((md_path.parent / "upload-report.json").read_text(encoding="utf-8"))
        self.assertEqual("failed_upload", report["results"][0]["status"])
        self.assertIn("Canonical Markdown is missing", report["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
