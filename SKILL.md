---
name: nlm-course-slides
description: Run a staged, human-confirmed NotebookLM course-slides workflow from a local course JSON or a FIRA tenant/course pair. Use when Codex needs to fetch course JSON, list section resources, upload sections to NotebookLM, create one slide deck per section, monitor/rename decks, optionally download PPTX files, and summarize or retry only failed sections.
---

# NLM Course Slides

## Overview

默认路径不再是“一次性整课批处理”。先按阶段推进，并在关键节点由 Codex 向用户汇报、停顿、确认；脚本只负责产出阶段结果，不在脚本内部做交互式暂停。

本 skill 固定使用 CLI 和本地脚本链执行 NotebookLM 流程。不要在任何阶段询问用户选择 `MCP` 还是 `CLI`。阶段确认只用于决定是否继续，不用于决定执行通道。

默认流程分为 7 个阶段：

1. 阶段 A：获取课程 JSON 到本地
2. 阶段 B：解析 JSON，生成 section 清单
3. 阶段 C：串行上传资源到 NotebookLM
4. 阶段 D：串行发起 slide 创建请求
5. 阶段 E：轮询完成状态并重命名
6. 阶段 F：仅在用户要求时下载到本地
7. 阶段 G：汇总整体状态，并只对失败项继续处理

关键规则：

- 如果用户目标只是“下载课程 JSON”，阶段 A 完成即可结束。
- 如果用户目标还包括上传或生成 slides，阶段 A 完成后直接进入阶段 B，不在 A 后停顿确认。
- 第一次确认点固定在阶段 B 结束后。
- 默认执行通道固定为 CLI。除非用户明确要求别的方式，否则后续阶段一律沿用 CLI 脚本链。
- 用户未提供 `notebook_id` 时，在阶段 C 上传前自动创建 notebook，并把 notebook 标题与 notebook ID 汇报给用户。
- 现有 `generate_course_slides.py` / `generate_section_slides.py` 保留，但仅作为手动高级入口，不是默认推荐路径。

## Prerequisites

- 开始前确认 `nlm` 已安装并已登录；如有需要，运行 `nlm login`。
- 输入优先使用本地 JSON；如果用户给的是 `tenant_id + course_id`，先完成约定样例的 MCP 验证；验证通过后，再调用 `fira-insights` 的 `get_course_content_json`，并按下游用途显式传 `html_block_render_mode`：默认用 `markdown`，只有用户明确需要原始 HTML 标签时才用 `html`，再把返回的 `signed_url` 下载到本地课程目录。
- 课程结构解析继续沿用现有 manifest/FIRA 解析逻辑与 skip 规则；只有叶子 section 会进入后续阶段。
- 对 HTML xblock 为主的课程，只有在上面的 MCP 验证通过后，才把 `markdown` 渲染模式作为默认正式调用路径，让导出的 `text` 字段保留标题、强调、列表和段落结构；如果下游明确要消费原始标签，再切到 `html` 渲染模式；如果仍在验证前阶段，继续沿用旧路径或先停在验证环节。
- 阶段 B 里如果 section 走 `html` 路线，分页规则固定读取 [references/html-pagination-rules.md](references/html-pagination-rules.md)；它是 skill 自带的分页 prompt 资产，不要临时改用别的分页说明。
- 如果 manifest 结构不明确，读取 [references/manifest-format.md](references/manifest-format.md)。

## Default Workflow

### 阶段 A：获取课程 JSON

目标：确保课程 JSON 已经位于本地课程目录，并写出 `course-json-report.json`。

处理方式：

- 用户已经提供本地 JSON：直接复用并复制到课程目录。
- 用户提供 `tenant_id + course_id`：先确认 MCP 验证已通过，再按下游用途调用 `get_course_content_json`；默认传 `html_block_render_mode="markdown"`，只有明确需要原始 HTML 时才传 `html_block_render_mode="html"`，然后下载 `signed_url`。
- 阶段产物：
  - `course-json-report.json`
  - 课程目录内的 JSON 文件

