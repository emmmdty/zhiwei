"""S0 executor 集：empty（plumbing 冒烟）与 legacy（冻结资产适配）。"""

from __future__ import annotations

from zhiwei.evals.executors.base import EvalExecutor
from zhiwei.evals.executors.empty import EmptyExecutor
from zhiwei.evals.executors.legacy import LegacyExecutor

__all__ = ["EmptyExecutor", "EvalExecutor", "LegacyExecutor"]
