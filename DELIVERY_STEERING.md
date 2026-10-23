# 交付说明：运行中引导（Steering）人机协同机制

实现 Claude Code 同款"任务不停、指令插队"：AI 任务执行期间用户可继续在聊天框输入并发送，
消息不打断当前执行，而是进入该会话的 FIFO 队列；当前**轮次**（一次 LLM 调用 + 其触发的全部
工具执行）结束后的**安全点**（下一次模型请求构建之前），队首积压按序合并注入，agent 带着
新指令继续执行。用户全程无需停止任务。

## 一、改动清单（增量，无无关重构）

### 后端
| 文件 | 改动 |
|---|---|
| `ctfagent/storage/migrations/006_steering.sql`（+rollback） | **迁移 v6**：`messages.delivery_status`（`queued\|delivered`，存量默认 delivered）+ 索引 |
| `ctfagent/storage/db.py` | `SCHEMA_VERSION=6`，注册迁移与回滚 |
| `ctfagent/storage/repos.py` | `MessageRepo.append(delivery_status=)`；`mark_delivered(ids)`；`list_queued(cid)`；`to_chat_messages(exclude_queued=)` 且**相邻 user 行合并**（与运行时合并语义一致）；`_normalize_message` 暴露 `deliveryStatus` |
| `ctfagent/steering.py`（新增） | `ConversationHub`（会话级事件枢纽：seq 单调递增 + 回放缓冲 + 多订阅者）；`SteeringCoordinator`（按会话登记运行任务、FIFO 队列、会话锁、`drain`/`has_pending`/`cancel`、任务结束 watcher） |
| `ctfagent/events.py` | 新事件类型：`round_started / message_queued / message_delivered / task_done / task_error` |
| `ctfagent/agent/loop.py` | `AgentRunner.run` 新参数 `steering` / `pre_persisted_user_message_id`；主循环**安全点注入**（取消先于 drain 判定）；自然收尾前**队列非空则续段**；`round_started` / `message_delivered` 事件；触发消息已预落库时跳过重复落库 |
| `ctfagent/agent/prompts.py` | 系统提示词补充【运行中引导】说明（仅主代理、启用 steering 时） |
| `ctfagent/runtime.py` | 挂载 `runtime.steering`；`start_run` 透传 steering 参数 |
| `ctfagent/api/routes_steer.py`（新增） | 统一入口 / state / events(SSE) / cancel 四个 API |
| `ctfagent/tools/execution_service.py` | hard 取消传播：内建/MCP handler 工具执行任务可被 `task.cancel()` 直接打断（yaml 配方工具仍走协作终止 + 进程组清理），消除僵尸工具协程 |
| `ctfagent/app.py` | 注册 routes_steer |

### 前端（`web/`）
| 文件 | 改动 |
|---|---|
| `js/api.js` | `sendConversationMessage / conversationState / cancelConversationTask`；`streamConversationEvents`（GET SSE，Last-Event-ID 断点续传）；SSE 回调透传事件 `id` |
| `js/app.js` | 发送分流（空闲→启动任务 / 运行中→入队）；**输入框运行中不再禁用**；会话级长驻事件流管理（断线自动重连续传）；`message_queued/message_delivered/task_done/task_error` 处理；队列计数提示；**按 runId 过滤过期运行事件**；`recoverSteerState()` 刷新恢复；停止按钮走会话级取消（hard） |
| `js/timeline.js` | 用户气泡投递状态角标（排队中/已送达/未投递）与翻转 API；历史渲染区分未投递消息 |
| `index.html` / `css/style.css` | 队列提示位 + 角标样式（全部走 CSS Variables，无硬编码色值） |

### 测试（`tests/`，本项目首次建立测试目录）
- `conftest.py`：手工装配最小 Runtime + 脚本化 FakeModel（记录每次请求体）+ httpx 客户端；
- `test_steering_flow.py`：12 个行为测试（对应验收标准 S1–S9）；
- `test_steering_frontend.py`：6 个前端静态接线断言（项目无 Node 构建链，按惯例做静态检查）。

## 二、我做了选择的地方（及理由）

1. **事件通道用 SSE**（需求允许 SSE/WS 二选一）：项目既有事件面全部是 FastAPI + sse-starlette
   （无 WebSocket 基础设施），会话级流沿用 SSE，支持 `Last-Event-ID` 断点补发（`id:` 帧由
   会话枢纽的 seq 提供）。
2. **事件命名 `message_queued` 等下划线风格**（需求写的是 `message.queued`）：跟随项目既有
   事件命名惯例（`message_saved`/`tool_call`…），JS handler 键友好；载荷结构与契约一致。
   `delta/tool.call/tool.result` 的语义由既有 `response_delta/tool_call/tool_result` 事件承载，
   会话枢纽原样转发，不重复造事件。
3. **统一入口请求体为超集**：`{text}` 之外接受可选 `mode/orchestration/challengeId/attachmentIds`
   （仅"启动任务"分支生效，排队消息忽略、沿用任务启动时的配置）；响应含 `queueLength` 便于前端即时渲染。
4. **注入合并的两个一致性设计**：
   - 一次安全点把积压合并为**一条** user 消息（空行分隔、无系统前缀）；若上下文尾部已是 user
     消息（本轮触发消息或 finalizer/截断续跑提示），合并进该条，绝不产生连续多条 user 消息；
   - 引导消息**逐条落库**（保留前端气泡粒度），历史还原时对相邻 user 行做同样的合并——重载后的
     模型上下文与运行时所见一致。