命令：

```bash
python3 scripts/prepare_course_json.py \
  --source-file /path/to/course.json \
  --course-name "课程名称"
```

或：

```bash
python3 scripts/prepare_course_json.py \
  --signed-url "<signed_url>" \
  --course-name "课程名称" \
  --filename "课程内容.json"
```

执行规则：

- 如果用户的目标只是下载课程文件，到这里结束。
- 如果用户还要上传或生成 slides，不要在 A 后停顿，直接进入阶段 B。

### 阶段 B：生成 section 清单

目标：把后续会上传的 section 清晰列出来，并在这里进行第一次人工确认。

补充说明：

- 对 `imagesgallery` 路线，阶段 B 直接规范化已有分页文本。
- 对 `html` 路线，阶段 B 会读取 [references/html-pagination-rules.md](references/html-pagination-rules.md) 作为分页 prompt/规则资产，把 Markdown 教学正文切成 canonical page 内容，再写入 `section-list.json` 和对应 `.md` 文件。

命令：

```bash
python3 scripts/list_course_sections.py \
  /path/to/course.json
```

阶段产物：

- `section-list.json`

向用户汇报时至少展示：

- `section_id`
- `section_title`
- `resource_title`

执行规则：

- A 和 B 可以连续执行。
- B 结束后必须先汇报清单，再询问用户是否继续进入阶段 C。
- 默认确认话术应等价于：`阶段 A/B 已完成，是否继续进入阶段 C 上传？如果继续，我将直接按 CLI 流程进入阶段 C。`
- 不允许追加“如果继续，请告诉我用 MCP 还是 CLI”之类的工具选择问题。
- 未确认前不要进入上传阶段。

### 阶段 C：资源上传

目标：按 section 串行上传文本资源，并写出 `upload-report.json`。

命令：

```bash
python3 scripts/upload_course_sections.py \
  /path/to/course.json \
  --section-list /path/to/section-list.json \
  --notebook-id <existing-notebook-id>
```

如果没有 notebook ID，可省略 `--notebook-id`，脚本会创建 notebook：

```bash
python3 scripts/upload_course_sections.py \
  /path/to/course.json \
  --section-list /path/to/section-list.json
```

执行规则：

- 上传必须串行。
- 每节之间固定延迟 `5` 秒。
- 不并发上传。
- 本阶段固定沿用 CLI 脚本链执行，不重新选择工具方式。
- 如果脚本创建了 notebook，先把 notebook 标题和 notebook ID 汇报给用户，再继续说明上传结果。
- 如果用户提供了 notebook ID，也要先明确向用户汇报“将复用该 notebook”。
- 阶段结束后汇报成功项/失败项。
- 如果有失败项，询问用户是否只重试失败项。

阶段产物：

- `upload-report.json`

关键字段：

- `section_id`
- `section_title`
- `resource_title`
- `status`
- `source_id`
- `error`

仅重试失败项时使用：

```bash
python3 scripts/upload_course_sections.py \
  /path/to/course.json \
  --retry-failed-from-report /path/to/upload-report.json \
  --notebook-id <notebook-id>
```

### 阶段 D：发起 slide 创建

目标：只对已有 `source_id` 的 section 串行发起 `slides create` 请求，并写出 `create-report.json`。

命令：

```bash
python3 scripts/create_slides_from_sources.py \
  /path/to/course.json \
  --upload-report /path/to/upload-report.json
```

执行规则：

- `slides create` 必须串行调用。
- 每次 create 之间固定延迟 `10` 秒。
- 允许 notebook 内同时存在多个远端生成中的 slide。
- 本阶段固定沿用 CLI 脚本链执行，不重新选择工具方式。
- 如果某个 create 失败，先记录，不要在脚本内暂停。
- 阶段结束后汇报成功项/失败项。
- 如果有失败项，停下来询问用户是否只重试失败项。

