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
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import common  # noqa: E402
import list_course_sections  # noqa: E402
import upload_course_sections  # noqa: E402
from fake_codex import run_stage_b_command, plugin_output, _write_fake_codex_home  # noqa: E402


MIXED_IMAGESGALLERY_MANIFEST = Path(
    "D:/work/ai建课/20260814/企业流程智能化-LTC流程智能化/fira_course-v1_FIRAx_1040045_20260807.json"
)


def create_pure_html_manifest(test_case: unittest.TestCase) -> Path:
    tempdir = tempfile.TemporaryDirectory()
    test_case.addCleanup(tempdir.cleanup)
    manifest_path = Path(tempdir.name) / "pure-html-course.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "测试课程",
                "chapters": [
                    {
                        "name": "第一章",
                        "sections": [
                            {"name": "第一章"},
                            {
                                "name": "1.2 博弈规则——【法国】合规宽严度与生存策略",
                                "verticals": [
                                    {
                                        "name": "正文",
                                        "blocks": [
                                            {
                                                "category": "html",
                                                "name": "文字讲解",
                                                "text": (
                                                    "<h2>博弈规则——【法国】合规宽严度与生存策略</h2>"
                                                    "<p>法国合规要求与企业经营策略。</p>"
                                                    "<h2>风险边界</h2><p>识别经营中的风险边界。</p>"
                                                    "<h2>本节要点</h2><p>回顾本节的关键内容。</p>"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest_path


class StageBMarkdownContractTest(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        fake_home = _write_fake_codex_home(Path(tempdir.name))
        env_patch = patch.dict("os.environ", {"CODEX_HOME": str(fake_home), "AZURE_OPENAI_API_KEY": "fake-key"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run_stage_b(self, manifest_path: Path) -> tuple[dict, Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        output_dir = Path(tempdir.name)
        report_path = output_dir / "section-list.json"
        exit_code = run_stage_b_command(manifest_path, output_dir, report_path)
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

    @unittest.skipUnless(
        MIXED_IMAGESGALLERY_MANIFEST.is_file(),
        "external gallery fixture is unavailable",
    )
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
        payload, snapshot_dir = self._run_stage_b(create_pure_html_manifest(self))
        section = self._find_section(
            payload,
            "1.2 博弈规则——【法国】合规宽严度与生存策略",
        )

        self.assertEqual("html", section["block_type"])
        self.assertEqual(section["page_count"], len(section["page_content"]))
        self.assertLessEqual(section["page_count"], 20)
        self.assertTrue(section["page_content"][0].startswith("# 博弈规则——【法国】合规宽严度与生存策略"))
        self.assertTrue(
            any(page.splitlines()[0].lstrip("# ") == "本节要点" for page in section["page_content"])
        )
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

    def test_stage_b_preserves_teaching_text_key_points_in_all_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "course_scene": common.COURSE_SCENE_STANDARD,
                        "sections": [
                            {
                                "id": "1.1",
                                "title": "1.1 示例章节",
                                "block_type": "html",
                                "content": "## 正文\n\n原始正文。\n\n## 本节要点\n\n源摘要仍保留。",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original_manifest = manifest_path.read_bytes()

            def fake_model(request: common.HtmlPaginationRequest) -> dict:
                prompt = request.prompt
                self.assertIn("源摘要仍保留", prompt)
                return plugin_output([
                    "## 正文\n\n原始正文。",
                    "## 本节要点\n\n源摘要仍保留。",
                ])

            with patch("common._run_html_pagination_model", side_effect=fake_model):
                payload, output_dir = self._run_stage_b(manifest_path)

            section = payload["sections"][0]
            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")

            self.assertEqual(
                ["# 示例章节", "## 正文\n\n原始正文。", "## 本节要点\n\n源摘要仍保留。"],
                section["page_content"],
            )
            self.assertEqual(3, section["page_count"])
            self.assertEqual(section["page_count"], len(section["page_content"]))
            self._assert_marker_alignment(md_body, section["page_count"])
            self.assertEqual(
                section["page_content"],
                common.parse_canonical_markdown_pages(
                    md_body,
                    section_title=section["section_title"],
                    output_stem=Path(section["md_name"]).stem,
                ),
            )
            self.assertIn("本节要点", md_body)
            self.assertIn("源摘要仍保留", md_body)
            self.assertEqual(original_manifest, manifest_path.read_bytes())
            self.assertIn("源摘要仍保留", manifest_path.read_text(encoding="utf-8"))

    def test_stage_b_preserves_other_html_tails_and_inline_key_points_mentions(self) -> None:
        cases = [
            (["## 正文\n\n正文。", "## 总结\n\n总结内容。"], "## 总结"),
            (["## 正文\n\n正文。", "## 复盘\n\n复盘内容。"], "## 复盘"),
            (["## 正文\n\n正文提到本节要点，但这是普通正文。"], "正文提到本节要点"),
        ]
        for pages, expected_tail in cases:
            with self.subTest(expected_tail=expected_tail), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                manifest_path = root / "manifest.json"
                manifest_path.write_text(
                    json.dumps(
                        {
                            "course_title": "测试课程",
                            "course_scene": common.COURSE_SCENE_STANDARD,
                            "sections": [
                                {
                                    "id": "1.2",
                                    "title": "1.2 边界测试",
                                    "block_type": "html",
                                    "content": "原始课程正文。",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch("common._run_html_pagination_model", return_value=plugin_output(pages)):
                    payload, _output_dir = self._run_stage_b(manifest_path)

                section = payload["sections"][0]
                self.assertIn(expected_tail, section["page_content"][-1])
                self.assertEqual(section["page_count"], len(section["page_content"]))

    def test_stage_b_omits_explicit_html_summary_page_after_validating_source_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "course_scene": common.COURSE_SCENE_STANDARD,
                        "sections": [
                            {
                                "id": "1.3",
                                "title": "1.3 显式分页",
                                "block_type": "html",
                                "page_content": [
                                    "## 正文\n\n内容。",
                                    "## 本节要点\n\n摘要。",
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
            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")

            self.assertEqual(["# 显式分页", "## 正文\n\n内容。"], section["page_content"])
            self.assertEqual(2, section["page_count"])
            self.assertNotIn("摘要。", md_body)
            self._assert_marker_alignment(md_body, 2)

    def test_stage_b_preserves_only_teaching_key_points_and_adds_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "course_scene": common.COURSE_SCENE_STANDARD,
                        "sections": [
                            {
                                "id": "1.4",
                                "title": "1.4 仅有摘要",
                                "block_type": "html",
                                "content": "## 本节要点\n\n摘要。",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch(
                "common._run_html_pagination_model",
                return_value=plugin_output(["## 本节要点\n\n摘要。"]),
            ):
                payload, output_dir = self._run_stage_b(manifest_path)

            section = payload["sections"][0]
            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")

            self.assertEqual(["# 仅有摘要", "## 本节要点\n\n摘要。"], section["page_content"])
            self.assertEqual(2, section["page_count"])
            self.assertIn("本节要点", md_body)
            self._assert_marker_alignment(md_body, 2)

    def test_stage_b_prepends_semantic_title_cover_for_both_scenes(self) -> None:
        for scene in (common.COURSE_SCENE_STANDARD, common.COURSE_SCENE_OVERSEAS):
            with self.subTest(scene=scene), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                manifest_path = root / "manifest.json"
                output_dir = root / "out"
                report_path = output_dir / "section-list.json"
                manifest_path.write_text(
                    json.dumps(
                        {
                            "course_title": "测试课程",
                            "course_scene": scene,
                            "sections": [
                                {
                                    "id": "2.3",
                                    "title": "2.3 语义章节标题",
                                    "output_name": "与标题无关的文件名.pptx",
                                    "md_name": "与标题无关的文件名.md",
                                    "block_type": "imagesgallery",
                                    "content": "- 第 1 页\n\n## 正文一\n\n第一段。\n\n- 第 2 页\n\n## 正文二\n\n第二段。",
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
                md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")

                self.assertEqual("# 语义章节标题", section["page_content"][0])
                self.assertEqual(3, section["page_count"])
                self.assertEqual(section["page_count"], len(section["page_content"]))
                self._assert_marker_alignment(md_body, section["page_count"])
                self.assertTrue(md_body.startswith("- 第 1 页\n\n# 语义章节标题"))
                self.assertEqual(
                    section["page_content"],
                    common.parse_canonical_markdown_pages(
                        md_body,
                        section_title=section["section_title"],
                        output_stem=Path(section["md_name"]).stem,
                    ),
                )

    def test_stage_b_removes_cover_title_when_repeated_at_start_of_body(self) -> None:
        cover_title = "从 AI 建议转向建议执行监督闭环"
        body_pages = [
            f"# {cover_title}\n\n本节介绍的是{cover_title}。",
            f"## {cover_title}\n\n后续正文。",
        ]
        for block_type in ("imagesgallery", "html"):
            with self.subTest(block_type=block_type), tempfile.TemporaryDirectory() as tmpdir:
                manifest_path = Path(tmpdir) / "manifest.json"
                content = (
                    common.serialize_canonical_markdown(body_pages)
                    if block_type == "imagesgallery"
                    else "\n\n".join(body_pages)
                )
                manifest_path.write_text(
                    json.dumps(
                        {
                            "course_title": "测试课程",
                            "course_scene": common.COURSE_SCENE_STANDARD,
                            "sections": [
                                {
                                    "id": "2.4",
                                    "title": f"2.4 {cover_title}",
                                    "block_type": block_type,
                                    "content": content,
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                payload, _output_dir = self._run_stage_b(manifest_path)
                section = payload["sections"][0]

                self.assertEqual(
                    [
                        f"# {cover_title}",
                        f"本节介绍的是{cover_title}。",
                        f"## {cover_title}\n\n后续正文。",
                    ],
                    section["page_content"],
                )
                self.assertEqual(3, section["page_count"])

    def test_stage_b_merges_imagesgallery_pages_before_adding_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            output_dir = root / "out"
            report_path = output_dir / "section-list.json"
            body_pages = [
                f"## 正文{i}\n\n第{i}页内容。"
                for i in range(1, 20)
            ] + ["## 本节要点\n\n总结内容。"]
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "course_scene": common.COURSE_SCENE_STANDARD,
                        "sections": [
                            {
                                "id": "4.2",
                                "title": "4.2 物流合作",
                                "block_type": "imagesgallery",
                                "content": common.serialize_canonical_markdown(body_pages),
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
            section = json.loads(report_path.read_text(encoding="utf-8"))["sections"][0]
            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")

        self.assertEqual("# 物流合作", section["page_content"][0])
        self.assertEqual(20, section["page_count"])
        self.assertEqual(20, len(section["page_content"]))
        self.assertIn("第1页内容。", section["page_content"][1])
        self.assertIn("第2页内容。", section["page_content"][1])
        self.assertEqual("## 本节要点\n\n总结内容。", section["page_content"][-1])
        self._assert_marker_alignment(md_body, 20)

    def test_stage_b_preserves_teaching_key_points_with_cover_page_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            output_dir = root / "out"
            report_path = output_dir / "section-list.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "course_scene": common.COURSE_SCENE_OVERSEAS,
                        "sections": [
                            {
                                "id": "1",
                                "title": "1.1 出海章节",
                                "content": "来源正文。",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            model_pages = [
                f"## 正文{i}\n\n第{i}页内容。"
                for i in range(1, 20)
            ] + ["## 本节要点\n\n总结内容。"]

            def fake_model(request: common.HtmlPaginationRequest) -> dict:
                prompt = request.prompt
                self.assertIn("最多生成 20 页", prompt)
                return plugin_output(model_pages)

            argv = [
                "list_course_sections.py",
                str(manifest_path),
                "--output-dir",
                str(output_dir),
                "--report-path",
                str(report_path),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("common._run_html_pagination_model", side_effect=fake_model),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = list_course_sections.main()

            self.assertEqual(0, exit_code)
            section = json.loads(report_path.read_text(encoding="utf-8"))["sections"][0]
            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")

        self.assertEqual("# 出海章节", section["page_content"][0])
        self.assertEqual(20, section["page_count"])
        self.assertEqual(20, len(section["page_content"]))
        self.assertIn("第1页内容。", section["page_content"][1])
        self.assertEqual("## 本节要点\n\n总结内容。", section["page_content"][-1])
        self._assert_marker_alignment(md_body, 20)

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
                [
                    "# 显式元数据",
                    "## 第一页\n\n第一页正文。",
                    "## 第二页\n\n第二页正文。",
                ],
                section["page_content"],
            )
            self.assertEqual(3, section["page_count"])

            md_body = (output_dir / section["md_name"]).read_text(encoding="utf-8")
            self.assertEqual(
                "- 第 1 页\n\n# 显式元数据\n\n- 第 2 页\n\n## 第一页\n\n第一页正文。"
                "\n\n- 第 3 页\n\n## 第二页\n\n第二页正文。\n",
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

    def test_html_pagination_uses_plugin_prompt_for_model_path(self) -> None:
        markdown = "## 标题\n\n第一页正文。"

        def fake_model(request: common.HtmlPaginationRequest) -> dict:
            prompt = request.prompt
            self.assertEqual(common.HTML_PAGINATION_USER_PATH.read_text(encoding="utf-8").replace("{text}", markdown), prompt)
            return plugin_output([markdown])

        with patch("common._run_html_pagination_model", side_effect=fake_model):
            pages = common.paginate_markdown_content(markdown)
        self.assertEqual([markdown], pages)

    def test_plugin_pagination_limits_model_pages_to_twenty(self) -> None:
        markdown = "## 标题\n\n正文。"
        model_pages = [f"## 正文{i}\n\n内容。" for i in range(1, 23)]
        with patch("common._run_html_pagination_model", return_value=plugin_output(model_pages)):
            pages = common.paginate_markdown_content(markdown)
        self.assertEqual(model_pages[:20], pages)


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

        exit_code = run_stage_b_command(manifest_path, output_dir, report_path)
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
        exit_code = run_stage_b_command(manifest_path, output_dir, report_path)
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
            create_pure_html_manifest(self),
            "1.2 博弈规则——【法国】合规宽严度与生存策略",
            "html",
        )

    @unittest.skipUnless(
        MIXED_IMAGESGALLERY_MANIFEST.is_file(),
        "external gallery fixture is unavailable",
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
