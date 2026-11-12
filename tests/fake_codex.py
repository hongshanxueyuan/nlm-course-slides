from __future__ import annotations

import importlib
import json
import os
import re
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from openai import DefaultHttpxClient as SdkDefaultHttpxClient


sdk_httpx = importlib.import_module(
    SdkDefaultHttpxClient.__mro__[1].__module__.split(".", maxsplit=1)[0]
)


def plugin_output(pages: list[str]) -> dict:
    return {"pages": [{"content": page} for page in pages]}


def responses_api_payload(output_text: str) -> dict:
    try:
        parsed = json.loads(output_text)
    except (ValueError, TypeError):
        pass
    else:
        if isinstance(parsed, dict) and isinstance(parsed.get("pages"), list) and all(isinstance(page, str) for page in parsed["pages"]):
            output_text = json.dumps(plugin_output(parsed["pages"]), ensure_ascii=False)
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "gpt-6.1-terra",
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
                        "text": output_text,
                    }
                ],
            }
        ],
    }


def _fake_sdk_http_client(**kwargs):
    def send(request):
        request_payload = json.loads(request.content.decode("utf-8"))
        prompt = request_payload["input"][1]["content"]
        match = re.search(
            r"## 文档内容\s*```(?:markdown)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?\n\s*## 输出格式",
            prompt,
            flags=re.DOTALL,
        )
        teaching_text = match.group(1).strip() if match else "Fake pagination output."
        pages = [page.strip() for page in re.split(r"\n\n(?=## )", teaching_text) if page.strip()]
        response_text = json.dumps({"pages": pages}, ensure_ascii=False)
        return sdk_httpx.Response(
            200,
            json=responses_api_payload(response_text),
            request=request,
        )

    return SdkDefaultHttpxClient(
        transport=sdk_httpx.MockTransport(send),
        **kwargs,
    )


def _write_fake_codex_home(parent: Path) -> Path:
    codex_home = parent / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "temporary-test-key"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'model = "gpt-6-luna"\n'
        'model_provider = "azure"\n'
        "[model_providers.azure]\n"
        'base_url = "https://provider.example.test/v1"\n'
        'env_key = "AZURE_OPENAI_API_KEY"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )
    return codex_home


def run_stage_b_command(
    manifest_path: Path,
    output_dir: Path,
    report_path: Path,
    *,
    extra_args: list[str] | None = None,
) -> int:
    codex_home = _write_fake_codex_home(output_dir)
    argv = [
        "list_course_sections.py",
        str(manifest_path),
        "--output-dir",
        str(output_dir),
        "--report-path",
        str(report_path),
        *(extra_args or []),
    ]
    with (
        patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(codex_home),
                "AZURE_OPENAI_API_KEY": "temporary-azure-key",
            },
        ),
        patch("openai.DefaultHttpxClient", side_effect=_fake_sdk_http_client),
        patch.object(sys, "argv", argv),
        redirect_stdout(StringIO()),
    ):
        import list_course_sections

        return list_course_sections.main()
