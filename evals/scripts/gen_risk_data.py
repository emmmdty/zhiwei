"""合成经营数据生成器（风险引擎的零污染评测底座）。

设计依据：docs/RISK_EVAL.md。核心思想借鉴 InsightBench——**在数据里预先植入模式，
再看系统能否把它挖出来**——但判分改为确定性规则匹配，不依赖 LLM-as-judge。

为什么必须换掉"推断第 81 难"：那道题的标准答案本身就是世界知识，裸 LLM 不查数据也能答对，
于是防污染机制会把它标记为知识题并剔出核心指标，`risk_hit_rate` 因此无题可算。
合成数据没有这个问题——**没有任何模型见过「云梯科技」的经营数据**。

产物::

    evals/risk/yunti.db              SQLite（事实表 + 维度表）
    evals/risk/csv/*.csv             同数据的 CSV 形态
    evals/risk/planted_manifest.json 植入清单（ground truth）

用法::

    python evals/scripts/gen_risk_data.py
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

SEED = 20260811
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "risk"

MONTHS = [f"{y}-{m:02d}" for y in (2023, 2024, 2025) for m in range(1, 13)]  # 36 期
PRODUCTS = ["云梯-企业版", "云梯-标准版", "云梯-硬件", "云梯-服务"]
REGIONS = ["华东", "华北", "华南", "西部"]
CUSTOMERS = [f"C{i:02d}" for i in range(1, 21)]
SUPPLIERS = [f"S{i:02d}" for i in range(1, 13)]

rng = random.Random(SEED)


def midx(m: str) -> int:
    return MONTHS.index(m)


def in_window(m: str, start: str, _end: str) -> bool:
    """窗口开始之后即受影响（含窗口结束后的持续期）。

    注意不是 ``start <= m <= end``：真实的经营恶化在窗口结束后不会瞬间复原。
    若效应只在窗口内生效，序列会在窗口两端出现人造断点，
    检测器可以靠"找不连续点"作弊，而不是真的识别趋势——那样测出来的 recall 是假的。
    """
    return midx(m) >= midx(start)


def ramp(m: str, start: str, end: str) -> float:
    """效应强度：窗口前为 0，窗口内线性爬升 0→1，窗口后保持 1（持续恶化，不回弹）。"""
    a, b, i = midx(start), midx(end), midx(m)
    if i < a:
        return 0.0
    if b == a or i >= b:
        return 1.0
    return (i - a) / (b - a)


# ---------------------------------------------------------------- 植入声明

NOISE = {"gross_margin_rate": 0.012, "revenue": 0.06,
         "dso_days": 3.2, "revenue_share": 0.02, "on_time_rate": 0.015,
         "cash_to_revenue": 0.05}

# 难度分档（docs/RISK_EVAL.md §2.3）：由信噪比决定，不由主观判断决定
BANDS = {"easy": (3.0, 999.0), "medium": (1.5, 3.0), "hard": (0.8, 1.5)}
DISTRACTOR_SNR_MAX = 0.8   # 干扰项必须低于 hard 档下沿，否则它就不是干扰项而是漏标的植入项

# `delta` 的语义按 kind 区分（写清楚，否则极易写反）：
#   trend / seasonal / ratio / concentration —— **加性或相对幅度**，直接与噪声标准差比得到 snr
#   baseline_deviation                      —— **乘数**（1.4 表示抬升 40%），
#                                              有效幅度 = 基线 × (delta - 1)
PLANTED: list[dict[str, Any]] = [
    # P1 趋势恶化：毛利率连续下滑
    {"id": "P1-001", "kind": "trend", "dim": "product_line", "value": "云梯-企业版",
     "metric": "gross_margin_rate", "window": ("2024-06", "2025-03"),
     "delta": -0.09, "snr": 7.5, "difficulty": "easy"},
    {"id": "P1-002", "kind": "trend", "dim": "product_line", "value": "云梯-服务",
     "metric": "gross_margin_rate", "window": ("2024-11", "2025-08"),
     "delta": -0.030, "snr": 2.5, "difficulty": "medium"},
    {"id": "P1-003", "kind": "trend", "dim": "region", "value": "西部",
     "metric": "gross_margin_rate", "window": ("2025-01", "2025-10"),
     "delta": -0.015, "snr": 1.25, "difficulty": "hard"},
    # P2 集中度上升
    {"id": "P2-001", "kind": "concentration", "dim": "customer_top5", "value": "全公司",
     "metric": "revenue_share", "window": ("2024-01", "2025-06"),
     "delta": 0.24, "snr": 12.0, "difficulty": "easy"},
    {"id": "P2-002", "kind": "concentration", "dim": "supplier_top3", "value": "全公司",
     "metric": "purchase_share", "window": ("2024-08", "2025-09"),
     "delta": 0.05, "snr": 2.5, "difficulty": "medium"},
    # P3 季节规律断裂
    {"id": "P3-001", "kind": "seasonal", "dim": "region", "value": "华北",
     "metric": "revenue", "window": ("2025-03", "2025-08"),
     "delta": -0.15, "snr": 2.5, "difficulty": "medium"},
    {"id": "P3-002", "kind": "seasonal", "dim": "product_line", "value": "云梯-硬件",
     "metric": "revenue", "window": ("2025-06", "2025-11"),
     "delta": -0.07, "snr": 1.17, "difficulty": "hard"},
    # P4 账期突变（delta 为乘数）
    {"id": "P4-001", "kind": "baseline_deviation", "dim": "customer", "value": "C03",
     "metric": "dso_days", "window": ("2025-02", "2025-12"),
     "delta": 2.40, "snr": 20.6, "difficulty": "easy"},
    {"id": "P4-002", "kind": "baseline_deviation", "dim": "customer", "value": "C11",
     "metric": "dso_days", "window": ("2024-09", "2025-12"),
     "delta": 1.18, "snr": 2.64, "difficulty": "medium"},
    {"id": "P4-003", "kind": "baseline_deviation", "dim": "customer", "value": "C17",
     "metric": "dso_days", "window": ("2025-05", "2025-12"),
     "delta": 1.09, "snr": 1.32, "difficulty": "hard"},
    # P5 结构性背离：营收升而经营现金流降
    {"id": "P5-001", "kind": "ratio", "dim": "company", "value": "全公司",
     "metric": "cash_to_revenue", "window": ("2024-07", "2025-07"),
     "delta": 0.12, "snr": 2.4, "difficulty": "medium"},
    {"id": "P5-002", "kind": "ratio", "dim": "product_line", "value": "云梯-标准版",
     "metric": "cash_to_revenue", "window": ("2025-01", "2025-11"),
     "delta": 0.07, "snr": 1.4, "difficulty": "hard"},
    # P6 供应商尾部风险：占比上升 + 准时率同步下滑
    {"id": "P6-001", "kind": "concentration_signal", "dim": "supplier", "value": "S02",
     "metric": "purchase_share+on_time_rate", "window": ("2024-10", "2025-09"),
     "delta": 0.22, "snr": 11.0, "difficulty": "easy"},
    {"id": "P6-002", "kind": "concentration_signal", "dim": "supplier", "value": "S07",
     "metric": "purchase_share+on_time_rate", "window": ("2025-02", "2025-12"),
     "delta": 0.05, "snr": 2.5, "difficulty": "medium"},
]

# 干扰项：形态相似但信噪比低于 hard 档下沿，正确行为是**不报**。
DISTRACTORS: list[dict[str, Any]] = [
    {"id": "D-001", "kind": "trend", "dim": "product_line", "value": "云梯-硬件",
     "metric": "gross_margin_rate", "window": ("2024-03", "2024-09"),
     "delta": -0.008, "snr": 0.67},
    {"id": "D-002", "kind": "trend", "dim": "region", "value": "华东",
     "metric": "gross_margin_rate", "window": ("2025-04", "2025-10"),
     "delta": -0.007, "snr": 0.58},
    {"id": "D-003", "kind": "baseline_deviation", "dim": "customer", "value": "C08",
     "metric": "dso_days", "window": ("2025-06", "2025-12"),
     "delta": 1.04, "snr": 0.59},
    {"id": "D-004", "kind": "baseline_deviation", "dim": "customer", "value": "C14",
     "metric": "dso_days", "window": ("2024-04", "2024-11"),
     "delta": 1.035, "snr": 0.51},
    {"id": "D-005", "kind": "concentration", "dim": "supplier_top3", "value": "全公司",
     "metric": "purchase_share", "window": ("2024-05", "2025-02"),
     "delta": 0.012, "snr": 0.60},
    {"id": "D-006", "kind": "seasonal", "dim": "region", "value": "华南",
     "metric": "revenue", "window": ("2025-07", "2025-11"),
     "delta": -0.04, "snr": 0.67},
    {"id": "D-007", "kind": "concentration_signal", "dim": "supplier", "value": "S05",
     "metric": "purchase_share+on_time_rate", "window": ("2025-03", "2025-10"),
     "delta": 0.012, "snr": 0.60},
]


def check_declarations() -> None:
    """自检：难度档与信噪比必须自洽，干扰项必须真的低于阈值。

    这个检查存在的理由：难度档如果靠主观标注，报告里的"分档 recall"就没有意义——
    可以通过把难题标成 hard 来粉饰。让 snr 决定档位，档位就无法被事后调整。
    """
    for p in PLANTED:
        lo, hi = BANDS[p["difficulty"]]
        if not (lo <= p["snr"] < hi):
            raise SystemExit(
                f"[risk] {p['id']} 难度档 {p['difficulty']} 与 snr={p['snr']} 不符，"
                f"应落在 [{lo}, {hi})")
    for d in DISTRACTORS:
        if d["snr"] >= DISTRACTOR_SNR_MAX:
            raise SystemExit(
                f"[risk] 干扰项 {d['id']} 的 snr={d['snr']} 未低于 {DISTRACTOR_SNR_MAX}，"
                f"它会变成一个漏标的植入项，从而污染 precision")


def check_plantability() -> None:
    """自检：每条声明的模式都必须真的有对应的生成逻辑。

    这个检查存在的理由：一条无法被植入数据的"幽灵模式"不会报错，
    只会让 recall 的上限被悄悄压低——分数看起来变差了，但原因与系统能力无关。
    """
    supported = {
        ("trend", "product_line"), ("trend", "region"),
        ("seasonal", "region"), ("seasonal", "product_line"),
        ("concentration", "customer_top5"), ("concentration", "supplier_top3"),
        ("baseline_deviation", "customer"),
        ("ratio", "company"), ("ratio", "product_line"),
        ("concentration_signal", "supplier"),
    }
    for p in PLANTED + DISTRACTORS:
        if (p["kind"], p["dim"]) not in supported:
            raise SystemExit(
                f"[risk] {p['id']}: ({p['kind']}, {p['dim']}) 没有对应的生成逻辑，"
                f"会成为永远检不出的幽灵模式")


def _hits(kind: str, dim: str, value: str) -> list[dict]:
    return [p for p in PLANTED + DISTRACTORS
            if p["kind"] == kind and p["dim"] == dim and p["value"] == value]


# ---------------------------------------------------------------- 生成

def gen_revenue() -> list[dict]:
    rows = []
    base_rev = {"云梯-企业版": 820, "云梯-标准版": 460, "云梯-硬件": 300, "云梯-服务": 210}
    base_mgn = {"云梯-企业版": 0.62, "云梯-标准版": 0.48, "云梯-硬件": 0.27, "云梯-服务": 0.55}
    region_w = {"华东": 0.38, "华北": 0.26, "华南": 0.22, "西部": 0.14}

    for m in MONTHS:
        i = midx(m)
        season = 1 + 0.16 * (1 if (i % 12) in (2, 5, 8, 11) else -0.35)
        growth = 1 + 0.0075 * i
        for p in PRODUCTS:
            for r in REGIONS:
                rev = base_rev[p] * region_w[r] * season * growth
                rev *= 1 + rng.gauss(0, NOISE["revenue"])

                # 季节断裂（P3 / D-006）
                for pat in _hits("seasonal", "region", r) + _hits("seasonal", "product_line", p):
                    if in_window(m, *pat["window"]):
                        rev *= 1 + pat["delta"] * ramp(m, *pat["window"])

                mgn = base_mgn[p] + rng.gauss(0, NOISE["gross_margin_rate"])
                # 毛利率趋势恶化（P1 / D-001 / D-002）
                for pat in (_hits("trend", "product_line", p) + _hits("trend", "region", r)):
                    if in_window(m, *pat["window"]):
                        mgn += pat["delta"] * ramp(m, *pat["window"])

                rev = round(rev, 2)
                cost = round(rev * (1 - mgn), 2)
                rows.append({
                    "month": m, "product_line": p, "region": r,
                    "revenue_k": rev, "cost_k": cost,
                    "gross_margin_rate": round(mgn, 4),
                    "order_count": max(1, int(rev / 7 + rng.gauss(0, 4))),
                })
    return rows


def gen_receivable(rev_rows: list[dict]) -> list[dict]:
    monthly_rev = {}
    for r in rev_rows:
        monthly_rev[r["month"]] = monthly_rev.get(r["month"], 0) + r["revenue_k"]

    # 客户份额：让 P2-001 的 top5 集中度按窗口上升
    base_share = [0.13, 0.11, 0.095, 0.085, 0.075] + [0.0555] * 15
    rows = []
    for m in MONTHS:
        shares = list(base_share)
        for pat in _hits("concentration", "customer_top5", "全公司"):
            if in_window(m, *pat["window"]):
                lift = pat["delta"] * ramp(m, *pat["window"])
                for k in range(5):
                    shares[k] += lift / 5
                for k in range(5, 20):
                    shares[k] -= lift / 15
        tot = sum(shares)
        shares = [s / tot for s in shares]

        for c, sh in zip(CUSTOMERS, shares, strict=True):
            dso = 47 + rng.gauss(0, NOISE["dso_days"])
            for pat in _hits("baseline_deviation", "customer", c):
                if in_window(m, *pat["window"]):
                    dso *= 1 + (pat["delta"] - 1) * min(1.0, ramp(m, *pat["window"]) * 3)
            bal = monthly_rev[m] * sh * dso / 30
            rows.append({
                "month": m, "customer_id": c,
                "receivable_k": round(bal, 2), "dso_days": round(dso, 1),
                "overdue": 1 if dso > 75 else 0,
                "revenue_share": round(sh, 4),
            })
    return rows


def gen_supply() -> list[dict]:
    rows = []
    base = [0.16, 0.14, 0.11, 0.10, 0.09, 0.08, 0.07, 0.06, 0.06, 0.05, 0.04, 0.04]
    for m in MONTHS:
        shares = list(base)
        for idx, s in enumerate(SUPPLIERS):
            for pat in _hits("concentration_signal", "supplier", s):
                if in_window(m, *pat["window"]):
                    shares[idx] += pat["delta"] * ramp(m, *pat["window"])
        # 供应商 top3 集中度（P2-002 / D-005）
        for pat in _hits("concentration", "supplier_top3", "全公司"):
            if in_window(m, *pat["window"]):
                lift = pat["delta"] * ramp(m, *pat["window"])
                for k in range(3):
                    shares[k] += lift / 3
                for k in range(3, len(shares)):
                    shares[k] -= lift / (len(shares) - 3)
        shares = [max(0.001, x) for x in shares]
        tot = sum(shares)
        shares = [x / tot for x in shares]
        for s, sh in zip(SUPPLIERS, shares, strict=True):
            otr = 0.965 + rng.gauss(0, NOISE["on_time_rate"])
            for pat in _hits("concentration_signal", "supplier", s):
                if in_window(m, *pat["window"]):
                    otr -= pat["delta"] * 0.55 * ramp(m, *pat["window"])
            rows.append({
                "month": m, "supplier_id": s,
                "purchase_k": round(1400 * sh * (1 + rng.gauss(0, 0.05)), 2),
                "purchase_share": round(sh, 4),
                "on_time_rate": round(min(1.0, max(0.0, otr)), 4),
                "defect_return_rate": round(max(0.0, 0.018 + rng.gauss(0, 0.006)), 4),
            })
    return rows


def gen_cashflow(rev_rows: list[dict]) -> list[dict]:
    """经营现金流（月 × 产品线）：P5 让营收与现金流出现剪刀差。

    粒度必须到产品线，否则 P5-002（产品线级背离）无处可植 —— 那会造成幽灵模式。
    公司级背离（P5-001）通过对所有产品线同时施加实现，聚合后即可观测。
    """
    by_key: dict[tuple[str, str], float] = {}
    for r in rev_rows:
        k = (r["month"], r["product_line"])
        by_key[k] = by_key.get(k, 0) + r["revenue_k"]

    rows = []
    for m in MONTHS:
        for p in PRODUCTS:
            rev = by_key[(m, p)]
            cash = rev * 0.31 * (1 + rng.gauss(0, NOISE["cash_to_revenue"]))
            for pat in (_hits("ratio", "company", "全公司")
                        + _hits("ratio", "product_line", p)):
                if in_window(m, *pat["window"]):
                    cash *= 1 - pat["delta"] * ramp(m, *pat["window"])
            rows.append({"month": m, "product_line": p, "revenue_k": round(rev, 2),
                         "operating_cash_k": round(cash, 2),
                         "cash_to_revenue": round(cash / rev, 4)})
    return rows


def dirty(rows: list[dict], cols: list[str], rate: float = 0.03) -> list[dict]:
    """注入真实世界的脏度：缺失值。避免『合成数据太干净』导致检出率虚高。"""
    for r in rows:
        for c in cols:
            if rng.random() < rate:
                r[c] = None
    return rows


# ---------------------------------------------------------------- 出口

def main() -> None:
    check_declarations()
    check_plantability()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "csv").mkdir(exist_ok=True)

    rev = gen_revenue()
    recv = gen_receivable(rev)
    sup = gen_supply()
    cash = gen_cashflow(rev)

    # 重复记录（另一类真实脏度）：随机复制 12 行营收
    rev += [dict(rev[rng.randrange(len(rev))]) for _ in range(12)]
    dirty(recv, ["overdue"])
    dirty(sup, ["defect_return_rate"])

    db = OUT / "yunti.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    tables = {
        "fact_revenue": (rev, ("month TEXT, product_line TEXT, region TEXT, revenue_k REAL,"
                               " cost_k REAL, gross_margin_rate REAL, order_count INTEGER")),
        "fact_receivable": (recv, ("month TEXT, customer_id TEXT, receivable_k REAL,"
                                   " dso_days REAL, overdue INTEGER, revenue_share REAL")),
        "fact_supply": (sup, ("month TEXT, supplier_id TEXT, purchase_k REAL,"
                              " purchase_share REAL, on_time_rate REAL,"
                              " defect_return_rate REAL")),
        "fact_cashflow": (cash, ("month TEXT, product_line TEXT, revenue_k REAL,"
                                 " operating_cash_k REAL, cash_to_revenue REAL")),
    }
    for name, (rows, ddl) in tables.items():
        con.execute(f"CREATE TABLE {name} ({ddl})")
        cols = list(rows[0].keys())
        con.executemany(
            f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows])
        with (OUT / "csv" / f"{name}.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    con.commit()
    con.close()

    manifest = {
        "seed": SEED,
        "company": "云梯科技（虚构）",
        "period": {"start": MONTHS[0], "end": MONTHS[-1], "months": len(MONTHS)},
        "noise_std": NOISE,
        "planted": [{
            "id": p["id"], "kind": p["kind"],
            "entity": {"dim": p["dim"], "value": p["value"]},
            "metric": p["metric"],
            "window": {"start": p["window"][0], "end": p["window"][1]},
            "magnitude": {"delta": p["delta"]},
            "difficulty": p["difficulty"],
            "snr": p["snr"],
            "expected_evidence": {"min_rows": 6, "must_reference": [p["metric"], p["dim"]]},
        } for p in PLANTED],
        "distractors": [{
            "id": d["id"], "kind": d["kind"],
            "entity": {"dim": d["dim"], "value": d["value"]},
            "window": {"start": d["window"][0], "end": d["window"][1]},
            "magnitude": {"delta": d["delta"]}, "snr": d["snr"],
            "expected_behavior": "信噪比低于 hard 档下沿，正确行为是不报；报出即计入 distractor_fp_rate",
        } for d in DISTRACTORS],
        "scoring": {
            "match_rule": "kind 一致 且 entity 一致 且 window IoU >= 0.5",
            "metrics": ["pattern_recall(分难度档)", "pattern_precision",
                        "distractor_fp_rate", "evidence_validity", "confidence_calibration(ECE)"],
        },
        "limitations": [
            "生成过程与检测规则共享同一套模式定义，存在结构性优势——真实数据上表现会显著更差",
            "只评测模式识别与证据有效性，不评测预测正确性",
            "单一虚构公司、单一行业形态，外部效度有限",
            "36 个月窗口不足以评测长周期风险与监控/复盘闭环",
        ],
    }
    (OUT / "planted_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    diff: dict[str, int] = {}
    for p in PLANTED:
        diff[p["difficulty"]] = diff.get(p["difficulty"], 0) + 1
    print(f"[risk] {len(MONTHS)} 期 · 营收 {len(rev)} 行 · 应收 {len(recv)} 行 · "
          f"供应 {len(sup)} 行 · 现金流 {len(cash)} 行")
    print(f"[risk] 植入模式 {len(PLANTED)} 条 {diff}，干扰项 {len(DISTRACTORS)} 条")
    print("[risk] → evals/risk/")


if __name__ == "__main__":
    main()
