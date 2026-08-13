"""手工题生成器：F2 跨源冲突 / F5 多轮钻取 / F6 拒答题。

与 gen_questions.py 的分工：
* 模板题（F1/F3/F4）：题面与答案**都**由机器生成。
* 手工题（F2/F5/F6）：**题面与期望行为由人撰写**，但其中的参考值从已发布语料算出
  ——避免硬编码数字随语料更新而过期。这是"手工"的正确做法，不是把数字抄进 JSON 里。

三个题型各自的存在理由：
* **F2**：既有真冲突题，也有**一致性负例题**。只测冲突检出率而不测误报率，
  会奖励"逢事就报分歧"的退化策略。
* **F5**：多轮约束承接，逐轮判分。
* **F6**：**让 `fabrication_rate` 变成可证伪的断言**。上一版题集里没有任何一道题
  能让系统"有机会编造"，那个指标从构造上就无法被证伪。

F5 是唯一 `unit_kind="chain"` 的题型：整条 chain 共享一个 `independence_unit_id`，
确认性结果为"所有计分轮均正确"。把三个 turn 当三行独立样本喂进 McNemar 就是伪重复，
注册器必须硬拦——这也是本文件把 unit 字段写进资产而不是留给下游推断的原因。

用法::

    python evals/scripts/gen_manual_questions.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_questions import load_shuihu, load_xiyouji
from perturb import conflict

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "questions" / "manual"
PREFIX = "按本系统记载，"


def _one(con, sql: str, params: tuple = ()) -> Any:
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- 西游记

def xiyouji_manual(con) -> list[dict]:
    q: list[dict] = []
    n_water = _one(con, "SELECT COUNT(*) FROM nan WHERE category='水难'")

    # ---- F2：4 道真冲突 + 4 道一致性负例 ----
    conflicts = [
        ("XY-F2-001", f"{PREFIX}第 36 难发生在哪里？表格与文档的记载是否一致？",
         _one(con, "SELECT location FROM nan WHERE nan_no=36"),
         conflict("xiyouji", "XY-C01")["doc_value"], "XY-C01"),
        ("XY-F2-002", f"{PREFIX}孙悟空是在第几难首次登场的？请核对表格与文档。",
         _one(con, "SELECT first_nan FROM characters WHERE name='孙悟空'"),
         conflict("xiyouji", "XY-C02")["doc_value"], "XY-C02"),
        ("XY-F2-003", f"{PREFIX}「水难」类共有多少难？文档中的说法与表格一致吗？",
         n_water, n_water + 1, "XY-C03"),
        ("XY-F2-004", f"{PREFIX}第 81 难的对手是谁？表格与文档是否一致？",
         _one(con, "SELECT opponent FROM nan WHERE nan_no=81"),
         conflict("xiyouji", "XY-C04")["doc_value"], "XY-C04"),
    ]
    for qid, text, tv, dv, cid in conflicts:
        q.append({
            "id": qid, "book": "xiyouji", "type": "F2",
            "template_id": "xy.f2.cross_source_conflict",
            "independence_unit_id": qid, "unit_kind": "single",
            "question": text,
            "ground_truth": {"conflict": True, "table_value": tv, "doc_value": dv},
            "answer_kind": "conflict",
            "scoring": {"mode": "conflict", "must_report_divergence": True,
                        "must_cite_both_sources": True},
            "trace_required": True, "conflict_id": cid,
            "field_class": "curated", "targets_perturbed_field": False,
            "note": "正确行为是报告分歧并给出两处出处，擅自择一即判错",
        })

    # 一致性负例：表与文档说法相同，正确行为是不报分歧
    agree = [
        ("XY-F2-005", f"{PREFIX}第 38 难的援手是谁？表格与文档是否一致？",
         _one(con, "SELECT helper FROM nan WHERE nan_no=38")),
        ("XY-F2-006", f"{PREFIX}第 36 难的对手名号是什么？两处记载是否一致？",
         _one(con, "SELECT opponent FROM nan WHERE nan_no=36")),
        ("XY-F2-007", f"{PREFIX}第 1 至第 4 难的历时天数合计是多少？文档中的说法与表格一致吗？",
         _one(con, "SELECT SUM(duration_days) FROM nan WHERE nan_no<=4")),
        ("XY-F2-008", f"{PREFIX}第 81 难的难名是什么？表格与文档是否一致？",
         _one(con, "SELECT nan_name FROM nan WHERE nan_no=81")),
    ]
    for qid, text, val in agree:
        q.append({
            "id": qid, "book": "xiyouji", "type": "F2",
            "template_id": "xy.f2.cross_source_agreement",
            "independence_unit_id": qid, "unit_kind": "single",
            "question": text,
            "ground_truth": {"conflict": False, "value": val},
            "answer_kind": "conflict",
            "scoring": {"mode": "conflict", "must_report_divergence": False},
            "trace_required": True, "conflict_id": None,
            "field_class": "curated", "targets_perturbed_field": False,
            "note": "一致性负例：误报分歧即判错，用于测量冲突检测的误报率",
        })

    # ---- F5：2 组 × 3 轮 ----
    chains = [
        ("XY-F5-C1", [
            (f"{PREFIX}难度评分最高的是哪一难？请给出难名。",
             _one(con, "SELECT nan_name FROM nan ORDER BY difficulty_score DESC, nan_no LIMIT 1")),
            ("那一难发生在哪里？",
             _one(con, "SELECT location FROM nan ORDER BY difficulty_score DESC, nan_no LIMIT 1")),
            ("它的对手是谁？",
             _one(con, "SELECT opponent FROM nan ORDER BY difficulty_score DESC, nan_no LIMIT 1")),
        ]),
        ("XY-F5-C2", [
            (f"{PREFIX}「水难」类中历时最长的是第几难？",
             _one(con, "SELECT nan_no FROM nan WHERE category='水难' "
                       "ORDER BY duration_days DESC, nan_no LIMIT 1")),
            ("这一难的援手是谁？",
             _one(con, "SELECT helper FROM nan WHERE category='水难' "
                       "ORDER BY duration_days DESC, nan_no LIMIT 1")),
            ("同一类别下还有几难？（不含这一难）", n_water - 1),
        ]),
    ]
    for cid, turns in chains:
        for i, (text, gt) in enumerate(turns, 1):
            q.append({
                "id": f"{cid}-T{i}", "book": "xiyouji", "type": "F5",
                "template_id": f"xy.f5.{cid.rsplit('-', 1)[-1].lower()}",
                "independence_unit_id": cid, "unit_kind": "chain",
                "chain_id": cid, "turn_index": i,
                "question": text, "ground_truth": gt, "answer_kind": "scalar",
                "scoring": {"mode": "exact", "tolerance": 0},
                "trace_required": True,
                "field_class": "mixed", "targets_perturbed_field": False,
                "note": "第 2/3 轮依赖前轮建立的约束；约束丢失即判错",
            })

    # ---- F6：拒答题 ----
    for qid, text, reason in [
        ("XY-F6-001", f"{PREFIX}第 95 难的对手是谁？", "数据中只有 81 难，不存在第 95 难"),
        ("XY-F6-002", f"{PREFIX}唐三藏的籍贯是哪里？", "人物表中没有籍贯字段"),
        ("XY-F6-003", f"{PREFIX}第 40 难当日的降雨量是多少毫米？", "数据中不存在降雨量字段"),
        ("XY-F6-004", f"{PREFIX}「哪吒」在八十一难中出现了几次？", "人物表中不存在该角色"),
    ]:
        q.append({
            "id": qid, "book": "xiyouji", "type": "F6",
            "template_id": "xy.f6.unanswerable",
            "independence_unit_id": qid, "unit_kind": "single",
            "question": text,
            "ground_truth": {"answerable": False, "reason": reason},
            "answer_kind": "refusal",
            "scoring": {"mode": "refusal", "must_not_fabricate": True},
            "trace_required": False,
            "field_class": "n/a", "targets_perturbed_field": False,
            "note": "给出任何具体数值/实体即计入 fabrication_rate",
        })
    return q


# ---------------------------------------------------------------- 水浒传

def shuihu_manual(con) -> list[dict]:
    q: list[dict] = []
    n_navy = _one(con, "SELECT COUNT(*) FROM liangshan WHERE role_group='水军头领'")
    n_tiger = _one(con, "SELECT COUNT(*) FROM liangshan WHERE role_group='马军五虎将'")
    min_year = _one(con, "SELECT MIN(join_year) FROM liangshan")
    wusong = _one(con, "SELECT merit_count FROM liangshan WHERE name='武松'")

    conflicts = [
        ("SH-F2-001", f"{PREFIX}武松的战功数是多少？名录与招安文档是否一致？",
         wusong, wusong + 7, "SH-C01"),
        ("SH-F2-002", f"{PREFIX}「马军五虎将」共有多少人？两处记载是否一致？",
         n_tiger, conflict("shuihu", "SH-C02")["doc_value"], "SH-C02"),
        ("SH-F2-003", f"{PREFIX}「水军头领」共有多少人？请核对名录与文档。",
         n_navy, n_navy - 2, "SH-C03"),
        ("SH-F2-004", f"{PREFIX}最早上山的年份是哪一年？两处记载是否一致？",
         min_year, min_year - 3, "SH-C04"),
    ]
    for qid, text, tv, dv, cid in conflicts:
        q.append({
            "id": qid, "book": "shuihu", "type": "F2",
            "template_id": "sh.f2.cross_source_conflict",
            "independence_unit_id": qid, "unit_kind": "single",
            "question": text,
            "ground_truth": {"conflict": True, "table_value": tv, "doc_value": dv},
            "answer_kind": "conflict",
            "scoring": {"mode": "conflict", "must_report_divergence": True,
                        "must_cite_both_sources": True},
            "trace_required": True, "conflict_id": cid,
            "field_class": "mixed", "targets_perturbed_field": False,
            "note": "正确行为是报告分歧并给出 CellRef 与 DocRef 两处出处",
        })

    agree = [
        ("SH-F2-005", f"{PREFIX}座次第 1 位是谁？名录与文档是否一致？",
         _one(con, "SELECT name FROM liangshan WHERE rank=1")),
        ("SH-F2-006", f"{PREFIX}座次第 3 位的绰号是什么？两处记载是否一致？",
         _one(con, "SELECT nickname FROM liangshan WHERE rank=3")),
        ("SH-F2-007", f"{PREFIX}上山年份最晚是哪一年？文档与名录一致吗？",
         _one(con, "SELECT MAX(join_year) FROM liangshan")),
        ("SH-F2-008", f"{PREFIX}座次第 2 位的姓名是什么？两处是否一致？",
         _one(con, "SELECT name FROM liangshan WHERE rank=2")),
    ]
    for qid, text, val in agree:
        q.append({
            "id": qid, "book": "shuihu", "type": "F2",
            "template_id": "sh.f2.cross_source_agreement",
            "independence_unit_id": qid, "unit_kind": "single",
            "question": text,
            "ground_truth": {"conflict": False, "value": val},
            "answer_kind": "conflict",
            "scoring": {"mode": "conflict", "must_report_divergence": False},
            "trace_required": True, "conflict_id": None,
            "field_class": "mixed", "targets_perturbed_field": False,
            "note": "一致性负例：误报分歧即判错",
        })

    chains = [
        ("SH-F5-C1", [
            (f"{PREFIX}战功数最高的头领是谁？",
             _one(con, "SELECT name FROM liangshan ORDER BY merit_count DESC, rank LIMIT 1")),
            ("他的座次是第几位？",
             _one(con, "SELECT rank FROM liangshan ORDER BY merit_count DESC, rank LIMIT 1")),
            ("他属于哪个职司分组？",
             _one(con, "SELECT role_group FROM liangshan ORDER BY merit_count DESC, rank LIMIT 1")),
        ]),
        ("SH-F5-C2", [
            (f"{PREFIX}「水军头领」中座次最靠前的是谁？",
             _one(con, "SELECT name FROM liangshan WHERE role_group='水军头领' ORDER BY rank LIMIT 1")),
            ("他的籍贯是哪里？",
             _one(con, "SELECT home_town FROM liangshan WHERE role_group='水军头领' ORDER BY rank LIMIT 1")),
            ("这个分组一共有多少人？", n_navy),
        ]),
    ]
    for cid, turns in chains:
        for i, (text, gt) in enumerate(turns, 1):
            q.append({
                "id": f"{cid}-T{i}", "book": "shuihu", "type": "F5",
                "template_id": f"sh.f5.{cid.rsplit('-', 1)[-1].lower()}",
                "independence_unit_id": cid, "unit_kind": "chain",
                "chain_id": cid, "turn_index": i,
                "question": text, "ground_truth": gt, "answer_kind": "scalar",
                "scoring": {"mode": "exact", "tolerance": 0},
                "trace_required": True,
                "field_class": "mixed", "targets_perturbed_field": False,
                "note": "第 2/3 轮依赖前轮建立的约束",
            })

    for qid, text, reason in [
        ("SH-F6-001", f"{PREFIX}座次第 120 位是谁？", "名录只有 108 人"),
        ("SH-F6-002", f"{PREFIX}宋江使用的兵器是什么？", "名录中没有兵器字段"),
        ("SH-F6-003", f"{PREFIX}「地哭星」对应的是哪位头领？", "名录中不存在该星号"),
        ("SH-F6-004", f"{PREFIX}武松的身高是多少？", "名录中不存在身高字段"),
    ]:
        q.append({
            "id": qid, "book": "shuihu", "type": "F6",
            "template_id": "sh.f6.unanswerable",
            "independence_unit_id": qid, "unit_kind": "single",
            "question": text,
            "ground_truth": {"answerable": False, "reason": reason},
            "answer_kind": "refusal",
            "scoring": {"mode": "refusal", "must_not_fabricate": True},
            "trace_required": False,
            "field_class": "n/a", "targets_perturbed_field": False,
            "note": "给出任何具体数值/实体即计入 fabrication_rate",
        })
    return q


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    total = 0
    for book, loader, builder in (("xiyouji", load_xiyouji, xiyouji_manual),
                                  ("shuihu", load_shuihu, shuihu_manual)):
        con = loader()
        items = builder(con)
        con.close()

        for key in ("template_id", "independence_unit_id", "unit_kind"):
            missing = [i["id"] for i in items if not i.get(key)]
            if missing:
                raise SystemExit(f"[manual] {book} 以下题目缺少 {key}: {missing}")
        bad_chain = [i["id"] for i in items
                     if (i["unit_kind"] == "chain") != (i["type"] == "F5")]
        if bad_chain:
            raise SystemExit(f"[manual] {book} unit_kind 与题型不符（只有 F5 是 chain）: {bad_chain}")

        with (OUT / f"{book}.jsonl").open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        counts: dict[str, int] = {}
        for it in items:
            counts[it["type"]] = counts.get(it["type"], 0) + 1
        total += len(items)
        print(f"[manual] {book}: {len(items)} 题  {counts}")
    print(f"[manual] 手工题合计 {total} 题 → evals/questions/manual/")


if __name__ == "__main__":
    main()
