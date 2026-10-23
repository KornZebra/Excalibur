-- v7：黑板（facts）按「题目」隔离
--
-- 旧模型：facts 以 conversation_id 为主键。同一题目的多个 Solver 会话各写各的黑板，
-- 控制台看不到彼此的结论；会话升级为题目后，历史事实仍留在会话作用域里「丢失」。
--
-- 新模型：引入统一作用域键 scope
--   'challenge:<challenge_id>'     —— 题目隔离单元（同题共享，跨题隔离）
--   'conversation:<conversation_id>' —— 未绑定题目的普通会话
-- conversation_id / challenge_id 仅作审计与项目聚合，不再参与唯一性。

CREATE TABLE IF NOT EXISTS facts_scoped (
    scope           TEXT NOT NULL,
    key             TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    challenge_id    TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (scope, key)
);

-- 回填：会话已绑定题目 → 归入题目作用域；否则保持会话作用域。
-- 同一 (scope, key) 多次出现时，按 updated_at 升序覆盖 → 最新结论胜出。
INSERT OR REPLACE INTO facts_scoped(
    scope, key, summary, body, tags, conversation_id, challenge_id, created_at, updated_at
)
SELECT
    CASE
        WHEN COALESCE(c.challenge_id, '') <> '' THEN 'challenge:' || c.challenge_id
        ELSE 'conversation:' || f.conversation_id
    END,
    f.key, f.summary, f.body, f.tags, f.conversation_id, NULLIF(c.challenge_id, ''),
    f.created_at, f.updated_at
FROM facts f
LEFT JOIN conversations c ON c.id = f.conversation_id
ORDER BY f.updated_at ASC;

DROP TABLE facts;
ALTER TABLE facts_scoped RENAME TO facts;

CREATE INDEX IF NOT EXISTS idx_facts_scope_updated ON facts(scope, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_facts_challenge ON facts(challenge_id, updated_at DESC);
