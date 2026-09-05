"""ChangeBrief pack impact-analysis skill entry.

skills/impact-analysis.yaml 声明的 entry 模块：S10-T5 只冻结入口契约（模块存在 +
入口签名），运行期实现由 S10-T6 经公共扩展点落地。依赖纪律见
tests/architecture/test_app_boundaries.py 的 pack runtime 约束——不得直接导入
DB / model provider / 基础设施工具。
"""

from __future__ import annotations

from typing import Any


def analyze_impact(
    repository: str,
    commit_or_pr: dict[str, Any],
    candidates: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Compute the impact report consumed by the Verify/Synthesize tasks."""
    raise NotImplementedError("impact-analysis runtime ships in S10-T6")
