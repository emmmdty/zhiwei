# S11 local-product 应用镜像（specs/s11 §2：非 root、health/readiness、无源码密钥）。
# 基础镜像与 uv 构建器都 digest pin；.dockerignore 保证 .env/tests/evals/docs 不进构建
# 上下文（tests/contract/deploy/test_app_image.py 冻结）。
# 根文件系统只读运行（compose read_only: true）：唯一可写面是 tmpfs /tmp 与挂载卷。
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

LABEL org.opencontainers.image.source="https://example.invalid/zhiwei" \
      org.opencontainers.image.description="ZhiWei Agent Core local-product runtime"

COPY --from=ghcr.io/astral-sh/uv:0.11.8@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 \
     /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 依赖层先于源码层：pyproject/uv.lock 未变时缓存命中
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

# 非 root 运行账号（数字 uid，compose user 声明与镜像内属主一致）
RUN useradd --uid 10001 --user-group --home-dir /nonexistent --shell /usr/sbin/nologin zhiwei
USER 10001:10001

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# 默认健康检查面向 API 进程；worker 容器在 compose 覆盖为 readiness 文件检查
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
  CMD ["python", "-m", "zhiwei.healthcheck", "http://127.0.0.1:8000/healthz"]

CMD ["uvicorn", "zhiwei.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
