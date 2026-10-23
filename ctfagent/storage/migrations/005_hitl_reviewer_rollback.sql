-- ============================================================================
-- CTF-Agent 迁移 005 回滚：删除多模式审查新增对象
-- ----------------------------------------------------------------------------
-- 1) 删除 hitl_decisions 表与其索引（一定可逆）；
-- 2) 在支持的环境（SQLite >= 3.35）DROP COLUMN 移除 conversations.hitl_reviewer，
--    保证 up→down→up 往返不产生 duplicate column；旧版 SQLite 上该步会失败，
--    此时保留该列（全 NULL 列不影响 v4 行为），配合备份恢复即可。
-- 执行方式：python -m ctfagent.storage.migrate down --to 4
-- ============================================================================

DROP INDEX IF EXISTS idx_hitl_dec_reviewer;
DROP INDEX IF EXISTS idx_hitl_dec_conv;
DROP TABLE IF EXISTS hitl_decisions;
ALTER TABLE conversations DROP COLUMN hitl_reviewer;
