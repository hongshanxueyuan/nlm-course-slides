from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import common  # noqa: E402
import html_pagination_worker  # noqa: E402
import list_course_sections  # noqa: E402
from fake_codex import responses_api_payload, sdk_httpx, SdkDefaultHttpxClient, plugin_output, _write_fake_codex_home  # noqa: E402


def _write_provider(root: Path, *, wire_api: str = "responses", base_url: str = "https://provider.example.test/v1") -> Path:
    codex_home = root / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model_provider = "example"\n[model_providers.example]\n'
        f'base_url = "{base_url}"\n'
        f'env_key = "TEST_API_KEY"\nwire_api = "{wire_api}"\n',
        encoding="utf-8",
    )
    return codex_home


def _transport(send, **kwargs):
    return SdkDefaultHttpxClient(transport=sdk_httpx.MockTransport(send), **kwargs)


class HtmlPaginationWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        fake_home = _write_fake_codex_home(Path(tempdir.name))
        env_patch = patch.dict("os.environ", {"CODEX_HOME": str(fake_home), "AZURE_OPENAI_API_KEY": "fake-key"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

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

    def test_worker_preserves_plugin_pages_and_aggregates_content(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            workers = root / "workers"
            codex_home = _write_provider(root)
            raw = {"pages": [
                {"marp_code": "## Short\n- A", "content": "## Generated page\n\nFull example and details.\n\n- Parent\n\n    - Child\n\n        Continuation.\n\n```markdown\n\n###\n\n    code\n\n```"},
                {"marp_code": "## Short\n- B", "content": "## 本节要点\n\nThis is part of the teaching text."},
            ]}
            requests = []
            def send(request):
                requests.append(json.loads(request.content.decode("utf-8")))
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw, ensure_ascii=False)), request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                html_pagination_worker.run_job(manifest_path, workers, 1)
                raw_path = workers / "section-0001.model.json"
                self.assertEqual(raw, json.loads(raw_path.read_text(encoding="utf-8")))
                manifest = common.load_course_manifest(manifest_path)
                html_pagination_worker.apply_results(manifest, workers)
                common.materialize_markdown_artifacts(manifest, output_dir=root / "output")
            section = manifest.sections[0]
            markdown = (root / "output" / section.md_name).read_text(encoding="utf-8")
            self.assertEqual(["# HTML section", "## Generated page\n\nFull example and details.\n\n- Parent\n\n    - Child\n\n        Continuation.\n\n```markdown\n\n###\n\n    code\n\n```", "## 本节要点\n\nThis is part of the teaching text."], section.page_content)
            self.assertEqual(section.page_content, common.parse_canonical_markdown_pages(markdown, section_title=section.title, output_stem=Path(section.md_name).stem))
            self.assertEqual(raw, json.loads(raw_path.read_text(encoding="utf-8")))
            self.assertEqual("gpt-6.1-terra", requests[0]["model"])
            self.assertEqual(["system", "user"], [message["role"] for message in requests[0]["input"]])
            self.assertIn("HTML section source.", requests[0]["input"][1]["content"])
            self.assertEqual(common.HTML_PAGINATION_SYSTEM_PATH.read_text(encoding="utf-8"), requests[0]["input"][0]["content"])
            self.assertEqual(common.HTML_PAGINATION_USER_PATH.read_text(encoding="utf-8").replace("{text}", "## Teaching\n\nHTML section source."), requests[0]["input"][1]["content"])
            self.assertEqual("max", requests[0]["reasoning"]["effort"])
            self.assertEqual(json.loads(common.HTML_PAGINATION_SCHEMA_PATH.read_text(encoding="utf-8")), requests[0]["text"]["format"]["schema"])

    def test_twenty_model_pages_are_cleaned_once_and_merged_without_losing_text(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            workers = root / "workers"
            codex_home = _write_provider(root)
            contents = ["# HTML section\n\n## Same\n\n###\n\nFirst detail.", "## Same\n\n    - Second detail."]
            contents += [f"## Page {index}\n\nDetail {index}." for index in range(3, 21)]
            contents += ["", "## Excess 22\n\nExcess twenty-two."]
            raw = {"pages": [{"marp_code": f"- slide {index}", "content": content} for index, content in enumerate(contents)]}
            def send(request):
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw, ensure_ascii=False)), request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                html_pagination_worker.run_job(manifest_path, workers, 1)
                manifest = common.load_course_manifest(manifest_path)
                html_pagination_worker.apply_results(manifest, workers)
                common.materialize_markdown_artifacts(manifest, output_dir=root / "output")
            section = manifest.sections[0]
            markdown = (root / "output" / section.md_name).read_text(encoding="utf-8")
            self.assertEqual(20, section.page_count)
            self.assertEqual(section.page_content, common.parse_canonical_markdown_pages(markdown, section_title=section.title, output_stem=Path(section.md_name).stem))
            self.assertEqual(raw, json.loads((workers / "section-0001.model.json").read_text(encoding="utf-8")))
            self.assertEqual(1, markdown.count("## Same"))
            self.assertIn("\n\n    - Second detail.", markdown)
            self.assertNotIn("###\n", markdown)
            self.assertNotIn("Excess twenty-two.", markdown)
            for fragment in ["First detail.", "Second detail.", *[f"Detail {index}." for index in range(3, 21)]]:
                self.assertEqual(1, markdown.count(fragment))

    def test_separate_key_points_component_is_excluded_but_teaching_text_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = root / "course.json"
            course = {"name": "课程", "chapters": [{"name": "第一章", "sections": [
                {"name": "第一章"}, {"name": "1.1 测试", "verticals": [{"name": "正文", "blocks": [
                    {"category": "html", "name": "文字讲解", "text": "<h2>本节要点</h2><p>教学中的内容。</p>"},
                    {"category": "html", "name": "本节要点", "text": "<h2>本节要点</h2><p>独立摘要组件。</p>"},
                    {"category": "html", "name": "作业说明", "text": "<p>不属于文字讲解的作业。</p>"},
                ]}, {"name": "训战", "blocks": [
                    {"category": "html", "name": "文字讲解", "text": "<p>另一份训战讲解。</p>"}
                ]}]}]}]}
            manifest_path.write_text(json.dumps(course, ensure_ascii=False), encoding="utf-8")
            original = manifest_path.read_bytes()
            codex_home = _write_provider(root)
            requests = []
            raw = {"pages": [{"marp_code": "- short", "content": "## 本节要点\n\n教学中的内容。"}]}
            def send(request):
                requests.append(json.loads(request.content.decode("utf-8")))
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw, ensure_ascii=False)), request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                html_pagination_worker.run_job(manifest_path, root / "workers", 1)
                manifest = common.load_course_manifest(manifest_path)
                html_pagination_worker.apply_results(manifest, root / "workers")
                common.materialize_markdown_artifacts(manifest, output_dir=root / "output")
            prompt = requests[0]["input"][1]["content"]
            self.assertIn("教学中的内容", prompt)
            self.assertNotIn("独立摘要组件", prompt)
            self.assertNotIn("不属于文字讲解的作业", prompt)
            self.assertNotIn("另一份训战讲解", prompt)
            self.assertIn("## 本节要点\n\n教学中的内容。", manifest.sections[0].page_content)
            self.assertEqual(original, manifest_path.read_bytes())

    def test_worker_invalidates_cached_results_when_provider_or_prompt_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            workers = root / "workers"
            codex_home = _write_provider(root)
            config_path = codex_home / "config.toml"
            requests = []
            raw = {"pages": [{"marp_code": "- short", "content": "## Body\n\nOriginal content."}]}
            def send(request):
                requests.append(str(request.url))
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw)), request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                self.assertEqual("complete", html_pagination_worker.run_job(manifest_path, workers, 1)["status"])
                self.assertEqual("cached", html_pagination_worker.run_job(manifest_path, workers, 1)["status"])
                config_path.write_text(config_path.read_text(encoding="utf-8").replace("provider.example.test", "new-provider.example.test"), encoding="utf-8")
                self.assertEqual("pending", html_pagination_worker.list_jobs(manifest_path, workers)["jobs"][0]["status"])
                self.assertEqual("complete", html_pagination_worker.run_job(manifest_path, workers, 1)["status"])
                with patch.object(common, "HTML_PAGINATION_REASONING_EFFORT", "high"):
                    self.assertEqual("pending", html_pagination_worker.list_jobs(manifest_path, workers)["jobs"][0]["status"])
                changed_schema = root / "schema.json"
                schema = json.loads(common.HTML_PAGINATION_SCHEMA_PATH.read_text(encoding="utf-8"))
                schema["description"] = "updated output contract"
                changed_schema.write_text(json.dumps(schema), encoding="utf-8")
                with patch.object(common, "HTML_PAGINATION_SCHEMA_PATH", changed_schema):
                    self.assertEqual("pending", html_pagination_worker.list_jobs(manifest_path, workers)["jobs"][0]["status"])
                changed_prompt = root / "system.txt"
                changed_prompt.write_text(common.HTML_PAGINATION_SYSTEM_PATH.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
                with patch.object(common, "HTML_PAGINATION_SYSTEM_PATH", changed_prompt):
                    self.assertEqual("pending", html_pagination_worker.list_jobs(manifest_path, workers)["jobs"][0]["status"])
                    self.assertEqual("complete", html_pagination_worker.run_job(manifest_path, workers, 1)["status"])
            self.assertEqual(3, len(requests))
            self.assertIn("new-provider.example.test", requests[1])

    def test_worker_restores_missing_raw_model_output_from_valid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            workers = root / "workers"
            codex_home = _write_provider(root)
            requests = []
            raw = {"pages": [{"marp_code": "- short", "content": "## Body\n\nFull details."}]}
            def send(request):
                requests.append(1)
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw)), request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                html_pagination_worker.run_job(manifest_path, workers, 1)
                raw_path = workers / "section-0001.model.json"
                raw_path.unlink()
                self.assertEqual("pending", html_pagination_worker.list_jobs(manifest_path, workers)["jobs"][0]["status"])
                self.assertEqual("complete", html_pagination_worker.run_job(manifest_path, workers, 1)["status"])
                self.assertEqual(raw, json.loads(raw_path.read_text(encoding="utf-8")))
            self.assertEqual(1, len(requests))

    def test_invalid_model_content_is_not_reused_as_a_successful_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            workers = root / "workers"
            codex_home = _write_provider(root)
            requests = []
            def send(request):
                requests.append(1)
                content = "<p>Invalid raw HTML</p>" if len(requests) == 1 else "## Body\n\nValid text."
                raw = {"pages": [{"marp_code": "- slide", "content": content}]}
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw)), request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                with self.assertRaisesRegex(RuntimeError, "raw HTML"):
                    html_pagination_worker.run_job(manifest_path, workers, 1)
                self.assertFalse((workers / "section-0001.result.json").exists())
                self.assertEqual("complete", html_pagination_worker.run_job(manifest_path, workers, 1)["status"])
            self.assertEqual(2, len(requests))

    def test_chat_provider_receives_same_plugin_contract_and_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            codex_home = _write_provider(root, wire_api="chat")
            requests = []
            raw = {"pages": [{"marp_code": "## short", "content": "## Body\n\nFull detail."}]}
            def send(request):
                requests.append(json.loads(request.content.decode("utf-8")))
                response = {"id": "chat-test", "object": "chat.completion", "created": 0,
                            "model": "gpt-6.1-terra", "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(raw)}}]}
                return sdk_httpx.Response(200, json=response, request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                html_pagination_worker.run_job(manifest_path, root / "workers", 1)
            body = requests[0]
            self.assertEqual("gpt-6.1-terra", body["model"])
            self.assertEqual("max", body["reasoning_effort"])
            self.assertEqual(["system", "user"], [message["role"] for message in body["messages"]])
            self.assertEqual(common.HTML_PAGINATION_SYSTEM_PATH.read_text(encoding="utf-8"), body["messages"][0]["content"])
            self.assertEqual(json.loads(common.HTML_PAGINATION_SCHEMA_PATH.read_text(encoding="utf-8")), body["response_format"]["json_schema"]["schema"])

    def test_worker_rejects_returned_model_or_reasoning_downgrades(self) -> None:
        for returned_model, returned_effort in [("other-model", "max"), ("gpt-6.1-terra", "low")]:
            with self.subTest(model=returned_model, effort=returned_effort), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                manifest_path = self._write_manifest(root)
                codex_home = _write_provider(root)
                raw = {"pages": [{"marp_code": "- slide", "content": "## Body\n\nFull detail."}]}
                def send(request):
                    payload = responses_api_payload(json.dumps(raw))
                    payload["model"] = returned_model
                    payload["reasoning"] = {"effort": returned_effort}
                    return sdk_httpx.Response(200, json=payload, request=request)

                with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                    with self.assertRaisesRegex(RuntimeError, "model|reasoning"):
                        html_pagination_worker.run_job(manifest_path, root / "workers", 1)
                self.assertFalse((root / "workers" / "section-0001.result.json").exists())

    def test_worker_rejects_incomplete_response_even_when_it_has_page_text(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            codex_home = _write_provider(root)
            raw = {"pages": [{"marp_code": "- slide", "content": "## Body\n\nTruncated text."}]}
            def send(request):
                payload = responses_api_payload(json.dumps(raw))
                payload["status"] = "incomplete"
                payload["incomplete_details"] = {"reason": "max_output_tokens"}
                return sdk_httpx.Response(200, json=payload, request=request)

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)):
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    html_pagination_worker.run_job(manifest_path, root / "workers", 1)
            self.assertFalse((root / "workers" / "section-0001.result.json").exists())

    def test_worker_records_provider_configuration_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            codex_home = root / "bad-codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('model_provider = "missing"\n', encoding="utf-8")
            workers = root / "workers"
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                with self.assertRaisesRegex(RuntimeError, "active Codex model provider"):
                    html_pagination_worker.run_job(manifest_path, workers, 1)
            self.assertFalse((workers / "section-0001.result.json").exists())
            failure = json.loads((workers / "section-0001.failure.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", failure["status"])
            self.assertEqual("1.1", failure["section_id"])

    def test_teaching_blocks_are_selected_before_non_training_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            path = root / "course.json"
            course = {"name": "课程", "chapters": [{"name": "第一章", "sections": [
                {"name": "第一章"}, {"name": "1.1 测试", "verticals": [
                    {"name": "普通小节", "blocks": [{"category": "html", "name": "作业说明", "text": "<p>作业内容</p>"}]},
                    {"name": "训战", "blocks": [{"category": "html", "name": "文字讲解", "text": "<p>训战讲解内容</p>"}]},
                ]}]}]}
            path.write_text(json.dumps(course, ensure_ascii=False), encoding="utf-8")
            manifest = common.load_course_manifest(path)
            self.assertEqual(1, len(manifest.sections))
            with patch("common._run_html_pagination_model", return_value=plugin_output(["训战讲解内容"])) as model:
                html_pagination_worker.run_job(path, root / "workers", 1)
            prompt = model.call_args.args[0].prompt
            self.assertIn("训战讲解内容", prompt)
            self.assertNotIn("作业内容", prompt)

    def test_direct_aggregation_keeps_raw_outputs_for_duplicate_section_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            path = root / "manifest.json"
            path.write_text(json.dumps({"course_title": "Duplicate IDs", "sections": [
                {"id": "same", "title": "First", "content": "Identical source."},
                {"id": "same", "title": "Second", "content": "Identical source."},
            ]}), encoding="utf-8")
            codex_home = _write_provider(root)
            requests = []
            def send(request):
                requests.append(1)
                raw = plugin_output([f"## Body\n\nGenerated {len(requests)}."])
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw)), request=request)

            output = root / "out"
            argv = ["list_course_sections.py", str(path), "--output-dir", str(output)]
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)), patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(0, list_course_sections.main())
                self.assertEqual(0, list_course_sections.main())
            report = json.loads((output / "section-list.json").read_text(encoding="utf-8"))
            self.assertEqual(2, len(requests))
            self.assertIn("Generated 1.", report["sections"][0]["page_content"][1])
            self.assertIn("Generated 2.", report["sections"][1]["page_content"][1])
            model_files = sorted((output / ".html-pagination-model").glob("*.json"))
            self.assertEqual(2, len(model_files))
            self.assertIn("Generated 1.", model_files[0].read_text(encoding="utf-8"))
            self.assertIn("Generated 2.", model_files[1].read_text(encoding="utf-8"))

    def test_worker_uses_one_request_snapshot_if_configuration_changes_mid_run(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(root)
            workers = root / "workers"
            codex_home = _write_provider(root, base_url="https://old.example.test/v1")
            config = codex_home / "config.toml"
            system = root / "system.txt"
            system.write_text("original system", encoding="utf-8")
            user_prompt = root / "user.txt"
            original_user_prompt = common.HTML_PAGINATION_USER_PATH.read_text(encoding="utf-8")
            user_prompt.write_text(original_user_prompt, encoding="utf-8")
            schema = root / "schema.json"
            schema.write_text(common.HTML_PAGINATION_SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            original_schema = json.loads(schema.read_text(encoding="utf-8"))
            requests = []
            raw = plugin_output(["## Body\n\nPage content."])
            def send(request):
                requests.append((str(request.url), json.loads(request.content.decode("utf-8"))))
                return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(raw)), request=request)

            original_load_provider = common._load_codex_html_pagination_config
            snapshots = []
            def change_after_provider_snapshot(**kwargs):
                provider = original_load_provider(**kwargs)
                snapshots.append(provider)
                if len(snapshots) == 1:
                    system.write_text("changed system", encoding="utf-8")
                    user_prompt.write_text(original_user_prompt + "\nchanged user template", encoding="utf-8")
                    common.HTML_PAGINATION_REASONING_EFFORT = "high"
                    modified = dict(original_schema, description="changed schema")
                    schema.write_text(json.dumps(modified), encoding="utf-8")
                    config.write_text(config.read_text(encoding="utf-8").replace("old.example.test", "new.example.test"), encoding="utf-8")
                return provider
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home), "TEST_API_KEY": "fake-key"}), patch("openai.DefaultHttpxClient", side_effect=partial(_transport, send)), patch.object(common, "HTML_PAGINATION_SYSTEM_PATH", system), patch.object(common, "HTML_PAGINATION_USER_PATH", user_prompt), patch.object(common, "HTML_PAGINATION_SCHEMA_PATH", schema), patch.object(common, "HTML_PAGINATION_REASONING_EFFORT", "max"), patch.object(common, "_load_codex_html_pagination_config", side_effect=change_after_provider_snapshot):
                html_pagination_worker.run_job(manifest_path, workers, 1)
                self.assertIn("old.example.test", requests[0][0])
                self.assertEqual("original system", requests[0][1]["input"][0]["content"])
                self.assertNotIn("changed user template", requests[0][1]["input"][1]["content"])
                self.assertEqual("max", requests[0][1]["reasoning"]["effort"])
                self.assertEqual(original_schema, requests[0][1]["text"]["format"]["schema"])
                self.assertEqual("pending", html_pagination_worker.list_jobs(manifest_path, workers)["jobs"][0]["status"])
                html_pagination_worker.run_job(manifest_path, workers, 1)
                self.assertIn("new.example.test", requests[1][0])
                self.assertEqual("changed system", requests[1][1]["input"][0]["content"])
                self.assertIn("changed user template", requests[1][1]["input"][1]["content"])
                self.assertEqual("high", requests[1][1]["reasoning"]["effort"])
                self.assertEqual("cached", html_pagination_worker.list_jobs(manifest_path, workers)["jobs"][0]["status"])
            self.assertEqual(2, len(requests))

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
                return_value=plugin_output(["## Generated page\n\nGenerated section content."]),
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
                return_value=plugin_output(["## Generated page\n\nGenerated section content."]),
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
                return_value=plugin_output(["## Generated page\n\nGenerated section content."]),
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
