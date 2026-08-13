# P2 · 外部信息通道 Spec

## 1. Goal / Non-Goals
**Goal**：外部通道三类用途（事实补充 / 风险对照基准 / 政策新闻跟踪）就绪，可整体关闭（纯内网模式）。
**Non-Goals**：不做通用爬虫平台；不做外部主导分析。

## 2. 契约（详见 docs/ARCHITECTURE.md 外部通道节）
- `external/search.py`：搜索矩阵（SearXNG 主力 -> Brave -> Jina s.jina.ai -> Tavily）+ 学术直连（arXiv/OpenAlex/Crossref/S2）。
- `external/fetcher.py`：静态（httpx+trafilatura）-> JS（Jina/playwright 可选）-> PDF/Office（anydoc）。
- `external/anti_block.py`：UA 轮换/限速/退避/代理池（可配置）/robots 策略。
- `external/signals.py`：主题订阅 -> 定期抓取 -> 信号事件（供 RiskInsight）。
- `extraction/`：图片/扫描件三级通道（VISION API -> GPU 服务 -> RapidOCR）；extraction_method 标注。
- `core/snapshot.py`：外部证据快照 + SHA-256 冻结。
- 开关：`EXTERNAL_ENABLED=false` 全通道关闭。

## 3. 测试计划
1. 后端解析与降级矩阵（429/超时/空 -> 下一后端；全失败记录 degraded_reason）。
2. 反封锁：限速触发/退避/代理切换；403/验证码 -> 降级 Jina -> 付费 API。
3. anydoc：Office 14 格式 -> Markdown；加密/损坏/纯图 PDF 正确分类（纯图转视觉通道）。
4. 视觉三级通道顺序与降级；低置信度块禁入关键论断。
5. 信号：订阅 -> 抓取 -> 结构化信号事件。
6. 冻结：hash 变化 -> 重抓 + 标记。
7. 纯内网模式：开关关闭后功能完整（回归测试）。

## 4. 验收标准
- [ ] 三类用途端到端（1 个事实补充 + 1 个政策订阅）；纯内网开关生效。
- [ ] 外部指标可算（coverage@10/fetch_success_rate/blocked_rate）。
- [ ] 故障注入全绿（降级而非崩溃）。

## 5. 风险
- 反爬/网络不稳 -> 降级矩阵 + 代理池 + 国内源配置。
- anydoc 过新 -> 锁版本 + 契约测试 + 备选 markitdown/LibreOffice。

## 6. 工作量
约 7 个工作日（复用既有设计资产）。
