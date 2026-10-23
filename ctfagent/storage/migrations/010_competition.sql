-- v10：竞赛自动化模式（M5）——竞赛级全局黑板 + 能力注册 + 题目池 + 提交记录 + 策略状态
--
-- 设计要点：
-- 1) competition_facts 是**竞赛级**黑板（append-only），与题目级 memories 分离：
--    规则/协议/约束是跨题共享的，生命周期是「一场比赛」而非「一道题」。
-- 2) capabilities 是文档解析产物，enabled 默认 0 —— 解析错误不得直接打生产。
-- 3) submissions 唯一索引 (competition, exercise, flag) 天然去重。
-- 4) strategy_state 存令牌桶/退避/冷却/置顶等运行态（key-value，避免频繁改表）。
CREATE TABLE IF NOT EXISTS competitions (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    platform_kind  TEXT NOT NULL DEFAULT 'generic_rest',
    base_url       TEXT NOT NULL,
    auth_kind      TEXT NOT NULL DEFAULT 'header',
    auth_header    TEXT NOT NULL DEFAULT '',
    auth_ref       TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'idle',
    strategy       TEXT NOT NULL DEFAULT '{}',
    meta           TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competition_facts (
    id             TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL,
    kind           TEXT NOT NULL,
    summary        TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'doc',
    ref            TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cfacts ON competition_facts(competition_id, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS capabilities (
    competition_id TEXT NOT NULL,
    name           TEXT NOT NULL,
    transport      TEXT NOT NULL DEFAULT 'rest',
    spec           TEXT NOT NULL DEFAULT '{}',
    confidence     TEXT NOT NULL DEFAULT 'medium',
    enabled        INTEGER NOT NULL DEFAULT 0,
    last_test_at   TEXT,
    last_test_ok   INTEGER,
    PRIMARY KEY (competition_id, name)
);

CREATE TABLE IF NOT EXISTS platform_exercises (
    competition_id TEXT NOT NULL,
    exercise_key   TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL DEFAULT '',
    score          REAL NOT NULL DEFAULT 0,
    difficulty     TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'open',
    challenge_id   TEXT,
    env            TEXT NOT NULL DEFAULT '{}',
    flags_got      INTEGER NOT NULL DEFAULT 0,
    flags_total    INTEGER NOT NULL DEFAULT 1,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_sync      TEXT,
    PRIMARY KEY (competition_id, exercise_key)
);
CREATE INDEX IF NOT EXISTS idx_pex_status ON platform_exercises(competition_id, status);

CREATE TABLE IF NOT EXISTS submissions (
    id             TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL,
    exercise_key   TEXT NOT NULL,
    flag           TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    platform_msg   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_uniq ON submissions(competition_id, exercise_key, flag);
CREATE INDEX IF NOT EXISTS idx_sub_time ON submissions(competition_id, created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_state (
    competition_id TEXT NOT NULL,
    key            TEXT NOT NULL,
    value          TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (competition_id, key)
);
