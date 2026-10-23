-- v7 回滚：facts 回到以 conversation_id 为主键的结构。
-- 题目作用域的事实回落到「最后写入的会话」；conversation_id 为空时用 scope 的后半段。

CREATE TABLE IF NOT EXISTS facts_legacy (
    conversation_id TEXT NOT NULL,
    key             TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (conversation_id, key)
);

INSERT OR REPLACE INTO facts_legacy(conversation_id, key, summary, body, tags, created_at, updated_at)
SELECT
    COALESCE(NULLIF(conversation_id, ''), substr(scope, instr(scope, ':') + 1)),
    key, summary, body, tags, created_at, updated_at
FROM facts
ORDER BY updated_at ASC;

DROP TABLE facts;
ALTER TABLE facts_legacy RENAME TO facts;

CREATE INDEX IF NOT EXISTS idx_facts_updated ON facts(conversation_id, updated_at DESC);
