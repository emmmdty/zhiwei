"""tests/ 根 conftest：路径注入、httpx 类宇宙归一、CLI 输出消色。

只做进程级环境归一，不声明任何 fixture（避免影响既有测试收集行为）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx2

httpx2.alias_httpx()

# CLI 契约测试断言 help 输出含选项文本（--format/--suite…）。GitHub Actions
# runner 会向 step 注入 FORCE_COLOR，rich 依据它对 CLI help 渲染 ANSI 码切断
# 子串断言；且 FORCE_COLOR 优先级高于 NO_COLOR，必须移除而不是覆盖。
os.environ.pop("FORCE_COLOR", None)
os.environ.setdefault("NO_COLOR", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))
