"""S2 runtime: handler package for task handler base, registry, and implementations。

事实源：design doc §4.3、S2-T2 plan。
"""

from __future__ import annotations

from zhiwei.runtime.handlers.base import TaskHandler
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry

__all__ = ["TaskHandler", "TaskHandlerRegistry"]
