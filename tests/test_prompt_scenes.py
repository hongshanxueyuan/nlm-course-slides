from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (  # noqa: E402
    COURSE_SCENE_OVERSEAS,
    COURSE_SCENE_STANDARD,
    build_focus_prompt,
    load_course_manifest,
)


class PromptSceneTests(unittest.TestCase):
    def assert_shared_constraints(self, prompt: str) -> None:
        for required in (
            "1:1 逐页强对应法则",
            "严禁合并与拆分",
            "严禁自行增删页面",
            "第 1 页固定为封面页",
            "演示文稿只使用中文",
            "任何幻灯片可见文字中都不得出现",
        ):
            with self.subTest(required=required):
                self.assertIn(required, prompt)
        self.assertNotIn("页数要求：10-16页", prompt)
        self.assertNotIn("演示文稿以中文为主", prompt)

    def test_outbound_course_title_selects_outbound_prompt(self) -> None:
        prompt = build_focus_prompt(
            "3.3 社交的尺度：距离把控与禁忌规避",
            course_title="出海攻心为上：【埃及】商业互信与职场协同实战",
        )
        self.assertIn("目标受众：中国出海企业员工", prompt)
        self.assertIn("文稿题目：社交的尺度：距离把控与禁忌规避", prompt)
        self.assert_shared_constraints(prompt)

    def test_explicit_course_scene_selects_outbound_prompt(self) -> None:
        prompt = build_focus_prompt(
            "4.2 激励的锚点：核心驱动力排序与应用",
            course_scene=COURSE_SCENE_OVERSEAS,
        )
        self.assertIn("目标受众：中国出海企业员工", prompt)
        self.assert_shared_constraints(prompt)

    def test_manifest_course_scene_is_preserved(self) -> None:
        payload = {
            "course_title": "出海攻心为上：【埃及】商业互信与职场协同实战",
            "course_scene": COURSE_SCENE_OVERSEAS,
            "sections": [
                {"id": "1.1", "title": "1.1 测试章节", "content": "课程正文"}
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "section-list.json"
            manifest_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            manifest = load_course_manifest(manifest_path)

        self.assertEqual(manifest.course_scene, COURSE_SCENE_OVERSEAS)

    def test_other_courses_keep_process_template_with_shared_constraints(self) -> None:
        prompt = build_focus_prompt(
            "1.1 流程治理基础",
            course_title="企业流程智能化管理基础",
        )
        self.assertIn("流程与智能化中的企业领导者", prompt)
        self.assert_shared_constraints(prompt)
        self.assertEqual(COURSE_SCENE_STANDARD, "企业流程与智能化场景")


if __name__ == "__main__":
    unittest.main()
