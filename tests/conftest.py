"""tests/ 根 conftest：路径注入、httpx 类宇宙归一、CLI 输出消色。

只做进程级环境归一，不声明任何 fixture（避免影响既有测试收集行为）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx2
import rich.console

httpx2.alias_httpx()

# CLI 契约测试断言 help 输出含选项文本（--format/--suite…）。GitHub Actions
# runner 会向 step 注入 FORCE_COLOR（rich 按存在性判定，空串也触发），env 层
# 覆盖存在时序与 runner 注入行为的不确定性——直接钉死进程内所有 rich Console：
# 着色关闭、按非终端渲染，与本地非 tty 捕获一致。


def _plain_console_init(self: rich.console.Console, *args: object, **kwargs: object) -> None:
    kwargs["no_color"] = True
    kwargs["force_terminal"] = False
    rich.console.Console.__init__orig(self, *args, **kwargs)


rich.console.Console.__init__orig = rich.console.Console.__init__  # type: ignore[attr-defined]
rich.console.Console.__init__ = _plain_console_init  # type: ignore[assignment,misc]

sys.path.insert(0, str(Path(__file__).resolve().parent))
