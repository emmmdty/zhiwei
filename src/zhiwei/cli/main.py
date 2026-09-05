"""zhiwei 命令行入口。

错误处理约定：任何失败都转成一行可读信息 + 非零退出码，不得向用户抛裸 traceback；也不得
回显凭据。因此关闭 typer 的 pretty exception——未捕获异常宁可静默失败也不印栈。
"""

from __future__ import annotations

import typer

from zhiwei.cli import (
    assets,
    context,
    db,
    dev,
    evals,
    models,
    providers,
    release,
    risk,
    runtime,
    sources,
)

app = typer.Typer(
    help="知微 ZhiWei — 企业 Agent Core 命令行工具",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(dev.app, name="dev", help="开发诊断与调试命令")
app.add_typer(db.app, name="db", help="数据库迁移与 schema 检查")
app.add_typer(assets.app, name="assets", help="冻结基准资产与校验和 lock")
app.add_typer(evals.app, name="eval", help="评测执行与密封")
app.add_typer(release.app, name="release", help="Release 声明检查与出处 attestation")
app.add_typer(risk.app, name="risk", help="Numeric Risk Detector Pack：风险发现生成与校验")
app.add_typer(runtime.app, name="runtime", help="Agent Runtime 诊断与评测绑定")
app.add_typer(context.app, name="verify", help="验证上下文清单与线绑定完整性")
app.add_typer(models.app, name="models", help="模型 profiles 与 fixture attestation")
app.add_typer(providers.app, name="provider", help="Provider lifecycle：inspect/test/admit")
app.add_typer(sources.app, name="source", help="知识源管理：sync/status 操作")


if __name__ == "__main__":
    app()
