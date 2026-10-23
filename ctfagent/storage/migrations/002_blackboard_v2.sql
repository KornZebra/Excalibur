-- ============================================================================
-- CTF-Agent 迁移 002：blackboard v2（挑战级账本）
-- ----------------------------------------------------------------------------
-- 增量迁移：只新增表与索引，**不修改任何 v1 表结构**（v1 的 conversations /
-- messages / tool_executions / facts / challenges / flags / agent_traces / runs
-- 全部保持原样），因此 v2 → v1 回滚只需 DROP 本次新增的对象。
--
-- 术语：
--   Idea   = "接下来该往哪走"的假设，带生命周期，是调度依据
--   Memory = "已确认/已发生"的事实记录，追加式不可变，带类型
--   challenge_session = 一次自主解题会话的元状态（预算/状态/终止结论）
-- ============================================================================

-- 方向假设（调度依据） ------------------------------------------------------
CREATE TABLE IF NOT EXISTS ideas (
    id                 TEXT PRIMARY KEY,
    challenge_id       TEXT NOT NULL,
    text               TEXT NOT NULL,                      -- 一句可验证的假设/方向
    status             TEXT NOT NULL DEFAULT 'pending',    -- pending|testing|verified|failed|skipped
    created_by         TEXT,                               -- 创建者（solver_run id 或 system）
    claimed_by         TEXT,                               -- 认领该 Idea 的 solver_run id
    created_at         TEXT,
    updated_at         TEXT,
    last_transition_at TEXT,
    meta               TEXT NOT NULL DEFAULT '{}'
);

-- 事实记录（追加式，不允许改写历史条目） ------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    challenge_id  TEXT NOT NULL,
    kind          TEXT NOT NULL,                           -- fact|evidence|failure|note|hint
    summary       TEXT NOT NULL,                           -- 一行：什么 + 在哪 + 如何验证
    body          TEXT NOT NULL DEFAULT '',                -- 完整细节（不进上下文，按需取）
    source        TEXT NOT NULL DEFAULT 'agent',           -- agent|human|system
    solver_run_id TEXT,
    created_at    TEXT
);

-- Solver 生命周期记录 -------------------------------------------------------
CREATE TABLE IF NOT EXISTS solver_runs (
    id              TEXT PRIMARY KEY,
    challenge_id    TEXT NOT NULL,
    conversation_id TEXT,
    task_type       TEXT NOT NULL,                         -- bootstrap|explore|reason
    idea_id         TEXT,
    status          TEXT NOT NULL DEFAULT 'running',       -- running|concluding|done|timeout|failed|cancelled
    started_at      TEXT,
    ended_at        TEXT,
    concluded       INTEGER NOT NULL DEFAULT 0,            -- 0/1：是否已走过收尾阶段
    outcome         TEXT,                                  -- done|timeout_forced_conclude|cancelled|...
    error           TEXT,
    meta            TEXT NOT NULL DEFAULT '{}'
);

-- 挑战会话（自主解题会话的元状态；不复用 v1 challenges 表，保证 v1 零改动） --
CREATE TABLE IF NOT EXISTS challenge_sessions (
    challenge_id        TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'idle',      -- idle|running|stopping|terminated|aborted
    time_budget_minutes INTEGER NOT NULL DEFAULT 120,
    started_at          TEXT,
    ended_at            TEXT,
    verdict             TEXT,                              -- TERMINATE_SUCCESS|TERMINATE_EXHAUSTED|TERMINATE_TIMEUP|...
    reason              TEXT,
    spawned_solvers     INTEGER NOT NULL DEFAULT 0,
    meta                TEXT NOT NULL DEFAULT '{}'
);

-- 索引：调度回路每 interval 秒快照一次，必须走索引 --------------------------
CREATE INDEX IF NOT EXISTS idx_ideas_challenge_status   ON ideas(challenge_id, status);
CREATE INDEX IF NOT EXISTS idx_ideas_challenge_created  ON ideas(challenge_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_challenge_kind  ON memories(challenge_id, kind);
CREATE INDEX IF NOT EXISTS idx_memories_challenge_time  ON memories(challenge_id, created_at);
CREATE INDEX IF NOT EXISTS idx_solver_runs_challenge    ON solver_runs(challenge_id, status);