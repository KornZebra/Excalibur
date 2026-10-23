-- ============================================================================
-- CTF-Agent 迁移 004（U4）：项目（比赛）维度组织
-- ----------------------------------------------------------------------------
-- 纯增量：新增 projects 表，并给 conversations 追加可空外键列 project_id。
-- 单列可空 ⇒ 一个会话属于 0 或 1 个项目（永不出现多项目归属），无需额外约束。
-- v1/v2 既有表结构与既有语义保持不变；不传 project_id 的会话行为与升级前完全一致。
--
-- 关于回滚：SQLite 的 ALTER TABLE 直到 3.35 才支持 DROP COLUMN，且 ADD COLUMN
-- 没有官方逆操作；004_projects_rollback.sql 会在支持的环境删除该列以支持
-- up→down→up 往返，不支持的环境只能保留该列（全 NULL 列不影响 v3 行为）。
-- ============================================================================

-- 项目（比赛）----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'active',     -- active|paused|archived（不强制枚举，留扩展空间）
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    meta        TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_projects_status_updated ON projects(status, updated_at DESC);

-- 会话的项目归属（0 或 1）----------------------------------------------------
ALTER TABLE conversations ADD COLUMN project_id TEXT;
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id);