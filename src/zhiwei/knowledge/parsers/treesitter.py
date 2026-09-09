"""tree-sitter parser stub.

Fallback parser when SCIP is unavailable. Provides basic symbol extraction
using tree-sitter grammars. Less precise than SCIP but covers more languages
out of the box.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.connectors.github import SymbolKind
from zhiwei.knowledge.parsers.scip import SCIPSymbol


class TreeSitterError(Exception):
    """Base error for tree-sitter parser operations."""


class TreeSitterUnavailableError(TreeSitterError):
    """Raised when tree-sitter is not available for parsing."""


class TreeSitterParseError(TreeSitterError):
    """Raised when tree-sitter fails to parse a file."""


class TreeSitterSymbol(BaseModel):
    """A symbol extracted by tree-sitter parsing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=new_id)
    name: str = Field(min_length=1)
    kind: SymbolKind
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    node_type: str = ""
    definition: str | None = None
    references: tuple[str, ...] = Field(default_factory=tuple)
    imports: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TreeSitterIndex(BaseModel):
    """Result of tree-sitter indexing for a file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str
    language: str
    symbols: tuple[TreeSitterSymbol, ...] = Field(default_factory=tuple)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TreeSitterParser:
    """Parser using tree-sitter for basic code symbol extraction.

    tree-sitter provides syntax-aware parsing with incremental updates.
    This stub defines the interface; the full implementation depends on
    tree-sitter and language grammars being available at runtime.
    """

    PARSER_VERSION = "treesitter-stub-1"

    def __init__(self) -> None:
        self._available = False
        self._loaded_grammars: set[str] = set()

    @property
    def is_available(self) -> bool:
        """Whether the tree-sitter parser is available for use."""
        return self._available

    def parse_file(
        self,
        file_path: str,
        content: str,
        *,
        language: str | None = None,
    ) -> TreeSitterIndex:
        """Parse a file using tree-sitter and extract symbols.

        Args:
            file_path: Path to the file within the repository.
            content: File content as string.
            language: Optional language hint.

        Returns:
            TreeSitterIndex with extracted symbols.

        Raises:
            TreeSitterUnavailableError: If tree-sitter is not installed.
            TreeSitterParseError: If parsing fails.
        """
        if not self._available:
            raise TreeSitterUnavailableError(
                "tree-sitter parser is not available; use exact search fallback"
            )

        raise TreeSitterParseError("tree-sitter parser stub: full implementation pending")

    def parse_files_batch(
        self,
        files: dict[str, str],
        *,
        language_map: dict[str, str] | None = None,
    ) -> list[TreeSitterIndex]:
        """Parse multiple files using tree-sitter.

        Args:
            files: Mapping of file_path -> content.
            language_map: Optional mapping of file extension -> language.

        Returns:
            List of TreeSitterIndex results, one per file.
        """
        if not self._available:
            raise TreeSitterUnavailableError(
                "tree-sitter parser is not available; use exact search fallback"
            )

        raise TreeSitterParseError("tree-sitter parser stub: full implementation pending")

    def to_scip_symbols(self, index: TreeSitterIndex) -> list[SCIPSymbol]:
        """Convert tree-sitter symbols to SCIP-compatible format.

        This enables the system to use a unified symbol representation
        regardless of which parser produced the results.
        """
        scip_symbols: list[SCIPSymbol] = []
        for sym in index.symbols:
            scip_symbols.append(
                SCIPSymbol(
                    id=sym.id,
                    name=sym.name,
                    kind=sym.kind,
                    line_start=sym.line_start,
                    line_end=sym.line_end,
                    definition=sym.definition,
                    references=sym.references,
                    imports=sym.imports,
                    metadata=sym.metadata,
                )
            )
        return scip_symbols

    def detect_language(self, file_path: str) -> str | None:
        """Detect programming language from file extension.

        Returns None for unsupported file types.
        """
        _EXTENSION_MAP: dict[str, str] = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".jsx": "javascript",
            ".tsx": "typescript",
            ".java": "java",
            ".go": "go",
            ".rs": "rust",
            ".c": "c",
            ".cpp": "cpp",
            ".h": "c",
            ".hpp": "cpp",
            ".rb": "ruby",
            ".swift": "swift",
            ".kt": "kotlin",
            ".scala": "scala",
            ".cs": "csharp",
            ".php": "php",
        }
        for ext, lang in _EXTENSION_MAP.items():
            if file_path.endswith(ext):
                return lang
        return None

    def load_grammar(self, language: str) -> bool:
        """Attempt to load a tree-sitter grammar for a language.

        Returns True if the grammar was loaded successfully.
        """
        return language in self._loaded_grammars

    def enable(self) -> None:
        """Enable the tree-sitter parser."""
        self._available = True

    def disable(self) -> None:
        """Disable the tree-sitter parser."""
        self._available = False
        self._loaded_grammars.clear()
