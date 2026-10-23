-- ============================================================================
-- CTF-Agent 迁移 002 回滚：删除 blackboard v2 的全部新增对象
-- ----------------------------------------------------------------------------
-- 安全性：v1 表结构从未被本迁移修改，因此 DROP 之后 v1 代码即可继续正常运行
-- （v1 不会查询这些表）。执行方式：
--     python -m ctfagent.storage.migrate down --to 1
-- 该命令会先把数据库文件备份为 data/ctfagent.db.bak.<时间戳>。
-- ============================================================================

DROP INDEX IF EXISTS idx_solver_runs_challenge;
DROP INDEX IF EXISTS idx_memories_challenge_time;
DROP INDEX IF EXISTS idx_memories_challenge_kind;
DROP INDEX IF EXISTS idx_ideas_challenge_created;
DROP INDEX IF EXISTS idx_ideas_challenge_status;

DROP TABLE IF EXISTS challenge_sessions;
DROP TABLE IF EXISTS solver_runs;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS ideas;

-- 把 schema 版本退回 1（由 migrate CLI 负责写回，这里不直接改 schema_meta，
-- 以免在版本记录与实际结构之间产生不一致）。