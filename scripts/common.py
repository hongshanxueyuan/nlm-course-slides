#!/usr/bin/env python3
"""
Shared helpers for the nlm-course-slides skill.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast


class _FcntlLike(Protocol):
    LOCK_EX: int
    LOCK_UN: int

    def flock(self, fd: int, op: int) -> None: ...

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback
    class _FcntlCompat:
        LOCK_EX = 0
        LOCK_UN = 0

        @staticmethod
        def flock(_fd: int, _op: int) -> None:
            return None

    fcntl: _FcntlLike = _FcntlCompat()
else:
    fcntl = cast(_FcntlLike, _fcntl)


UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
SECTION_ID_PREFIX_RE = re.compile(r"^\s*([一二三四五六七八九十百千万零两\d]+(?:[.\-、]\d+)*)\s+")

SECTION_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[一二三四五六七八九十百千万零两\d]+\s*[章节讲部分单元课]|"
    r"[一二三四五六七八九十百千万零两\d]+(?:[.\-、]\d+)*[.\-、)]?|"
    r"\(\d+(?:\.\d+)*\)|"
    r"（\d+(?:\.\d+)*）"
    r")\s*"
)

COURSE_SCENE_STANDARD = "企业流程与智能化场景"
COURSE_SCENE_OVERSEAS = "企业出海场景"
COURSE_SCENE_ALIASES = {
    COURSE_SCENE_STANDARD: COURSE_SCENE_STANDARD,
    COURSE_SCENE_OVERSEAS: COURSE_SCENE_OVERSEAS,
    "标准": COURSE_SCENE_STANDARD,
    "企业流程与智能化": COURSE_SCENE_STANDARD,
    "出海": COURSE_SCENE_OVERSEAS,
    "企业出海": COURSE_SCENE_OVERSEAS,
}

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
FOCUS_PROMPT_TEMPLATE_FILES = {
    COURSE_SCENE_STANDARD: PROMPTS_DIR / "企业流程与智能化场景.txt",
    COURSE_SCENE_OVERSEAS: PROMPTS_DIR / "企业出海场景.txt",
}


def _load_focus_prompt_template(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"Missing focus prompt template: {path}")
    template = path.read_text(encoding="utf-8").strip()
    if not template:
        raise RuntimeError(f"Focus prompt template is empty: {path}")
    return template


FOCUS_PROMPT_TEMPLATES = {
    scene: _load_focus_prompt_template(path)
    for scene, path in FOCUS_PROMPT_TEMPLATE_FILES.items()
}

FIRA_SKIP_CHAPTER_NAMES = {"训战启程", "训战总结、训战输出", "满意度调查"}
PROMPT_TITLE_RE = re.compile(r"文稿题目：([^\n\r]+)")
NLM_API_DELAY_SECONDS = 15.0
NLM_STATUS_API_DELAY_SECONDS = 60.0
NLM_RATE_LIMIT_MAX_DELAY_SECONDS = 240.0
NLM_RATE_LIMIT_MAX_RETRIES = 4
NLM_RATE_LIMIT_LOCK_PATH = Path(tempfile.gettempdir()) / "nlm-course-slides.rate-limit.lock"
NLM_STATUS_RATE_LIMIT_LOCK_PATH = Path(tempfile.gettempdir()) / "nlm-course-slides.status-rate-limit.lock"
PAGE_MARKER_RE = re.compile(r"^\s*-\s*第\s*(\d+)\s*页\s*$", re.MULTILINE)
HTML_TAG_RE = re.compile(r"<[A-Za-z/!][^>]*>")
EMPTY_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s*$")
SUMMARY_HEADING_TEXTS = {"本节要点", "总结", "复盘"}
HTML_PAGINATION_MAX_PAGES = 20
HTML_PAGINATION_COMMAND_ENV = "NLM_HTML_PAGINATION_COMMAND"
HTML_PAGINATION_RULES_PATH = (
    Path(__file__).resolve().parent.parent / "references" / "html-pagination-rules.md"
)


@dataclass
class Section:
    id: str
    order: int
    title: str
    content: str
    slug: str
    output_name: str
    focus: str | None = None
    resource_title: str | None = None
    md_name: str | None = None
    block_type: str | None = None
    page_content: list[str] | None = None
    page_count: int | None = None
    source_blocks: list[SectionSourceBlock] | None = None


@dataclass
class SectionSourceBlock:
    category: str
    name: str
    text: str
    vertical_name: str | None = None


@dataclass
class CourseManifest:
    course_title: str
    sections: list[Section]
    source_path: str
    course_scene: str = COURSE_SCENE_STANDARD
    source_kind: str = "generic"
    default_block_type: str = "html"


@dataclass
class RecoverySectionState:
    section_id: str
    section_title: str
    resource_title: str
    output_name: str
    status: str
    source_id: str | None
    artifact_id: str | None
    artifact_status: str | None
    local_output_exists: bool
    local_sidecar_exists: bool
    resume_action: str
    notes: list[str]


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str, *, fallback: str = "section") -> str:
    text = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[-\s]+", "-", text, flags=re.UNICODE).strip("-")
    return text or fallback


def sanitize_filename(value: str, *, fallback: str = "section") -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "-", value, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE).strip(" .")
    return text or fallback


def default_output_name(section_id: str, title: str) -> str:
    clean_title = sanitize_filename(title)
    if title_starts_with_section_id(title, section_id):
        return f"{clean_title}.pptx"
    return f"{section_id} {clean_title}.pptx"


def default_md_name(output_name: str) -> str:
    return f"{Path(output_name).stem}.md"


def default_resource_title(section_id: str, title: str) -> str:
    if title_starts_with_section_id(title, section_id):
        return title.strip()
    return f"{section_id} {title.strip()}"


def extract_section_id(title: str) -> str | None:
    match = SECTION_ID_PREFIX_RE.match(title or "")
    if not match:
        return None
    return match.group(1).rstrip(".-、)")


def title_starts_with_section_id(title: str, section_id: str) -> bool:
    extracted = extract_section_id(title)
    return bool(extracted and extracted == section_id)


def strip_section_prefix(title: str) -> str:
    cleaned = title.strip()
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = SECTION_PREFIX_RE.sub("", cleaned).strip()
    return cleaned or title.strip()


def normalize_course_scene(course_scene: str | None) -> str:
    normalized = str(course_scene or COURSE_SCENE_STANDARD).strip()
    if normalized in COURSE_SCENE_ALIASES:
        return COURSE_SCENE_ALIASES[normalized]
    raise RuntimeError(
        f"Unsupported course_scene '{normalized}'. Expected one of: "
        f"{', '.join(FOCUS_PROMPT_TEMPLATES)}"
    )


def build_focus_prompt(title: str, *, course_scene: str | None = None) -> str:
    resolved_course_scene = normalize_course_scene(course_scene)
    template = FOCUS_PROMPT_TEMPLATES[resolved_course_scene]
    return template.format(section_title=strip_section_prefix(title))


def resolve_section_focus(
    section: Section,
    *,
    course_scene: str | None = None,
    explicit_focus: str | None = None,
) -> str:
    return str(
        explicit_focus
        or section.focus
        or build_focus_prompt(section.title, course_scene=course_scene)
    ).strip()


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_empty_heading_blocks(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n{2,}", text) if block.strip()]
    kept = [block for block in blocks if not EMPTY_MARKDOWN_HEADING_RE.fullmatch(block)]
    return "\n\n".join(kept).strip()


def load_html_pagination_rules() -> str:
    if not HTML_PAGINATION_RULES_PATH.exists():
        raise RuntimeError(
            f"Missing html pagination rules reference: {HTML_PAGINATION_RULES_PATH}"
        )
    rules = HTML_PAGINATION_RULES_PATH.read_text(encoding="utf-8").strip()
    if not rules:
        raise RuntimeError(
            f"html pagination rules reference is empty: {HTML_PAGINATION_RULES_PATH}"
        )
    return rules


def _clean_page_body(text: str) -> str:
    normalized = _normalize_newlines(text or "").strip()
    if HTML_TAG_RE.search(normalized):
        raise RuntimeError(
            "Canonical Markdown must not contain raw HTML tags; use markdown-rendered teaching text"
        )
    normalized = _strip_empty_heading_blocks(normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _normalize_explicit_page_content(page_content: list[str]) -> list[str]:
    pages: list[str] = []
    for index, page in enumerate(page_content, start=1):
        if not isinstance(page, str):
            raise RuntimeError(f"page_content[{index - 1}] must be a string")
        normalized = _clean_page_body(page)
        if PAGE_MARKER_RE.search(normalized):
            raise RuntimeError("page_content entries must not contain page marker lines")
        if not normalized:
            raise RuntimeError(f"page_content[{index - 1}] must not be empty")
        pages.append(normalized)
    if not pages:
        raise RuntimeError("page_content must contain at least one page")
    return pages


def _heading_level(block: str) -> int | None:
    first_line = block.splitlines()[0].strip()
    match = re.match(r"^(#{1,6})\s+(.+)$", first_line)
    if not match:
        return None
    return len(match.group(1))


def _heading_text(block: str) -> str | None:
    first_line = block.splitlines()[0].strip()
    match = re.match(r"^#{1,6}\s+(.+)$", first_line)
    if not match:
        return None
    return match.group(1).strip()


def _is_summary_block(block: str) -> bool:
    title = _heading_text(block)
    return bool(title and title in SUMMARY_HEADING_TEXTS)


def _split_markdown_blocks(text: str) -> list[str]:
    return [block.strip() for block in re.split(r"\n{2,}", text) if block.strip()]


def _group_blocks_into_units(blocks: list[str]) -> list[str]:
    units: list[str] = []
    current: list[str] = []
    for block in blocks:
        level = _heading_level(block)
        if current and (level is not None and level <= 2):
            units.append("\n\n".join(current).strip())
            current = [block]
            continue
        if current and _is_summary_block(block):
            units.append("\n\n".join(current).strip())
            current = [block]
            continue
        current.append(block)
    if current:
        units.append("\n\n".join(current).strip())
    return [unit for unit in units if unit]


def _split_long_unit(unit: str, *, max_chars: int = 1800) -> list[str]:
    if len(unit) <= max_chars:
        return [unit]

    blocks = _split_markdown_blocks(unit)
    split_units: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in blocks:
        level = _heading_level(block)
        next_len = current_len + len(block)
        should_split = current and (
            (level is not None and level >= 3 and current_len >= max_chars // 2)
            or (next_len > max_chars and current_len >= max_chars // 2)
        )
        if should_split:
            split_units.append("\n\n".join(current).strip())
            current = [block]
            current_len = len(block)
            continue
        current.append(block)
        current_len = next_len + 2
    if current:
        split_units.append("\n\n".join(current).strip())
    return [split_unit for split_unit in split_units if split_unit]


def _chunk_units(units: list[str], chunk_count: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    total_units = len(units)
    for remaining_chunks in range(chunk_count, 0, -1):
        remaining_units = total_units - start
        size = math.ceil(remaining_units / remaining_chunks)
        chunk_units = units[start : start + size]
        chunks.append("\n\n".join(chunk_units).strip())
        start += size
    return [chunk for chunk in chunks if chunk]


def _collapse_units_to_page_limit(
    units: list[str],
    *,
    max_pages: int,
    preserve_summary_page: bool,
) -> list[str]:
    if len(units) <= max_pages:
        return [unit.strip() for unit in units if unit.strip()]

    keep_summary = preserve_summary_page and bool(units) and _is_summary_block(units[-1])
    main_units = units[:-1] if keep_summary else units
    page_slots = max_pages - 1 if keep_summary else max_pages
    if page_slots <= 0:
        raise RuntimeError(f"Could not fit paginated content within {max_pages} pages")

    pages = _chunk_units(main_units, page_slots)
    if keep_summary:
        pages.append(units[-1].strip())
    return [page for page in pages if page]


def _requires_summary_tail_preservation(rules: str) -> bool:
    return all(
        fragment in rules
        for fragment in (
            "本节要点",
            "总结",
            "复盘",
            "最后一页",
        )
    )


def build_html_pagination_prompt(text: str, *, rules: str) -> str:
    if "{text}" in rules:
        return rules.replace("{text}", text)
    return "\n\n".join([rules, text])


def _extract_json_payload(raw_output: str) -> str:
    stripped = raw_output.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _parse_html_pagination_model_output(raw_output: str) -> list[str]:
    payload_text = _extract_json_payload(raw_output)
    if not payload_text:
        raise RuntimeError("html pagination model returned empty output")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("html pagination model must return valid JSON") from exc

    if isinstance(payload, dict):
        pages = payload.get("pages")
    else:
        pages = payload
    if not isinstance(pages, list):
        raise RuntimeError("html pagination model must return a JSON list or an object with a 'pages' list")
    return _normalize_explicit_page_content(pages)


def _run_html_pagination_model(prompt: str) -> list[str] | None:
    command = os.environ.get(HTML_PAGINATION_COMMAND_ENV, "").strip()
    if not command:
        return None

    args = shlex.split(command, posix=os.name != "nt")
    result = subprocess.run(
        args,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"html pagination model command failed with exit code {result.returncode}: {stderr or 'no stderr'}"
        )
    return _parse_html_pagination_model_output(result.stdout)


def _paginate_markdown_content_with_rules(text: str, *, rules: str) -> list[str]:
    normalized = _clean_page_body(text)
    if not normalized:
        raise RuntimeError("html route requires non-empty Markdown-rendered teaching text")

    blocks = _split_markdown_blocks(normalized)
    units = _group_blocks_into_units(blocks)
    expanded_units: list[str] = []
    for unit in units:
        expanded_units.extend(_split_long_unit(unit))

    pages = _collapse_units_to_page_limit(
        expanded_units,
        max_pages=HTML_PAGINATION_MAX_PAGES,
        preserve_summary_page=_requires_summary_tail_preservation(rules),
    )
    normalized_pages = [_clean_page_body(page) for page in pages if _clean_page_body(page)]
    if not normalized_pages:
        raise RuntimeError("html pagination did not produce any pages")
    if len(normalized_pages) > HTML_PAGINATION_MAX_PAGES:
        raise RuntimeError("html pagination exceeded the 20-page contract")
    return normalized_pages


def paginate_markdown_content(text: str) -> list[str]:
    rules = load_html_pagination_rules()
    normalized = _clean_page_body(text)
    if not normalized:
        raise RuntimeError("html route requires non-empty Markdown-rendered teaching text")

    prompt = build_html_pagination_prompt(normalized, rules=rules)
    model_pages = _run_html_pagination_model(prompt)
    if model_pages is not None:
        if len(model_pages) > HTML_PAGINATION_MAX_PAGES:
            raise RuntimeError("html pagination exceeded the 20-page contract")
        return model_pages
    return _paginate_markdown_content_with_rules(normalized, rules=rules)


def _strip_legacy_preface(
    text: str,
    *,
    section_title: str,
    output_stem: str,
) -> str:
    match = PAGE_MARKER_RE.search(text)
    if not match:
        return text

    preface = text[: match.start()].strip()
    if not preface:
        return text

    preface_lines = [re.sub(r"^#{1,6}\s*", "", line).strip() for line in preface.splitlines()]
    preface_lines = [line for line in preface_lines if line]
    if len(preface_lines) != 1:
        return text

    normalized_preface = preface_lines[0]
    valid_titles = {
        section_title.strip(),
        strip_section_prefix(section_title).strip(),
        output_stem.strip(),
    }
    if normalized_preface in valid_titles:
        return text[match.start() :].lstrip()
    return text


def parse_canonical_markdown_pages(
    text: str,
    *,
    section_title: str,
    output_stem: str,
    allow_legacy_preface: bool = False,
) -> list[str]:
    normalized = _normalize_newlines(text or "").strip()
    if allow_legacy_preface:
        normalized = _strip_legacy_preface(
            normalized,
            section_title=section_title,
            output_stem=output_stem,
        )

    matches = list(PAGE_MARKER_RE.finditer(normalized))
    if not matches:
        raise RuntimeError("Canonical Markdown must contain at least one page marker")
    if normalized[: matches[0].start()].strip():
        raise RuntimeError("Canonical Markdown must begin with the page-1 marker")

    pages: list[str] = []
    for index, match in enumerate(matches):
        expected_page_number = index + 1
        actual_page_number = int(match.group(1))
        if actual_page_number != expected_page_number:
            raise RuntimeError(
                f"Canonical Markdown page markers must be sequential; expected page {expected_page_number} but found page {actual_page_number}"
            )
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        body = _clean_page_body(normalized[start:end])
        if not body:
            raise RuntimeError(f"Page {index + 1} must not be empty")
        if PAGE_MARKER_RE.search(body):
            raise RuntimeError("Nested page markers are not allowed inside page bodies")
        pages.append(body)
    return pages


def serialize_canonical_markdown(page_content: list[str]) -> str:
    pages = _normalize_explicit_page_content(page_content)
    rendered_pages = [
        f"- 第 {index} 页\n\n{page}"
        for index, page in enumerate(pages, start=1)
    ]
    return "\n\n".join(rendered_pages).strip() + "\n"


def _collect_fira_source_blocks(section_data: dict[str, Any]) -> list[SectionSourceBlock]:
    source_blocks: list[SectionSourceBlock] = []
    for vertical in section_data.get("verticals") or []:
        if not isinstance(vertical, dict):
            continue
        vertical_name = str(vertical.get("name") or "").strip() or None
        for block in vertical.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or "").strip()
            category = str(block.get("category") or "").strip()
            name = str(block.get("name") or "").strip()
            if not text or not category:
                continue
            source_blocks.append(
                SectionSourceBlock(
                    category=category,
                    name=name,
                    text=text,
                    vertical_name=vertical_name,
                )
            )
    return source_blocks


def _select_fira_blocks(
    source_blocks: list[SectionSourceBlock],
    *,
    category: str,
) -> list[SectionSourceBlock]:
    preferred = [
        block
        for block in source_blocks
        if block.category == category and "训战" not in (block.vertical_name or "")
    ]
    if preferred:
        return preferred
    return [block for block in source_blocks if block.category == category]


def _extract_fira_section_content(
    section_data: dict[str, Any],
    *,
    course_block_type: str,
) -> tuple[str, list[SectionSourceBlock]]:
    source_blocks = _collect_fira_source_blocks(section_data)
    if course_block_type == "imagesgallery":
        gallery_blocks = _select_fira_blocks(source_blocks, category="imagesgallery")
        if gallery_blocks:
            return gallery_blocks[0].text.strip(), source_blocks

    html_blocks = _select_fira_blocks(source_blocks, category="html")
    html_texts = [block.text.strip() for block in html_blocks if block.text.strip()]
    return "\n\n".join(html_texts).strip(), source_blocks


def _fira_course_has_imagesgallery(chapters: list[Any]) -> bool:
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        chapter_name = str(chapter.get("name") or "").strip()
        if chapter_name in FIRA_SKIP_CHAPTER_NAMES:
            continue
        chapter_sections = chapter.get("sections") or []
        if not isinstance(chapter_sections, list):
            continue
        for index, section_data in enumerate(chapter_sections):
            if index == 0 or not isinstance(section_data, dict):
                continue
            source_blocks = _collect_fira_source_blocks(section_data)
            if _select_fira_blocks(source_blocks, category="imagesgallery"):
                return True
    return False


def _resolve_section_block_type(section: Section, *, default_block_type: str) -> str:
    block_type = (section.block_type or default_block_type or "html").strip()
    if block_type not in {"imagesgallery", "html"}:
        raise RuntimeError(
            f"Unsupported block_type '{block_type}' for section {section.id}"
        )
    return block_type


def _resolve_section_md_name(section: Section) -> str:
    md_name = str(section.md_name or default_md_name(section.output_name)).strip()
    if not md_name.lower().endswith(".md"):
        raise RuntimeError(f"md_name must end with .md: {md_name}")
    if Path(md_name).stem != Path(section.output_name).stem:
        raise RuntimeError(
            f"md_name '{md_name}' must share the same stem as output_name '{section.output_name}'"
        )
    return md_name


def _select_imagesgallery_source_text(section: Section) -> str:
    if section.source_blocks:
        gallery_blocks = _select_fira_blocks(section.source_blocks, category="imagesgallery")
        if gallery_blocks:
            return gallery_blocks[0].text.strip()
    if section.content and PAGE_MARKER_RE.search(section.content):
        return section.content.strip()
    raise RuntimeError(
        f"Section {section.id} is on the imagesgallery route but has no usable imagesgallery text"
    )


def _select_html_source_text(section: Section) -> str:
    if section.source_blocks:
        html_blocks = _select_fira_blocks(section.source_blocks, category="html")
        html_texts = [_clean_page_body(block.text) for block in html_blocks if block.text.strip()]
        html_texts = [text for text in html_texts if text]
        if html_texts:
            return "\n\n".join(html_texts).strip()
    if section.content.strip():
        return _clean_page_body(section.content)
    raise RuntimeError(
        f"Section {section.id} is on the html route but has no usable teaching text"
    )


def materialize_section_markdown(
    section: Section,
    *,
    output_dir: Path,
    default_block_type: str,
) -> Path:
    block_type = _resolve_section_block_type(section, default_block_type=default_block_type)
    md_name = _resolve_section_md_name(section)

    if section.page_content is not None:
        pages = _normalize_explicit_page_content(section.page_content)
    elif block_type == "imagesgallery":
        pages = parse_canonical_markdown_pages(
            _select_imagesgallery_source_text(section),
            section_title=section.title,
            output_stem=Path(md_name).stem,
            allow_legacy_preface=True,
        )
    else:
        pages = paginate_markdown_content(_select_html_source_text(section))

    if section.page_count is not None and section.page_count != len(pages):
        raise RuntimeError(
            f"page_count {section.page_count} does not match len(page_content) {len(pages)} for section {section.id}"
        )
    if block_type == "html" and len(pages) > HTML_PAGINATION_MAX_PAGES:
        raise RuntimeError(
            f"html pagination exceeded the 20-page contract for section {section.id}"
        )

    md_path = output_dir / md_name
    md_path.write_text(serialize_canonical_markdown(pages), encoding="utf-8")

    section.md_name = md_name
    section.block_type = block_type
    section.page_content = pages
    section.page_count = len(pages)
    return md_path


def materialize_markdown_artifacts(
    manifest: CourseManifest,
    *,
    output_dir: Path,
) -> None:
    for section in manifest.sections:
        materialize_section_markdown(
            section,
            output_dir=output_dir,
            default_block_type=manifest.default_block_type,
        )


def build_section_list_entry(section: Section) -> dict[str, Any]:
    if section.md_name is None or section.block_type is None:
        raise RuntimeError(
            f"Section {section.id} is missing canonical Markdown metadata"
        )
    if section.page_content is None or section.page_count is None:
        raise RuntimeError(
            f"Section {section.id} is missing page_content/page_count metadata"
        )

    return {
        "section_id": section.id,
        "section_title": section.title,
        "resource_title": section.resource_title or default_resource_title(section.id, section.title),
        "output_name": section.output_name,
        "md_name": section.md_name,
        "block_type": section.block_type,
        "page_content": section.page_content,
        "page_count": section.page_count,
    }


def load_section_list_metadata_map(path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(path)
    metadata: dict[str, dict[str, Any]] = {}
    for item in payload.get("sections", []):
        if not isinstance(item, dict):
            continue
        section_id = item.get("section_id")
        if isinstance(section_id, str):
            metadata[section_id] = item
    return metadata


def load_canonical_markdown_body(
    section: Section,
    *,
    output_dir: Path,
    metadata: dict[str, Any] | None = None,
    default_block_type: str,
) -> tuple[str, str, str]:
    md_name = str((metadata or {}).get("md_name") or section.md_name or default_md_name(section.output_name)).strip()
    block_type = str((metadata or {}).get("block_type") or section.block_type or default_block_type).strip()
    if block_type not in {"imagesgallery", "html"}:
        raise RuntimeError(f"Unsupported block_type '{block_type}' for section {section.id}")
    if Path(md_name).stem != Path(section.output_name).stem:
        raise RuntimeError(
            f"md_name '{md_name}' must share the same stem as output_name '{section.output_name}'"
        )

    md_path = output_dir / md_name
    if not md_path.exists():
        raise FileNotFoundError(f"Canonical Markdown is missing: {md_path}")

    body = md_path.read_text(encoding="utf-8")
    pages = parse_canonical_markdown_pages(
        body,
        section_title=section.title,
        output_stem=Path(md_name).stem,
        allow_legacy_preface=False,
    )
    if block_type == "html" and len(pages) > HTML_PAGINATION_MAX_PAGES:
        raise RuntimeError(
            f"Canonical Markdown exceeded the 20-page contract for section {section.id}"
        )
    return md_name, block_type, body


def parse_first_uuid(text: str) -> str:
    match = UUID_RE.search(text)
    if not match:
        raise RuntimeError(f"Could not find UUID in command output:\n{text}")
    return match.group(0)


def log_message(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def configure_nlm_api_delay(delay_seconds: float) -> None:
    global NLM_API_DELAY_SECONDS
    NLM_API_DELAY_SECONDS = max(0.0, delay_seconds)


def _is_nlm_command(args: list[str]) -> bool:
    return bool(args) and Path(args[0]).name == "nlm"


def _read_rate_limit_state(handle: Any, *, base_delay: float) -> dict[str, float]:
    handle.seek(0)
    raw_value = handle.read().strip()
    if not raw_value:
        return {"last_started_at": 0.0, "current_delay": base_delay}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        try:
            return {
                "last_started_at": float(raw_value),
                "current_delay": base_delay,
            }
        except ValueError:
            return {"last_started_at": 0.0, "current_delay": base_delay}
    if isinstance(payload, (int, float)):
        return {
            "last_started_at": float(payload),
            "current_delay": base_delay,
        }
    if not isinstance(payload, dict):
        return {"last_started_at": 0.0, "current_delay": base_delay}
    return {
        "last_started_at": float(payload.get("last_started_at") or 0.0),
        "current_delay": max(
            base_delay,
            float(payload.get("current_delay") or base_delay),
        ),
    }


def _write_rate_limit_state(handle: Any, state: dict[str, float]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(state, ensure_ascii=False))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


def _rate_limit_delay_markers() -> list[str]:
    return [
        "rate limited",
        "rate limit",
        "api error (code 8)",
        "wait a few minutes before retrying",
        "too many requests",
    ]


def _is_rate_limit_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _rate_limit_delay_markers())


def _is_status_poll_command(args: list[str]) -> bool:
    return args[:4] == ["nlm", "studio", "status", "--json"] or args[:4] == [
        "nlm",
        "status",
        "artifacts",
        "--json",
    ] or args[:4] == ["nlm", "list", "artifacts", "--json"]


def _rate_limit_bucket(args: list[str]) -> tuple[Path, float]:
    if _is_status_poll_command(args):
        return NLM_STATUS_RATE_LIMIT_LOCK_PATH, NLM_STATUS_API_DELAY_SECONDS
    return NLM_RATE_LIMIT_LOCK_PATH, NLM_API_DELAY_SECONDS


def _rate_limit_nlm_command(args: list[str]) -> None:
    if not _is_nlm_command(args) or NLM_API_DELAY_SECONDS <= 0:
        return

    lock_path, base_delay = _rate_limit_bucket(args)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = _read_rate_limit_state(handle, base_delay=base_delay)
        last_started_at = state["last_started_at"]
        current_delay = max(base_delay, state["current_delay"])
        now = time.time()
        delay = max(0.0, last_started_at + current_delay - now)
        if delay > 0:
            log_message(
                f"[rate-limit] wait {delay:.1f}s before NotebookLM API call: {shlex.join(args)}"
            )
            time.sleep(delay)
        state["last_started_at"] = time.time()
        _write_rate_limit_state(handle, state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _update_rate_limit_delay(
    args: list[str],
    *,
    multiplier: float | None = None,
    reset: bool = False,
) -> float:
    lock_path, base_delay = _rate_limit_bucket(args)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = _read_rate_limit_state(handle, base_delay=base_delay)
        if reset:
            state["current_delay"] = base_delay
        elif multiplier is not None:
            state["current_delay"] = min(
                NLM_RATE_LIMIT_MAX_DELAY_SECONDS,
                max(base_delay, state["current_delay"]) * multiplier,
            )
        _write_rate_limit_state(handle, state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return state["current_delay"]


def run_cmd(
    args: list[str],
    *,
    dry_run: bool = False,
    cwd: str | None = None,
    capture_output: bool = True,
    check: bool = True,
) -> str:
    command = shlex.join(args)
    if dry_run:
        log_message(f"[dry-run] {command}")
        return ""

    attempts = 0
    while True:
        _rate_limit_nlm_command(args)
        completed = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=capture_output,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode == 0:
            if _is_nlm_command(args):
                _update_rate_limit_delay(args, reset=True)
            return stdout if capture_output else ""

        error_text = (
            f"Command failed with exit code {completed.returncode}: {command}\n"
            f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )
        if _is_nlm_command(args) and _is_rate_limit_error(error_text) and attempts < NLM_RATE_LIMIT_MAX_RETRIES:
            attempts += 1
            new_delay = _update_rate_limit_delay(args, multiplier=2.0)
            log_message(
                f"[rate-limit] NotebookLM rate limit on attempt {attempts} for {command}; "
                f"increase shared delay to {new_delay:.1f}s and retry"
            )
            continue

        if check:
            raise RuntimeError(error_text)
        return stdout if capture_output else ""


def ensure_authenticated(*, profile: str | None = None, dry_run: bool = False) -> None:
    if dry_run:
        args = ["nlm", "login", "--check"]
        if profile:
            args.extend(["--profile", profile])
        run_cmd(args, dry_run=True)
        return

    candidate_commands: list[list[str]] = [
        ["nlm", "login", "--check"],
        ["nlm", "auth", "status"],
    ]
    if profile:
        for command in candidate_commands:
            command.extend(["--profile", profile])

    unsupported_markers = [
        "Try 'nlm --help' for help.",
        "No such command",
        "No such option",
        "Usage: nlm [OPTIONS] COMMAND [ARGS]...",
    ]
    last_error: RuntimeError | None = None

    for args in candidate_commands:
        _rate_limit_nlm_command(args)
        completed = subprocess.run(
            args,
            text=True,
            capture_output=True,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode == 0:
            return

        combined = f"{stdout}\n{stderr}"
        if completed.returncode == 2 and any(marker in combined for marker in unsupported_markers):
            continue

        last_error = RuntimeError(
            f"Authentication check failed with exit code {completed.returncode}: {shlex.join(args)}\n"
            f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )
        break

    if last_error:
        raise last_error

    raise RuntimeError(
        "Could not find a supported NotebookLM authentication check command. "
        "This script supports `nlm login --check` and `nlm auth status`."
    )


def create_notebook(
    title: str,
    *,
    profile: str | None = None,
    dry_run: bool = False,
) -> str:
    args = ["nlm", "notebook", "create", title]
    if profile:
        args.extend(["--profile", profile])
    output = run_cmd(args, dry_run=dry_run)
    return "dry-run-notebook-id" if dry_run else parse_first_uuid(output)


def add_text_source(
    notebook_id: str,
    title: str,
    text: str,
    *,
    profile: str | None = None,
    dry_run: bool = False,
    wait_timeout: float = 600.0,
) -> str:
    args = [
        "nlm",
        "add",
        "text",
        "--title",
        title,
        "--wait",
        "--wait-timeout",
        str(wait_timeout),
    ]
    if profile:
        args.extend(["--profile", profile])
    args.extend([notebook_id, text])
    output = run_cmd(args, dry_run=dry_run)
    return "dry-run-source-id" if dry_run else parse_first_uuid(output)


def create_slide_deck(
    notebook_id: str,
    *,
    source_id: str,
    focus: str | None,
    language: str,
    deck_format: str,
    length: str,
    profile: str | None = None,
    dry_run: bool = False,
) -> str:
    args = [
        "nlm",
        "slides",
        "create",
        "--format",
        deck_format,
        "--length",
        length,
        "--language",
        language,
        "--source-ids",
        source_id,
        "--confirm",
    ]
    if focus:
        args.extend(["--focus", focus])
    if profile:
        args.extend(["--profile", profile])
    args.append(notebook_id)
    output = run_cmd(args, dry_run=dry_run)
    return "dry-run-artifact-id" if dry_run else parse_first_uuid(output)


def wait_for_artifact(
    notebook_id: str,
    artifact_id: str,
    *,
    profile: str | None = None,
    dry_run: bool = False,
    timeout_seconds: float = 1800.0,
    poll_interval: float = 30.0,
) -> dict[str, Any]:
    if dry_run:
        return {"id": artifact_id, "status": "completed", "type": "slide_deck"}

    deadline = time.time() + timeout_seconds
    status_commands = [
        ["nlm", "studio", "status", "--json"],
        ["nlm", "status", "artifacts", "--json"],
        ["nlm", "list", "artifacts", "--json"],
    ]
    if profile:
        for command in status_commands:
            command.extend(["--profile", profile])
    for command in status_commands:
        command.append(notebook_id)

    transient_markers = [
        "could not retrieve studio status.",
        "timed out",
        "timeout",
        "temporarily unavailable",
    ]
    transient_failures = 0

    while time.time() < deadline:
        saw_transient_error = False

        for args in status_commands:
            _rate_limit_nlm_command(args)
            completed = subprocess.run(
                args,
                text=True,
                capture_output=True,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            combined = f"{stdout}\n{stderr}".lower()

            if completed.returncode != 0:
                if any(marker in combined for marker in transient_markers):
                    saw_transient_error = True
                    continue
                raise RuntimeError(
                    f"Command failed with exit code {completed.returncode}: {shlex.join(args)}\n"
                    f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                )

            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid JSON from {shlex.join(args)}:\n{stdout}"
                ) from exc

            if isinstance(payload, dict) and payload.get("status") == "error":
                message = str(payload.get("error") or payload)
                if any(marker in message.lower() for marker in transient_markers):
                    saw_transient_error = True
                    continue
                raise RuntimeError(
                    f"Status query returned an error for artifact {artifact_id}: {message}"
                )

            if not isinstance(payload, list):
                raise RuntimeError(
                    f"Unexpected status payload from {shlex.join(args)}: {json.dumps(payload, ensure_ascii=False)}"
                )

            for item in payload:
                if item.get("id") != artifact_id:
                    continue
                status = item.get("status")
                if status == "completed":
                    return item
                if status in {"failed", "cancelled", "canceled"}:
                    raise RuntimeError(
                        f"Artifact {artifact_id} ended with status '{status}': {json.dumps(item, ensure_ascii=False)}"
                    )
                break

            # Successfully queried artifact list. If the target artifact is not
            # completed yet, wait for the next poll instead of trying fallback
            # commands in the same cycle.
            break

        if saw_transient_error:
            transient_failures += 1
            log_message(
                f"[artifact {artifact_id}] status query temporarily unavailable "
                f"(consecutive={transient_failures}), retry in {poll_interval} seconds"
            )
        else:
            transient_failures = 0
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Timed out waiting for artifact {artifact_id} after {timeout_seconds} seconds."
    )


def rename_artifact(
    artifact_id: str,
    new_title: str,
    *,
    profile: str | None = None,
    dry_run: bool = False,
) -> None:
    args = ["nlm", "rename", "studio"]
    if profile:
        args.extend(["--profile", profile])
    args.extend([artifact_id, new_title])
    run_cmd(args, dry_run=dry_run)


def download_slide_deck(
    notebook_id: str,
    artifact_id: str,
    output_path: Path,
    *,
    file_format: str = "pptx",
    retry_attempts: int = 3,
    retry_delay_seconds: float = 5.0,
    dry_run: bool = False,
) -> None:
    args = [
        "nlm",
        "download",
        "slide-deck",
        "--id",
        artifact_id,
        "--output",
        str(output_path),
        "--format",
        file_format,
        notebook_id,
    ]
    if dry_run:
        run_cmd(args, dry_run=True)
        return

    attempts = 0
    while True:
        try:
            run_cmd(args, dry_run=False)
            return
        except RuntimeError as exc:
            if attempts >= retry_attempts:
                raise
            attempts += 1
            log_message(
                f"[download {artifact_id}] download failed on attempt {attempts}: {exc}. "
                f"Retry in {retry_delay_seconds:.0f}s"
            )
            time.sleep(retry_delay_seconds)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_json_output(output: str, *, command: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {command}:\n{output}") from exc


def _first_non_empty(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _coerce_title(item: dict[str, Any]) -> str:
    value = _first_non_empty(
        item,
        [
            "title",
            "name",
            "display_name",
            "displayName",
            "artifact_title",
            "artifactTitle",
            "source_title",
            "sourceTitle",
        ],
    )
    if value in (None, "", [], {}):
        custom_instructions = str(item.get("custom_instructions") or "").strip()
        match = PROMPT_TITLE_RE.search(custom_instructions)
        if match:
            value = match.group(1).strip()
    return str(value or "").strip()


def _coerce_id(item: dict[str, Any]) -> str | None:
    value = _first_non_empty(item, ["id", "artifact_id", "artifactId", "source_id", "sourceId", "uuid"])
    if value is None:
        return None
    return str(value).strip() or None


def _coerce_status(item: dict[str, Any]) -> str | None:
    value = _first_non_empty(item, ["status", "state", "artifact_status", "artifactStatus"])
    if value is None:
        return None
    return str(value).strip().lower() or None


def _coerce_source_ids(item: dict[str, Any]) -> list[str]:
    raw = _first_non_empty(item, ["source_ids", "sourceIds", "sources"])
    if raw is None:
        return []
    if isinstance(raw, list):
        source_ids: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                source_ids.append(entry)
                continue
            if isinstance(entry, dict):
                source_id = _coerce_id(entry)
                if source_id:
                    source_ids.append(source_id)
        return source_ids
    if isinstance(raw, str):
        return [raw]
    return []


def list_notebook_sources(
    notebook_id: str,
    *,
    profile: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    args = ["nlm", "list", "sources", "--json"]
    if profile:
        args.extend(["--profile", profile])
    args.append(notebook_id)
    output = run_cmd(args, dry_run=dry_run)
    if dry_run:
        return []
    payload = parse_json_output(output, command=shlex.join(args))
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected source list payload: {json.dumps(payload, ensure_ascii=False)}")
    return payload


def list_notebook_artifacts(
    notebook_id: str,
    *,
    profile: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    commands = [
        ["nlm", "list", "artifacts", "--json"],
        ["nlm", "studio", "status", "--json"],
        ["nlm", "status", "artifacts", "--json"],
    ]
    if profile:
        for args in commands:
            args.extend(["--profile", profile])
    transient_markers = [
        "could not retrieve studio status.",
        "timed out",
        "timeout",
        "temporarily unavailable",
    ]
    last_error: RuntimeError | None = None
    for args in commands:
        full_args = [*args, notebook_id]
        command = shlex.join(full_args)
        if dry_run:
            run_cmd(full_args, dry_run=True)
            return []
        try:
            output = run_cmd(full_args, dry_run=False)
        except RuntimeError as exc:
            message = str(exc).lower()
            if any(marker in message for marker in transient_markers):
                last_error = exc
                continue
            raise
        payload = parse_json_output(output, command=command)
        if isinstance(payload, dict) and payload.get("status") == "error":
            message = str(payload.get("error") or payload)
            if any(marker in message.lower() for marker in transient_markers):
                last_error = RuntimeError(message)
                continue
            raise RuntimeError(f"Artifact list query returned an error: {message}")
        if isinstance(payload, list):
            return payload
        raise RuntimeError(f"Unexpected artifact list payload: {json.dumps(payload, ensure_ascii=False)}")
    if last_error:
        raise last_error
    return []


def match_sections_to_notebook_state(
    manifest: CourseManifest,
    *,
    notebook_id: str,
    output_dir: Path,
    profile: str | None = None,
    dry_run: bool = False,
) -> list[RecoverySectionState]:
    sources = list_notebook_sources(notebook_id, profile=profile, dry_run=dry_run)
    artifacts = list_notebook_artifacts(notebook_id, profile=profile, dry_run=dry_run)

    source_matches: dict[str, list[dict[str, Any]]] = {}
    for item in sources:
        source_title = _coerce_title(item)
        if source_title:
            source_matches.setdefault(source_title, []).append(item)

    artifact_matches: dict[str, list[dict[str, Any]]] = {}
    for item in artifacts:
        artifact_title = _coerce_title(item)
        if artifact_title:
            artifact_matches.setdefault(artifact_title, []).append(item)

    states: list[RecoverySectionState] = []
    for section in manifest.sections:
        resource_title = section.resource_title or default_resource_title(section.id, section.title)
        output_name = section.output_name
        output_stem = Path(output_name).stem
        output_path = output_dir / output_name
        sidecar_path = output_path.with_suffix(".slide.json")
        notes: list[str] = []

        matched_artifacts = list(artifact_matches.get(output_stem, []))
        if not matched_artifacts:
            stripped_title = strip_section_prefix(section.title)
            matched_artifacts = list(artifact_matches.get(stripped_title, []))
        matched_sources = list(source_matches.get(resource_title, []))
        if not matched_sources and output_stem != resource_title:
            matched_sources = list(source_matches.get(output_stem, []))

        local_output_exists = output_path.exists()
        local_sidecar_exists = sidecar_path.exists()

        if len(matched_artifacts) > 1:
            notes.append(f"Multiple artifacts matched title '{output_stem}'")
        if len(matched_sources) > 1:
            notes.append(f"Multiple sources matched title '{resource_title}'")

        artifact = matched_artifacts[0] if len(matched_artifacts) == 1 else None
        source = matched_sources[0] if len(matched_sources) == 1 else None

        artifact_id = _coerce_id(artifact) if artifact else None
        artifact_status = _coerce_status(artifact) if artifact else None
        source_id = _coerce_id(source) if source else None

        if local_output_exists:
            status = "completed_local"
            resume_action = "skip"
        elif notes:
            status = "ambiguous"
            resume_action = "manual_review"
        elif artifact_id and artifact_status == "completed":
            status = "completed_remote_needs_download"
            resume_action = "download_only"
        elif artifact_id and artifact_status in {"running", "processing", "pending", "queued", "in_progress"}:
            status = "running_remote"
            resume_action = "wait_and_finalize"
        elif artifact_id and artifact_status in {"failed", "cancelled", "canceled"}:
            status = "failed_remote"
            resume_action = "create_from_existing_source" if source_id else "upload_and_create"
        elif source_id:
            status = "source_only"
            resume_action = "create_from_existing_source"
        elif artifact_id:
            status = "unknown"
            resume_action = "manual_review"
            notes.append("Artifact exists but status could not be classified")
        else:
            status = "not_started"
            resume_action = "upload_and_create"

        states.append(
            RecoverySectionState(
                section_id=section.id,
                section_title=section.title,
                resource_title=resource_title,
                output_name=output_name,
                status=status,
                source_id=source_id,
                artifact_id=artifact_id,
                artifact_status=artifact_status,
                local_output_exists=local_output_exists,
                local_sidecar_exists=local_sidecar_exists,
                resume_action=resume_action,
                notes=notes,
            )
        )
    return states


def find_section(manifest: CourseManifest, section_id: str) -> Section:
    for section in manifest.sections:
        if section.id == section_id:
            return section
    raise KeyError(f"Section '{section_id}' was not found in {manifest.source_path}")


def manifest_to_dict(manifest: CourseManifest) -> dict[str, Any]:
    return {
        "course_title": manifest.course_title,
        "source_path": manifest.source_path,
        "course_scene": manifest.course_scene,
        "sections": [asdict(section) for section in manifest.sections],
    }


def load_course_manifest(
    path: str | Path,
    *,
    course_scene: str | None = None,
) -> CourseManifest:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Manifest does not exist: {source}")

    suffix = source.suffix.lower()
    if suffix == ".json":
        data = json.loads(source.read_text(encoding="utf-8"))
        return _normalize_manifest(data, source, course_scene=course_scene)
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "YAML manifest support requires PyYAML. Install it or use JSON/Markdown."
            ) from exc
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
        return _normalize_manifest(data, source, course_scene=course_scene)
    if suffix == ".md":
        return _load_markdown_manifest(source, course_scene=course_scene)
    raise RuntimeError(
        f"Unsupported manifest extension '{suffix}'. Use .json, .yaml, .yml, or .md."
    )


def _normalize_manifest(
    data: Any,
    source: Path,
    *,
    course_scene: str | None = None,
) -> CourseManifest:
    if not isinstance(data, dict):
        raise RuntimeError(f"Manifest root must be an object: {source}")

    resolved_course_scene = normalize_course_scene(
        course_scene or data.get("course_scene") or data.get("courseScene")
    )

    if isinstance(data.get("chapters"), list):
        return _load_fira_course_manifest(
            data,
            source,
            course_scene=resolved_course_scene,
        )

    course_title = str(data.get("course_title") or data.get("title") or source.stem).strip()
    raw_sections = data.get("sections") or data.get("chapters")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise RuntimeError(f"Manifest must contain a non-empty sections list: {source}")

    sections: list[Section] = []
    counter = 0

    def visit(items: list[Any], path_parts: list[int]) -> None:
        nonlocal counter
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise RuntimeError(f"Each section must be an object: {source}")

            next_path = [*path_parts, index]
            section_id = str(item.get("id") or ".".join(str(part) for part in next_path))
            title = str(item.get("title") or item.get("name") or "").strip()
            if not title:
                raise RuntimeError(f"Section {section_id} is missing a title in {source}")

            content = str(
                item.get("content")
                or item.get("text")
                or item.get("body")
                or item.get("markdown")
                or ""
            ).strip()
            focus = item.get("focus") or item.get("prompt")
            resource_title = item.get("resource_title") or item.get("source_title")
            output_name = item.get("output_name") or item.get("filename")
            md_name = item.get("md_name")
            block_type = item.get("block_type")
            raw_page_content = item.get("page_content")
            page_count = item.get("page_count")
            if raw_page_content is not None and not isinstance(raw_page_content, list):
                raise RuntimeError(
                    f"page_content for section {section_id} must be a list in {source}"
                )
            page_content = (
                _normalize_explicit_page_content(raw_page_content)
                if isinstance(raw_page_content, list)
                else None
            )
            if page_count is not None and not isinstance(page_count, int):
                raise RuntimeError(
                    f"page_count for section {section_id} must be an integer in {source}"
                )
            if block_type is not None and str(block_type).strip() not in {"imagesgallery", "html"}:
                raise RuntimeError(
                    f"block_type for section {section_id} must be 'imagesgallery' or 'html' in {source}"
                )

            if not content and page_content:
                content = "\n\n".join(page_content).strip()

            if content or page_content:
                counter += 1
                slug = str(item.get("slug") or slugify(title, fallback=f"section-{counter}"))
                resolved_output_name = str(output_name or default_output_name(section_id, title))
                resolved_md_name = str(md_name).strip() if md_name else None
                if resolved_md_name and Path(resolved_md_name).stem != Path(resolved_output_name).stem:
                    raise RuntimeError(
                        f"md_name '{resolved_md_name}' must share the same stem as output_name '{resolved_output_name}' in {source}"
                    )
                if page_count is not None and page_content is not None and page_count != len(page_content):
                    raise RuntimeError(
                        f"page_count {page_count} does not match len(page_content) {len(page_content)} for section {section_id} in {source}"
                    )
                sections.append(
                    Section(
                        id=section_id,
                        order=counter,
                        title=title,
                        content=content,
                        slug=slug,
                        output_name=resolved_output_name,
                        focus=str(focus).strip() if focus else None,
                        resource_title=str(resource_title).strip() if resource_title else None,
                        md_name=resolved_md_name,
                        block_type=str(block_type).strip() if block_type else None,
                        page_content=page_content,
                        page_count=page_count,
                    )
                )

            children = item.get("sections") or item.get("children") or []
            if children:
                if not isinstance(children, list):
                    raise RuntimeError(
                        f"Section {section_id} has non-list children in {source}"
                    )
                visit(children, next_path)

    visit(raw_sections, [])
    if not sections:
        raise RuntimeError(
            f"No leaf sections with content or explicit page_content were found in {source}"
        )
    return CourseManifest(
        course_title=course_title,
        sections=sections,
        source_path=str(source.resolve()),
        course_scene=resolved_course_scene,
        source_kind=source.suffix.lower().lstrip(".") or "json",
        default_block_type="html",
    )


def _load_fira_course_manifest(
    data: dict[str, Any],
    source: Path,
    *,
    course_scene: str | None = None,
) -> CourseManifest:
    course_title = str(data.get("name") or data.get("course_title") or source.stem).strip()
    chapters = data.get("chapters") or []
    if not isinstance(chapters, list):
        raise RuntimeError(f"FIRA course chapters must be a list: {source}")

    sections: list[Section] = []
    counter = 0
    course_block_type = "imagesgallery" if _fira_course_has_imagesgallery(chapters) else "html"

    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        chapter_name = str(chapter.get("name") or "").strip()
        if chapter_name in FIRA_SKIP_CHAPTER_NAMES:
            continue

        chapter_sections = chapter.get("sections") or []
        if not isinstance(chapter_sections, list):
            continue

        for index, section_data in enumerate(chapter_sections):
            if not isinstance(section_data, dict):
                continue
            if index == 0:
                continue

            title = str(section_data.get("name") or "").strip()
            if not title:
                continue

            content, source_blocks = _extract_fira_section_content(
                section_data,
                course_block_type=course_block_type,
            )
            if not content:
                continue

            counter += 1
            section_id = extract_section_id(title) or str(counter)
            sections.append(
                Section(
                    id=section_id,
                    order=counter,
                    title=title,
                    content=content,
                    slug=slugify(strip_section_prefix(title), fallback=f"section-{counter}"),
                    output_name=default_output_name(section_id, title),
                    resource_title=default_resource_title(section_id, title),
                    source_blocks=source_blocks,
                )
            )

    if not sections:
        raise RuntimeError(f"No slide-generation sections were found in {source}")

    return CourseManifest(
        course_title=course_title,
        sections=sections,
        source_path=str(source.resolve()),
        course_scene=normalize_course_scene(course_scene),
        source_kind="fira",
        default_block_type=course_block_type,
    )


def extract_fira_section_content(section_data: dict[str, Any]) -> str:
    content, _source_blocks = _extract_fira_section_content(
        section_data,
        course_block_type="html",
    )
    return content


def _load_markdown_manifest(
    source: Path,
    *,
    course_scene: str | None = None,
) -> CourseManifest:
    lines = source.read_text(encoding="utf-8").splitlines()
    course_title = source.stem
    sections: list[Section] = []
    current_title: str | None = None
    current_lines: list[str] = []
    counter = 0

    def flush() -> None:
        nonlocal counter, current_title, current_lines
        if not current_title:
            return
        content = "\n".join(current_lines).strip()
        if content:
            counter += 1
            section_id = str(counter)
            sections.append(
                Section(
                    id=section_id,
                    order=counter,
                    title=current_title,
                    content=content,
                    slug=slugify(current_title, fallback=f"section-{counter}"),
                    output_name=default_output_name(section_id, current_title),
                )
            )
        current_title = None
        current_lines = []

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not match:
            if current_title:
                current_lines.append(line)
            continue

        level = len(match.group(1))
        title = match.group(2).strip()
        if level == 1 and title and course_title == source.stem:
            course_title = title
            continue
        if level == 2:
            flush()
            current_title = title
            current_lines = []
            continue
        if current_title:
            current_lines.append(line)

    flush()
    if not sections:
        raise RuntimeError(
            f"Markdown manifest {source} must use '## Section Title' headings with body text."
        )
    return CourseManifest(
        course_title=course_title,
        sections=sections,
        source_path=str(source.resolve()),
        course_scene=normalize_course_scene(course_scene),
        source_kind="markdown",
        default_block_type="html",
    )
