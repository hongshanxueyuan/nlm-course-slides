from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import common  # noqa: E402
import prepare_course_json  # noqa: E402


class PrepareCourseJsonTest(unittest.TestCase):
    def test_saves_complete_stdin_json_without_rewriting(self) -> None:
        raw_json = b'{\n  "name": "Runtime course", "chapters": []\n}\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "courses"
            argv = [
                "prepare_course_json.py",
                "--json-stdin",
                "--course-name",
                "Runtime course",
                "--output-root",
                str(output_root),
            ]
            stdin = io.TextIOWrapper(io.BytesIO(raw_json), encoding="utf-8")
            with (
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdin", stdin),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(0, prepare_course_json.main())

            course_dir = output_root / "Runtime course"
            json_path = course_dir / "Runtime course.json"
            report_path = course_dir / "course-json-report.json"
            self.assertEqual(raw_json, json_path.read_bytes())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual("ready", report["status"])
            self.assertEqual(
                "insights_mcp_server:get_runtime_course_content",
                report["source_kind"],
            )
            self.assertEqual(str(json_path.resolve()), report["json_path"])
            self.assertEqual(str(report_path.resolve()), report["report_path"])
            self.assertEqual(report, json.loads(stdout.getvalue()))

    def test_rejects_invalid_json_before_writing_course_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "courses"
            argv = [
                "prepare_course_json.py",
                "--json-stdin",
                "--course-name",
                "Runtime course",
                "--output-root",
                str(output_root),
            ]
            stdin = io.TextIOWrapper(io.BytesIO(b'{"name":'), encoding="utf-8")
            with (
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdin", stdin),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                prepare_course_json.main()

            self.assertFalse((output_root / "Runtime course").exists())

    def test_rejects_legacy_input_sources(self) -> None:
        for legacy_args in (
            ["--source-file", "course.json"],
            ["--signed-url", "https://example.test/course.json"],
        ):
            with self.subTest(legacy_args=legacy_args):
                argv = [
                    "prepare_course_json.py",
                    *legacy_args,
                    "--course-name",
                    "Runtime course",
                ]
                with (
                    patch.object(sys, "argv", argv),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    prepare_course_json.main()

    def test_html_components_are_rendered_before_pagination(self) -> None:
        raw_html = (
            "<head><title>Discarded head</title></head>"
            "<style>.hidden{display:none}</style>"
            "<script>discarded()</script>"
            "<p>- 第 1 页</p><h2>Visible heading</h2>"
            "<p>Visible text<img src='inline.png'></p><p>- 第 2 页</p>"
            "![Markdown image](https://example.test/image.png)"
        )
        section = common.Section(
            id="1",
            order=1,
            title="1.1 Runtime section",
            content=raw_html,
            slug="runtime-section",
            output_name="1.1 Runtime section.pptx",
            block_type="html",
            source_blocks=[
                common.SectionSourceBlock(
                    category="html",
                    name="HTML content",
                    text=raw_html,
                )
            ],
        )
        paginated_input: list[str] = []

        def fake_paginate(text: str) -> list[str]:
            paginated_input.append(text)
            return ["## Visible heading\n\nVisible text"]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(common, "paginate_markdown_content", side_effect=fake_paginate):
                common.materialize_section_markdown(
                    section,
                    output_dir=Path(tmpdir),
                    default_block_type="html",
                )

        self.assertEqual(1, len(paginated_input))
        self.assertIn("## Visible heading", paginated_input[0])
        self.assertIn("Visible text", paginated_input[0])
        self.assertNotIn("Discarded head", paginated_input[0])
        self.assertNotIn("display:none", paginated_input[0])
        self.assertNotIn("discarded()", paginated_input[0])
        self.assertNotIn("img", paginated_input[0])
        self.assertNotIn("Markdown image", paginated_input[0])
        self.assertNotIn("第 1 页", paginated_input[0])
        self.assertNotIn("第 2 页", paginated_input[0])

    def test_imagesgallery_components_do_not_use_html_renderer(self) -> None:
        backslash = chr(92)
        gallery_text = (
            "- 第 1 页\n\n## Gallery page\n\nEscaped "
            f"{backslash}*text{backslash}* and {backslash}[link{backslash}](url)"
        )
        section = common.Section(
            id="1",
            order=1,
            title="1.1 Gallery section",
            content=gallery_text,
            slug="gallery-section",
            output_name="1.1 Gallery section.pptx",
            block_type="imagesgallery",
            source_blocks=[
                common.SectionSourceBlock(
                    category="imagesgallery",
                    name="Gallery content",
                    text=gallery_text,
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(common, "_render_html", side_effect=AssertionError("unexpected HTML rendering")):
                markdown_path = common.materialize_section_markdown(
                    section,
                    output_dir=Path(tmpdir),
                    default_block_type="html",
                )
                markdown_body = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(
            ["## Gallery page\n\nEscaped *text* and [link](url)"],
            section.page_content,
        )
        self.assertNotIn(backslash, markdown_body)


if __name__ == "__main__":
    unittest.main()