阶段产物：

- `create-report.json`

关键字段：

- `section_id`
- `section_title`
- `source_id`
- `artifact_id`
- `status`
- `error`

仅重试失败项时使用：

```bash
python3 scripts/create_slides_from_sources.py \
  /path/to/course.json \
  --upload-report /path/to/upload-report.json \
  --retry-failed-from-report /path/to/create-report.json
```

### 阶段 E：轮询与重命名

目标：只针对已成功发起的 artifact 轮询远端状态；完成后立即重命名，并写出 `finalize-report.json`。

命令：

```bash
python3 scripts/monitor_and_finalize_slides.py \
  /path/to/course.json \
  --create-report /path/to/create-report.json
```

执行规则：

- 只针对已有 `artifact_id` 的 section。
- 状态轮询默认每 `60` 秒一次。
- 完成后立即重命名远端 slide。
- 本阶段固定沿用 CLI 脚本链执行，不重新选择工具方式。
- 阶段结束后汇报完成项、仍在运行项、失败项。

阶段产物：

- `finalize-report.json`

关键字段：

- `section_id`
- `artifact_id`
- `artifact_status`
- `renamed`
- `downloaded`
- `output_path`
- `error`

### 阶段 F：按需下载

目标：只有用户明确要求下载时，才把已完成的 slide deck 拉到本地。

命令：

```bash
python3 scripts/monitor_and_finalize_slides.py \
  /path/to/course.json \
  --create-report /path/to/create-report.json \
  --download
```

执行规则：

- 默认不下载。
- 只有用户明确要求“下载到本地”时才执行。
- 下载失败自动重试 `3` 次。
- 本阶段固定沿用 CLI 脚本链执行，不重新选择工具方式。
- 阶段结束后汇报下载结果。

### 阶段 G：总结与失败项再处理

目标：聚合各阶段 report，给出整体状态摘要，并指导下一步。

命令：

```bash
python3 scripts/summarize_course_slide_status.py \
  /path/to/course.json
```

阶段产物：

- `course-status-summary.json`

建议汇报内容：

- 每个 section 的当前综合状态
- 失败项列表
- 可重试项列表

结束时引导用户决定：

- 是否只对失败项重试
- 是否对已完成项执行下载
- 是否结束本轮

## Report Files

默认报告文件：

- `course-json-report.json`
- `section-list.json`
- `upload-report.json`
- `create-report.json`
- `finalize-report.json`
- `course-status-summary.json`

统一状态建议：

- `pending`
- `uploaded`
- `failed_upload`
- `create_requested`
- `failed_create`
- `running`
- `completed_remote`
- `renamed`
- `downloaded_local`
- `failed_download`

## Operational Rules

- 上传阶段：固定串行，固定 `5` 秒节流，不并发。
- 创建阶段：固定串行，固定 `10` 秒节流；允许多个远端 artifact 同时生成中。
- 轮询阶段：单独使用 `60` 秒轮询节奏。
- 下载阶段：默认关闭，仅用户明确要求时执行，失败自动重试 `3` 次。
- 失败项优先通过对应阶段 report 进行定向重试，不要默认整课重跑。
- 不要自动删除 notebook、source 或 artifact。

## Manual Advanced Entry

以下脚本仍然保留，适合用户明确要求“一次跑完”或做高级恢复操作时使用。它们仍然是 CLI 入口，不应触发额外的 MCP/CLI 选择确认：

- `scripts/generate_course_slides.py`
- `scripts/generate_section_slides.py`
- `scripts/generate_recovery_report.py`
- `scripts/verify_course_slides.py`

这些脚本不是默认推荐路径。默认应优先使用阶段 A-G。

## References

- [references/manifest-format.md](references/manifest-format.md)
- [references/html-pagination-rules.md](references/html-pagination-rules.md)
