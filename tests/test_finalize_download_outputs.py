from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import monitor_and_finalize_slides  # noqa: E402


class FinalizeDownloadOutputsTest(unittest.TestCase):
    def test_write_section_markdown_does_not_prepend_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "section.md"
            section = SimpleNamespace(
                title="1.1 测试小节",
                content="## 标题\n\n正文",
                page_content=["## 标题", "正文"],
            )

            exported = monitor_and_finalize_slides._write_section_markdown(
                section,
                target_path,
                dry_run=False,
            )

            self.assertTrue(exported)
            self.assertEqual("## 标题\n\n正文\n", target_path.read_text(encoding="utf-8"))

    def test_finalize_download_uses_original_output_name_and_markdown_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "manifest.json"
            create_report_path = root / "create-report.json"
            output_dir = root / "out"
            manifest_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "sections": [
                            {
                                "id": "1.1",
                                "title": "1.1 测试小节",
                                "content": "## 标题\n\n正文",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            create_report_path.write_text(
                json.dumps(
                    {
                        "course_title": "测试课程",
                        "notebook_id": "notebook-1",
                        "results": [
                            {
                                "section_id": "1.1",
                                "status": "create_requested",
                                "artifact_id": "artifact-1",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def fake_download_slide_deck(_notebook_id, _artifact_id, output_path, **_kwargs):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"pptx")

            argv = [
                "monitor_and_finalize_slides.py",
                str(manifest_path),
                "--create-report",
                str(create_report_path),
                "--output-dir",
                str(output_dir),
                "--download",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("monitor_and_finalize_slides.ensure_authenticated"),
                patch(
                    "monitor_and_finalize_slides.wait_for_artifact",
                    return_value={"status": "completed"},
                ),
                patch("monitor_and_finalize_slides.rename_artifact"),
                patch(
                    "monitor_and_finalize_slides.download_slide_deck",
                    side_effect=fake_download_slide_deck,
                ),
                patch(
                    "monitor_and_finalize_slides._run_local_postprocess",
                    return_value={"output_path": str(output_dir / "1.1 测试小节.pptx")},
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = monitor_and_finalize_slides.main()

            self.assertEqual(0, exit_code)
            report = json.loads((output_dir / "finalize-report.json").read_text(encoding="utf-8"))
            result = report["results"][0]
            markdown_path = output_dir / "1.1 测试小节.md"
            self.assertEqual(str(output_dir / "1.1 测试小节.pptx"), result["output_path"])
            self.assertEqual(str(markdown_path), result["markdown_output_path"])
            self.assertNotIn("_水印版", result["output_path"])
            self.assertTrue(markdown_path.exists())
            self.assertEqual("## 标题\n\n正文\n", markdown_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
