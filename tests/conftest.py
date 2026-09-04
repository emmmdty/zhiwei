"""tests/ 根 conftest：把 tests/ 加入 sys.path，使 fixtures.policy_fake 可导入。

只做路径注入，不声明任何 fixture（避免影响既有测试收集行为）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx2

httpx2.alias_httpx()

sys.path.insert(0, str(Path(__file__).resolve().parent))
