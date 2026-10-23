-- 回滚 v10：删除竞赛自动化模式的全部表（M5 新增，无历史数据依赖）。
DROP INDEX IF EXISTS idx_sub_time;
DROP INDEX IF EXISTS idx_sub_uniq;
DROP TABLE IF EXISTS submissions;
DROP INDEX IF EXISTS idx_pex_status;
DROP TABLE IF EXISTS platform_exercises;
DROP TABLE IF EXISTS capabilities;
DROP INDEX IF EXISTS idx_cfacts;
DROP TABLE IF EXISTS competition_facts;
DROP TABLE IF EXISTS strategy_state;
DROP TABLE IF EXISTS competitions;
