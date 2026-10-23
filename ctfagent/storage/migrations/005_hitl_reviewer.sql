-- ============================================================================
-- CTF-Agent 迁移 005（U6a）：多模式审查——审查记录落库 + 会话级 reviewer 覆盖
-- ----------------------------------------------------------------------------
-- 纯增量：
--   1) 新增 hitl_decisions 表：每一条工具调用裁决（whitelist 免审 / policy 放行 /
--      human 人工 / audit_agent 快速审计）都留痕，支持闭环回填执行结果；
--   2) conversations 追加可空列 hitl_reviewer：NULL = 跟随全局 default_reviewer
--      （inherited），显式值 = 会话级覆盖（human | audit_agent | none）。
-- v1 的 hitl_requests 表（挂起审批）语义不变，两表并存、互不影响。
-- ============================================================================

-- 审查裁决留痕（decided_by 四种取值：human | audit_agent | policy | whitelist）--
CREATE TABLE IF NOT EXISTS hitl_decisions (
    id                      TEXT PRIMARY KEY,
    conversation_id         TEXT,
    run_id                  TEXT,
    tool_call_id            TEXT,
    tool_name               TEXT NOT NULL,
    arguments_json          TEXT NOT NULL DEFAULT '{}',
    reviewer                TEXT NOT NULL,
    mode                    TEXT DEFAULT '',
    decision                TEXT NOT NULL,
    comment                 TEXT DEFAULT '',
    edited_arguments_json   TEXT,
    elapsed_ms              INTEGER,
    execution_id            TEXT,
    execution_result_digest TEXT,
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hitl_dec_conv ON hitl_decisions(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hitl_dec_reviewer ON hitl_decisions(reviewer);

-- 会话级审查模式覆盖（NULL = 跟随全局）------------------------------------
ALTER TABLE conversations ADD COLUMN hitl_reviewer TEXT;