5. **队列真相源在数据库**：入队即落库（queued）、drain 在会话锁内原子取出并置 delivered；
   `/state` 的 `queuedMessages` 直接读库，刷新恢复不依赖内存。
6. **取消后排队消息不自动投递**：保持 `queued` 态留在消息历史（前端"未投递"角标），由用户决定
   是否重发——按需求 §三.5 字面语义；不静默丢弃。
7. **`/state` 额外返回 `lastEventId`**（枢纽当前 seq）：页面刷新后前端从"当前尾巴"接事件流，
   避免把已渲染历史对应的事件重放一遍；**断线重连**则用客户端已消费的 seq 真正续传（不丢事件）。
8. **前端按 runId 过滤运行级事件**：会话级流是长驻的，断点补发/重连可能重放旧任务事件——
   runId 不匹配当前运行的一律丢弃（浏览器端到端验证中发现并修复：旧 `done` 重放会误杀新运行状态）。
9. **`max_iterations` 触顶时队列若仍有积压**：任务正常结束，消息保持 queued 可见（同取消语义），
   不做无限续命。
10. **运行中发送暂不支持附件**：附件清单块需要拼进任务首轮输入，引导消息为纯文本注入；
    前端对运行中带附件的发送给出明确提示（附件保留在输入区）。

## 三、手工验收路径

准备：`python run.py` 启动（数据库自动应用迁移 v6），Web 界面配置好模型通道；
建议审查模式切"⚡ 完全访问"或"🤖 Agent 快速审查"避免人工审批打断观察。

1. **运行中连发 2 条 → 均排队**：发送一个耗时任务（如"对 127.0.0.1 做全端口扫描并分析"）；
   运行状态变为"运行中…"后，输入框**不 Disabled**，直接连发两条新指令。
   预期：两条气泡立即上屏并带"排队中"角标；输入框上方出现"⏳ 还有 N 条排队指令，将在当前步骤结束后送达"。
2. **当前轮结束按序注入**：等当前模型回复 + 工具执行结束进入下一轮。预期：两条角标翻转为
   "已送达"，队列提示消失；模型行为体现新指令（必要时先简述调整后的计划——系统提示词已告知）。
3. **刷新页面视图完整**：任务运行中刷新页面 → 从左侧列表点开会话。预期：运行状态恢复为
   "运行中…"，队列提示与角标恢复；已 delivered 的消息按普通历史渲染。
   （空闲时刷新 → 会话中取消遗留的 queued 消息显示"未投递"角标。）
4. **取消不丢消息**：运行中排队一条消息后点"停止"。预期：任务取消（当前工具被中止），
   排队消息仍在消息流中（"未投递"角标），可重新发送。
5. **断线重连**（可选）：任务运行中短暂断网/重启服务后页面自动重连事件流，漏掉的事件补发。

> 自动化等价物：`tests/test_steering_flow.py`（S1–S9）覆盖上述全部行为断言，其中"注入内容出现在
> 后续模型请求上下文"通过记录 mock LLM 请求体断言；SSE 流式行为用进程内真实 uvicorn（TCP）验证
> （httpx 的 ASGITransport 会缓冲整个响应，无法测长驻流）。

## 四、测试结果

```
$ python -m pytest tests/ -q
19 passed
```

- S1 空闲启动/运行中入队 + message_queued 事件 + 落库状态
- S2 FIFO 顺序注入、单条合并、请求体断言（恰好一次、内容完整、顺序正确）
- S3 最终回答前队列非空 → 新段继续（3 次模型调用）
- S4 注入后续段直至队列清空，task_done=completed
- S5 graceful/hard 取消：排队消息保留（queued 态）、模型从未见过、hard 中止进行中工具
- S6 空白消息 400；并发 5 条全部入队、FIFO、单次合并注入
- S7 /state：idle/running/round/queuedMessages 全程正确
- S8 /events：Last-Event-ID 断点补发（真实 TCP SSE）+ 实时投递顺序
- S9 会话隔离：A 队列不影响 B，B 空闲发送启动 B 自己的任务
- 前端静态接线 7 项（统一入口/事件处理/runId 过滤/角标/队列提示/CSS 变量约束等）

ruff 检查：本改动相关文件无告警（`orchestrator/solver_pool.py` 存在一个与本次无关的既有 ASYNC110 提示）。

## 五、已知限制

1. **进程重启丢运行态**：任务与队列是进程内状态（持久化的是消息行）；服务重启后运行中任务
   中断，未投递消息保持 queued 可见可重发（与项目现有 per-run SSE 行为一致，无变化）。
2. **刷新后当前轮已流出的增量不回放**：页面刷新从 `/state.lastEventId`（事件流当前尾巴）接入，
   避免整段历史重放；当前轮已经流式输出过的文本以历史消息表为准（最终答案落库后完整可见）。
   真正的**断线重连**（非刷新）用客户端已消费 seq 续传，不丢事件。
3. **运行中引导不支持附件**（见上，选择 10）。
4. **子代理运行不接收引导**：Steering 只作用于主代理循环；blackboard 模式的挑战会话
   （Dispatcher/Solver）不接入本机制（其 hint 通道 §10.1 已存在）。
5. **旧会话流 `/api/agent/stream` 不广播到会话事件流**：CLI 与旧接口行为不变；新前端统一走
   `/messages` + `/events`。
6. Python 层无 `-race` 等价物：并发正确性由会话锁 + 单消费者队列保证，并以并发测试
   （S6 并发 5 条、S9 双会话并行）作行为验证。
