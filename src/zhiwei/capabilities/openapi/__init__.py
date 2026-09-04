"""OpenAPI 3.1 capability provider.

Imports, validates and normalizes OpenAPI 3.1 specifications into governed
tool definitions. Enforces $ref limits, operation-only selection, immutable
host and typed parameters.
"""

from __future__ import annotations
