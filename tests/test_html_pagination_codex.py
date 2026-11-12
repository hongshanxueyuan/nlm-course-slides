from __future__ import annotations

import io
import importlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from openai import DefaultHttpxClient as SdkDefaultHttpxClient


httpx = importlib.import_module(
    SdkDefaultHttpxClient.__mro__[1].__module__.split(".", maxsplit=1)[0]
)


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import common  # noqa: E402
import list_course_sections  # noqa: E402
from fake_codex import responses_api_payload  # noqa: E402


class HtmlPaginationCodexTest(unittest.TestCase):
    def _write_manifest(self, root: Path, sections: list[dict]) -> Path:
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "course_title": "SDK boundary course",
                    "course_scene": "企业流程与智能化场景",
                    "sections": sections,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest_path

    def _write_codex_home(
        self,
        codex_home: Path,
        *,
        auth_mode: str = "apikey",
        wire_api: str = "responses",
        env_key: str | None = "AZURE_OPENAI_API_KEY",
    ) -> None:
        codex_home.mkdir(parents=True)
        codex_home.joinpath("auth.json").write_text(
            json.dumps({"auth_mode": auth_mode, "OPENAI_API_KEY": "temporary-test-key"}),
            encoding="utf-8",
        )
        config = (
            'model = "gpt-6-luna"\n'
            'model_provider = "azure"\n'
            "[model_providers.azure]\n"
            'base_url = "https://provider.example.test/v1"\n'
        )
        if env_key:
            config += f'env_key = "{env_key}"\n'
        config += f'wire_api = "{wire_api}"\n'
        codex_home.joinpath("config.toml").write_text(config, encoding="utf-8")

    def _run_stage_b(
        self,
        root: Path,
        manifest_path: Path,
        *,
        http_client_factory: object,
        codex_home: Path | None,
        default_home: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, Path, Path, str, dict | None]:
        output_dir = root / "output"
        report_path = output_dir / "section-list.json"
        argv = [
            "list_course_sections.py",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--report-path",
            str(report_path),
        ]
        env = {
            "CODEX_HOME": str(codex_home) if codex_home else "",
            "AZURE_OPENAI_API_KEY": "temporary-azure-key",
        }
        env.update(extra_env or {})
        stdout = io.StringIO()
        home_patch = (
            patch.object(common.Path, "home", return_value=default_home)
            if default_home is not None
            else nullcontext()
        )
        with (
            patch.dict("os.environ", env),
            patch.object(sys, "argv", argv),
            patch("openai.DefaultHttpxClient", http_client_factory),
            home_patch,
            redirect_stdout(stdout),
        ):
            exit_code = list_course_sections.main()
        payload = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
        return exit_code, output_dir, report_path, stdout.getvalue(), payload

    @staticmethod
    def _http_client_for_response(response_or_error: str | Exception, requests: list[dict]):
        def send(request: httpx.Request) -> httpx.Response:
            requests.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "authorization": request.headers.get("authorization"),
                    "api_key_header": request.headers.get("api-key"),
                    "timeout": request.extensions.get("timeout"),
                    "body": json.loads(request.content.decode("utf-8")),
                }
            )
            if isinstance(response_or_error, Exception):
                return httpx.Response(
                    400,
                    json={"error": {"message": str(response_or_error)}},
                    request=request,
                )
            return httpx.Response(
                200,
                json=responses_api_payload(response_or_error),
                request=request,
            )

        return lambda **kwargs: SdkDefaultHttpxClient(
            transport=httpx.MockTransport(send),
            **kwargs,
        )

    @staticmethod
    def _html_section(
        content: str = "## Teaching\n\nRendered course text.",
        *,
        section_id: str = "1.1",
        title: str = "1.1 SDK boundary",
    ) -> dict:
        return {
            "id": section_id,
            "title": title,
            "block_type": "html",
            "content": content,
        }

    def test_stage_b_uses_codex_provider_and_pins_gpt_6_1_terra(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "codex-home"
            self._write_codex_home(codex_home)
            manifest_path = self._write_manifest(root, [self._html_section()])
            requests: list[dict] = []
            transport_factory = self._http_client_for_response(
                json.dumps({"pages": ["## Generated body\n\nSDK split output."]}),
                requests,
            )

            exit_code, output_dir, report_path, stdout, payload = self._run_stage_b(
                root,
                manifest_path,
                http_client_factory=transport_factory,
                codex_home=codex_home,
            )
            self.assertTrue(report_path.exists())
            section = payload["sections"][0]
            markdown = (output_dir / section["md_name"]).read_text(encoding="utf-8")
            report_json = report_path.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(requests))
        self.assertEqual("POST", requests[0]["method"])
        self.assertEqual("https://provider.example.test/v1/responses", requests[0]["url"])
        self.assertEqual("Bearer temporary-azure-key", requests[0]["authorization"])
        self.assertEqual("temporary-azure-key", requests[0]["api_key_header"])
        self.assertEqual(600, requests[0]["timeout"]["read"])
        self.assertEqual("gpt-6.1-terra", requests[0]["body"]["model"])
        self.assertIn("Rendered course text.", requests[0]["body"]["input"][1]["content"])
        self.assertEqual(
            ["# SDK boundary", "## Generated body\n\nSDK split output."],
            section["page_content"],
        )
        self.assertEqual(section["page_count"], len(section["page_content"]))
        self.assertIn("- 第 1 页", markdown)
        self.assertIn("- 第 2 页", markdown)
        self.assertNotIn("temporary-test-key", stdout + markdown + report_json)
        self.assertNotIn("temporary-azure-key", stdout + markdown + report_json)

    def test_stage_b_uses_active_azure_provider_key_and_auth_header(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "codex-home"
            self._write_codex_home(
                codex_home,
                env_key="AZURE_OPENAI_API_KEY",
            )
            manifest_path = self._write_manifest(root, [self._html_section()])
            requests: list[dict] = []
            transport_factory = self._http_client_for_response(
                json.dumps({"pages": ["## Generated body\\n\\nSDK split output."]}),
                requests,
            )

            self._run_stage_b(
                root,
                manifest_path,
                http_client_factory=transport_factory,
                codex_home=codex_home,
                extra_env={"AZURE_OPENAI_API_KEY": "temporary-azure-key"},
            )

        self.assertEqual("temporary-azure-key", requests[0]["api_key_header"])
        self.assertEqual("Bearer temporary-azure-key", requests[0]["authorization"])
        self.assertEqual(600, requests[0]["timeout"]["read"])

    def test_stage_b_reports_progress_and_reuses_successful_pages_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "codex-home"
            self._write_codex_home(codex_home)
            manifest_path = self._write_manifest(
                root,
                [
                    self._html_section(
                        "## First body\n\nFIRST PAGE",
                        section_id="1.1",
                        title="1.1 First section",
                    ),
                    self._html_section(
                        "## Second body\n\nSECOND PAGE",
                        section_id="1.2",
                        title="1.2 Second section",
                    ),
                ],
            )
            first_run_requests: list[str] = []

            def fail_second_section(request: httpx.Request) -> httpx.Response:
                request_payload = json.loads(request.content.decode("utf-8"))
                prompt = request_payload["input"][1]["content"]
                first_run_requests.append(prompt)
                if "SECOND PAGE" in prompt:
                    return httpx.Response(
                        500,
                        json={"error": {"message": "temporary provider failure"}},
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json=responses_api_payload(
                        json.dumps({"pages": ["## First body\\n\\nFIRST PAGE"]})
                    ),
                    request=request,
                )

            first_transport = lambda **kwargs: SdkDefaultHttpxClient(
                transport=httpx.MockTransport(fail_second_section),
                **kwargs,
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaisesRegex(RuntimeError, "request failed"):
                self._run_stage_b(
                    root,
                    manifest_path,
                    http_client_factory=first_transport,
                    codex_home=codex_home,
                )

            self.assertEqual(2, len(first_run_requests))
            output_dir = root / "output"
            self.assertFalse((output_dir / "1.1 First section.md").exists())
            self.assertFalse((output_dir / "section-list.json").exists())
            failure_path = output_dir / "section-list-failure.json"
            self.assertEqual("failed", json.loads(failure_path.read_text(encoding="utf-8"))["status"])
            self.assertIn("1/2", stderr.getvalue())
            self.assertIn("2/2", stderr.getvalue())

            retry_requests: list[str] = []

            def succeed_all_sections(request: httpx.Request) -> httpx.Response:
                request_payload = json.loads(request.content.decode("utf-8"))
                prompt = request_payload["input"][1]["content"]
                retry_requests.append(prompt)
                page_text = "SECOND PAGE" if "SECOND PAGE" in prompt else "FIRST PAGE"
                heading = "Second body" if "SECOND PAGE" in prompt else "First body"
                return httpx.Response(
                    200,
                    json=responses_api_payload(
                        json.dumps({"pages": [f"## {heading}\\n\\n{page_text}"]})
                    ),
                    request=request,
                )

            retry_transport = lambda **kwargs: SdkDefaultHttpxClient(
                transport=httpx.MockTransport(succeed_all_sections),
                **kwargs,
            )
            self._run_stage_b(
                root,
                manifest_path,
                http_client_factory=retry_transport,
                codex_home=codex_home,
            )
            self.assertEqual(1, len(retry_requests))
            self.assertIn("SECOND PAGE", retry_requests[0])
            self.assertTrue((output_dir / "section-list.json").exists())
            self.assertFalse(failure_path.exists())

    def test_stage_b_logs_html_section_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "codex-home"
            self._write_codex_home(codex_home)
            manifest_path = self._write_manifest(root, [self._html_section()])
            requests: list[dict] = []
            transport_factory = self._http_client_for_response(
                json.dumps({"pages": ["## Generated body\\n\\nSDK split output."]}),
                requests,
            )
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                self._run_stage_b(
                    root,
                    manifest_path,
                    http_client_factory=transport_factory,
                    codex_home=codex_home,
                )

        self.assertIn("1/1", stderr.getvalue())
        self.assertIn("1.1 SDK boundary", stderr.getvalue())
        self.assertIn("complete", stderr.getvalue().lower())

    def test_stage_b_reads_the_standard_codex_home_when_code_home_is_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / ".codex"
            self._write_codex_home(codex_home)
            manifest_path = self._write_manifest(root, [self._html_section()])
            requests: list[dict] = []
            transport_factory = self._http_client_for_response(
                json.dumps({"pages": ["## Body\n\nContent."]}),
                requests,
            )

            exit_code, _output_dir, report_path, _stdout, _payload = self._run_stage_b(
                root,
                manifest_path,
                http_client_factory=transport_factory,
                codex_home=None,
                default_home=root,
            )
            self.assertTrue(report_path.exists())

        self.assertEqual(0, exit_code)
        self.assertEqual(["gpt-6.1-terra"], [request["body"]["model"] for request in requests])

    def test_stage_b_requires_the_active_provider_key_and_ignores_legacy_command(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "codex-home"
            self._write_codex_home(codex_home, env_key="AZURE_OPENAI_API_KEY")
            manifest_path = self._write_manifest(root, [self._html_section()])

            def unexpected_http_client() -> None:
                raise AssertionError("the SDK must not be initialized without Codex auth")

            with self.assertRaisesRegex(RuntimeError, "AZURE_OPENAI_API_KEY") as error:
                self._run_stage_b(
                    root,
                    manifest_path,
                    http_client_factory=unexpected_http_client,
                    codex_home=codex_home,
                    extra_env={
                        "AZURE_OPENAI_API_KEY": "",
                        "NLM_HTML_PAGINATION_COMMAND": "missing-legacy-command",
                    },
                )

        self.assertNotIn("temporary-test-key", str(error.exception))

    def test_stage_b_does_not_use_auth_json_key_when_provider_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "codex-home"
            self._write_codex_home(codex_home, env_key="AZURE_OPENAI_API_KEY")
            manifest_path = self._write_manifest(root, [self._html_section()])

            def unexpected_http_client() -> None:
                raise AssertionError("the SDK must not be initialized with OAuth auth")

            with self.assertRaisesRegex(RuntimeError, "AZURE_OPENAI_API_KEY"):
                self._run_stage_b(
                    root,
                    manifest_path,
                    http_client_factory=unexpected_http_client,
                    codex_home=codex_home,
                    extra_env={"AZURE_OPENAI_API_KEY": ""},
                )

    def test_stage_b_rejects_invalid_active_provider_settings(self) -> None:
        cases = [
            ('model_provider = "missing"', "active Codex model provider"),
            ('wire_api = "unsupported"', "unsupported wire_api"),
        ]
        for replacement, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                codex_home = root / "codex-home"
                self._write_codex_home(codex_home)
                config_path = codex_home / "config.toml"
                config = config_path.read_text(encoding="utf-8")
                if replacement.startswith("model_provider"):
                    config = config.replace('model_provider = "azure"', replacement)
                else:
                    config = config.replace('wire_api = "responses"', replacement)
                config_path.write_text(config, encoding="utf-8")
                manifest_path = self._write_manifest(root, [self._html_section()])

                def unexpected_http_client() -> None:
                    raise AssertionError("unsupported provider settings must stop before the SDK")

                with self.assertRaisesRegex(RuntimeError, diagnostic):
                    self._run_stage_b(
                        root,
                        manifest_path,
                        http_client_factory=unexpected_http_client,
                        codex_home=codex_home,
                    )

    def test_stage_b_stops_on_sdk_and_page_validation_failures_without_exposing_key(self) -> None:
        cases = [
            ("SDK failure", RuntimeError("request failure: temporary-test-key"), "request failed"),
            ("empty text", "", "did not contain text output"),
            ("invalid JSON", "not JSON", "valid JSON"),
            ("invalid page", json.dumps({"pages": [""]}), "must not be empty"),
        ]
        for label, response_or_error, diagnostic in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                codex_home = root / "codex-home"
                self._write_codex_home(codex_home)
                manifest_path = self._write_manifest(root, [self._html_section()])

                requests: list[dict] = []
                transport_factory = self._http_client_for_response(response_or_error, requests)

                with self.assertRaisesRegex(RuntimeError, diagnostic) as error:
                    self._run_stage_b(
                        root,
                        manifest_path,
                        http_client_factory=transport_factory,
                        codex_home=codex_home,
                    )
                self.assertNotIn("temporary-test-key", str(error.exception))
                self.assertEqual(1, len(requests))
                self.assertFalse((root / "output" / "section-list.json").exists())

    def test_stage_b_does_not_call_sdk_for_imagesgallery_or_explicit_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manifest_path = self._write_manifest(
                root,
                [
                    {
                        "id": "1.1",
                        "title": "1.1 Gallery",
                        "block_type": "imagesgallery",
                        "content": "- 第 1 页\n\n## Gallery page\n\nGallery text.",
                    },
                    {
                        "id": "1.2",
                        "title": "1.2 Existing pages",
                        "block_type": "html",
                        "page_content": ["## Existing page\n\nExisting text."],
                        "page_count": 1,
                    },
                ],
            )

            def unexpected_http_client() -> None:
                raise AssertionError("non-HTML pagination paths must not initialize the SDK")

            exit_code, _output_dir, report_path, _stdout, payload = self._run_stage_b(
                root,
                manifest_path,
                http_client_factory=unexpected_http_client,
                codex_home=None,
            )
            self.assertTrue(report_path.exists())

        self.assertEqual(0, exit_code)
        self.assertEqual(["imagesgallery", "html"], [s["block_type"] for s in payload["sections"]])
        self.assertIn("Gallery text.", payload["sections"][0]["page_content"][1])
        self.assertIn("Existing text.", payload["sections"][1]["page_content"][1])


if __name__ == "__main__":
    unittest.main()
