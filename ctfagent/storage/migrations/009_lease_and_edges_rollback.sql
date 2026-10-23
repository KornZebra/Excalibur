-- 回滚 v9：删除因果边表与租约列（SQLite 3.35+ 支持 DROP COLUMN）。
DROP INDEX IF EXISTS idx_sources_idea;
DROP INDEX IF EXISTS idx_sources_memory;
DROP TABLE IF EXISTS idea_sources;
DROP INDEX IF EXISTS idx_ideas_claim;
ALTER TABLE memories DROP COLUMN from_idea_id;
ALTER TABLE ideas DROP COLUMN claim_expires_at;
ALTER TABLE solver_runs DROP COLUMN lease_ttl_seconds;
ALTER TABLE solver_runs DROP COLUMN heartbeat_at;
