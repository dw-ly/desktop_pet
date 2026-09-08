-- 队列收编（data-consistency S1/S2，plan §2.1）
-- 把 sync 层自带的 sync_queue.db（outbox/seen_events/meta）并入统一 core.db：
--   events 表承载发送队列（status 区分 pending/sent/failed）与接收去重（event_id 唯一）
--   kv 表承载 meta（seq 游标等持久化 kv）
-- 旧库数据迁移由代码层 migrate_legacy_queue 幂等执行（决策 D11），本脚本仅做 schema 演进。

ALTER TABLE events ADD COLUMN status     TEXT NOT NULL DEFAULT 'received';  -- 发送侧 pending/sent/failed；接收侧 received
ALTER TABLE events ADD COLUMN expires_at INTEGER;                           -- 发送侧过期时间
ALTER TABLE events ADD COLUMN seq        INTEGER;                           -- 发送方序号（补发按 seq 升序）

CREATE TABLE kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX idx_events_queue ON events(status, peer, seq);
