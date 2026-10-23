-- v8：作战图（solve boards）—— 题目 × 战法 的显式编排快照。
--
-- 设计（对齐 rust_nuclei 的 PlanState，但去掉坐标）：执行范围只来自 edges，
-- 节点只记录引用，不做笛卡尔积。同一题目的多条连线按序「接力」。
--
-- 所有列均为新增，不动既有表。
CREATE TABLE IF NOT EXISTS solve_boards (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    project_id  TEXT,
    nodes       TEXT NOT NULL DEFAULT '[]',   -- [{id, kind:'challenge'|'tactic', ref}]
    edges       TEXT NOT NULL DEFAULT '[]',   -- [{challenge, tactic}]，顺序即执行顺序
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_solve_boards_project ON solve_boards(project_id);
