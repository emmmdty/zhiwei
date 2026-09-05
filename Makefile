PY   ?= .venv/bin/python
S    := evals/scripts
SUMS := evals/CHECKSUMS.sha256
# evals/knowledge 是 S5 评测先行的冻结语料（ADR-013 决策 2）：纳入 ART 即登记进
# CHECKSUMS.sha256，与 novels/questions/risk 同等冻结。
ART  := evals/novels evals/questions evals/risk evals/knowledge
HANDOFF_BASE ?= HEAD

.PHONY: help evals corpus questions risk checksums validate determinism clean-evals handoff-check

help:
	@echo "make evals         重建全部基准资产 → 写校验和 → 校验（数据即代码）"
	@echo "make corpus        仅重建两部名著语料（四形态）"
	@echo "make questions     仅重建 120 题题集"
	@echo "make risk          仅重建合成经营数据与植入清单"
	@echo "make checksums     重写 $(SUMS)"
	@echo "make validate      跑一致性 / 可复算 / 自校验 / 篡改检测（CI 门禁）"
	@echo "make determinism   连续两次干净重建，断言产物逐字节一致"
	@echo "make handoff-check HANDOFF_BASE=<RED commit> 校验 GREEN 阶段锁定测试与 evals/"
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

# LC_ALL=C + -f 固定"折叠大小写的字节序"：任何 locale 下 checksum 行序逐字节一致，
# 且与冻结基线（zh_CN collation 生成）字节序一致——纯 C 字节序会把 README.md 排到
# docs/ 之前，造成冻结资产漂移。
checksums:
	@find $(ART) -type f -exec sha256sum {} \; | LC_ALL=C sort -f -k2 > $(SUMS)
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

# 交接门禁：RED 已提交后，GREEN 阶段不得修改锁定测试，任何阶段都不得修改冻结资产。
# HANDOFF_BASE 应指向 RED commit；默认 HEAD 仅用于兼容既有交接单。untracked 文件同样算漂移。
# 详见 docs/DEV_ALLOCATION.md §4。
handoff-check:
	@bad=0; \
	if ! git rev-parse --verify "$(HANDOFF_BASE)^{commit}" >/dev/null 2>&1; then \
	  echo "[handoff] ✗ HANDOFF_BASE 不是有效 commit: $(HANDOFF_BASE)"; exit 2; \
	fi; \
	t=$$({ git diff --name-status "$(HANDOFF_BASE)" -- tests/; \
	        git ls-files --others --exclude-standard tests/ | sed 's/^/??\t/'; } 2>/dev/null); \
	if [ -n "$$t" ]; then \
	  echo "[handoff] ✗ tests/ 相对 RED commit 漂移——GREEN 阶段不得修改锁定测试:"; \
	  echo "$$t" | sed 's/^/           /'; bad=1; \
	fi; \
	e=$$({ git diff --name-status "$(HANDOFF_BASE)" -- evals/; \
	        git ls-files --others --exclude-standard evals/ | sed 's/^/??\t/'; } 2>/dev/null); \
	if [ -n "$$e" ]; then \
	  echo "[handoff] ✗ evals/ 冻结资产相对交接基线漂移:"; \
	  echo "$$e" | sed 's/^/           /'; bad=1; \
	fi; \
	if [ $$bad -eq 0 ]; then echo "[handoff] ✓ 锁定测试与 evals/ 相对 $(HANDOFF_BASE) 未漂移"; fi; \
	exit $$bad

clean-evals:
	rm -rf evals/novels/xiyouji/sql evals/novels/xiyouji/csv evals/novels/xiyouji/docs \
	       evals/novels/shuihu/xlsx evals/novels/shuihu/docs \
	       evals/questions/*.jsonl evals/questions/manual/*.jsonl \
	       evals/risk/*.db evals/risk/csv evals/risk/planted_manifest.json
