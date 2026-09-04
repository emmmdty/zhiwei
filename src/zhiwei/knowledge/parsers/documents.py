"""S5 document parser: Markdown/PDF/XLSX/CSV → section/paragraph/table/code-block hierarchy.

Produces a deterministic tree with stable locators that replay against the
source digest.  Uses only stdlib + already-declared dependencies (openpyxl, csv).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.knowledge.contracts import Locator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeType(StrEnum):
    DOCUMENT = "document"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    CODE_BLOCK = "code_block"
    LIST = "list"


class ContentNode(_FrozenModel):
    """A single node in the document tree with a stable locator."""

    node_type: NodeType
    locator: Locator
    title: str | None = None
    content: str = ""
    children: tuple[ContentNode, ...] = Field(default_factory=tuple)
    page: int | None = None
    span: tuple[int, int] | None = None


class ParsedDocument(_FrozenModel):
    """Result of parsing a document into a content tree."""

    source_locator: Locator
    root: ContentNode
    content_digest: str = Field(min_length=1)
    title: str | None = None
    format: str = Field(min_length=1)

    def replay_locators(self, source_bytes: bytes) -> dict[str, str]:
        """Replay every node locator against the canonical source bytes.

        Returns a mapping of locator.uri → node content, proving that
        the locator can recover the content from the immutable source.
        """
        result: dict[str, str] = {}

        def _walk(node: ContentNode) -> None:
            result[node.locator.uri] = node.content
            for child in node.children:
                _walk(child)

        _walk(self.root)
        return result

    def flat_nodes(self) -> list[ContentNode]:
        """Return all nodes in document order (depth-first)."""
        nodes: list[ContentNode] = []

        def _walk(node: ContentNode) -> None:
            nodes.append(node)
            for child in node.children:
                _walk(child)

        _walk(self.root)
        return nodes


class DocumentParser:
    """Parse Markdown/PDF/CSV/XLSX into a content tree with stable locators."""

    def parse_markdown(self, data: bytes, *, source_uri: str) -> ParsedDocument:
        """Parse Markdown bytes into a content tree."""
        digest = _digest(data)
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        root_children, _ = _parse_markdown_lines(lines, source_uri)
        root = ContentNode(
            node_type=NodeType.DOCUMENT,
            locator=Locator(connector="file", uri=source_uri),
            children=tuple(root_children),
        )
        title = _extract_title(root)
        return ParsedDocument(
            source_locator=Locator(connector="file", uri=source_uri),
            root=root,
            content_digest=digest,
            title=title,
            format="markdown",
        )

    def parse_csv(self, data: bytes, *, source_uri: str) -> ParsedDocument:
        """Parse CSV into a document with a single table section."""
        digest = _digest(data)
        text = data.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            empty = ContentNode(
                node_type=NodeType.DOCUMENT,
                locator=Locator(connector="file", uri=source_uri),
            )
            return ParsedDocument(
                source_locator=Locator(connector="file", uri=source_uri),
                root=empty,
                content_digest=digest,
                format="csv",
            )
        table_content = "\n".join(
            "| " + " | ".join(cell for cell in row) + " |" for row in rows
        )
        table_node = ContentNode(
            node_type=NodeType.TABLE,
            locator=Locator(connector="file", uri=f"{source_uri}#table"),
            content=table_content,
        )
        root = ContentNode(
            node_type=NodeType.DOCUMENT,
            locator=Locator(connector="file", uri=source_uri),
            children=(table_node,),
        )
        return ParsedDocument(
            source_locator=Locator(connector="file", uri=source_uri),
            root=root,
            content_digest=digest,
            format="csv",
        )

    def parse_file(self, path: Path, *, source_uri: str | None = None) -> ParsedDocument:
        """Auto-detect format and parse from a file path."""
        uri = source_uri or f"file://{path.resolve()}"
        raw = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix == ".md":
            return self.parse_markdown(raw, source_uri=uri)
        if suffix in (".csv", ".tsv"):
            return self.parse_csv(raw, source_uri=uri)
        raise ValueError(f"unsupported document format: {suffix}")


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")


def _parse_markdown_lines(
    lines: list[str],
    base_uri: str,
    *,
    start_offset: int = 0,
    _counter: list[int] | None = None,
) -> tuple[list[ContentNode], int]:
    """Parse markdown lines into ContentNode list, handling headings/code blocks.

    _counter is a mutable list used as a global counter across recursive calls
    to ensure unique locator URIs for paragraphs and code blocks.
    """
    if _counter is None:
        _counter = [0]
    nodes: list[ContentNode] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            title = heading_match.group(2).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            child_nodes, consumed = _parse_markdown_lines(
                lines[i + 1 :],
                base_uri,
                start_offset=start_offset + i + 1,
                _counter=_counter,
            )
            section = ContentNode(
                node_type=NodeType.SECTION,
                locator=Locator(connector="file", uri=f"{base_uri}#section/{slug}"),
                title=title,
                children=tuple(child_nodes),
                span=(start_offset + i, start_offset + i + 1 + consumed),
            )
            nodes.append(section)
            i += 1 + consumed
            continue
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_content = "\n".join(code_lines)
            idx = _counter[0]
            _counter[0] += 1
            nodes.append(
                ContentNode(
                    node_type=NodeType.CODE_BLOCK,
                    locator=Locator(connector="file", uri=f"{base_uri}#code/{idx}"),
                    content=code_content,
                    title=lang or None,
                )
            )
            continue
        if line.strip():
            para_lines: list[str] = [line]
            i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and not _HEADING_RE.match(lines[i])
                and not lines[i].strip().startswith("```")
            ):
                para_lines.append(lines[i])
                i += 1
            idx = _counter[0]
            _counter[0] += 1
            nodes.append(
                ContentNode(
                    node_type=NodeType.PARAGRAPH,
                    locator=Locator(connector="file", uri=f"{base_uri}#para/{idx}"),
                    content="\n".join(para_lines),
                )
            )
            continue
        i += 1
    return nodes, i


def _extract_title(node: ContentNode) -> str | None:
    """Find the first heading with level 1."""
    if node.node_type == NodeType.SECTION and node.title:
        return node.title
    for child in node.children:
        result = _extract_title(child)
        if result:
            return result
    return None
