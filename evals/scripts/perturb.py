"""扰动引擎（防污染机制 1）。

设计原则：
1. **声明式**：所有扰动写在 ``SPECS`` 里，逐条可读、可审计、可 diff。不使用随机扰动——
   随机扰动无法解释"为什么这条题世界知识会答错"。
2. **结构性冲突而非表层替换**：优先扰动"关系"（归属 / 次序 / 分类），而不只是换个名字。
   纯词法混淆已被证明容易被现代 LLM 绕过（见 docs/BENCHMARK.md L2）。
3. **可复算**：manifest 记录 before/after，``validate_corpus.py`` 可逐条回验。

用法::

    from perturb import apply_perturbations
    rows, manifest = apply_perturbations("xiyouji", base_rows)
"""

from __future__ import annotations

import copy
from typing import Any

SEED = 20260811

# 语料版本：**故意不写构建日期**。
# 若 manifest 里嵌入 date.today()，隔天重建产物就会变化，CHECKSUMS 无故对不上、CI 无故变红——
# 一个只在跨天时才暴露的伪失败。生成时间由 git 历史记录，版本号在语料语义变化时手动 +1。
CORPUS_VERSION = "v1.0.0"

# ---------------------------------------------------------------- 扰动声明

# kind:
#   swap    —— 交换两条记录在某字段上的取值（制造次序/归属冲突）
#   rename  —— 改写某条记录的某字段取值
#   retype  —— 改写分类字段（制造"类别归属"层面的冲突）
SPECS: dict[str, list[dict[str, Any]]] = {
    "xiyouji": [
        {"id": "XY-P01", "kind": "swap", "key_field": "nan_no", "keys": [24, 25],
         "field": "opponent",
         "rationale": "金角/银角次序是强世界知识，互换后凭记忆必错"},
        {"id": "XY-P02", "kind": "rename", "key_field": "nan_no", "keys": [36, 37, 38],
         "field": "opponent", "after": "玄鲤大王",
         "rationale": "通天河妖王改名，检验是否真读了数据"},
        {"id": "XY-P03", "kind": "swap", "key_field": "nan_no", "keys": [61, 63],
         "field": "nan_name",
         "rationale": "难名互换，破坏『难序号→难名』的记忆映射"},
        {"id": "XY-P04", "kind": "retype", "key_field": "nan_no", "keys": [47],
         "field": "category", "after": "妖魔",
         "rationale": "火焰山由天灾改判为妖魔，影响所有按类别的聚合题"},
        {"id": "XY-P05", "kind": "rename", "key_field": "nan_no", "keys": [81],
         "field": "opponent", "after": "白鼍",
         "rationale": "第 81 难对手改名，直击最强的记忆锚点"},
        {"id": "XY-P05b", "kind": "rename", "key_field": "nan_no", "keys": [81],
         "field": "nan_name", "after": "通天河白鼍淹经",
         "rationale": "配套 XY-P05：难名中含对手名，必须同步改写以保持记录内部自洽。"
                      "若只改 opponent 而留 nan_name='通天河老鼋淹经'，"
                      "同一行内即出现自相矛盾，会把『扰动题』污染成『冲突题』"},
        {"id": "XY-P06", "kind": "rename", "key_field": "nan_no", "keys": [13],
         "field": "helper", "after": "观音",
         "rationale": "援手归属改写，制造『谁救的』层面的结构冲突"},
        {"id": "XY-P07", "kind": "rename", "key_field": "nan_no", "keys": [32],
         "field": "location", "after": "洛水河",
         "rationale": "地点改写，影响按地点的分组聚合"},
        {"id": "XY-P08", "kind": "rename", "key_field": "nan_no", "keys": [46],
         "field": "opponent", "after": "四耳猕猴",
         "rationale": "六耳→四耳，数字型记忆锚点"},
    ],
    "shuihu": [
        {"id": "SH-P01", "kind": "swap", "key_field": "rank", "keys": [18, 24],
         "field": "name",
         "rationale": "座次 18/24 的人物互换，排名类世界知识必错"},
        {"id": "SH-P02", "kind": "swap", "key_field": "rank", "keys": [18, 24],
         "field": "nickname",
         "rationale": "绰号随人物一并互换，保持记录内部自洽"},
        {"id": "SH-P03", "kind": "swap", "key_field": "rank", "keys": [3, 4],
         "field": "name",
         "rationale": "吴用/公孙胜座次互换"},
        {"id": "SH-P04", "kind": "swap", "key_field": "rank", "keys": [3, 4],
         "field": "nickname",
         "rationale": "同上，保持自洽"},
        {"id": "SH-P05", "kind": "rename", "key_field": "rank", "keys": [13],
         "field": "nickname", "after": "铁和尚",
         "rationale": "花和尚→铁和尚，强记忆绰号改写"},
        {"id": "SH-P06", "kind": "rename", "key_field": "rank", "keys": [107],
         "field": "nickname", "after": "屋上鹞",
         "rationale": "鼓上蚤→屋上鹞"},
        {"id": "SH-P07", "kind": "retype", "key_field": "rank", "keys": [15],
         "field": "role_group", "after": "马军八骠骑",
         "rationale": "董平由五虎将改判八骠骑，影响按职司的聚合题"},
    ],
}

