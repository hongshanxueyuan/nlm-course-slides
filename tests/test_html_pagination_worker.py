from __future__ import annotations

import json
import io
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
import html_pagination_worker  # noqa: E402
import list_course_sections  # noqa: E402


class HtmlPaginationWorkerTest(unittest.TestCase):
    def _write_manifest(self, root: Path) -> Path:
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "course_title": "Parallel pagination course",
                    "sections": [
                        {
                            "id": "1.1",
                            "title": "1.1 HTML section",
                            "block_type": "html",
                            "content": "## Teaching\n\nHTML section source.",
                        },
                        {
                            "id": "1.2",
                            "title": "1.2 Gallery section",
                            "block_type": "imagesgallery",
                            "content": "- 第 1 页\n\n## Existing page\n\nGallery source.",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest_path

    def test_worker_plan_runs_one_section_and_aggregator_reuses_its_result(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            output_dir = root / "workers"

            plan = html_pagination_worker.list_jobs(manifest_path, output_dir)
            self.assertEqual(1, plan["total_jobs"])
            self.assertEqual(1, plan["jobs"][0]["section_index"])
            self.assertEqual("pending", plan["jobs"][0]["status"])

            with patch(
                "common._run_html_pagination_model",
                return_value=["## Generated page\n\nGenerated section content."],
            ) as model_call:
                result = html_pagination_worker.run_job(manifest_path, output_dir, 1)
            self.assertEqual("complete", result["status"])
            self.assertEqual(1, model_call.call_count)

            with patch(
                "common._run_html_pagination_model",
                side_effect=AssertionError("a completed worker result must be reused"),
            ):
                cached_result = html_pagination_worker.run_job(manifest_path, output_dir, 1)
            self.assertEqual("cached", cached_result["status"])
            self.assertEqual("cached", html_pagination_worker.list_jobs(manifest_path, output_dir)["jobs"][0]["status"])

            manifest = common.load_course_manifest(manifest_path)
            html_pagination_worker.apply_results(manifest, output_dir)

        self.assertEqual(
            ["## Generated page\n\nGenerated section content."],
            manifest.sections[0].page_content,
        )
        self.assertEqual("imagesgallery", manifest.sections[1].block_type)

    def test_worker_rejects_results_for_changed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            output_dir = root / "workers"
            with patch(
                "common._run_html_pagination_model",
                return_value=["## Generated page\n\nGenerated section content."],
            ):
                html_pagination_worker.run_job(manifest_path, output_dir, 1)

            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace("HTML section source", "Updated source"),
                encoding="utf-8",
            )
            manifest = common.load_course_manifest(manifest_path)
            with self.assertRaisesRegex(RuntimeError, "Missing or stale"):
                html_pagination_worker.apply_results(manifest, output_dir)

    def test_worker_records_a_failure_without_writing_a_success_result(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            output_dir = root / "workers"

            with (
                patch(
                    "common._run_html_pagination_model",
                    side_effect=RuntimeError("provider request failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "provider request failed"),
            ):
                html_pagination_worker.run_job(manifest_path, output_dir, 1)

            self.assertFalse((output_dir / "section-0001.result.json").exists())
            failure = json.loads((output_dir / "section-0001.failure.json").read_text(encoding="utf-8"))

        self.assertEqual("failed", failure["status"])
        self.assertEqual("1.1", failure["section_id"])
        self.assertNotIn("key", failure["error"].lower())

    def test_aggregator_writes_the_section_list_from_worker_results(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            workers_dir = root / "workers"
            with patch(
                "common._run_html_pagination_model",
                return_value=["## Generated page\n\nGenerated section content."],
            ):
                html_pagination_worker.run_job(manifest_path, workers_dir, 1)

            output_dir = root / "output"
            report_path = output_dir / "section-list.json"
            argv = [
                "list_course_sections.py",
                str(manifest_path),
                "--output-dir",
                str(output_dir),
                "--report-path",
                str(report_path),
                "--pagination-results-dir",
                str(workers_dir),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "common._run_html_pagination_model",
                    side_effect=AssertionError("the aggregator must not call the model"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, list_course_sections.main())

            report = json.loads(report_path.read_text(encoding="utf-8"))
            section = report["sections"][0]
            markdown = (output_dir / section["md_name"]).read_text(encoding="utf-8")

        self.assertEqual(["# HTML section", "## Generated page\n\nGenerated section content."], section["page_content"])
        self.assertEqual(section["page_count"], len(section["page_content"]))
        self.assertEqual(section["page_count"], markdown.count("- 第 "))


if __name__ == "__main__":
    unittest.main()
