-- data-consistency schema 基线（impl §2.2 / plan G1）
-- 约定：schema 版本号由迁移框架（migrate.py）在事务内写入 schema_version，本文件不含版本插入。
-- 演进：新增表/字段一律以 NNNN_*.sql 增量脚本追加，禁止修改本基线。

CREATE TABLE schema_version (
  version INTEGER NOT NULL
);

-- 配对与伴侣信息（会话密钥归 sync key_store 单一职责，本表不冗余存储）
CREATE TABLE pairing (
  id            INTEGER PRIMARY KEY,
  pairing_id    TEXT UNIQUE,        -- 本机身份（peer_id）
  peer_id       TEXT,               -- 伴侣 pairing-id
  peer_name     TEXT,               -- 伴侣用户昵称
  peer_pet_name TEXT,               -- 伴侣宠物名（pet.profile 同步）
  created_at    INTEGER
);

-- 事件日志（去重 + 审计；行号主键 id，避免与发送序号 seq 混淆）
CREATE TABLE events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- 本端行号
  event_id     TEXT UNIQUE,                        -- "{from}:{seq}" 全局唯一（去重键）
  type         TEXT NOT NULL,                      -- msg.carry / mood.sync / pet.feed ...
  peer         TEXT,                               -- 对端 peer_id
  payload_json TEXT,                               -- 明文（仅本端可见）
  created_at   INTEGER NOT NULL,
  acked_at     INTEGER                             -- 送达确认时间（发送侧记录）
);

-- 带话（落地 schema 与 carry-message impl §3.1 一致）
CREATE TABLE carries (
  id         TEXT PRIMARY KEY,     -- carryId（uuid4 hex）
  direction  TEXT NOT NULL,        -- 'out' 发送方记录 / 'in' 接收方记录
  text       TEXT NOT NULL,        -- 带话正文
  status     TEXT NOT NULL,        -- draft/sent/delivered/revoked/failed
  expires_at INTEGER NOT NULL,     -- 业务过期时间 Unix 秒
  created_at INTEGER NOT NULL,     -- 本端落库时间
  sent_at    INTEGER,              -- out: 发送方发送时间（撤回窗口基准）
  read_at    INTEGER,              -- 送达确认时间（ack）
  revoked_at INTEGER               -- 撤回时间
);
CREATE INDEX idx_carries_out_status ON carries(direction, status);

-- 礼物（发送记录）
CREATE TABLE gift_offers (
  gift_id     TEXT PRIMARY KEY,    -- giftId
  from_peer   TEXT,                -- 发送方 pairing-id
  to_peer     TEXT,                -- 接收方 pairing-id
  item_id     TEXT,                -- 对应 user_items.item_id
  state       TEXT NOT NULL,       -- sent / accepted / expired
  sent_at     INTEGER,
  expire_at   INTEGER,             -- 24h 过期判定
  accepted_at INTEGER
);

-- 礼物（解锁物品）
CREATE TABLE user_items (
  item_id   TEXT PRIMARY KEY,
  name      TEXT,
  type      TEXT,
  rarity    TEXT,
  source    TEXT,                  -- 谁送的 / 解锁来源
  expire_at INTEGER
);

-- 养成状态（本端一份；key 为 level/exp/intimacy/streak_days 等）
CREATE TABLE pet_state (
  key   TEXT PRIMARY KEY,
  value INTEGER
);

-- 纪念日
CREATE TABLE anniversaries (
  id                TEXT PRIMARY KEY,
  title             TEXT,
  date              TEXT,
  repeat            TEXT,
  calendar          TEXT,           -- solar / lunar
  notify_days_before INTEGER,
  updated_at        INTEGER
);
