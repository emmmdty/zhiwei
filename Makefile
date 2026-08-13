PY   ?= .venv/bin/python
S    := evals/scripts
SUMS := evals/CHECKSUMS.sha256
ART  := evals/novels evals/questions evals/risk

.PHONY: help evals corpus questions risk checksums validate determinism clean-evals handoff-check

help:
	@echo "make evals         重建全部基准资产 → 写校验和 → 校验（数据即代码）"
	@echo "make corpus        仅重建两部名著语料（四形态）"
	@echo "make questions     仅重建 120 题题集"
	@echo "make risk          仅重建合成经营数据与植入清单"
	@echo "make checksums     重写 $(SUMS)"
	@echo "make validate      跑一致性 / 可复算 / 自校验 / 篡改检测（CI 门禁）"
	@echo "make determinism   连续两次干净重建，断言产物逐字节一致"
	@echo "make handoff-check 校验交接规则：tests/ 与 evals/ 未被实现方改动"
	@echo "make assets-lock   校验冻结资产与 $(SUMS) 无漂移（等价 zhiwei assets lock --check）"

# 冻结资产 lock Gate：默认只读校验，漂移即失败；重写需显式 --write
assets-lock:
	$(PY) -m zhiwei.cli.main assets lock --check

# 顺序不可颠倒：题集的 ground truth 由已发布语料算出，语料必须先就位
evals: corpus questions risk checksums validate

corpus:
	$(PY) $(S)/build_corpus.py

questions:
	$(PY) $(S)/gen_questions.py
	$(PY) $(S)/gen_manual_questions.py

risk:
	$(PY) $(S)/gen_risk_data.py

# LC_ALL=C 固定字节序排序：任何 locale 下 checksum 行序逐字节一致，
# 否则 zh_CN/C.UTF-8 环境下 sort 的 collation 差异会让同一批资产产生不同的 lock 顺序。
checksums:
	@find $(ART) -type f -exec sha256sum {} \; | LC_ALL=C sort -k2 > $(SUMS)
	@echo "[checksums] $$(wc -l < $(SUMS)) 个产物 → $(SUMS)"

validate:
	$(PY) $(S)/validate_corpus.py

# 可复现性是本基准的核心承诺之一，值得一个独立门禁。
# 两轮之间 sleep 跨秒：XLSX/PDF 的时间戳类不确定性只在跨秒时才暴露。
determinism:
	@$(MAKE) --no-print-directory clean-evals >/dev/null 2>&1
	@$(MAKE) --no-print-directory corpus questions risk >/dev/null 2>&1
	@find $(ART) -type f -exec sha256sum {} \; | sort | sha256sum > /tmp/zhiwei-det-1
	@sleep 1.1
	@$(MAKE) --no-print-directory clean-evals >/dev/null 2>&1
	@$(MAKE) --no-print-directory corpus questions risk >/dev/null 2>&1
	@find $(ART) -type f -exec sha256sum {} \; | sort | sha256sum > /tmp/zhiwei-det-2
	@cmp -s /tmp/zhiwei-det-1 /tmp/zhiwei-det-2 \
	  && echo "[determinism] ✓ 两次干净重建产物逐字节一致" \
	  || { echo "[determinism] ✗ 产物不可复现"; exit 1; }
	@$(MAKE) --no-print-directory checksums

# 交接门禁：实现方（B/C 档）只写实现，不得动测试与冻结资产。
# 前提是交接前 RED 已提交——因此这里比对的基线就是 HEAD，untracked 的新测试文件同样算违规。
# 详见 docs/DEV_ALLOCATION.md §4。
handoff-check:
	@bad=0; \
	t=$$(git status --porcelain -- tests/ 2>/dev/null); \
	if [ -n "$$t" ]; then \
	  echo "[handoff] ✗ tests/ 被改动——实现方不得修改测试（含加 skip / 改断言 / 新增用例）:"; \
	  echo "$$t" | sed 's/^/           /'; bad=1; \
	fi; \
	e=$$(git status --porcelain -- evals/ 2>/dev/null); \
	if [ -n "$$e" ]; then \
	  echo "[handoff] ✗ evals/ 冻结资产被改动:"; \
	  echo "$$e" | sed 's/^/           /'; bad=1; \
	fi; \
	if [ $$bad -eq 0 ]; then echo "[handoff] ✓ tests/ 与 evals/ 未被改动"; fi; \
	exit $$bad

clean-evals:
	rm -rf evals/novels/xiyouji/sql evals/novels/xiyouji/csv evals/novels/xiyouji/docs \
	       evals/novels/shuihu/xlsx evals/novels/shuihu/docs \
	       evals/questions/*.jsonl evals/questions/manual/*.jsonl \
	       evals/risk/*.db evals/risk/csv evals/risk/planted_manifest.json
