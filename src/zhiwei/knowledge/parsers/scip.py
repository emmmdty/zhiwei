"""SCIP (Source Code Intelligence Protocol) parser stub.

SCIP is the preferred parser for code symbol indexing. This stub defines
the interface that the full implementation will follow. When SCIP is
unavailable, the system falls back to tree-sitter or exact search.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.connectors.github import SymbolKind


class SCIPError(Exception):
    """Base error for SCIP parser operations."""


class SCIPUnavailableError(SCIPError):
    """Raised when SCIP is not available for parsing."""


class SCIPParseError(SCIPError):
    """Raised when SCIP fails to parse a file."""


class SCIPSymbol(BaseModel):
    """A symbol extracted by SCIP parsing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=new_id)
    name: str = Field(min_length=1)
    kind: SymbolKind
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    definition: str | None = None
    references: tuple[str, ...] = Field(default_factory=tuple)
    implementation: str | None = None
    imports: tuple[str, ...] = Field(default_factory=tuple)
    dependencies: tuple[str, ...] = Field(default_factory=tuple)
    test_of: str | None = None
    documentation: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SCIPIndex(BaseModel):
    """Result of SCIP indexing for a file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str
    language: str
    symbols: tuple[SCIPSymbol, ...] = Field(default_factory=tuple)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SCIPParser:
    """Parser using SCIP for code symbol extraction.

    SCIP provides rich symbol information including definitions, references,
    implementations, and cross-repository relationships. This stub defines
    the interface; the full implementation depends on a SCIP binary or
    library being available at runtime.
    """

    PARSER_VERSION = "scip-stub-1"

    def __init__(self) -> None:
        self._available = False

    @property
    def is_available(self) -> bool:
        """Whether the SCIP parser is available for use."""
        return self._available

    def parse_file(
        self,
        file_path: str,
        content: str,
        *,
        language: str | None = None,
    ) -> SCIPIndex:
        """Parse a file using SCIP and extract symbols.

        Args:
            file_path: Path to the file within the repository.
            content: File content as string.
            language: Optional language hint.

        Returns:
            SCIPIndex with extracted symbols.

        Raises:
            SCIPUnavailableError: If SCIP is not installed.
            SCIPParseError: If parsing fails.
        """
        if not self._available:
            raise SCIPUnavailableError(
                "SCIP parser is not available; use tree-sitter or exact search fallback"
            )

        raise SCIPParseError("SCIP parser stub: full implementation pending")

    def parse_directory(
        self,
        directory_path: str,
        files: dict[str, str],
        *,
        language_map: dict[str, str] | None = None,
    ) -> list[SCIPIndex]:
        """Parse all files in a directory using SCIP.

        Args:
            directory_path: Root directory path.
            files: Mapping of file_path -> content for all files.
            language_map: Optional mapping of file extension -> language.

        Returns:
            List of SCIPIndex results, one per file.
        """
        if not self._available:
            raise SCIPUnavailableError(
                "SCIP parser is not available; use tree-sitter or exact search fallback"
            )

        raise SCIPParseError("SCIP parser stub: full implementation pending")

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

    def enable(self) -> None:
        """Enable the SCIP parser (called when SCIP binary is detected)."""
        self._available = True

    def disable(self) -> None:
        """Disable the SCIP parser."""
        self._available = False
