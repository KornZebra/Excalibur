-- v3 回滚：仅移除附件元数据表（磁盘上的附件文件不动，避免不可逆的数据丢失）
DROP INDEX IF EXISTS idx_attachments_msg;
DROP INDEX IF EXISTS idx_attachments_conv;
DROP TABLE IF EXISTS attachments;
