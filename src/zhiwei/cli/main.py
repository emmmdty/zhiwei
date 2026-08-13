"""zhiwei 命令行入口。

错误处理约定：任何失败都转成一行可读信息 + 非零退出码，不得向用户抛裸 traceback；也不得
回显凭据。因此关闭 typer 的 pretty exception——未捕获异常宁可静默失败也不印栈。
"""

from __future__ import annotations

import typer

from zhiwei.cli import db, dev

app = typer.Typer(
    help="知微 ZhiWei — 企业 Agent Core 命令行工具",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(dev.app, name="dev", help="开发诊断与调试命令")
app.add_typer(db.app, name="db", help="数据库迁移与 schema 检查")


if __name__ == "__main__":
    app()
