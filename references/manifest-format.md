# Manifest Format

Use `generate_course_slides.py` and `verify_course_slides.py` with one of these manifest formats.

## FIRA Course JSON

The scripts can read the exported FIRA course JSON directly.

- Root title: `name`
- Chapters: `chapters`
- Section title: `chapters[].sections[].name`
- Slide source text: `chapters[].sections[].verticals[].blocks[]` where `name == "文字讲解"`

Built-in filtering rules for this format:

- Skip the chapter named `训战启程`
- Skip the chapter named `训战总结、训战输出`
- Skip the chapter named `满意度调查`
- Skip the first section of every remaining chapter, which is usually the `概述`
- Prefer `文字讲解` blocks under non-`训战` verticals; only fall back to other `文字讲解` blocks if needed

Example:

```bash
python3 scripts/generate_course_slides.py /path/to/fira_course.json
```

## Preferred JSON / YAML

Use JSON or YAML when the course has nested chapters or when each section needs its own `focus`, `output_name`, or `resource_title`.

```yaml
course_title: 医学导论
sections:
  - id: 1
    title: 医疗领域介绍
    content: |
      这里放这一节的正文内容。
    focus: 医疗领域介绍
  - title: 临床流程概览
    sections:
      - id: 2.1
        title: 分诊
        resource_title: 2.1 分诊
        output_name: 2.1 分诊.pptx
        content: |
          这里放分诊的内容。
```

Supported section fields:

- `id`: explicit section id; otherwise the script uses `1`, `1.1`, `1.2`, ...
- `title` or `name`: section title
- `content`, `text`, `body`, or `markdown`: section body sent to NotebookLM
- `focus` or `prompt`: slide generation focus
- `course_scene`: selects the course-wide slide prompt. Supported values are `企业流程与智能化场景` and `企业出海场景`. If omitted, the skill infers the scene from the course title; section-level `focus` takes precedence.
- `resource_title` or `source_title`: NotebookLM source title
- `output_name` or `filename`: output PPTX filename
- `sections` or `children`: nested subsections

Only entries that include actual content become slide-generation tasks.

## Markdown Outline

Use Markdown for a simple linear course. The parser treats the first `#` heading as the course title and each `##` heading as one section. Everything until the next `##` heading becomes the section content.

```markdown
# 医学导论

## 医疗领域介绍
这里放第一节内容。

## 临床流程概览
这里放第二节内容。
```

Markdown mode is intentionally simple. If you need nested sections or custom file names, switch to JSON or YAML.
