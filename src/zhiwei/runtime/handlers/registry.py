"""S2 runtime: TaskHandlerRegistry for registering and looking up task handlers。

事实源：design doc §4.3、S2-T2 plan。

Registry maps task primitive types to handler implementations and validates
completeness before a run starts.
"""

from __future__ import annotations

from zhiwei.runtime.handlers.base import TaskHandler


class TaskHandlerRegistryError(RuntimeError):
    """Registry error (duplicate, missing, version mismatch)."""


class TaskHandlerRegistry:
    """Registry of task handlers indexed by primitive type and version.

    Validates completeness: all task types in a graph must have registered handlers
    before a run starts.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, dict[int, TaskHandler]] = {}

    def register(self, handler: TaskHandler) -> None:
        """Register a task handler.

        Raises TaskHandlerRegistryError if a handler with the same type and version
        is already registered.
        """
        ptype = handler.primitive_type
        version = handler.handler_version
        if ptype not in self._handlers:
            self._handlers[ptype] = {}
        if version in self._handlers[ptype]:
            raise TaskHandlerRegistryError(
                f"Handler for '{ptype}' version {version} already registered"
            )
        self._handlers[ptype][version] = handler

    def get(self, primitive_type: str, version: int = 1) -> TaskHandler:
        """Get a handler by primitive type and version."""
        versions = self._handlers.get(primitive_type)
        if versions is None:
            raise TaskHandlerRegistryError(
                f"No handler registered for primitive type '{primitive_type}'"
            )
        handler = versions.get(version)
        if handler is None:
            available = sorted(versions.keys())
            raise TaskHandlerRegistryError(
                f"No handler for '{primitive_type}' version {version}; "
                f"available versions: {available}"
            )
        return handler

    def has_handler(self, primitive_type: str, version: int = 1) -> bool:
        """Check if a handler is registered for the given type and version."""
        versions = self._handlers.get(primitive_type)
        if versions is None:
            return False
        return version in versions

    def registered_types(self) -> set[str]:
        """Return all registered primitive types."""
        return set(self._handlers.keys())

    def validate_completeness(self, task_types: set[str]) -> None:
        """Validate that all required task types have registered handlers.

        Raises TaskHandlerRegistryError if any type is missing.
        """
        registered = self.registered_types()
        missing = task_types - registered
        if missing:
            raise TaskHandlerRegistryError(
                f"Missing handlers for task types: {sorted(missing)}"
            )
