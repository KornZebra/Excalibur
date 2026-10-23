-- U2a：对话附件元数据（增量迁移 v3；附件实体落在 workspace/conversations/{id}/attachments/）
CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_id      TEXT,
    filename        TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    size            INTEGER NOT NULL DEFAULT 0,
    mime            TEXT NOT NULL DEFAULT '',
    sha256          TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_conv ON attachments(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);
