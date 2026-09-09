"""uvicorn 部署入口：`uvicorn zhiwei.asgi:app`。

compose/生产参考的唯一 API 进程入口。settings 从环境变量加载（load_settings 不读
.env）；缺配置在导入期抛错 → 容器启动即失败（fail closed），而不是带病监听。
"""

from __future__ import annotations

from zhiwei.app import create_app
from zhiwei.config.settings import load_settings

settings = load_settings()
app = create_app(settings)
