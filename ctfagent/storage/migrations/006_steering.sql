-- v6：运行中引导（Steering）——消息投递状态
--
-- 用户消息在任务运行期间发送时立即落库（delivery_status='queued'），
-- 任务在安全点把队列取出注入模型上下文后置为 'delivered'。
-- 存量数据全部视为已投递（默认值），历史行为不变。

ALTER TABLE messages ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'delivered';

CREATE INDEX IF NOT EXISTS idx_messages_conv_delivery
    ON messages(conversation_id, delivery_status, seq);
