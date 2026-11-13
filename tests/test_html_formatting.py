from __future__ import annotations

import io
import hashlib
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common
import html_pagination_worker
import list_course_sections
from fake_codex import (_fake_sdk_http_client, _write_fake_codex_home,
                        responses_api_payload, SdkDefaultHttpxClient, sdk_httpx)
from markdown_it import MarkdownIt


def splitting_client(*tokens, classifications=None, edit_page=None, requests=None):
    def client(**kwargs):
        def send(request):
            payload = json.loads(request.content.decode('utf8'))
            if requests is not None:
                requests.append(payload)
            prompt = payload['input'][1]['content']
            source = re.search(r'## 文档内容\s*```markdown\s*\n(.*?)\n```\s*\n\s*## 输出格式', prompt, re.S).group(1)
            pages, cursor = [], 0
            for token in tokens:
                pos = source.index(token, cursor)
                pages.append(source[cursor:pos].strip())
                cursor = pos
            pages.append(source[cursor:].strip())
            if edit_page:
                pages[0] = edit_page(pages[0])
            result = {'pages': [{'content': page} for page in pages]}
            if classifications is not None:
                result['heading_groups'] = classifications
            else:
                inventory = re.search(r'## 编号标题组判断[\s\S]*?\n(\[{"group_id":.*\])(?:\n|$)', prompt)
                if inventory:
                    result['heading_groups'] = [
                        {'group_id': group['group_id'], 'kind': 'topics'}
                        for group in json.loads(inventory.group(1))
                    ]
            return sdk_httpx.Response(200, json=responses_api_payload(json.dumps(result, ensure_ascii=False)), request=request)
        return SdkDefaultHttpxClient(transport=sdk_httpx.MockTransport(send), **kwargs)
    return client


class HtmlFormattingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = _write_fake_codex_home(self.root)

    def run_course(self, html, client=_fake_sdk_http_client):
        course = {'name': 'Course', 'chapters': [{'name': 'Chapter', 'sections': [
            {'name': 'Overview'}, {'name': '1.1 Section', 'verticals': [{'name': 'Lesson', 'blocks': [
                {'category': 'html', 'name': '文字讲解', 'text': html}]}]}]}]}
        path = self.root / 'course.json'
        path.write_text(json.dumps(course, ensure_ascii=False), encoding='utf8')
        output = self.root / 'out'
        argv = ['list_course_sections.py', str(path), '--output-dir', str(output)]
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex), 'AZURE_OPENAI_API_KEY': 'fake'}), \
             patch('openai.DefaultHttpxClient', side_effect=client), \
             patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            self.assertEqual(0, list_course_sections.main())
        section = json.loads((output / 'section-list.json').read_text(encoding='utf8'))['sections'][0]
        text = (output / section['md_name']).read_text(encoding='utf8')
        self.assertEqual(section['page_count'], len(section['page_content']))
        self.assertEqual(section['page_content'], common.parse_canonical_markdown_pages(text, section_title=section['section_title'], output_stem=Path(section['md_name']).stem))
        return section['page_content'][1:]

    def run_markdown(self, body, client=_fake_sdk_http_client):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps({'course_title': 'Course', 'sections': [
            {'id': '1', 'title': 'Section', 'block_type': 'html', 'content': body}]}, ensure_ascii=False), encoding='utf8')
        output = self.root / 'out'
        argv = ['list_course_sections.py', str(path), '--output-dir', str(output)]
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex), 'AZURE_OPENAI_API_KEY': 'fake'}), \
             patch('openai.DefaultHttpxClient', side_effect=client), \
             patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            self.assertEqual(0, list_course_sections.main())
        section = json.loads((output / 'section-list.json').read_text(encoding='utf8'))['sections'][0]
        markdown = (output / section['md_name']).read_text(encoding='utf8')
        self.assertEqual(section['page_count'], len(section['page_content']))
        self.assertEqual(section['page_content'], common.parse_canonical_markdown_pages(
            markdown, section_title=section['section_title'], output_stem=Path(section['md_name']).stem))
        return section['page_content'][1:]

    def test_chinese_numbered_breaks_render_as_list_items(self):
        pages = self.run_course('<h2>标题</h2><p>1、第一项<br>2、第二项<br>3、第三项</p>')
        rendered = MarkdownIt('commonmark').render(pages[0])
        self.assertEqual(3, rendered.count('<li>'))
        self.assertIn('第一项', rendered)
        self.assertIn('第二项', rendered)
        self.assertIn('第三项', rendered)

    def test_html_bold_with_punctuation_is_rendered_without_losing_other_bold(self):
        pages = self.run_course('<p>甲<strong>“重点”</strong>后续；<strong>正常</strong>，<code>**code**</code>。</p>')
        rendered = MarkdownIt('commonmark').render(pages[0])
        self.assertIn('<strong>“重点”</strong>', rendered)
        self.assertIn('<strong>正常</strong>', rendered)
        self.assertIn('<code>**code**</code>', rendered)

    def test_nested_html_bold_remains_readable_without_private_markers(self):
        pages = self.run_course('<p><strong>一个<strong>重点</strong>内容</strong></p>')
        rendered = MarkdownIt('commonmark').render(pages[0])
        self.assertIn('重点', rendered)
        self.assertNotIn('\ue000', pages[0])
        self.assertNotIn('\ue001', pages[0])

    def test_heading_numbers_use_parent_scope_and_nested_letters(self):
        pages = self.run_course(
            '<h2>上级一</h2><h3>1、父一</h3><h4>1、子甲</h4><ul><li>条目</li></ul>'
            '<h4>2、子乙</h4><p>内容</p><h3>2、父二</h3><h4>1、子丙</h4><p>内容</p>'
            '<h4>2、子丁</h4><p>内容</p><h2>上级二</h2><h3>1、唯一</h3>'
            '<p>1、操作一<br>2、操作二</p>'
        )
        joined = '\n'.join(pages)
        for heading in ['### 1、父一', '### 2、父二', '#### a、子甲', '#### b、子乙',
                        '#### a、子丙', '#### b、子丁', '### 唯一']:
            self.assertIn(heading, joined)
        self.assertIn('1. 操作一', joined)
        self.assertIn('2. 操作二', joined)

    def test_decimal_section_numbers_are_not_mistaken_for_heading_markers(self):
        pages = self.run_course('<h2>1.2 安全规程</h2><p>正文。</p>')
        self.assertIn('<h2>1.2 安全规程</h2>', MarkdownIt('commonmark').render(pages[0]))

    def test_numbered_prefix_wholly_bold_leaves_no_empty_emphasis(self):
        pages = self.run_course('<h2>上级</h2><h3><strong>1、</strong> 标题</h3><p>内容</p>')
        self.assertIn('### 标题', pages[0])
        self.assertNotIn('****', pages[0])

    def test_numbered_subheadings_are_preserved_for_referenced_procedure_steps(self):
        pages = self.run_course('<h2>主题</h2><h3>1、设备检查流程</h3>'
                                '<h4>1、先断电</h4><p>内容。</p><h4>2、再检查</h4><p>完成第2步后复位。</p>',
                                splitting_client(classifications=[{'group_id': 'h3-l4', 'kind': 'steps'}]))
        self.assertIn('#### 1、先断电', pages[0])
        self.assertIn('#### 2、再检查', pages[0])
        self.assertIn('第2步', pages[0])

    def test_numbered_action_sequence_is_preserved_without_explicit_step_words(self):
        body = ('## 开机\n\n### 1、设备准备\n\n#### 1、先断电\n\n#### 2、再检查'
                '\n\n### 2、后续作业')
        self.assertEqual([body], self.run_markdown(body, splitting_client(classifications=[{'group_id': 'h3-l4', 'kind': 'steps'}])))

    def test_model_identifies_unlisted_action_headings_as_steps(self):
        body = ('## 启动\n\n### 1、准备工作\n\n#### 1、校准\n\n#### 2、预热'
                '\n\n### 2、后续操作')
        requests = []
        client = splitting_client(classifications=[{'group_id': 'h3-l4', 'kind': 'steps'}], requests=requests)
        self.assertEqual([body], self.run_markdown(body, client))
        self.assertEqual(1, len(requests))
        self.assertIn(body, requests[0]['input'][1]['content'])
        self.assertIn('"group_id": "h3-l4"', requests[0]['input'][1]['content'])
        schema = requests[0]['text']['format']['schema']
        self.assertIn('heading_groups', schema['required'])
        self.assertEqual(['steps', 'topics', 'uncertain'], schema['properties']['heading_groups']['items']['properties']['kind']['enum'])

    def test_multiple_group_ids_are_applied_by_identity_not_model_order(self):
        body = ('## 主题\n\n### 1、设备准备\n\n#### 1、校准\n\n#### 2、预热'
                '\n\n### 2、风险类别\n\n#### 1、机械风险\n\n#### 2、电气风险')
        classifications = [{'group_id': 'h9-l4', 'kind': 'topics'}, {'group_id': 'h3-l4', 'kind': 'steps'}]
        expected = body.replace('#### 1、机械', '#### a、机械').replace('#### 2、电气', '#### b、电气')
        self.assertEqual([expected], self.run_markdown(body, splitting_client(classifications=classifications)))

    def test_no_candidates_keep_existing_pagination_contract(self):
        requests = []
        body = '## 主题\n\n### 1、唯一\n\n正文。'
        self.assertEqual(['## 主题\n\n### 唯一\n\n正文。'], self.run_markdown(body, splitting_client(requests=requests)))
        self.assertNotIn('heading_groups', requests[0]['text']['format']['schema']['properties'])
        self.assertNotIn('## 编号标题组判断', requests[0]['input'][1]['content'])

    def test_classification_covers_all_groups_and_rejects_uncertain_or_forged_ids(self):
        body = '## 主题\n\n### 1、分类\n\n#### 1、甲\n\n#### 2、乙\n\n### 2、其他'
        invalid = [
            [],
            [{'group_id': 'h3-l4', 'kind': 'uncertain'}],
            [{'group_id': 'h3-l4', 'kind': 'topics'}, {'group_id': 'h3-l4', 'kind': 'steps'}],
            [{'group_id': 'h999-l4', 'kind': 'steps'}],
            [{'group_id': 'h3-l4', 'kind': []}],
        ]
        for classifications in invalid:
            with self.subTest(classifications=classifications), self.assertRaisesRegex(RuntimeError, 'heading group'):
                self.run_markdown(body, splitting_client(classifications=classifications))
        self.assertFalse((self.root / 'out' / 'section-list.json').exists())

    def test_classification_transforms_siblings_across_pages_without_losing_repetitions(self):
        body = ('## 标题\n\n### 1、风险\n\n#### 1、机械风险\n\n重复说明。'
                '\n\n#### 2、电气风险\n\n重复说明。\n\n### 2、后续')
        pages = self.run_markdown(body, splitting_client('#### 2、电气风险',
            classifications=[{'group_id': 'h3-l4', 'kind': 'topics'}]))
        self.assertEqual(['## 标题\n\n### 1、风险\n\n#### a、机械风险\n\n重复说明。',
                          '#### b、电气风险\n\n重复说明。\n\n### 2、后续'], pages)
        self.assertEqual(2, '\n'.join(pages).count('重复说明。'))

    def test_numbered_headings_inside_a_quote_use_semantic_classification(self):
        body = ('## 主题\n\n> ### 1、风险分类\n>\n> #### 1、机械风险\n>\n'
                '> #### 2、电气风险\n\n### 2、后续')
        pages = self.run_markdown(body, splitting_client(classifications=[{'group_id': 'h3-l4', 'kind': 'topics'}]))
        self.assertEqual([body.replace('#### 1、机械', '#### a、机械').replace('#### 2、电气', '#### b、电气')], pages)

    def test_model_cannot_drop_or_rewrite_source_while_returning_classifications(self):
        body = '## 主题\n\n### 1、分类\n\n#### 1、甲\n\n重复。\n\n#### 2、乙\n\n重复。\n\n### 2、其他'
        client = splitting_client(classifications=[{'group_id': 'h3-l4', 'kind': 'topics'}],
                                  edit_page=lambda page: page.replace('重复。', '', 1))
        with self.assertRaisesRegex(RuntimeError, 'source passage|cover all source'):
            self.run_markdown(body, client)

    def test_page_boundary_cannot_split_a_heading_that_must_be_renumbered(self):
        body = '## 主题\n\n### 1、分类\n\n#### 1、机械风险\n\n#### 2、电气风险\n\n### 2、其他'
        client = splitting_client('风险\n\n#### 2', classifications=[{'group_id': 'h3-l4', 'kind': 'topics'}])
        with self.assertRaisesRegex(RuntimeError, 'split a numbered heading'):
            self.run_markdown(body, client)

    def test_imperative_action_headings_keep_numbers_without_sequence_adverbs(self):
        body = ('## 操作\n\n### 1、设备准备\n\n#### 1、打开阀门\n\n#### 2、确认压力'
                '\n\n#### 3、关闭阀门\n\n### 2、后续作业')
        self.assertEqual([body], self.run_markdown(body, splitting_client(classifications=[{'group_id': 'h3-l4', 'kind': 'steps'}])))

    def test_external_step_references_do_not_turn_categories_into_steps(self):
        body = ('## 主题\n\n### 1、风险分类\n\n#### 1、机械风险\n\n评估时参考第1步的结果。'
                '\n\n#### 2、电气风险\n\n评估时参考第2步的结果。\n\n### 2、责任分工')
        self.assertEqual(['## 主题\n\n### 1、风险分类\n\n#### a、机械风险\n\n评估时参考第1步的结果。'
                          '\n\n#### b、电气风险\n\n评估时参考第2步的结果。\n\n### 2、责任分工'], self.run_markdown(body))

    def test_parallel_concepts_still_use_letters_and_single_heading_loses_number(self):
        body = ('## 主题\n\n### 1、风险管理\n\n#### 1、检查方法\n\n正文提到先断电、再检查。'
                '\n\n#### 2、确认机制\n\n### 2、人员管理\n\n#### 1、培训安排\n\n正文。')
        pages = self.run_markdown(body)
        self.assertEqual(['## 主题\n\n### 1、风险管理\n\n#### a、检查方法\n\n正文提到先断电、再检查。'
                          '\n\n#### b、确认机制\n\n### 2、人员管理\n\n#### 培训安排\n\n正文。'], pages)

    def test_fence_inside_list_is_not_a_heading(self):
        body = '# Parent\n\n- ```text\n  ## 1. literal in code\n  ```'
        path = self.root / 'manifest.json'
        path.write_text(json.dumps({'course_title': 'Course', 'sections': [
            {'id': '1', 'title': 'Section', 'block_type': 'html', 'content': body}]}, ensure_ascii=False), encoding='utf8')
        output = self.root / 'out'
        argv = ['list_course_sections.py', str(path), '--output-dir', str(output)]
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex), 'AZURE_OPENAI_API_KEY': 'fake'}), \
             patch('openai.DefaultHttpxClient', side_effect=_fake_sdk_http_client), \
             patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            self.assertEqual(0, list_course_sections.main())
        section = json.loads((output / 'section-list.json').read_text(encoding='utf8'))['sections'][0]
        self.assertIn('  ## 1. literal in code', '\n'.join(section['page_content']))

    def test_top_level_numbered_heading_is_not_an_isolated_subheading(self):
        pages = self.run_course('<h2>1、章节标题</h2><p>正文。</p>')
        self.assertIn('## 1、章节标题', pages[0])

    def test_single_nested_heading_loses_its_marker_but_steps_keep_theirs(self):
        pages = self.run_course('<h2>上级</h2><h3>1、父标题</h3><h4>1、唯一子标题</h4>'
                                '<p>1、第一步<br>2、第二步</p>')
        self.assertIn('### 父标题', pages[0])
        self.assertIn('#### 唯一子标题', pages[0])
        self.assertIn('1. 第一步', pages[0])
        self.assertIn('2. 第二步', pages[0])

    def test_standard_ordered_list_is_preserved(self):
        pages = self.run_course('<h2>标题</h2><ol><li>第一项</li><li>第二项</li></ol>')
        rendered = MarkdownIt('commonmark').render(pages[0])
        self.assertEqual(2, rendered.count('<li>'))
        self.assertIn('<ol>', rendered)

    def test_letter_subheadings_and_nested_list_keep_visual_hierarchy(self):
        pages = self.run_course('<h2>主题</h2><h3>1、父标题</h3>'
                                '<h4>1、子甲</h4><ul><li>父项<ul><li>子项</li></ul></li></ul>'
                                '<h4>2、子乙</h4><p>内容。</p>')
        rendered = MarkdownIt('commonmark').render(pages[0])
        self.assertIn('<h4>a、子甲</h4>', rendered)
        self.assertIn('<h4>b、子乙</h4>', rendered)
        self.assertRegex(rendered, r'<li>父项\s*<ul>\s*<li>子项</li>')

    def test_a_numbered_list_starting_after_one_can_follow_an_intro_line(self):
        pages = self.run_course('<p>说明：<br>2、第二项<br>3、第三项</p>')
        rendered = MarkdownIt('commonmark').render(pages[0])
        self.assertEqual(2, rendered.count('<li>'))
        self.assertIn('<ol start="2">', rendered)

    def test_uncertain_bold_and_fenced_code_are_preserved_without_a_report(self):
        body = '## 标题\n\n**正常**和 **未配对\n\n\\*\\*转义\\*\\*\n\n```text\n~~~\n### 1、代码\n```'
        path = self.root / 'manifest.json'
        path.write_text(json.dumps({'course_title': 'Course', 'sections': [
            {'id': '1', 'title': 'Section', 'block_type': 'html', 'content': body}]}, ensure_ascii=False), encoding='utf8')
        output = self.root / 'out'
        argv = ['list_course_sections.py', str(path), '--output-dir', str(output)]
        errors = io.StringIO()
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex), 'AZURE_OPENAI_API_KEY': 'fake'}), \
             patch('openai.DefaultHttpxClient', side_effect=_fake_sdk_http_client), \
             patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()), redirect_stderr(errors):
            self.assertEqual(0, list_course_sections.main())
        section = json.loads((output / 'section-list.json').read_text(encoding='utf8'))['sections'][0]
        self.assertEqual(body, section['page_content'][1])
        self.assertFalse((output / 'section-list-failure.json').exists())
        self.assertNotIn('bold', errors.getvalue().lower())

    def test_split_avoidance_guide_is_rejected_even_if_source_coverage_is_complete(self):
        with self.assertRaisesRegex(RuntimeError, '避坑指南'):
            self.run_course('<h2>避坑指南：四个坑</h2><p>先看重点。</p>'
                            '<ol><li>第一坑</li><li>第二坑</li></ol><hr><p>总结。</p>', splitting_client('2. 第二坑'))
        self.assertFalse((self.root / 'out' / 'section-list.json').exists())

    def test_inline_guide_can_be_separate_from_previous_topic_and_summary(self):
        pages = self.run_course('<h2>主题</h2><p>之前的主题。</p><p><strong>避坑指南</strong>：</p>'
                                '<ul><li>第一坑</li><li>第二坑</li></ul><hr><p>全节总结。</p>',
                                splitting_client('**避坑指南**', '---'))
        self.assertEqual(3, len(pages))
        self.assertIn('之前的主题', pages[0])
        self.assertIn('第一坑', pages[1])
        self.assertIn('第二坑', pages[1])
        self.assertIn('全节总结', pages[2])

    def test_inline_guide_does_not_absorb_a_later_unrelated_paragraph(self):
        pages = self.run_course('<h2>主题</h2><p><strong>避坑指南</strong>：</p>'
                                '<ul><li>第一坑</li><li>第二坑</li></ul><p>后续主题。</p>',
                                splitting_client('后续主题'))
        self.assertEqual(2, len(pages))
        self.assertIn('第二坑', pages[0])
        self.assertIn('后续主题', pages[1])

    def test_guide_can_be_followed_by_separate_summary_heading(self):
        pages = self.run_course('<h2>避坑指南</h2><p>说明。</p><ol><li>第一个</li><li>第二个</li></ol>'
                                '<h3>本节总结</h3><p>总结内容。</p>', splitting_client('### 本节总结'))
        self.assertEqual(2, len(pages))
        self.assertIn('第二个', pages[0])
        self.assertIn('本节总结', pages[1])

    def test_labeled_summary_heading_can_start_a_new_page(self):
        pages = self.run_course('<h2>避坑指南</h2><p>说明。</p><ol><li>第一个</li><li>第二个</li></ol>'
                                '<h3>本节总结：关键要点</h3><p>总结内容。</p>', splitting_client('### 本节总结'))
        self.assertEqual(2, len(pages))

    def test_bracketed_and_quoted_guide_labels_protect_following_lists(self):
        for label in ('<p>【避坑指南】</p>', '<blockquote>避坑指南</blockquote>'):
            with self.subTest(label=label):
                html = '<h2>主题</h2>' + label + '<ul><li>第一坑</li><li>第二坑</li></ul>'
                with self.assertRaisesRegex(RuntimeError, '避坑指南'):
                    self.run_course(html, splitting_client('* 第二坑'))

    def test_guide_note_with_parenthesis_and_quoted_list_cannot_be_split(self):
        for html, token in (
            ('<h2>主题</h2><p>避坑指南（请注意）：</p><ul><li>第一坑</li><li>第二坑</li></ul>', '* 第二坑'),
            ('<h2>主题</h2><blockquote><p>避坑指南：</p><ul><li>第一坑</li><li>第二坑</li></ul></blockquote>', '> * 第二坑'),
        ):
            with self.subTest(html=html), self.assertRaisesRegex(RuntimeError, '避坑指南'):
                self.run_course(html, splitting_client(token))

    def test_quoted_guide_heading_and_dash_introduction_cannot_be_split(self):
        samples = (
            ('<blockquote><h3>避坑指南：注意事项</h3><p>引导</p>'
             '<ul><li>第一坑</li><li>第二坑</li></ul></blockquote>', '> * 第二坑'),
            ('<p>避坑指南——请注意：</p><ul><li>第一坑</li><li>第二坑</li></ul>', '* 第二坑'),
        )
        for html, token in samples:
            with self.subTest(html=html), self.assertRaisesRegex(RuntimeError, '避坑指南'):
                self.run_course(html, splitting_client(token))

    def test_ordinary_quoted_heading_remains_splittable(self):
        pages = self.run_course('<blockquote><h3>一般指南</h3><p>引导</p>'
                                '<ul><li>第一项</li><li>第二项</li></ul></blockquote>',
                                splitting_client('> * 第二项'))
        self.assertEqual(2, len(pages))
        self.assertIn('第一项', pages[0])
        self.assertIn('第二项', pages[1])

    def test_quoted_guide_does_not_absorb_following_unquoted_topic(self):
        pages = self.run_course('<blockquote><h3>避坑指南</h3><p>引导</p>'
                                '<ul><li>第一坑</li><li>第二坑</li></ul></blockquote><p>后续主题</p>',
                                splitting_client('后续主题'))
        self.assertEqual(2, len(pages))
        self.assertIn('第二坑', pages[0])
        self.assertIn('后续主题', pages[1])

    def test_bold_inline_guide_with_colon_inside_bold_cannot_split_its_list(self):
        html = '<h2>主题</h2><p><strong>避坑指南：</strong></p><ul><li>第一坑</li><li>第二坑</li></ul>'
        with self.assertRaisesRegex(RuntimeError, '避坑指南'):
            self.run_course(html, splitting_client('* 第二坑'))

    def test_long_guide_stays_on_one_page_and_reports_crowding(self):
        html = '<h2>避坑指南</h2><p>引导。</p><ol><li>' + '长内容' * 600 + '</li><li>第二坑</li></ol>'
        logs = io.StringIO()
        with redirect_stderr(logs):
            pages = self.run_course(html)
        self.assertEqual(1, len(pages))
        self.assertIn('第二坑', pages[0])
        self.assertIn('may crowd', logs.getvalue())

    def test_siblings_keep_numbers_when_model_places_them_on_separate_pages(self):
        pages = self.run_course('<h2>上级</h2><h3>1、第一标题</h3><p>第一内容</p>'
                                '<h3>2、第二标题</h3><p>第二内容</p>', splitting_client('### 2、'))
        self.assertIn('### 1、第一标题', pages[0])
        self.assertIn('### 2、第二标题', pages[1])

    def test_worker_result_with_split_guide_is_not_reused(self):
        html = '<h2>避坑指南</h2><p>引导。</p><ol><li>第一坑</li><li>第二坑</li></ol>'
        self.run_course(html)
        source = self.root / 'course.json'
        workers = self.root / 'workers'
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex), 'AZURE_OPENAI_API_KEY': 'fake'}), \
             patch('openai.DefaultHttpxClient', side_effect=_fake_sdk_http_client):
            html_pagination_worker.run_job(source, workers, 1)
            path = workers / 'section-0001.result.json'
            payload = json.loads(path.read_text(encoding='utf8'))
            text = payload['pages'][0]
            pos = text.index('2. 第二坑')
            payload['pages'] = [text[:pos].strip(), text[pos:].strip()]
            payload['page_count'] = 2
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf8')
            self.assertEqual('pending', html_pagination_worker.list_jobs(source, workers)['jobs'][0]['status'])
            with self.assertRaisesRegex(RuntimeError, 'Missing or stale'):
                html_pagination_worker.apply_results(common.load_course_manifest(source), workers)

    def test_tampered_cache_cannot_bypass_guide_validation(self):
        html = '<h2>避坑指南</h2><p>引导。</p><ol><li>第一坑</li><li>第二坑</li></ol>'
        self.run_course(html)
        cache_path = self.root / 'out' / '.html-pagination-cache.json'
        cache = json.loads(cache_path.read_text(encoding='utf8'))
        entry = next(iter(cache['sections'].values()))
        text = entry['model_output']['pages'][0]['content']
        pos = text.index('2. 第二坑')
        entry['model_output']['pages'] = [{'content': text[:pos].strip()}, {'content': text[pos:].strip()}]
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf8')
        pages = self.run_course(html)
        self.assertEqual(1, len(pages))
        self.assertIn('第一坑', pages[0])
        self.assertIn('第二坑', pages[0])

    def test_invalid_cached_classification_is_recomputed(self):
        body = '## 主题\n\n### 1、准备\n\n#### 1、校准\n\n#### 2、预热\n\n### 2、后续'
        classifications = [{'group_id': 'h3-l4', 'kind': 'steps'}]
        self.assertEqual([body], self.run_markdown(body, splitting_client(classifications=classifications)))
        cache_path = self.root / 'out' / '.html-pagination-cache.json'
        cache = json.loads(cache_path.read_text(encoding='utf8'))
        entry = next(iter(cache['sections'].values()))
        entry['model_output']['heading_groups'][0]['group_id'] = 'forged'
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf8')
        calls = []
        def client(**kwargs):
            calls.append(1)
            return splitting_client(classifications=classifications)(**kwargs)
        self.assertEqual([body], self.run_markdown(body, client))
        self.assertEqual(1, len(calls))

    def test_worker_revalidates_classification_and_reconstructs_final_pages(self):
        body = '## 主题\n\n### 1、准备\n\n#### 1、校准\n\n#### 2、预热\n\n### 2、后续'
        classifications = [{'group_id': 'h3-l4', 'kind': 'steps'}]
        self.run_markdown(body, splitting_client(classifications=classifications))
        source = self.root / 'manifest.json'
        workers = self.root / 'workers'
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex), 'AZURE_OPENAI_API_KEY': 'fake'}), \
             patch('openai.DefaultHttpxClient', side_effect=splitting_client(classifications=classifications)):
            html_pagination_worker.run_job(source, workers, 1)
            result_path = workers / 'section-0001.result.json'
            model_path = workers / 'section-0001.model.json'
            result = json.loads(result_path.read_text(encoding='utf8'))
            model_bytes = model_path.read_bytes()
            self.assertEqual([body], result['pages'])
            output = self.root / 'aggregated'
            argv = ['list_course_sections.py', str(source), '--output-dir', str(output),
                    '--pagination-results-dir', str(workers)]
            with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
                self.assertEqual(0, list_course_sections.main())
            report = json.loads((output / 'section-list.json').read_text(encoding='utf8'))['sections'][0]
            self.assertEqual(['# Section', body], report['page_content'])

            forged = json.loads(model_bytes)
            forged['heading_groups'][0]['group_id'] = 'forged'
            model_path.write_text(json.dumps(forged, ensure_ascii=False), encoding='utf8')
            result['model_output_sha256'] = hashlib.sha256(model_path.read_bytes()).hexdigest()
            result_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf8')
            self.assertEqual('pending', html_pagination_worker.list_jobs(source, workers)['jobs'][0]['status'])
            with self.assertRaisesRegex(RuntimeError, 'Missing or stale'):
                html_pagination_worker.apply_results(common.load_course_manifest(source), workers)

            model_path.write_bytes(model_bytes)
            result['model_output_sha256'] = hashlib.sha256(model_bytes).hexdigest()
            result['pages'] = [body.replace('1、校准', 'a、校准').replace('2、预热', 'b、预热')]
            result_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf8')
            self.assertEqual('pending', html_pagination_worker.list_jobs(source, workers)['jobs'][0]['status'])
            with self.assertRaisesRegex(RuntimeError, 'Missing or stale'):
                html_pagination_worker.apply_results(common.load_course_manifest(source), workers)

    def test_worker_reuses_semantic_topics_after_deterministic_renumbering(self):
        body = '## 主题\n\n### 1、分类\n\n#### 1、机械风险\n\n#### 2、电气风险\n\n### 2、后续'
        expected = body.replace('#### 1、机械', '#### a、机械').replace('#### 2、电气', '#### b、电气')
        self.run_markdown(body)
        source = self.root / 'manifest.json'
        workers = self.root / 'workers'
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex), 'AZURE_OPENAI_API_KEY': 'fake'}), \
             patch('openai.DefaultHttpxClient', side_effect=_fake_sdk_http_client):
            self.assertEqual('complete', html_pagination_worker.run_job(source, workers, 1)['status'])
            result = json.loads((workers / 'section-0001.result.json').read_text(encoding='utf8'))
            raw = json.loads((workers / 'section-0001.model.json').read_text(encoding='utf8'))
            self.assertEqual([expected], result['pages'])
            self.assertEqual(body, raw['pages'][0]['content'])
            self.assertEqual('topics', raw['heading_groups'][0]['kind'])
            with patch('openai.DefaultHttpxClient', side_effect=AssertionError('unexpected model call')):
                self.assertEqual('cached', html_pagination_worker.run_job(source, workers, 1)['status'])
                output = self.root / 'aggregated'
                argv = ['list_course_sections.py', str(source), '--output-dir', str(output),
                        '--pagination-results-dir', str(workers)]
                with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
                    self.assertEqual(0, list_course_sections.main())
            report = json.loads((output / 'section-list.json').read_text(encoding='utf8'))['sections'][0]
            self.assertEqual(['# Section', expected], report['page_content'])