# 受控跨源冲突（供 F2 题型）：同一事实在两种形态中故意给出不同值。
# 正确行为是**报告分歧并给出两处出处**，而不是擅自选一个。
CONFLICTS: dict[str, list[dict[str, Any]]] = {
    "xiyouji": [
        {"id": "XY-C01", "fact": "第 36 难的发生地",
         "doc_value": "黑水河",
         "expected_behavior": "报告两源分歧，各自给出 QueryReplay 与 DocRef"},
        {"id": "XY-C02", "fact": "孙悟空首次登场的难序号",
         "doc_value": 5,
         "expected_behavior": "报告两源分歧（表为 8，文档为 5）"},
        {"id": "XY-C03", "fact": "水难类的难数",
         "doc_value": "表中实际值 + 1",
         "expected_behavior": "报告文档声明的总数与表中聚合结果不一致"},
        {"id": "XY-C04", "fact": "第 81 难的对手",
         "doc_value": "老鼋",
         "expected_behavior": "报告分歧。⚠️ 文档刻意保留了未扰动的世界知识值，"
                              "系统若『觉得文档更合理』而擅自采信文档，即暴露了世界知识偏置"},
    ],
    "shuihu": [
        {"id": "SH-C01", "fact": "武松的战功数",
         "doc_value": "表中值 + 7",
         "expected_behavior": "报告两源分歧，给出 CellRef 与 DocRef"},
        {"id": "SH-C02", "fact": "马军五虎将的人数",
         "doc_value": 6,
         "expected_behavior": "报告文档声明 6 人与表中聚合结果不一致"},
        {"id": "SH-C03", "fact": "水军头领的人数",
         "doc_value": "表中实际值 - 2",
         "expected_behavior": "报告分歧"},
        {"id": "SH-C04", "fact": "最早上山的年份",
         "doc_value": "表中最小值 - 3",
         "expected_behavior": "报告分歧"},
    ],
}


def conflict(book: str, cid: str) -> dict[str, Any]:
    """按 id 取受控冲突声明，供文档生成器注入。"""
    return next(c for c in CONFLICTS[book] if c["id"] == cid)


# ---------------------------------------------------------------- 执行

def apply_perturbations(book: str, rows: list[dict]) -> tuple[list[dict], dict]:
    """对 base 行应用声明的扰动，返回 (扰动后行, manifest)。"""
    out = copy.deepcopy(rows)
    index: dict[Any, dict] = {}
    records: list[dict] = []

    for spec in SPECS[book]:
        kf, field = spec["key_field"], spec["field"]
        index = {r[kf]: r for r in out}

        if spec["kind"] == "swap":
            a, b = spec["keys"]
            ra, rb = index[a], index[b]
            before = [ra[field], rb[field]]
            ra[field], rb[field] = rb[field], ra[field]
            after = [ra[field], rb[field]]
        elif spec["kind"] in ("rename", "retype"):
            before, after = [], []
            for k in spec["keys"]:
                r = index[k]
                before.append(r[field])
                r[field] = spec["after"]
                after.append(spec["after"])
        else:  # pragma: no cover - 声明表由本模块控制
            raise ValueError(f"未知扰动类型: {spec['kind']}")

        records.append({
            "id": spec["id"], "kind": spec["kind"],
            "key_field": kf, "keys": spec["keys"], "field": field,
            "before": before, "after": after,
            "rationale": spec["rationale"],
        })

    manifest = {
        "book": book,
        "seed": SEED,
        "corpus_version": CORPUS_VERSION,
        "perturbation_count": len(records),
        "perturbations": records,
        "conflicts": CONFLICTS.get(book, []),
        "note": (
            "ground truth 一律以本清单 + 扰动后语料为准，不以原著为准。"
            "凭世界知识作答的系统在受扰动字段上必然出错——这正是本清单存在的意义。"
        ),
    }
    return out, manifest


def perturbed_keys(book: str) -> dict[str, set[int]]:
    """返回 {字段名: {被扰动的主键值}}，供题集生成器标记 F4 反事实题。"""
    out: dict[str, set[int]] = {}
    for spec in SPECS[book]:
        out.setdefault(spec["field"], set()).update(spec["keys"])
    return out
