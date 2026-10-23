# CTF-Agent Web 控制台

零构建依赖的单页静态前端：**纯原生 HTML / CSS / JavaScript**，不使用 Node 构建、npm、
打包器或任何 CDN 外部依赖，由 FastAPI 直接静态托管后端同源提供。

> ⚠ 合规声明：本工具仅限 **CTF 竞赛 / 自建靶场 / 已授权环境** 使用。

---

## 1. 目录结构

```
web/
├── index.html          页面骨架：三栏布局 + 五个主视图 + 脚本加载顺序
├── README.md           本文件
├── css/
│   └── style.css       深色安全工具主题（三栏 CSS Grid、等宽代码样式、审批弹窗）
└── js/
    ├── markdown.js     极简 Markdown 渲染器（自行 HTML 转义，防 XSS）
    ├── api.js          REST + SSE 客户端封装（fetch + ReadableStream 手写 SSE）
    ├── timeline.js     时间线渲染（消息流、工具卡片、子代理分组、todo、审批弹窗）
    └── app.js          入口：初始化、哈希路由/标签页、单一状态源、SSE 事件接线
```

### 脚本加载顺序（不可调整）

```html
<script src="js/markdown.js"></script>  <!-- 提供 CTFA.markdown -->
<script src="js/api.js"></script>       <!-- 提供 CTFA.api -->
<script src="js/timeline.js"></script>  <!-- 依赖 markdown + api -->
<script src="js/app.js"></script>       <!-- 依赖以上全部，最后启动 -->
```

四个脚本都是经典脚本（非 ES Module），共用全局命名空间 `window.CTFA`，因此可以
直接以 `file://` 之外的任意静态方式加载，无需构建。

---

## 2. 页面结构

```
┌──────────────────────── 顶栏：Logo / 视图标签 / health / 模式 / 刷新 ────────────────────────┐
├──────────────┬──────────────────────────────────────────────┬───────────────────────────────┤
│ 左栏         │ 主区                                          │ 右栏                          │
│ · 合规声明   │ · todo 进度条（主区顶部，固定）                │ · 黑板 facts（搜索 + 详情）   │
│ · 模式切换   │ · progress 行                                 │ · flag 列表                   │
│ · 题目选择   │ · 消息时间线（用户/代理卡片、思考块、工具卡片、  │ · 题目元数据                  │
│ · 会话列表   │   子代理分组）                                 │                               │
│ · 工具列表   │ · 「回到底部」按钮 / token 用量角标            │                               │
│ · 子代理列表 │                                               │                               │
├──────────────┴──────────────────────────────────────────────┴───────────────────────────────┤
│ 输入区：多行 textarea（Ctrl/Cmd+Enter 发送）· 发送 · 停止 · 运行状态                        │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 哈希路由

| 路由 | 视图 |
|---|---|
| `#/chat` | 消息时间线（默认） |
| `#/c/<conversationId>` | 选中会话并切到对话视图（`history.replaceState`，不会重新加载历史） |
| `#/tools` | 工具清单表格（`GET /api/tools`） |
| `#/agents` | 子代理卡片（`GET /api/agents`） |
| `#/config` | 配置 JSON 编辑器（`GET` / `PUT /api/config`） |
| `#/challenges` | 题目列表（`GET /api/challenges`），可「选择此题目」 |

---

## 3. 状态管理（app.js）

`app.js` 维护**单一 state 对象**，`timeline.js` 只负责把事件增量写成 DOM，
渲染函数一律从 state 读取数据，二者不交叉持有业务状态。

```js
state = {
  mode, orchestration, health, online,
  tools, toolQuery, agents, conversations, challenges, config,
  conversationId, challengeId, messages,
  facts, factQuery, factKey, flags,
  running, runId, controller, cancelled,
  toolCards, todos, usage, route
}
```

调试时可在浏览器控制台直接访问 `CTFA.state`、`CTFA.refs`，或调用 `CTFA.reload()`。

---

## 4. SSE 事件 → 渲染行为对照表

请求入口：

- 单代理：`POST /api/agent/stream`，body `{message, conversation_id?, challenge_id?}`
- 多代理：`POST /api/multi-agent/stream`，body `{..., orchestration: "deep"|"plan_execute"}`

`api.js` 的 `streamAgent(url, body, handlers, options)` 使用 `fetch` +
`ReadableStream` 手写 SSE 解析（`EventSource` 不支持 POST，故不使用）：

