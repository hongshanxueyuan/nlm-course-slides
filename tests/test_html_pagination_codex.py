from __future__ import annotations

import io
import importlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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

import common  # noqa: E402
import list_course_sections  # noqa: E402


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
    ) -> None:
        codex_home.mkdir(parents=True)
        codex_home.joinpath("auth.json").write_text(
            json.dumps({"auth_mode": auth_mode, "OPENAI_API_KEY": "temporary-test-key"}),
            encoding="utf-8",
        )
        codex_home.joinpath("config.toml").write_text(
            'model = "gpt-6-luna"\n'
            'model_provider = "azure"\n'
            "[model_providers.azure]\n"
            'base_url = "https://provider.example.test/v1"\n'
            f'wire_api = "{wire_api}"\n',
            encoding="utf-8",
        )

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
        env = {"CODEX_HOME": str(codex_home) if codex_home else ""}
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
                json={
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": "gpt-5.4",
                    "output": [
                        {
                            "id": "msg_test",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "annotations": [],
                                    "text": response_or_error,
                                }
                            ],
                        }
                    ],
                },
                request=request,
            )

        return lambda: SdkDefaultHttpxClient(transport=httpx.MockTransport(send))

    @staticmethod
    def _html_section(content: str = "## Teaching\n\nRendered course text.") -> dict:
        return {
            "id": "1.1",
            "title": "1.1 SDK boundary",
            "block_type": "html",
            "content": content,
        }

    def test_stage_b_uses_codex_provider_and_pins_gpt_54(self) -> None:
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
        self.assertEqual("Bearer temporary-test-key", requests[0]["authorization"])
        self.assertEqual("gpt-5.4", requests[0]["body"]["model"])
        self.assertIn("Rendered course text.", requests[0]["body"]["input"])
        self.assertEqual(
            ["# SDK boundary", "## Generated body\n\nSDK split output."],
            section["page_content"],
        )
        self.assertEqual(section["page_count"], len(section["page_content"]))
        self.assertIn("- 第 1 页", markdown)
        self.assertIn("- 第 2 页", markdown)
        self.assertNotIn("temporary-test-key", stdout + markdown + report_json)

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
        self.assertEqual(["gpt-5.4"], [request["body"]["model"] for request in requests])

    def test_stage_b_fails_without_codex_auth_and_ignores_legacy_command(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "empty-codex-home"
            codex_home.mkdir()
            manifest_path = self._write_manifest(root, [self._html_section()])

            def unexpected_http_client() -> None:
                raise AssertionError("the SDK must not be initialized without Codex auth")

            with self.assertRaisesRegex(RuntimeError, "Codex API-key authentication") as error:
                self._run_stage_b(
                    root,
                    manifest_path,
                    http_client_factory=unexpected_http_client,
                    codex_home=codex_home,
                    extra_env={"NLM_HTML_PAGINATION_COMMAND": "missing-legacy-command"},
                )

        self.assertNotIn("temporary-test-key", str(error.exception))

    def test_stage_b_rejects_non_api_key_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_home = root / "codex-home"
            self._write_codex_home(codex_home, auth_mode="oauth")
            manifest_path = self._write_manifest(root, [self._html_section()])

            def unexpected_http_client() -> None:
                raise AssertionError("the SDK must not be initialized with OAuth auth")

            with self.assertRaisesRegex(RuntimeError, "requires API-key authentication"):
                self._run_stage_b(
                    root,
                    manifest_path,
                    http_client_factory=unexpected_http_client,
                    codex_home=codex_home,
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
