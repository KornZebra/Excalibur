-- v6 回滚：移除 messages.delivery_status 与配套索引
DROP INDEX IF EXISTS idx_messages_conv_delivery;
ALTER TABLE messages DROP COLUMN delivery_status;
