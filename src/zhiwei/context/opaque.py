"""S3 §5 hidden reasoning 销毁点：正文 → 确定性 opaque ref。

「正文不出现在 PG/Object/Temporal/Redis/log/trace」的实现语义：reasoning 正文
在进入投影/编译产物/任何持久化面之前被替换为 `opaque:sha256:<hex>` ref 加
token 计数元数据。替换是纯函数——同一正文两次替换逐字节一致（sha256 与校准
估算器都不依赖环境），因此事件重放、幂等重试与 digest 链复算得到同一结果。

允许残留的只有 digest 与 token 计数这类元数据；明文正文只存在于替换发生前的
请求处理内存中，`context.opaque.terminal` 之后连 opaque 项一并从投影移除。
"""

from __future__ import annotations

from typing import Any

from zhiwei.context.budget import estimate_tokens_text
from zhiwei.contracts.canonical import digest_bytes

OPAQUE_REF_PREFIX = "opaque:"

# reasoning 正文键名清单（F-R2-06/T-P3.7）：默认四键——S3 原单键口径扩展，
# 覆盖各 provider 常用 reasoning 字段名。部署期常量；显式收窄经 scrub 的
# keys 参数（UoW 构造期注入）。
SCRUB_KEYS: tuple[str, ...] = (
    "hidden_reasoning",
    "reasoning_content",
    "thinking",
    "reasoning",
)


def opaque_reasoning_ref(body: str) -> dict[str, Any]:
    """销毁正文，返回确定性元数据 ref（同正文恒同 ref）。"""
    return {
        "opaque_ref": f"{OPAQUE_REF_PREFIX}{digest_bytes(body.encode('utf-8'))}",
        "token_count": estimate_tokens_text(body),
    }


def scrub_hidden_reasoning(value: Any, keys: tuple[str, ...] | None = None) -> Any:
    """Deep-replace every string-valued reasoning key with its opaque ref.

    递归遍历 dict/list：reasoning 可能出现在 content/updates/entries/value 等
    任意 payload 位置。只有 str 值被视为正文本体；已是 ref 结构的值（重放已
    scrub 的事件）保持不变——替换天然幂等。未命中时原对象原样返回，不产生
    副本，保证对无 reasoning 的既有负载零行为差异。

    键名清单可配置（F-R2-06/T-P3.7）：默认覆盖 SCRUB_KEYS 四键；调用方可显式
    收窄（keys=("hidden_reasoning",) 恢复 S3 原口径）。keys 是部署期常量而非
    请求期可变值——确定性纯函数语义不变。
    """
    scrub_keys = SCRUB_KEYS if keys is None else keys
    if isinstance(value, dict):
        changed = False
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            if key in scrub_keys and isinstance(item, str):
                scrubbed[key] = opaque_reasoning_ref(item)
                changed = True
            else:
                replaced = scrub_hidden_reasoning(item, scrub_keys)
                changed = changed or replaced is not item
                scrubbed[key] = replaced
        return scrubbed if changed else value
    if isinstance(value, list):
        replaced_items = [scrub_hidden_reasoning(item, scrub_keys) for item in value]
        if all(
            replaced is item
            for replaced, item in zip(replaced_items, value, strict=True)
        ):
            return value
        return replaced_items
    if isinstance(value, tuple):
        # python-mode payload（model_dump(mode="python")）可携带 tuple；逐元素
        # 处理并保持 tuple 形状，JSON 化前同样不漏正文。
        replaced_tuple = tuple(scrub_hidden_reasoning(item, scrub_keys) for item in value)
        if all(
            replaced is item
            for replaced, item in zip(replaced_tuple, value, strict=True)
        ):
            return value
        return replaced_tuple
    return value
