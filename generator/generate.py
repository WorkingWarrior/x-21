#!/usr/bin/env python3
"""Generate a safe, static X-21 forum archive from a XenForo SQL dump."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


ARCHIVE_NAME = "Archiwum Forum X-21"
BUILD_MARKER = ".x21-archive"
DEFAULT_EXCLUDED_FORUMS = ("Reaktor",)
USER_POSTS_PER_PAGE = 50
REQUIRED_TABLES = {
    "xf_node",
    "xf_forum",
    "xf_thread",
    "xf_post",
    "xf_attachment",
    "xf_attachment_data",
}
INSERT_START = re.compile(r"^INSERT INTO `([^`]+)` \((.*?)\) VALUES\s*$")
TAG_RE = re.compile(r"\[(/?)([A-Za-z*][A-Za-z0-9_-]*)(?:(?:=|\s+)([^\]]*))?\]")
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
COLOR_CLASSES = {
    color: f"bb-color-{color}"
    for color in ("black", "white", "gray", "red", "green", "blue", "yellow", "orange", "purple", "pink", "cyan", "magenta")
}
COLOR_CLASSES["grey"] = "bb-color-gray"


def e(value: Any, *, quote: bool = True) -> str:
    """Escape a value for HTML text or attribute context."""
    return html.escape(str(value if value is not None else ""), quote=quote)


def safe_url(value: str, *, allow_relative: bool = False) -> str | None:
    value = html.unescape(value.strip())
    if any(char in value for char in ('<', '>', '"', "'", "\n", "\r", "\t")):
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() in {"http", "https", "mailto"}:
        return value
    if (
        allow_relative
        and not parsed.scheme
        and not value.startswith(("//", "\\"))
        and re.fullmatch(r"/?[A-Za-z0-9_.~%/-]+(?:[?#][A-Za-z0-9_.~%&=+/?#-]*)?", value)
    ):
        return value
    return None


def youtube_id(value: str) -> str | None:
    value = html.unescape(value.strip())
    if YOUTUBE_ID_RE.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "youtube-nocookie.com", "m.youtube.com"}:
        if parsed.path.startswith("/embed/"):
            candidate = parsed.path.split("/embed/", 1)[1].split("/", 1)[0]
        else:
            from urllib.parse import parse_qs

            candidate = parse_qs(parsed.query).get("v", [""])[0]
    else:
        return None
    return candidate if YOUTUBE_ID_RE.fullmatch(candidate) else None


def decode_sql_string(source: str, pos: int) -> tuple[str, int]:
    if source[pos] != "'":
        raise ValueError(f"Expected SQL string at offset {pos}")
    pos += 1
    out: list[str] = []
    escapes = {
        "0": "\0",
        "b": "\b",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "Z": "\x1a",
        "'": "'",
        '"': '"',
        "\\": "\\",
    }
    while pos < len(source):
        char = source[pos]
        if char == "'":
            if pos + 1 < len(source) and source[pos + 1] == "'":
                out.append("'")
                pos += 2
                continue
            return "".join(out), pos + 1
        if char == "\\" and pos + 1 < len(source):
            following = source[pos + 1]
            out.append(escapes.get(following, following))
            pos += 2
            continue
        out.append(char)
        pos += 1
    raise ValueError("Unterminated SQL string")


def decode_hex(value: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8", errors="replace")
    except ValueError:
        return ""


def parse_sql_value(source: str, pos: int) -> tuple[Any, int]:
    while pos < len(source) and source[pos].isspace():
        pos += 1
    if source.startswith("_binary", pos):
        pos += len("_binary")
        while pos < len(source) and source[pos].isspace():
            pos += 1
    if pos < len(source) and source[pos] == "'":
        return decode_sql_string(source, pos)
    end = pos
    while end < len(source) and source[end] not in ",)":
        end += 1
    token = source[pos:end].strip()
    upper = token.upper()
    if upper == "NULL":
        return None, end
    if token.lower().startswith("0x"):
        return decode_hex(token[2:]), end
    if re.fullmatch(r"-?\d+", token):
        return int(token), end
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", token):
        return float(token), end
    return token, end


def parse_insert_rows(statement: str) -> tuple[str, list[dict[str, Any]]]:
    first_line, _, values = statement.partition("\n")
    match = INSERT_START.match(first_line.rstrip("\r"))
    if not match:
        # HeidiSQL may place VALUES on the same line for small tables.
        match = re.match(r"^INSERT INTO `([^`]+)` \((.*?)\) VALUES\s*", statement)
        if not match:
            raise ValueError(f"Unsupported INSERT header: {first_line[:120]}")
        values = statement[match.end() :]
    table = match.group(1)
    columns = re.findall(r"`([^`]+)`", match.group(2))
    pos = 0
    rows: list[dict[str, Any]] = []
    values = values.strip()
    if values.endswith(";"):
        values = values[:-1]
    while pos < len(values):
        while pos < len(values) and (values[pos].isspace() or values[pos] == ","):
            pos += 1
        if pos >= len(values):
            break
        if values[pos] != "(":
            raise ValueError(f"Expected row at offset {pos} in {table}")
        pos += 1
        parsed: list[Any] = []
        while True:
            value, pos = parse_sql_value(values, pos)
            parsed.append(value)
            while pos < len(values) and values[pos].isspace():
                pos += 1
            if pos >= len(values):
                raise ValueError(f"Unterminated row in {table}")
            if values[pos] == ",":
                pos += 1
                continue
            if values[pos] == ")":
                pos += 1
                break
            raise ValueError(f"Unexpected token at offset {pos} in {table}")
        if len(parsed) != len(columns):
            raise ValueError(
                f"Column/value mismatch in {table}: {len(columns)} columns, {len(parsed)} values"
            )
        rows.append(dict(zip(columns, parsed, strict=True)))
    return table, rows


def statement_complete(text: str) -> bool:
    """Return true when an SQL statement ends outside a quoted string."""
    in_string = False
    escaped = False
    pos = 0
    while pos < len(text):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                if pos + 1 < len(text) and text[pos + 1] == "'":
                    pos += 1
                else:
                    in_string = False
        elif char == "'":
            in_string = True
        elif char == ";":
            return not text[pos + 1 :].strip()
        pos += 1
    return False


def scan_statement_chunk(text: str, in_string: bool, escaped: bool) -> tuple[bool, bool, bool]:
    """Advance SQL quote state for one chunk without rescanning prior content."""
    pos = 0
    complete = False
    while pos < len(text):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                if pos + 1 < len(text) and text[pos + 1] == "'":
                    pos += 1
                else:
                    in_string = False
        elif char == "'":
            in_string = True
        elif char == ";":
            complete = True
        pos += 1
    return in_string, escaped, complete and not in_string


def load_dump(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read only the six public-content tables required by the archive."""
    tables: dict[str, list[dict[str, Any]]] = {}
    collecting = False
    buffer: list[str] = []
    in_string = False
    escaped = False
    with path.open("r", encoding="utf-8-sig", errors="strict", newline=None) as handle:
        for line in handle:
            if not collecting:
                match = re.match(r"^INSERT INTO `([^`]+)`", line)
                if match and match.group(1) in REQUIRED_TABLES:
                    collecting = True
                    buffer = [line]
                    in_string, escaped, complete = scan_statement_chunk(line, False, False)
                    if complete:
                        statement = "".join(buffer)
                        table, rows = parse_insert_rows(statement)
                        tables.setdefault(table, []).extend(rows)
                        collecting = False
                continue
            buffer.append(line)
            in_string, escaped, complete = scan_statement_chunk(line, in_string, escaped)
            if complete:
                statement = "".join(buffer)
                table, rows = parse_insert_rows(statement)
                tables.setdefault(table, []).extend(rows)
                collecting = False
                buffer = []
                in_string = False
                escaped = False
    missing = REQUIRED_TABLES - tables.keys()
    if missing:
        raise ValueError(f"Dump does not contain required table data: {', '.join(sorted(missing))}")
    return tables


