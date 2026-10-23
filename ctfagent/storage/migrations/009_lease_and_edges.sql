-- v9：M1 租约化 + M2 因果边（纯增量：2 个可空列 + 1 张边表 + 索引）
--
-- M1：Solver 认领的租约化。heartbeat_at 由 SolverRun 看门狗周期续约，
--     claim_expires_at = heartbeat_at + lease_ttl_seconds；过期即视为死亡（policy 判定）。
--     旧数据两列为 NULL → 自动退回原有 soft_timeout 启发式，行为不变。
-- M2：Fact-Intent 因果边。idea_sources 记录"这条方向从哪些事实推来"；
--     memories.from_idea_id 记录"这条事实由哪条方向产出"。均为可空/可选，旧写入路径不变。
ALTER TABLE solver_runs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE solver_runs ADD COLUMN lease_ttl_seconds INTEGER NOT NULL DEFAULT 180;
ALTER TABLE ideas ADD COLUMN claim_expires_at TEXT;
CREATE INDEX IF NOT EXISTS idx_ideas_claim ON ideas(challenge_id, claim_expires_at);

CREATE TABLE IF NOT EXISTS idea_sources (
    idea_id      TEXT NOT NULL,
    memory_id    TEXT NOT NULL,
    challenge_id TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (idea_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_sources_memory ON idea_sources(memory_id);
CREATE INDEX IF NOT EXISTS idx_sources_idea ON idea_sources(idea_id);

ALTER TABLE memories ADD COLUMN from_idea_id TEXT;