- 按空行（`\n\n` / `\r\n\r\n`）切分事件块；
- 块内解析 `event:` 与 `data:` 行，**`data:` 支持多行并以 `\n` 拼接**；
- 以 `:` 开头的行为注释（忽略）；`retry` 忽略；
- `data` 尝试 `JSON.parse`，失败则原样以字符串下发；
- `options.signal` / `options.controller` 支持 `AbortController` 中断，中断时回调
  `cancelled` 事件；另提供 `CTFA.api.createStream(url, body, handlers)` 直接返回
  `{controller, promise}`。

| event | app.js 中的处理 | 视图表现 |
|---|---|---|
| `conversation` | 写入 `state.conversationId / runId / challengeId`，静默更新 URL | 顶栏/状态栏、会话列表高亮 |
| `message_saved` | 追加 `state.messages`（`role=user` 时与本地回显去重） | 用户消息已在发送时即时上屏，不重复插入 |
| `progress` | `timeline.setProgress(stage, message, iteration)` | 主区顶部 progress 行（轮次 · 阶段 · 说明） |
| `planning` | `timeline.appendPlanning(content)` | 「🗺 规划」可折叠块（整块覆盖） |
| `todos` | 写入 `state.todos` → `timeline.renderTodos` | 顶部 todo 进度条：完成比例 + 每项状态 |
| `thinking_stream_delta` | `timeline.appendThinking(delta)` | 「💭 思考中…」折叠块，默认折叠，带动态点 |
| `response_start` | `timeline.beginResponse()`（同时收起思考块） | 新建正文流容器 |
| `response_delta` | `timeline.appendResponse(delta)` | 正文 Markdown 流式追加（rAF 节流） |
| `tool_call` | 记录 `state.toolCards[id]` → `upsertToolCall` | 工具卡片出现，状态 `running`；`name === "task"` 时创建子代理分组 |
| `tool_result` | 回填结果 → `setToolResult` | 卡片状态 `ok`/`error`，结果区可展开、超长截断、`executionId` 可复制；`flag_submit` 成功自动刷新 flag |
| `sub_agent_reply_stream_delta` | `appendSubAgentDelta(agentId, agentName, delta)` | 子代理分组内的流式 Markdown |
| `usage_summary` | 写入 `state.usage` | 主区右下角 token 角标 |
| `hitl_request` | `timeline.showHitl(...)` + `markToolApproval(toolCallId)` | 审批弹窗 + 卡片转「等待审批」 |
| `error` | `timeline.showError(message)` | 红色错误块 + toast |
| `cancelled` | `state.cancelled = true` | toast 提示，收尾状态显示「已取消」 |
| `response` | `finalizeResponse(content)` + 追加 assistant 消息 | 以最终内容为权威覆盖流式文本 |
| `done` | 收尾、刷新会话/facts/flags | 代理卡片状态「已完成」，恢复输入 |

**兼容性说明**：`tool_call.arguments` 既可能是对象也可能是 JSON 字符串，
`timeline.js` 的 `parseMaybeJson()` 统一兼容两种形式；工具结果同样兼容字符串与
`{output|content|text|result|stdout|...}` 包装对象。

---

## 5. 关键实现细节

### 5.1 流式渲染节流（增量，不整段重绘）

`timeline.js` 的 `queueDelta(target, text)` 把增量写入目标缓冲区并入队，
`schedule()` 用 `requestAnimationFrame` 批量刷新：

- `thinking_stream_delta` / `sub_agent_reply_stream_delta` / `response_delta`
  不会每个 delta 触发一次 DOM 重排；
- Markdown 目标额外做 70ms 时间节流，未到时间的目标留到下一帧（`flushDirty`）；
- 收尾时 `flushNow()` 立即冲刷，保证最后一段文本完整上屏。

### 5.2 自动滚动

`timeline` 容器监听 `scroll`：距底部 > 80px 视为「用户主动上滚」，
暂停自动滚动并显示「↓ 回到底部」按钮；回到底部或距离 < 80px 时自动恢复。

### 5.3 工具卡片

- 标题行：状态图标（`◐` running / `✔` ok / `✖` error / `⏳` awaiting-approval）
  + 工具名 + 子代理徽标；
- 展开后分「参数」「结果」两段，等宽可滚动；超过 2600 字符或 40 行自动截断，
  提供「查看完整输出」按钮；
- 若事件带 `persistedFile`，额外提供「下载/打开完整输出 ↗」，通过
  `GET /api/files?path=<encodeURIComponent(path)>` 在新标签打开
  （前端强制校验 `path` 以 `workspace/` 开头）；
- 展示 `executionId` 并提供复制按钮。

### 5.4 子代理分组

多代理模式下 `tool_call.name === "task"` 会创建子代理分组卡片，分组内部嵌套：