@dataclass
class TextNode:
    value: str


@dataclass
class TagNode:
    name: str
    option: str | None
    children: list["Node"]
    closed: bool = False


Node = TextNode | TagNode


CONTAINER_TAGS = {
    "b",
    "i",
    "u",
    "s",
    "color",
    "size",
    "font",
    "url",
    "img",
    "quote",
    "code",
    "spoiler",
    "list",
    "center",
    "right",
    "table",
    "tr",
    "td",
    "youtube",
    "media",
    "video",
    "audio",
    "mp3",
    "attach",
    "user",
}


def parse_bbcode(source: str) -> list[Node]:
    root: list[Node] = []
    stack: list[TagNode] = []

    def destination() -> list[Node]:
        return stack[-1].children if stack else root

    cursor = 0
    for match in TAG_RE.finditer(source):
        if match.start() > cursor:
            destination().append(TextNode(source[cursor : match.start()]))
        closing, raw_name, option = match.groups()
        name = raw_name.lower()
        literal = match.group(0)
        if name == "*" and not closing:
            destination().append(TagNode("*", None, [], closed=True))
        elif name not in CONTAINER_TAGS:
            destination().append(TextNode(literal))
        elif not closing:
            node = TagNode(name, option, [])
            destination().append(node)
            stack.append(node)
        elif stack and stack[-1].name == name:
            stack.pop().closed = True
        else:
            destination().append(TextNode(literal))
        cursor = match.end()
    if cursor < len(source):
        destination().append(TextNode(source[cursor:]))
    return root


def plain_text(nodes: Iterable[Node]) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, TextNode):
            parts.append(node.value)
        else:
            parts.append(plain_text(node.children))
    return "".join(parts)


@dataclass
class RenderContext:
    attachments: dict[int, dict[str, Any]]
    attachment_paths: dict[int, str]


@dataclass
class UserStats:
    user_id: int
    username: str
    posts: list[dict[str, Any]]
    started_threads: list[dict[str, Any]]
    thread_post_counts: Counter[int]
    forum_post_counts: Counter[int]


