"""Scorer 隔离层：评分输入面与被测系统内部状态严格隔离。"""

from __future__ import annotations

from zhiwei.evals.scorers.base import Scorer, ScorerInput, ScorerVerdict
from zhiwei.evals.scorers.generic import ExactMatchScorer
from zhiwei.evals.scorers.human import HumanJudgeProtocol, HumanJudgeRefused

__all__ = [
    "ExactMatchScorer",
    "HumanJudgeProtocol",
    "HumanJudgeRefused",
    "Scorer",
    "ScorerInput",
    "ScorerVerdict",
]