1. 该子代理产生的 `sub_agent_reply_stream_delta` 流式文本；
2. 后续 `agent` 字段归属该分组的工具卡片。

分组名优先从 `arguments` 中的 `agent / agent_name / subagent_type / name` 推断；
若拿不到，则在收到第一个 `sub_agent_reply_stream_delta` 时把 `agentName` 绑定到
最近的待定分组。

### 5.5 HITL 审批弹窗

`hitl_request` 触发模态框，显示工具名、格式化参数（**可编辑 JSON**）、
按 `timeoutAt`（epoch ms）计算的倒计时，三个按钮：

| 按钮 | 提交内容 |
|---|---|
| 批准 | `{decision: "approve", reason: "", arguments: null}` |
| 拒绝 | `{decision: "reject", reason: "<必填>", arguments: null}` |
| 改参批准 | `{decision: "approve", reason: "", arguments: <编辑后的 JSON>}`（参数未变化时退化为普通批准） |

倒计时归零后标记「已超时（自动拒绝）」并自动关闭；JSON 非法时阻止提交并内联报错。

### 5.6 Markdown 安全

`markdown.js` 的渲染顺序是「先转义，后套格式」：行内代码先抽成占位符保护，
其余文本整体 `escapeHtml`，格式标记只在**已转义**的文本上匹配，
因此任何原始 HTML / 脚本都无法注入。链接协议白名单：
`http(s) / mailto / tel / ftp`、相对路径与 `#锚点`；`javascript:`、`data:`、
`vbscript:` 一律降级为纯文本。所有外链均带 `target="_blank" rel="noopener noreferrer"`。

支持：标题、粗体、斜体、删除线、行内代码、围栏代码块（带语言角标）、
无序/有序列表（支持缩进嵌套）、引用块、表格、水平线、链接、换行，以及
`<img>`（同样经过 URL 白名单）。**不支持原始 HTML（会被转义显示）**。

---

## 6. 如何扩展

| 目标 | 做法 |
|---|---|
| 新增一个 SSE 事件 | ① 在 `app.js` 的 `streamHandlers()` 里加同名键（事件名即函数名），更新 state；② 在 `timeline.js` 里加渲染函数并导出；③ 在 `CTFA.timeline` 导出表中登记 |
| 新增一类工具卡片样式 | 在 `timeline.js` 的 `createToolCard()` 中按 `call.name` 分支，或加 `card.el.classList.add('tool-card-xxx')`，再在 `style.css` 里补样式 |
| 新增主视图 / 标签页 | ① `index.html` 中加 `<section class="view" data-view="xxx" hidden>` 与 `data-route="/xxx"` 的按钮；② `app.js` 的 `renderXxxView()` 渲染；③ 路由自动生效 |
| 更换强调色/主题 | 只改 `style.css` 顶部的 `:root` 变量（`--accent`、`--accent-2`、`--bg-*` 等） |
| 新增右栏面板 | `index.html` 加 `button[data-tab]` 与 `.tabpane[data-pane]`，`app.js` 的 tab 切换逻辑按 data 属性自动匹配，无需改 JS |
| 承载新的文件下载 | 统一走 `CTFA.api.openFile(path)`（内部强制 `workspace/` 前缀 + URL 编码） |

---

## 7. 后端对接速查

| 用途 | 调用 |
|---|---|
| 健康检查 | `GET /api/health` |
| 工具 / 子代理 | `GET /api/tools`、`GET /api/agents` |
| 配置读写 | `GET /api/config`、`PUT /api/config` |
| 会话与历史 | `GET /api/conversations`、`GET /api/conversations/{id}/messages` |
| 黑板 facts | `GET /api/facts?conversation_id=&query=`、`GET /api/facts/{key}?conversation_id=` |
| Flag 列表 | `GET /api/flags?conversation_id=` |
| 题目 | `GET /api/challenges` |
| 文件下载 | `GET /api/files?path=workspace/...`（前端 URL 编码） |
| 人工审批 | `POST /api/hitl/{request_id}` |
| 取消运行 | `POST /api/runs/{run_id}/cancel` |
| 流式对话 | `POST /api/agent/stream`、`POST /api/multi-agent/stream` |

所有请求同源，基址为空字符串（相对路径），由 FastAPI 静态托管时天然对齐。

---

## 8. 本地自检

```bash
# 1) 列出前端文件
python3 -c "import glob;print(sorted(glob.glob('web/**/*',recursive=True)))"

# 2) JS 语法检查（需要 node）
for f in web/js/*.js; do node --check "$f" && echo "OK $f"; done

# 3) 静态托管（示例，任意静态服务器均可）
python3 -m http.server 8080 -d web
```