def render_text(value: str) -> str:
    return e(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>\n")


def render_nodes(nodes: Iterable[Node], context: RenderContext, *, table_context: bool = False) -> str:
    result: list[str] = []
    for node in nodes:
        if isinstance(node, TextNode):
            if table_context and not node.value.strip():
                continue
            result.append(render_text(node.value))
            continue
        result.append(render_tag(node, context))
    return "".join(result)


def render_link(url: str, label: str) -> str:
    checked = safe_url(url, allow_relative=True)
    if not checked:
        return f'<span class="broken-link" title="Niebezpieczny lub niepoprawny adres">{label}</span>'
    external = bool(urlparse(checked).scheme)
    extras = ' target="_blank" rel="nofollow noopener noreferrer"' if external else ""
    return f'<a href="{e(checked)}"{extras}>{label}</a>'


def render_list(node: TagNode, context: RenderContext) -> str:
    ordered = (node.option or "").strip().lower() in {"1", "a", "i"}
    tag = "ol" if ordered else "ul"
    groups: list[list[Node]] = []
    current: list[Node] = []
    saw_marker = False
    for child in node.children:
        if isinstance(child, TagNode) and child.name == "*":
            if saw_marker or current:
                groups.append(current)
            current = []
            saw_marker = True
        else:
            current.append(child)
    if saw_marker or current:
        groups.append(current)
    rendered_groups: list[str] = []
    for group in groups:
        if not plain_text(group).strip():
            continue
        rendered = render_nodes(group, context)
        rendered = re.sub(r"^(?:<br>\n?)+|(?:<br>\n?)+$", "", rendered)
        rendered_groups.append(f"<li>{rendered}</li>")
    items = "".join(rendered_groups)
    return f"<{tag}>{items}</{tag}>"


def render_tag(node: TagNode, context: RenderContext) -> str:
    name = node.name
    body = render_nodes(node.children, context, table_context=name in {"table", "tr"})
    text = plain_text(node.children).strip()
    if not node.closed and name != "*":
        option = f"={node.option}" if node.option is not None else ""
        return f"{e(f'[{name}{option}]')}{body}"
    simple = {
        "b": ("strong", "strong"),
        "i": ("em", "em"),
        "u": ("u", "u"),
        "s": ("s", "s"),
    }
    if name in simple:
        start, end = simple[name]
        return f"<{start}>{body}</{end}>"
    if name == "url":
        target = (node.option or text).strip('"\' ')
        return render_link(target, body or e(text))
    if name == "img":
        text = text.strip('"\' ')
        url = safe_url(text)
        dimensions = ""
        if node.option:
            requested_width = re.search(r'\bwidth\s*=\s*["\']?(\d+)', node.option, re.IGNORECASE)
            requested_height = re.search(r'\bheight\s*=\s*["\']?(\d+)', node.option, re.IGNORECASE)
            if requested_width and 1 <= int(requested_width.group(1)) <= 4096:
                dimensions += f' width="{int(requested_width.group(1))}"'
            if requested_height and 1 <= int(requested_height.group(1)) <= 4096:
                dimensions += f' height="{int(requested_height.group(1))}"'
        return (
            f'<img src="{e(url)}" alt="Obraz z archiwalnego postu"{dimensions} loading="lazy" decoding="async">'
            if url
            else f'<span class="broken-media">[obraz: {e(text)}]</span>'
        )
    if name in {"youtube", "media"}:
        provider = (node.option or "youtube").strip('"\' ').lower()
        text = text.strip('"\' ')
        video_id = youtube_id(text) if provider in {"youtube", "youtube.com", ""} else None
        if video_id:
            return (
                '<div class="video"><iframe '
                f'src="https://www.youtube-nocookie.com/embed/{e(video_id)}" '
                'title="Osadzony film YouTube" loading="lazy" allowfullscreen></iframe></div>'
            )
        return render_link(text, e(text)) if safe_url(text) else f'<span class="media-label">{e(text)}</span>'
    if name in {"video", "audio", "mp3"}:
        text = text.strip('"\' ')
        return render_link(text, e(text)) if safe_url(text) else f'<span class="media-label">{e(text)}</span>'
    if name == "quote":
        author = ""
        if node.option:
            author = node.option.strip('"\' ').split(",", 1)[0].strip()
        footer = f"<footer>— {e(author)}</footer>" if author else ""
        return f"<blockquote>{body}{footer}</blockquote>"
    if name == "code":
        return f"<pre><code>{e(plain_text(node.children))}</code></pre>"
    if name == "spoiler":
        label = (node.option or "Pokaż ukrytą treść").strip('"\' ')
        return f'<details><summary>{e(label)}</summary><div class="spoiler-content">{body}</div></details>'
    if name == "list":
        return render_list(node, context)
    if name in {"center", "right"}:
        return f'<div class="align-{name}">{body}</div>'
    if name == "color":
        color = (node.option or "").strip('"\' ').lower()
        css_class = COLOR_CLASSES.get(color)
        return f'<span class="{css_class}">{body}</span>' if css_class else body
    if name == "size":
        size = (node.option or "").strip('"\' ').lower()
        size_map = {"1": "xs", "2": "sm", "3": "md", "4": "lg", "5": "xl", "6": "2xl", "7": "3xl"}
        css_size = size_map.get(size, "md")
        return f'<span class="text-{css_size}">{body}</span>'
    if name == "font":
        return body
    if name == "user":
        return f'<span class="user-mention">@{body}</span>'
    if name == "attach":
        match = re.search(r"\d+", text)
        attachment_id = int(match.group()) if match else -1
        metadata = context.attachments.get(attachment_id)
        path = context.attachment_paths.get(attachment_id)
        if metadata and path:
            width = int(metadata.get("width") or 0)
            height = int(metadata.get("height") or 0)
            dimensions = f' width="{width}" height="{height}"' if width and height else ""
            alt = metadata.get("filename") or f"Załącznik {attachment_id}"
            return (
                f'<a href="{e(path)}" class="attachment-link">'
                f'<img src="{e(path)}" alt="{e(alt)}"{dimensions} loading="lazy" decoding="async"></a>'
            )
        return f'<span class="broken-media">[brak załącznika {attachment_id}]</span>'
    if name == "table":
        return f'<div class="table-scroll"><table><tbody>{body}</tbody></table></div>'
    if name == "tr":
        return f"<tr>{body}</tr>"
    if name == "td":
        return f"<td>{body}</td>"
    return body


def format_time(timestamp: Any, timezone: ZoneInfo) -> tuple[str, str]:
    moment = datetime.fromtimestamp(int(timestamp or 0), tz=timezone)
    return moment.isoformat(), moment.strftime("%d.%m.%Y %H:%M")


def slug_file(prefix: str, identifier: Any) -> str:
    return f"{prefix}_{int(identifier)}.html"


def user_page_file(user_id: int, page: int = 1) -> str:
    return f"user_{user_id}.html" if page == 1 else f"user_{user_id}_page_{page}.html"


def pl_count(value: int, singular: str, paucal: str, plural: str) -> str:
    absolute = abs(value)
    if absolute == 1:
        word = singular
    elif absolute % 10 in {2, 3, 4} and absolute % 100 not in {12, 13, 14}:
        word = paucal
    else:
        word = plural
    return f"{value} {word}"


def post_excerpt(message: Any, limit: int = 260) -> str:
    text = plain_text(parse_bbcode(str(message or "")))
    text = re.sub(r"\[/?[A-Za-z*][^\]]*\]", "", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def avatar_path(user_id: int, media_root: Path | None) -> str | None:
    if not media_root or user_id <= 0:
        return None
    group = user_id // 1000
    relative = Path("avatars") / "m" / str(group) / f"{user_id}.jpg"
    return f"media/{relative.as_posix()}" if (media_root / relative).exists() else None


def media_attachment_paths(
    attachment_rows: list[dict[str, Any]],
    data_rows: list[dict[str, Any]],
    media_root: Path | None,
) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
    data_by_id = {int(row["data_id"]): row for row in data_rows}
    metadata: dict[int, dict[str, Any]] = {}
    paths: dict[int, str] = {}
    for attachment in attachment_rows:
        attachment_id = int(attachment["attachment_id"])
        data_id = int(attachment["data_id"])
        data = data_by_id.get(data_id)
        if not data:
            continue
        metadata[attachment_id] = data
        if not media_root:
            continue
        group = data_id // 1000
        directory = media_root / "attachments" / str(group)
        pattern = f"{data_id}-{data.get('file_hash', '')}.*"
        matches = sorted(directory.glob(pattern)) if directory.exists() else []
        if matches:
            relative = matches[0].relative_to(media_root)
            paths[attachment_id] = f"media/{relative.as_posix()}"
    return metadata, paths


def page_head(title: str, description: str, canonical: str | None = None) -> str:
    escaped_title = e(title)
    if len(escaped_title) > 70:
        shortened = title
        while shortened and len(e(shortened.rstrip()) + "…") > 70:
            shortened = shortened[:-1]
        escaped_title = e(shortened.rstrip()) + "…"
    canonical_tag = f'\n    <link rel="canonical" href="{e(canonical)}">' if canonical else ""
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="description" content="{e(description)}">
    <meta name="color-scheme" content="dark">
    <title>{escaped_title}</title>{canonical_tag}
    <link rel="stylesheet" href="assets/styles.css">
</head>
<body>
<a class="skip-link" href="#main-content">Przejdź do głównej treści</a>
"""


def page_end() -> str:
    return "</body>\n</html>\n"


def breadcrumb(items: list[tuple[str, str | None]]) -> str:
    rendered: list[str] = []
    for index, (label, url) in enumerate(items):
        if index:
            rendered.append('<span aria-hidden="true">/</span>')
        if url:
            rendered.append(f'<a href="{e(url)}">{e(label)}</a>')
        else:
            rendered.append(f'<span aria-current="page">{e(label)}</span>')
    return f'<nav class="breadcrumb" aria-label="Okruszki">{"".join(rendered)}</nav>'


def write_page(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def render_index(
    destination: Path,
    nodes: list[dict[str, Any]],
    forums: dict[int, dict[str, Any]],
    threads_by_forum: dict[int, list[dict[str, Any]]],
    base_url: str | None,
) -> None:
    children: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        if int(node.get("display_in_list") or 0):
            children[int(node.get("parent_node_id") or 0)].append(node)
    for group in children.values():
        group.sort(key=lambda item: (int(item.get("display_order") or 0), int(item["node_id"])))

    sections: list[str] = []
    for category in children[0]:
        node_type = str(category.get("node_type_id") or "").lower()
        if node_type != "category":
            continue
        cards: list[str] = []
        for forum_node in children[int(category["node_id"])]:
            forum_id = int(forum_node["node_id"])
            if forum_id not in forums:
                continue
            stats = forums[forum_id]
            recent = threads_by_forum.get(forum_id, [])[:5]
            recent_html = "".join(
                f'<li><a href="{slug_file("thread", thread["thread_id"])}">{e(thread["title"])}</a></li>'
                for thread in recent
            )
            description = str(forum_node.get("description") or "")
            cards.append(
                f"""<article class="forum-card">
<h3><a href="{slug_file('forum', forum_id)}">{e(forum_node['title'])}</a></h3>
{f'<p>{e(description)}</p>' if description else ''}
{f'<h4>Najnowsze wątki</h4><ul>{recent_html}</ul>' if recent_html else ''}
<footer><span>Wątki: {int(stats.get('discussion_count') or 0)}</span>
<span>Wiadomości: {int(stats.get('message_count') or 0)}</span></footer>
</article>"""
            )
        if cards:
            sections.append(
                f'<section aria-labelledby="category-{int(category["node_id"])}">'
                f'<h2 id="category-{int(category["node_id"])}">{e(category["title"])}</h2>'
                f'<div class="forum-grid">{"".join(cards)}</div></section>'
            )
    canonical = f"{base_url.rstrip('/')}/" if base_url else None
    content = page_head(ARCHIVE_NAME, "Statyczne archiwum społeczności Forum X-21.", canonical)
    content += (
        f'<header class="hero"><h1>{ARCHIVE_NAME}</h1><p>Zachowana historia społeczności.</p>'
        '<nav class="hero-nav" aria-label="Główna nawigacja"><a class="button" href="users.html">Użytkownicy</a></nav></header>'
    )
    content += f'<main id="main-content" class="container">{"".join(sections)}</main>'
    content += page_end()
    write_page(destination / "index.html", content)


def render_forum(
    destination: Path,
    node: dict[str, Any],
    threads: list[dict[str, Any]],
    base_url: str | None,
) -> None:
    forum_id = int(node["node_id"])
    title = str(node["title"])
    canonical = f"{base_url.rstrip('/')}/{slug_file('forum', forum_id)}" if base_url else None
    description = str(node.get("description") or f"Lista wątków w forum {title}.")
    entries = "".join(
        f"""<li><a class="thread-row" href="{slug_file('thread', thread['thread_id'])}">
<span>{e(thread['title'])}</span><small>{int(thread.get('reply_count') or 0) + 1} postów</small>
</a></li>"""
        for thread in threads
    )
    content = page_head(f"{title} — {ARCHIVE_NAME}", description, canonical)
    content += '<div class="container page-shell">'
    content += breadcrumb([("Strona główna", "index.html"), (title, None)])
    content += f'<header class="page-header"><h1>{e(title)}</h1>{f"<p>{e(description)}</p>" if description else ""}</header>'
    content += f'<main id="main-content"><ul class="thread-list">{entries}</ul></main></div>'
    content += page_end()
    write_page(destination / slug_file("forum", forum_id), content)


def render_thread(
    destination: Path,
    thread: dict[str, Any],
    forum_node: dict[str, Any],
    posts: list[dict[str, Any]],
    timezone: ZoneInfo,
    media_root: Path | None,
    render_context: RenderContext,
    base_url: str | None,
) -> None:
    thread_id = int(thread["thread_id"])
    title = str(thread["title"])
    canonical = f"{base_url.rstrip('/')}/{slug_file('thread', thread_id)}" if base_url else None
    post_cards: list[str] = []
    for post in posts:
        post_id = int(post["post_id"])
        user_id = int(post.get("user_id") or 0)
        username = str(post.get("username") or "Gość")
        iso_time, display_time = format_time(post.get("post_date"), timezone)
        avatar = avatar_path(user_id, media_root)
        if avatar:
            identity = (
                f'<img class="avatar" src="{e(avatar)}" width="64" height="64" '
                f'alt="Awatar użytkownika {e(username)}" loading="lazy" decoding="async">'
            )
        else:
            initial = username[:1].upper() if username else "?"
            identity = f'<span class="avatar avatar-fallback" aria-hidden="true">{e(initial)}</span>'
        if user_id > 0:
            profile_url = user_page_file(user_id)
            identity = f'<a class="avatar-link" href="{profile_url}" aria-label="Profil użytkownika {e(username)}">{identity}</a>'
            username_html = f'<a class="user-name" href="{profile_url}">{e(username)}</a>'
        else:
            username_html = e(username)
        body = render_nodes(parse_bbcode(str(post.get("message") or "")), render_context)
        post_cards.append(
            f"""<li id="post-{post_id}"><article class="post" aria-labelledby="post-author-{post_id}">
<header class="post-author">{identity}<div><h2 id="post-author-{post_id}">{username_html}</h2>
<a class="post-date" href="#post-{post_id}"><time datetime="{e(iso_time)}">{e(display_time)}</time></a></div></header>
<div class="post-body">{body}</div>
</article></li>"""
        )
    forum_title = str(forum_node["title"])
    forum_id = int(forum_node["node_id"])
    description = f"Archiwalny wątek „{title}” z forum {forum_title}."
    content = page_head(f"{title} — {ARCHIVE_NAME}", description, canonical)
    content += '<div class="container page-shell">'
    content += breadcrumb(
        [("Strona główna", "index.html"), (forum_title, slug_file("forum", forum_id)), (title, None)]
    )
    content += f'<header class="page-header"><h1>{e(title)}</h1><p>{len(posts)} postów</p></header>'
    content += f'<main id="main-content"><ol class="post-list">{"".join(post_cards)}</ol></main></div>'
    content += page_end()
    write_page(destination / slug_file("thread", thread_id), content)


def collect_users(
    posts: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    thread_by_id: dict[int, dict[str, Any]],
) -> dict[int, UserStats]:
    users: dict[int, UserStats] = {}
    latest_identity: dict[int, tuple[int, str]] = {}

    def get_user(user_id: int, username: str) -> UserStats:
        if user_id not in users:
            users[user_id] = UserStats(user_id, username or f"Użytkownik {user_id}", [], [], Counter(), Counter())
        return users[user_id]

    for post in posts:
        user_id = int(post.get("user_id") or 0)
        thread = thread_by_id.get(int(post.get("thread_id") or 0))
        if user_id <= 0 or not thread:
            continue
        username = str(post.get("username") or f"Użytkownik {user_id}")
        stats = get_user(user_id, username)
        stats.posts.append(post)
        thread_id = int(thread["thread_id"])
        stats.thread_post_counts[thread_id] += 1
        stats.forum_post_counts[int(thread["node_id"])] += 1
        dated_name = (int(post.get("post_date") or 0), username)
        if dated_name[0] >= latest_identity.get(user_id, (-1, ""))[0]:
            latest_identity[user_id] = dated_name

    for thread in threads:
        user_id = int(thread.get("user_id") or 0)
        if user_id <= 0:
            continue
        username = str(thread.get("username") or f"Użytkownik {user_id}")
        stats = get_user(user_id, username)
        stats.started_threads.append(thread)
        dated_name = (int(thread.get("post_date") or 0), username)
        if dated_name[0] >= latest_identity.get(user_id, (-1, ""))[0]:
            latest_identity[user_id] = dated_name

    for user_id, stats in users.items():
        stats.username = latest_identity.get(user_id, (0, stats.username))[1]
        stats.posts.sort(key=lambda post: (int(post.get("post_date") or 0), int(post["post_id"])), reverse=True)
        stats.started_threads.sort(
            key=lambda thread: (int(thread.get("post_date") or 0), int(thread["thread_id"])), reverse=True
        )
    return users


def render_profile_avatar(user: UserStats, media_root: Path | None, size: int = 80) -> str:
    avatar = avatar_path(user.user_id, media_root)
    if avatar:
        return (
            f'<img class="avatar profile-avatar" src="{e(avatar)}" width="{size}" height="{size}" '
            f'alt="Awatar użytkownika {e(user.username)}" loading="lazy" decoding="async">'
        )
    initial = user.username[:1].upper() if user.username else "?"
    return f'<span class="avatar avatar-fallback profile-avatar" aria-hidden="true">{e(initial)}</span>'


def render_users_index(
    destination: Path,
    users: dict[int, UserStats],
    media_root: Path | None,
    timezone: ZoneInfo,
    base_url: str | None,
) -> None:
    ordered = sorted(users.values(), key=lambda user: (-len(user.posts), user.username.casefold(), user.user_id))
    cards: list[str] = []
    for user in ordered:
        last_seen = "Brak publicznych postów"
        if user.posts:
            _, last_seen = format_time(user.posts[0].get("post_date"), timezone)
        cards.append(
            f"""<li class="user-card" data-user-card>
<a class="avatar-link" href="{user_page_file(user.user_id)}">{render_profile_avatar(user, media_root, 64)}</a>
<div><h2><a href="{user_page_file(user.user_id)}">{e(user.username)}</a></h2>
<p>{pl_count(len(user.posts), 'post', 'posty', 'postów')} · {pl_count(len(user.started_threads), 'rozpoczęty wątek', 'rozpoczęte wątki', 'rozpoczętych wątków')}</p>
<small>Ostatni wpis: {e(last_seen)}</small></div>
</li>"""
        )
    canonical = f"{base_url.rstrip('/')}/users.html" if base_url else None
    content = page_head(f"Użytkownicy — {ARCHIVE_NAME}", "Lista autorów publicznych wpisów w archiwum Forum X-21.", canonical)
    content += '<div class="container page-shell">'
    content += breadcrumb([("Strona główna", "index.html"), ("Użytkownicy", None)])
    content += f'<header class="page-header"><h1>Użytkownicy</h1><p>{len(ordered)} autorów publicznych treści</p></header>'
    content += (
        '<main id="main-content"><label class="user-search-label" for="user-search">Znajdź użytkownika</label>'
        '<input class="user-search" id="user-search" type="search" placeholder="Wpisz nazwę…" autocomplete="off">'
        f'<ul class="user-grid">{"".join(cards)}</ul>'
        '<p class="empty-search" data-empty-search hidden>Nie znaleziono takiego użytkownika.</p></main></div>'
        '<script src="assets/users.js" defer></script>'
    )
    content += page_end()
    write_page(destination / "users.html", content)


def render_pagination(user_id: int, current_page: int, page_count: int) -> str:
    if page_count <= 1:
        return ""
    links = []
    for page in range(1, page_count + 1):
        current = ' aria-current="page"' if page == current_page else ""
        links.append(f'<a href="{user_page_file(user_id, page)}"{current}>{page}</a>')
    return f'<nav class="pagination" aria-label="Strony postów">{"".join(links)}</nav>'


def render_user_profiles(
    destination: Path,
    user: UserStats,
    thread_by_id: dict[int, dict[str, Any]],
    node_by_id: dict[int, dict[str, Any]],
    timezone: ZoneInfo,
    media_root: Path | None,
    base_url: str | None,
) -> None:
    page_count = max(1, (len(user.posts) + USER_POSTS_PER_PAGE - 1) // USER_POSTS_PER_PAGE)
    first_seen = last_seen = None
    if user.posts:
        _, first_seen = format_time(user.posts[-1].get("post_date"), timezone)
        _, last_seen = format_time(user.posts[0].get("post_date"), timezone)

    forum_rows = "".join(
        f'<li><a href="{slug_file("forum", forum_id)}">{e(node_by_id[forum_id]["title"])}</a><span>{pl_count(count, "post", "posty", "postów")}</span></li>'
        for forum_id, count in user.forum_post_counts.most_common()
        if forum_id in node_by_id
    )
    started_rows = "".join(
        f'<li><a href="{slug_file("thread", thread["thread_id"])}">{e(thread["title"])}</a></li>'
        for thread in user.started_threads
    )
    participated = sorted(
        user.thread_post_counts.items(),
        key=lambda item: (int(thread_by_id[item[0]].get("last_post_date") or 0), item[0]),
        reverse=True,
    )
    participated_rows = "".join(
        f'<li><a href="{slug_file("thread", thread_id)}">{e(thread_by_id[thread_id]["title"])}</a><span>{pl_count(count, "post", "posty", "postów")}</span></li>'
        for thread_id, count in participated
    )

    for page in range(1, page_count + 1):
        start = (page - 1) * USER_POSTS_PER_PAGE
        page_posts = user.posts[start : start + USER_POSTS_PER_PAGE]
        activity_rows: list[str] = []
        for post in page_posts:
            thread = thread_by_id[int(post["thread_id"])]
            forum_id = int(thread["node_id"])
            iso_time, display_time = format_time(post.get("post_date"), timezone)
            activity_rows.append(
                f"""<li><article class="activity-card">
<header><div><a class="activity-title" href="{slug_file('thread', thread['thread_id'])}#post-{int(post['post_id'])}">{e(thread['title'])}</a>
<a class="activity-forum" href="{slug_file('forum', forum_id)}">{e(node_by_id[forum_id]['title'])}</a></div>
<time datetime="{e(iso_time)}">{e(display_time)}</time></header>
<p>{e(post_excerpt(post.get('message')))}</p></article></li>"""
            )

        filename = user_page_file(user.user_id, page)
        canonical = f"{base_url.rstrip('/')}/{filename}" if base_url else None
        page_suffix = f" — strona {page}" if page > 1 else ""
        content = page_head(
            f"{user.username}{page_suffix} — {ARCHIVE_NAME}",
            f"Publiczna aktywność użytkownika {user.username} w archiwum Forum X-21.",
            canonical,
        )
        content += '<div class="container page-shell">'
        content += breadcrumb([("Strona główna", "index.html"), ("Użytkownicy", "users.html"), (user.username, None)])
        content += (
            f'<header class="profile-header">{render_profile_avatar(user, media_root)}<div><h1>{e(user.username)}</h1>'
            '<p>Publiczna aktywność w archiwum</p></div></header>'
        )
        content += '<main id="main-content">'
        if page == 1:
            date_stats = ""
            if first_seen and last_seen:
                date_stats = f'<div><dt>Pierwszy wpis</dt><dd>{e(first_seen)}</dd></div><div><dt>Ostatni wpis</dt><dd>{e(last_seen)}</dd></div>'
            content += (
                f'<dl class="profile-stats"><div><dt>Posty</dt><dd>{len(user.posts)}</dd></div>'
                f'<div><dt>Rozpoczęte wątki</dt><dd>{len(user.started_threads)}</dd></div>'
                f'<div><dt>Wątki z aktywnością</dt><dd>{len(user.thread_post_counts)}</dd></div>{date_stats}</dl>'
            )
            if forum_rows:
                content += f'<section class="profile-section"><h2>Aktywność w działach</h2><ul class="profile-link-list">{forum_rows}</ul></section>'
            if started_rows:
                content += f'<section class="profile-section"><h2>Rozpoczęte wątki</h2><ul class="profile-link-list compact">{started_rows}</ul></section>'
            if participated_rows:
                content += f'<section class="profile-section"><h2>Wątki, w których pisał</h2><ul class="profile-link-list">{participated_rows}</ul></section>'
        content += f'<section class="profile-section"><h2>Posty{page_suffix}</h2><ol class="activity-list">{"".join(activity_rows)}</ol>'
        content += render_pagination(user.user_id, page, page_count)
        content += '</section></main></div>' + page_end()
        write_page(destination / filename, content)


def render_404(destination: Path) -> None:
    content = page_head(f"404 — {ARCHIVE_NAME}", "Nie znaleziono strony w archiwum Forum X-21.")
    content += '<main id="main-content" class="container empty-state"><h1>404 — Strona zaginęła</h1>'
    content += '<p>Poszukiwana strona zaginęła w anomaliach Zony.</p><a class="button" href="index.html">Powrót do strony głównej</a></main>'
    content += page_end()
    write_page(destination / "404.html", content)


class ArchiveValidator(HTMLParser):
    def __init__(self, file: Path, root: Path) -> None:
        super().__init__(convert_charrefs=True)
        self.file = file
        self.root = root
        self.errors: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name for name, _ in attrs]
        duplicate = {name for name in names if names.count(name) > 1}
        if duplicate:
            self.errors.append(f"duplicate attributes on <{tag}>: {', '.join(sorted(duplicate))}")
        values = dict(attrs)
        identifier = values.get("id")
        if identifier:
            if identifier in self.ids:
                self.errors.append(f"duplicate id: {identifier}")
            self.ids.add(identifier)
        if tag == "img" and not values.get("alt"):
            self.errors.append("image without alt text")
        for attribute in ("href", "src"):
            value = values.get(attribute)
            if not value or value.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target = (self.file.parent / value.split("?", 1)[0].split("#", 1)[0]).resolve()
            try:
                target.relative_to(self.root.resolve())
            except ValueError:
                self.errors.append(f"path escapes archive: {value}")
                continue
            if not target.exists():
                self.errors.append(f"missing local target: {value}")


def validate_archive(root: Path) -> list[str]:
    errors: list[str] = []
    for file in sorted(root.glob("*.html")):
        validator = ArchiveValidator(file, root)
        try:
            validator.feed(file.read_text(encoding="utf-8"))
            validator.close()
        except Exception as exc:  # pragma: no cover - defensive reporting
            validator.errors.append(f"parser failure: {exc}")
        errors.extend(f"{file.name}: {message}" for message in validator.errors)
    return errors


def copy_media(media_root: Path | None, destination: Path) -> None:
    if not media_root:
        return
    output = destination / "media"
    output.mkdir(parents=True, exist_ok=True)
    for name in ("avatars", "attachments"):
        source = media_root / name
        if source.exists():
            shutil.copytree(source, output / name, dirs_exist_ok=True)


def select_forum_ids(
    forums: dict[int, dict[str, Any]],
    nodes: dict[int, dict[str, Any]],
    excluded_titles: Iterable[str],
) -> set[int]:
    excluded = {title.strip().casefold() for title in excluded_titles}
    return {
        forum_id
        for forum_id in forums.keys() & nodes.keys()
        if str(nodes[forum_id].get("title") or "").strip().casefold() not in excluded
    }


def build(args: argparse.Namespace) -> dict[str, int]:
    dump_path = args.dump.resolve()
    if not dump_path.is_file():
        raise FileNotFoundError(f"SQL dump not found: {dump_path}")
    media_root = args.media_source.resolve() if args.media_source else None
    if media_root and not media_root.is_dir():
        raise FileNotFoundError(f"Media directory not found: {media_root}")
    output = args.output.resolve()
    if output == Path(output.anchor) or output == Path.home().resolve():
        raise ValueError(f"Refusing unsafe output directory: {output}")
    if output.exists() and not output.is_dir():
        raise FileExistsError(f"Output path is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        if not args.force:
            raise FileExistsError(f"Output directory is not empty: {output}. Use --force to replace it.")
        if not (output / BUILD_MARKER).is_file():
            raise FileExistsError(
                f"Refusing to replace an unmarked directory: {output}. Choose a new output directory."
            )

    tables = load_dump(dump_path)
    nodes = tables["xf_node"]
    forums = {int(row["node_id"]): row for row in tables["xf_forum"]}
    node_by_id = {int(row["node_id"]): row for row in nodes}
    all_threads = tables["xf_thread"]
    all_posts = tables["xf_post"]

    if args.include_hidden:
        threads = all_threads
        posts = all_posts
    else:
        threads = [row for row in all_threads if row.get("discussion_state") == "visible"]
        posts = [row for row in all_posts if row.get("message_state") == "visible"]
    all_forum_ids = set(forums) & set(node_by_id)
    valid_forums = select_forum_ids(forums, node_by_id, args.exclude_forum)
    public_forums = {forum_id: forums[forum_id] for forum_id in valid_forums}
    excluded_forum_ids = all_forum_ids - valid_forums
    excluded_visible_threads = sum(
        1
        for row in all_threads
        if row.get("discussion_state") == "visible"
        and int(row.get("node_id") or -1) in excluded_forum_ids
    )
    threads = [row for row in threads if int(row.get("node_id") or -1) in valid_forums]
    valid_thread_ids = {int(row["thread_id"]) for row in threads}
    posts = [row for row in posts if int(row.get("thread_id") or -1) in valid_thread_ids]

    threads_by_forum: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for thread in threads:
        threads_by_forum[int(thread["node_id"])].append(thread)
    for group in threads_by_forum.values():
        group.sort(key=lambda item: (int(item.get("last_post_date") or 0), int(item["thread_id"])), reverse=True)
    posts_by_thread: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for post in posts:
        posts_by_thread[int(post["thread_id"])].append(post)
    for group in posts_by_thread.values():
        group.sort(key=lambda item: (int(item.get("position") or 0), int(item["post_id"])))
    thread_by_id = {int(thread["thread_id"]): thread for thread in threads}
    users = collect_users(posts, threads, thread_by_id)

    attachment_metadata, attachment_paths = media_attachment_paths(
        tables["xf_attachment"], tables["xf_attachment_data"], media_root
    )
    render_context = RenderContext(attachment_metadata, attachment_paths)
    timezone = ZoneInfo(args.timezone)
    assets_source = Path(__file__).resolve().parent / "assets"

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="x21-build-", dir=output.parent) as temporary:
        staging = Path(temporary) / "site"
        staging.mkdir()
        (staging / BUILD_MARKER).write_text("Generated by x21-generator.\n", encoding="utf-8")
        shutil.copytree(assets_source, staging / "assets")
        copy_media(media_root, staging)
        render_index(staging, nodes, public_forums, threads_by_forum, args.base_url)
        render_users_index(staging, users, media_root, timezone, args.base_url)
        for forum_id in sorted(valid_forums):
            render_forum(staging, node_by_id[forum_id], threads_by_forum.get(forum_id, []), args.base_url)
        for thread in threads:
            forum_node = node_by_id[int(thread["node_id"])]
            render_thread(
                staging,
                thread,
                forum_node,
                posts_by_thread.get(int(thread["thread_id"]), []),
                timezone,
                media_root,
                render_context,
                args.base_url,
            )
        for user_id in sorted(users):
            render_user_profiles(
                staging,
                users[user_id],
                thread_by_id,
                node_by_id,
                timezone,
                media_root,
                args.base_url,
            )
        render_404(staging)
        errors = validate_archive(staging)
        if errors:
            preview = "\n".join(f"  - {item}" for item in errors[:30])
            remainder = f"\n  ... and {len(errors) - 30} more" if len(errors) > 30 else ""
            raise ValueError(f"Generated archive failed validation:\n{preview}{remainder}")
        if output.exists():
            shutil.rmtree(output)
        shutil.move(str(staging), output)

    report = {
        "forums": len(valid_forums),
        "threads": len(threads),
        "posts": len(posts),
        "users": len(users),
        "attachments_found": len(attachment_paths),
        "private_forums_excluded": len(excluded_forum_ids),
        "private_threads_excluded": excluded_visible_threads,
    }
    (output / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dump", type=Path, required=True, help="Path to the XenForo MariaDB SQL dump")
    result.add_argument("--output", type=Path, required=True, help="Directory for the generated archive")
    result.add_argument(
        "--media-source",
        type=Path,
        help="Optional directory containing avatars/ and attachments/ (the old XenForo data directory)",
    )
    result.add_argument("--base-url", help="Optional canonical site URL, e.g. https://x-21.pl")
    result.add_argument("--timezone", default="Europe/Warsaw", help="IANA timezone for displayed dates")
    result.add_argument(
        "--exclude-forum",
        action="append",
        default=list(DEFAULT_EXCLUDED_FORUMS),
        metavar="TITLE",
        help="Forum title to omit completely; repeatable (default: Reaktor)",
    )
    result.add_argument(
        "--include-hidden",
        action="store_true",
        help="Also publish moderated/deleted threads and posts (off by default)",
    )
    result.add_argument("--force", action="store_true", help="Replace a non-empty output directory")
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        report = build(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        "Generated archive: "
        f"{report['forums']} forums, {report['threads']} threads, {report['posts']} posts, "
        f"{report['users']} users, {report['attachments_found']} local attachments."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
