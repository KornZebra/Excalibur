-- ============================================================================
-- CTF-Agent 迁移 004 回滚：删除项目维度新增对象
-- ----------------------------------------------------------------------------
-- SQLite 的 ALTER TABLE 历史限制：直到 3.35 才提供 DROP COLUMN，且 "ADD COLUMN"
-- 本身没有官方逆操作。因此本回滚：
--   1) 删除本次新增的索引与 projects 表（一定可逆）；
--   2) 在支持的环境（SQLite >= 3.35，本机为 3.53）显式 DROP COLUMN 移除
--      conversations.project_id，保证 up→down→up 往返不产生
--      "duplicate column name: project_id"；
--   3) 在不支持 DROP COLUMN 的旧版 SQLite 上，第 3 步会失败，此时应保留该列
--      （多一个全 NULL 列不影响 v3 行为，语义等价于"所有会话都不属于任何项目"），
--      并配合数据库备份恢复 / 手工跳过 004 的 ADD COLUMN。
-- 执行方式：
--     python -m ctfagent.storage.migrate down --to 3
-- 该命令会先把数据库文件备份为 data/ctfagent.db.bak.<时间戳>。
-- ============================================================================

DROP INDEX IF EXISTS idx_conversations_project;
DROP INDEX IF EXISTS idx_projects_status_updated;
DROP TABLE IF EXISTS projects;
ALTER TABLE conversations DROP COLUMN project_id;