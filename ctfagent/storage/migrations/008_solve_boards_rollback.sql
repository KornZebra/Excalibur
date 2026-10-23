-- 回滚 v8：删除作战图表（id 单调递增，删除后新建不会与历史 id 冲突）。
DROP INDEX IF EXISTS idx_solve_boards_project;
DROP TABLE IF EXISTS solve_boards;
