# P0 · 基座 Spec：骨架 + 模型池 + 登录 + 基础对话

## 1. Goal / Non-Goals
**Goal**：新仓库可运行骨架 + LLM 模型池（多端点/在线切换/健康/快照/降级）+ 登录会话 + 无数据源的基础对话回路（可演示）。
**Non-Goals**：不做数据接入（P1）；不做多轮语义（P3）；不做权限矩阵（P1/P5）。

## 2. 契约
- `src/zhiwei/core/llm/client.py`：OpenAI 兼容 chat completions（messages/response_format/json_schema/tool calls/streaming/usage 解析）。（见 docs/MODELS.md）
- `core/llm/registry.py`：端点 CRUD + 健康探测 + 多粒度解析（会话>组>全局）+ model_snapshot。
- `auth/login.py`：Argon2id + 会话令牌（HttpOnly SameSite）+ 邀请制基础。
- `gateway/api.py`：FastAPI + `POST /v1/auth/login|logout` + `POST /v1/conversations/{id}/messages`（基础版）+ `GET/POST /v1/admin/models`。
- `core/config.py`：pydantic-settings（LLM_MODEL_NAME/VISION_MODEL_NAME/DATABASE_URL/MASTER_KEY）。

## 3. 测试计划（L0，全 mock）
1. client：请求组装正确（含 json_schema）；usage 解析（含 thinking tokens）；坏 JSON 重试；429/5xx 退避；超时。
2. registry：端点 CRUD；健康探测（mock /models）；粒度优先级（会话>组>全局）；快照留痕。
3. 降级链：主 4xx -> 备用 -> 全失败 -> MODEL_UNAVAILABLE；降级事件入流。
4. auth：登录成功/失败；令牌哈希存储；过期；越权管理 API 拒绝（403）。
5. 对话：基础文本问答（FakeLLM）返回 Answer{text, trace_refs=[] , confidence}；模型覆盖下一轮生效。
6. 成本：usage x 定价 -> cost_usd 累计；会话成本上限。

## 4. 验收标准
- [ ] `uv run pytest tests/l0 tests/l1` 全绿。
- [ ] 演示：登录 -> 提问 -> 切换模型 -> 再问（模型快照不同）。
- [ ] 恶意/越权请求被拒（403）+ 审计记录。

## 5. 风险
- 模型 API 差异（thinking/工具格式）-> 客户端兼容层 + mock 多样性。
- 定价变动 -> 定价表集中 + 预算缓冲。

## 6. 工作量
约 8 个工作日。
