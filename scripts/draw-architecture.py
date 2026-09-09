#!/usr/bin/env python3
"""README 架构图生成（E-R1 后续产品化窗口引入）。

设计约束：
- 纯 Pillow 确定性绘制（固定坐标，无随机），中文标签，浅色主题（GitHub README 默认可读）。
- 运行：uv run --with pillow python scripts/draw-architecture.py
  （pillow 经 evals extra 的 reportlab 传递已在 venv 中；--with 显式化，零 pyproject 依赖改动）
- 产物 assets/architecture.png（≤300KB，README 嵌入）。

图的边界与 compose 实际拓扑对齐：proxy 是唯一发布面（8090）、应用端口不直接发布、
Agent Core 七域、Apps 构建于同一 Core、基础设施 8 件（specs/s11 compose 口径）。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1500, 1150
BG = "#ffffff"
INK = "#1f2328"
MUTED = "#57606a"
LINE = "#d0d7de"
BLUE = "#0969da"
BLUE_BG = "#f0f6ff"
GREEN = "#1a7f37"
GREEN_BG = "#f0fff4"
ORANGE = "#9a6700"
ORANGE_BG = "#fff8c5"
PURPLE = "#8250df"
PURPLE_BG = "#fbefff"
GRAY_BG = "#f6f8fa"

FONT_DIRS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/mnt/c/Windows/Fonts/NotoSansSC-VF.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    order = [FONT_DIRS[0], *FONT_DIRS[1:]] if bold else FONT_DIRS
    for path in order:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)  # type: ignore[return-value]


def chip(
    d: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    title: str,
    sub: str,
    border: str = LINE,
    fill: str = BG,
    tsize: int = 19,
    ssize: int = 14,
) -> None:
    x0, y0, x1, _y1 = xy
    d.rounded_rectangle(xy, radius=10, fill=fill, outline=border, width=2)
    cx = (x0 + x1) // 2
    d.text((cx, y0 + 14), title, font=font(tsize, bold=True), fill=INK, anchor="ma")
    if sub:
        d.text((cx, y0 + 14 + tsize + 8), sub, font=font(ssize), fill=MUTED, anchor="ma")


def band_label(d: ImageDraw.ImageDraw, x: int, y: int, text: str, color: str) -> None:
    d.text((x, y), text, font=font(21, bold=True), fill=color, anchor="lm")


def arrow(d: ImageDraw.ImageDraw, x: int, y0: int, y1: int, color: str = MUTED) -> None:
    d.line([(x, y0), (x, y1)], fill=color, width=3)
    d.polygon([(x - 7, y1 - 12), (x + 7, y1 - 12), (x, y1)], fill=color)


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 标题
    d.text((W // 2, 44), "ZhiWei 知微 · Agent Core 架构", font=font(34, bold=True), fill=INK, anchor="mm")
    d.text(
        (W // 2, 86),
        "event-sourced · RLS 纵深隔离 · Evidence 可复算 · 每个对外数字绑定 sealed artifact",
        font=font(17),
        fill=MUTED,
        anchor="mm",
    )

    # Band 0：访问面
    band_label(d, 40, 132, "访问面", MUTED)
    chip(d, (420, 112, 760, 178), "Web Workbench · Studio", "apps/web（React）", border=LINE)
    chip(d, (800, 112, 1120, 178), "API / SDK 嵌入方", "OpenAPI · 已发布 Agent")

    # Band 1：发布面
    band_label(d, 40, 232, "发布面", ORANGE)
    chip(
        d,
        (420, 208, 1120, 272),
        "proxy · nginx :8090",
        "唯一入口 —— TLS 终结 · 静态资源 · 反代 API（应用端口不直接发布）",
        border=ORANGE,
        fill=ORANGE_BG,
    )
    arrow(d, 590, 178, 208)
    arrow(d, 960, 178, 208)
    arrow(d, 770, 272, 330)

    # Band 2：Agent Core
    d.rounded_rectangle((60, 330, 1440, 742), radius=14, fill=BLUE_BG, outline=BLUE, width=3)
    d.text((90, 352), "Agent Core", font=font(24, bold=True), fill=BLUE, anchor="lm")
    d.text(
        (1440 - 90, 352),
        "权威状态 = 事件 + reducer · 模型切换完整迁移 inventory",
        font=font(15),
        fill=MUTED,
        anchor="rm",
    )
    row1_y, row2_y = 392, 502
    chip(d, (90, row1_y, 520, 486), "Runtime", "Temporal Durable Runs · outbox · 崩溃窗口契约", border=BLUE)
    chip(d, (550, row1_y, 950, 486), "Canonical Context", "ContextManifest · 三 wire protocol · 预算门禁", border=BLUE)
    chip(d, (980, row1_y, 1410, 486), "Knowledge Fabric", "Source Ledger · snapshot · ACL · 代码/表格/文档", border=BLUE)
    chip(d, (90, row2_y, 520, 596), "Memory", "user / team / case 全生命周期治理", border=BLUE)
    chip(d, (550, row2_y, 950, 596), "Models & Tools", "Capability Hub · MCP / OpenAPI / Skills 准入", border=BLUE)
    chip(d, (980, row2_y, 1410, 596), "Evidence", "Fact / Quote · verifier · ActionReceipt", border=BLUE)

    # Policy strip（Core 内底部）
    d.rounded_rectangle((90, 636, 1410, 712), radius=10, fill=BG, outline=BLUE, width=2)
    d.text(
        (750, 674),
        "Policy 纵深隔离：OIDC (PKCE) → RBAC → OPA → PostgreSQL RLS（19 张租户表 FORCE RLS）",
        font=font(18, bold=True),
        fill=INK,
        anchor="mm",
    )

    # Band 3：Agent Apps（构建于同一 Core）
    band_label(d, 40, 800, "Agent Apps", GREEN)
    for i, (name, sub) in enumerate(
        [("Ask", "跨源知识研究 · Evidence 绑定"), ("Discover", "持续风险发现 · 受审批动作"), ("ChangeBrief", "GitHub 触发 · 第三 App 样板")]
    ):
        x0 = 420 + i * 240
        chip(d, (x0, 776, x0 + 220, 842), name, sub, border=GREEN, fill=GREEN_BG, tsize=20, ssize=13)
    d.text((40, 776), "构建于同一 Core", font=font(14), fill=MUTED, anchor="la")

    # Band 4：基础设施
    band_label(d, 40, 922, "基础设施", PURPLE)
    infra = [
        ("PostgreSQL", "多库分离 · RLS"),
        ("Temporal", "durable exec"),
        ("OpenSearch", "hybrid 检索"),
        ("Garage", "S3 对象存储"),
        ("Redis", "cache"),
        ("Keycloak", "OIDC IdP"),
        ("OPA", "策略引擎"),
        ("OTel", "观测导出"),
    ]
    cw, gap, x0 = 150, 10, 200
    for i, (name, sub) in enumerate(infra):
        cx0 = x0 + i * (cw + gap)
        chip(d, (cx0, 882, cx0 + cw, 962), name, sub, border=LINE, fill=GRAY_BG, tsize=16, ssize=11)
    # Apps 构建于 Core：自 Apps 顶指向 Core 底
    arrow(d, 750, 776, 742)
    d.line([(200, 962), (200, 1010), (1450, 1010), (1450, 962)], fill=PURPLE, width=2)
    d.text(
        (750, 1034),
        "持久化 / 编排 / 检索 / 对象 / 缓存 / 身份 / 策略 / 观测 —— compose 单栈交付（Kustomize reference 同构）",
        font=font(15),
        fill=PURPLE,
        anchor="mm",
    )

    out = Path("assets/architecture.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, optimize=True)
    print(f"written {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
