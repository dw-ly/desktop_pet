# 数据模型与离线一致性 实现规格（impl）

> 关联 Spec：[[7-AI学习/11-AI桌宠情侣互联/spec/data-consistency/spec.md|spec/data-consistency]]
> 关联 Plan：[[7-AI学习/11-AI桌宠情侣互联/spec/data-consistency/plan.md|plan/data-consistency]]
> 创建日期：2026-08-04
> 状态：已确认（G1/G2 + S1-S6 + C1/C2 全部落地；实施历史见 §8）

## 目的

将 plan 的 TODO 细化为**可直接编码的契约**。本模块为横切基础设施，**G1/G2（`core.db` 统一数据库 + 迁移框架）先行落地**（plan §1/§4 决策），成为所有业务模块（带话/情绪/养成/纪念日/互赠）的持久化地基；carry-message 已按此调整（`carries` 表为 `core.db` 基线表，修订 carry impl D4）。

S1-S6/C1/C2 已按批次落地，以下各节契约即最终实现；本稿为 data-consistency 的完整实现规格。

---

## 0. 目录结构与模块归属

```
src/core/
├── db.py                ← plan G1  统一数据库连接管理（core.db）
├── migrations/
│   └── 0001_init.sql    ← plan G1  schema 基线（7 张业务表 + schema_version）
├── migrate.py           ← plan G2  迁移执行框架（启动时按序应用增量脚本）
├── events.py            ← plan S1  业务事件常量/聚合
├── prune.py             ← plan S3  事件日志清理
├── consistency.py       ← plan S4  冲突合并 + 亲密度重放
├── daily_sync.py        ← plan S5  每日零点对齐
├── backup_restore.py    ← plan S6  换机/重装恢复
└── ...
src/ui/settings_service.py  ← plan C1  备份/同步编排（UI 无关）
src/ui/settings_dialog.py   ← plan C1  备份/手动同步入口（PySide6 薄封装）
tools/consistency_demo.py   ← plan C2  联调脚本
tests/core/test_db.py       ← 本次
tests/core/test_migrate.py  ← 本次
```

---

## 1. 全局约定

### 1.1 数据库文件与线程模型

- 统一库文件名 `core.db`，位于 **`SyncConfig.data_dir`**（与 sync 层同一运行时目录；sync 的 `sync_queue.db` 在本模块 S1/S2 收编前继续并存）。
- 单连接 + `threading.RLock` 串行化所有 DB 操作；连接 `check_same_thread=False`、`row_factory=sqlite3.Row`、`PRAGMA journal_mode=WAL`、`PRAGMA synchronous=NORMAL`、`PRAGMA foreign_keys=ON`、`PRAGMA busy_timeout=5000`。
- 所有访问经 `Database.execute` / `Database.transaction()`；禁止裸持有连接跨线程。
- WAL 允许读/写并发；写操作在事务内原子。

### 1.2 命名与编码约定

| 约定 | 说明 |
|------|------|
| `event_id` | 全局唯一去重键 = `"{from}:{seq}"`（`from` 为发送方 peer_id，`seq` 为发送方单调序号） |
| events 表行号主键 | **`id`**（06 笔记原用 `seq`，与业务发送序号同名易混淆，本次改名，见决策记录 D6） |
| 时间 | 一律 Unix 秒（`int(time.time())`） |
| payload_json | 明文 JSON，仅本端可见；外发一律密文（spec §4.2） |
| 外键 | 本 schema 不建立跨表外键约束（`carries` 等按业务 id 关联，避免迁移与导出复杂化）；`foreign_keys=ON` 仅为未来扩展保留 |

### 1.3 与 sync-security 的边界（S1/S2 已收编，单库）

> 收编状态：✅ 批次2 已收编。`sync_queue.db` 不再创建/使用（旧数据已迁入 `core.db`），`queue_db_path` 字段保留兼容。

| 库 | 用途 | 归属 | 状态 |
|----|------|------|------|
| `sync_queue.db` | outbox + seen_events + meta（旧自带队列表） | sync 层（已落地） | 已收编进 `core.db.events`/`kv`（S1/S2） |
| `core.db` | 业务持久化 + 事件日志/去重/发送队列（pairing/events/kv/carries/gift_offers/user_items/pet_state/anniversaries） | 本模块 G1 | 唯一数据源 |

收编后，业务模块与 sync 层统一读写 `core.db`（sync 经 `SyncQueue(db, my_peer_id)` 鸭子类型访问，不直接依赖 core 类型）。

---

## 2. G1 db.py（本次实施）

### 2.1 连接管理契约

```python
class Database:
    """统一数据库。所有访问经本类；内部单连接 + threading.RLock。"""

    def __init__(self, data_dir: Path) -> None:
        # 建 data_dir/core.db；row_factory=sqlite3.Row；
        # PRAGMA WAL/synchronous=NORMAL/foreign_keys=ON/busy_timeout=5000；单连接 + RLock

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """加锁执行；不自动 commit（写事务显式用 transaction()）。"""

    def executemany(self, sql: str, seq_of_params: list[tuple]) -> None: ...

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """多行查询；Row 支持 row["col"] 与 row[0] 两种访问。"""

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """单行查询；无结果返回 None。"""

    def table_exists(self, name: str) -> bool: ...

    def transaction(self) -> ContextManager:
        """最外层 BEGIN/COMMIT，内层 SAVEPOINT/RELEASE（v2 已实现）。
        内层回滚到 savepoint 不影响外层已写内容，异常继续上抛。"""

    def run_script_in_transaction(self, sql_text: str, version: int | None = None) -> None:
        """迁移专用：单事务执行多语句脚本，可选写版本号；禁止嵌套在 transaction() 内。"""

    def get_schema_version(self) -> int:
        """无 schema_version 表（空库/旧库）→ 返回 0。"""

    def set_schema_version(self, version: int) -> None: ...

    def close(self) -> None:
        """幂等关闭（重复调用无副作用）。"""

def init_core(data_dir: Path) -> Database:
    """启动入口：建目录 → 连接 → migrate 到最新版本；迁移失败抛异常并关闭连接。"""
```

- `get_schema_version`：`SELECT version FROM schema_version ORDER BY version DESC LIMIT 1`；表不存在（尚未跑 0001）返回 0。
- `set_schema_version`：`INSERT`（version 单调递增，不做 upsert——迁移只增不减）。
- `row_factory`：连接级 `sqlite3.Row`（一次设置全局生效），便捷查询返回行对象；业务层如需要 dict 可显式 `dict(row)`。
- `busy_timeout=5000`：WAL 多连接写冲突时等待而非立即 `SQLITE_BUSY`。
- `init_core` 由 `core/__init__.py` 导出；内部延迟导入 `migrate` 避免循环依赖。业务代码在应用启动序列调用一次。

### 2.2 migrations/0001_init.sql 基线

> 表字段以 06 笔记 schema 为准（plan G1 验收项），差异见 §2.3 注。

```sql
-- 版本表（初始 1；版本号由迁移框架在事务内写入 schema_version，0001 脚本本身不含 INSERT）
CREATE TABLE schema_version (
  version INTEGER NOT NULL
);

-- 配对与伴侣信息（本机身份 + 伴侣元数据；会话密钥归 sync key_store 单一职责，本表不冗余存储）
CREATE TABLE pairing (
  id            INTEGER PRIMARY KEY,
  pairing_id    TEXT UNIQUE,        -- 本机身份（peer_id）
  peer_id       TEXT,               -- 伴侣 pairing-id
  peer_name     TEXT,               -- 伴侣用户昵称
  peer_pet_name TEXT,               -- 伴侣宠物名（pet.profile 同步）
  created_at    INTEGER
);

-- 事件日志（去重 + 审计；S1/S2 收编 sync_queue 的 outbox/seen 后统一承载）
CREATE TABLE events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- 本端行号
  event_id    TEXT UNIQUE,        -- "{from}:{seq}" 全局唯一（去重键）
  type        TEXT NOT NULL,      -- msg.carry / mood.sync / pet.feed ...
  peer        TEXT,               -- 对端 peer_id
  payload_json TEXT,              -- 明文（仅本端可见）
  created_at  INTEGER NOT NULL,
  acked_at    INTEGER             -- 送达确认时间（发送侧记录）
);

-- 带话（落地 schema 与 carry-message impl §3.1 一致）
CREATE TABLE carries (
  id         TEXT PRIMARY KEY,    -- carryId
  direction  TEXT NOT NULL,       -- out / in
  text       TEXT NOT NULL,
  status     TEXT NOT NULL,       -- draft/sent/delivered/revoked/failed
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  sent_at    INTEGER,
  read_at    INTEGER,
  revoked_at INTEGER
);
CREATE INDEX idx_carries_out_status ON carries(direction, status);

-- 礼物（发送记录）
CREATE TABLE gift_offers (
  gift_id     TEXT PRIMARY KEY,   -- giftId
  from_peer   TEXT,
  to_peer     TEXT,
  item_id     TEXT,
  state       TEXT NOT NULL,      -- sent / accepted / expired
  sent_at     INTEGER,
  expire_at   INTEGER,            -- 24h 过期判定
  accepted_at INTEGER
);

-- 礼物（解锁物品）
CREATE TABLE user_items (
  item_id   TEXT PRIMARY KEY,
  name      TEXT,
  type      TEXT,
  rarity    TEXT,
  source    TEXT,                 -- 谁送的 / 解锁来源
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
  calendar          TEXT,          -- solar / lunar
  notify_days_before INTEGER,
  updated_at        INTEGER
);
```

### 2.3 表字段差异说明（相对 06 笔记）

| 表 | 差异 | 理由 |
|----|------|------|
| `events` | 行号主键 `seq` → **`id`** | 避免与业务发送序号 `seq` 同名混淆（决策记录 D6） |
| `events` | 补 `id` AUTOINCREMENT；`event_id` 改 `TEXT UNIQUE` 并注释 `"{from}:{seq}"` | 与 sync 层去重键定义一致（spec §3.3.3） |
| `pairing` | 移除 `session_key` 列 | 密钥存储归 sync key_store 单一职责（决策 D10）；备份不含会话密钥（spec §3.4） |
| `carries` | 已按 carry impl §3.1 定稿 | 两文档同源，本表为落地基线 |

其余表与 06 笔记逐字段一致。

### 2.4 断言级测试（test_db.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 空库建表 | 新 data_dir 初始化 | `core.db` 存在；8 张表齐全（schema_version + 7 业务表）；`schema_version` 含 `version=1` |
| WAL 生效 | 初始化后查 PRAGMA | `journal_mode == "wal"` |
| 事务提交 | transaction 内插入+更新 | 提交后数据可见 |
| 事务回滚 | transaction 内抛异常 | 无部分写入（回滚前查询为空） |
| 并发写 | 2 线程各 50 次 execute | 无 `ProgrammingError`；最终行数正确 |
| 无锁禁止跨线程 | 裸持有 connection 跨线程访问 | （约定；测试以 Database API 为准） |
| 字段与 06 一致 | 逐表 PRAGMA table_info | 列名/类型与 §2.2 一致 |
| get_schema_version 空库 | 未跑 0001 的库 | 返回 0 |

---

## 3. G2 migrate.py（本次实施）

### 3.1 迁移框架契约

```python
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIG_PATTERN = re.compile(r"^(\d{4})_.*\.sql$")

def migrate(db: Database) -> None:
    """启动时调用：应用所有编号 > 当前版本的迁移脚本。幂等。"""
    current = db.get_schema_version()
    scripts = sorted(
        (int(m.group(1)), p)
        for p in MIGRATIONS_DIR.glob("*.sql")
        if (m := _MIG_PATTERN.match(p.name))
    )
    for version, path in scripts:
        if version <= current:
            continue
        with db.transaction():              # 每个脚本一个事务
            db.executescript(path.read_text(encoding="utf-8"))
            db.set_schema_version(version)  # 失败则回滚，版本号不变
```

约定：

| 约定 | 说明 |
|------|------|
| 文件命名 | `NNNN_name.sql`（4 位编号 + 下划线 + 语义名），如 `0001_init.sql` |
| 0001 特殊性 | 唯一含 `CREATE TABLE schema_version`；空库首次运行从 0001 起全部执行 |
| 事务 | 每个脚本一个事务；中途失败整体回滚（含版本号），应用不启动并提示用户 |
| 幂等 | 已应用版本跳过；重复执行无副作用 |
| 演进 | 新增表/字段一律以新编号迁移脚本追加，**禁止修改已发布脚本** |

### 3.2 断言级测试（test_migrate.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 空库初始化 | 空 data_dir 跑 `migrate` | schema_version=1；8 张表存在 |
| 增量应用 | 构造临时 `0002_add_test.sql`（建 test 表） | 跑后 version=2、test 表存在 |
| 脚本失败回滚 | `0002` 内含非法 SQL | 抛异常；version 仍 1；test 表不存在 |
| 重复执行幂等 | 连续两次 `migrate` | 无异常、version 不变、无副作用 |
| 乱序文件 | 003 与 002 同时存在 | 严格按编号升序应用 |
| 非迁移文件忽略 | 目录含 `README.txt`/`notes.sql` | 不参与、不报错 |

---

## 4. S 系列 / C 系列契约框架（后续批次）

> 以下为契约预留。实施前需填充对应批次契约，并同步更新本稿状态。关键衔接点先行固定。

### 4.1 S1/S2：queue 收编（`src/sync/queue.py` 改造 + 迁移）

> 状态：✅ 已落地（批次2）—— 收编后 `sync_queue.db` 不再使用（`queue_db_path` 字段保留兼容），
> outbox / seen_events / meta 全部并入 `core.db` 的 `events` / `kv` 表。

**目标**：sync 层 `outbox`/`seen_events`/`meta` 收编进 `core.db`（plan §2.1：统一并入 events，以状态字段区分 pending/sent/failed；消除双 schema 冲突）。

**schema 演进（0002 迁移脚本 `0002_events_queue.sql`）**：

```sql
ALTER TABLE events ADD COLUMN status     TEXT NOT NULL DEFAULT 'received';  -- 发送侧 pending/sent/failed；接收侧 received
ALTER TABLE events ADD COLUMN expires_at INTEGER;                           -- 发送侧过期时间
ALTER TABLE events ADD COLUMN seq        INTEGER;                           -- 发送方序号（补发按 seq 升序）
CREATE TABLE kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);                                            -- meta 收编（seq 游标等持久化 kv）
CREATE INDEX idx_events_queue ON events(status, peer, seq);
```

**events 表收编后语义**：

| 方向 | event_id | status | seq | peer | 用途 |
|------|----------|--------|-----|------|------|
| 发送侧 | `"{本端}:{seq}"` | pending → sent/failed | 分配序号 | 对方 | 离线补发（flush 按 `seq` 升序）；在线直发成功也落为 `sent`（D17） |
| 接收侧 | `"{对方}:{seq}"` | received（默认） | 对方序号 | 对方 | 去重（`event_id` UNIQUE 冲突即丢弃） |

- 同一库内发送侧/接收侧 `event_id` 前缀不同（本端 vs 对方），不冲突。
- 接收去重 = `INSERT INTO events(event_id, ...)` 唯一冲突即丢弃（幂等）。
- **在线直发落表（D17）**：直发成功经 `record_sent` 落为 `status='sent'`，events 表为**完整双向事件日志**（本端已发 + 本端已收）——双端重放同一事件集收敛（spec §3.3.6），daily_align 的亲密度兜底核对才成立。纯日志行由 S3 prune 按保留期清理（pet.feed 为支撑重放豁免）。

**SyncQueue 改造**（`src/sync/queue.py`，基于统一 Database，鸭子类型避免 core→sync 循环依赖）：

```python
class SyncQueue:
    """发送出站队列 + 接收去重（收编 core.db.events）。

    db 为 core.db（Database），仅依赖 execute/query_all/query_one/transaction 方法；
    my_peer_id 用于发送侧 event_id 前缀（f"{my_peer_id}:{seq}"）。
    """

    def __init__(self, db, my_peer_id: str) -> None: ...
    def meta_get(self, key: str) -> str | None: ...       # kv 表
    def meta_set(self, key: str, value: str) -> None: ...  # kv 表 upsert
    def make_seq_manager(self, peer_id: str) -> SeqManager: ...  # kv 表驱动；游标键控本端（D18）
    def enqueue(self, peer_id, seq, type, payload, expires_at=None) -> None:
        # INSERT INTO events(event_id=f"{my_peer_id}:{seq}", seq, type, peer=peer_id,
        #                    payload_json, created_at, status='pending', expires_at)
    def record_sent(self, peer_id, seq, type, payload, expires_at=None) -> None:
        # 在线直发成功后落 events 表（status='sent'，D17）：与 enqueue 同构仅状态不同
    async def flush(self, peer_id, send_cb) -> list[Exception]:
        # SELECT ... WHERE peer=? AND status='pending' ORDER BY seq
        # 过期（expires_at < now）→ status='failed'；send_cb 成功 → status='sent'；失败保持 pending
    def receive(self, message, deliver) -> bool:
        # 单事务：INSERT event_id（唯一冲突 → 丢弃返回 False）→ deliver(message)；
        # 投递抛异常整体回滚（不留下半插入）
    def close(self) -> None: ...   # no-op（连接归 core.db 管理）
```

**数据迁移（代码层幂等，决策 D11）**：

```python
LEGACY_MIGRATION_KEY = "legacy_queue_migrated"

def migrate_legacy_queue(db, queue_db_path: str, my_peer_id: str) -> int:
    """旧 sync_queue.db（outbox/seen_events/meta）→ core.db（events/kv）。幂等。

    - kv.legacy_queue_migrated 已存在 → 返回 0
    - 旧库文件缺失 → 设标记，返回 0
    - 旧库损坏 → log warning，设标记跳过（不阻塞启动）
    - 只读打开旧库；outbox → events（status 保留原值，event_id=f"{my_peer_id}:{seq}"）
      seen_events → events（type=''，peer=from_peer，status='received'，created_at=received_at）
      meta → kv 原样复制（seq 游标不丢，防序号回退）
    - 返回迁移的事件行数（outbox + seen）
    """
```

**SyncManager 改造**：

- 构造新增 `db` 参数（必填，`core.db` 由 Core 启动时 `init_core` 创建后注入）。
- `_async_main` 顺序：`_get_identity()` → `migrate_legacy_queue(db, queue_db_path, my_peer_id)` → `SyncQueue(db, my_peer_id)`。
- 不再实际使用 `queue_db_path`（字段保留兼容）。

**测试（tests/sync/test_queue.py 改造 + 迁移用例）**：

| 用例 | 断言 |
|------|------|
| enqueue/flush 按 id 升序 | sent 顺序 = 入队顺序 |
| flush 幂等不重发 | 二次 flush 不重复发送 |
| 过期标记 failed | sent == []；记录 status='failed' |
| 发送失败保留 pending | 二次 flush 重试成功 |
| receive 去重单投递 | 同 message 第二次返回 False |
| 不同发送方同 seq 不冲突 | 两条均投递 |
| receive 投递失败回滚 | 回滚后可再次投递 |
| migrate_legacy_queue 迁移 | 旧库 outbox/seen/meta → events/kv 正确；幂等；旧库缺失跳过 |

**衔接点**：`manager.make_seq_manager` 的 meta 已收编到 `kv` 表；`events.seq` 为补发排序键（plan S2"按序号升序补发"）。

### 4.2 S3：事件日志清理（`src/core/prune.py`）

> 状态：✅ 已落地（批次3，独立完成）—— 规则见 spec §3.3.5 / 06 笔记 §四。

**清理条件**（仅动 `events` 表；分批删除防长事务）：

```python
def prune_event_log(db, retention_days: int = 90, batch: int = 1000) -> int:
    """清理 events 表超过保留期的记录。返回删除行数。

    可删（满足其一，且 created_at < cutoff = now - retention_days*86400）：
      - status='received'（接收侧：插入即处理完，超期即可删）
      - acked_at 非空（发送侧已收到送达确认）
      - status='failed'（补发过期 / 业务失败标记——各业务模块在状态变更时同步标记）
      - expires_at 非空且 < now（未确认但超期，视为失败）
    保留：90 天内记录；或未确认未过期（仍在去重/补发窗口内）。

    实现：循环 DELETE ... LIMIT batch 直至删除 0 行（每批一次事务，不跨批持锁）。
    """
```

- **业务失败标记衔接**：带话 failed / 礼物 expired 等由各业务模块在状态变更时把对应 `events` 行标记为 `status='failed'`，纳入下一轮清理（06 笔记 §四注）；本轮仅按 `events` 自身字段清理。
- **pet.feed 豁免（D17）**：`type='pet.feed'` 的历史**不清理**——重放需要完整事件流收敛（spec §3.3.6）；feed 受每日 5 次上限约束，事件量有界，豁免不会导致无限增长。
- **触发**：Core 启动时 + 每日零点各一次（与 S5 对齐任务共用定时器；接入点随主项目启动序列接入）。

### 4.3 S4：冲突合并与亲密度重放（`src/core/consistency.py`）

> 状态：✅ 已落地（批次3）—— 规则见 spec §3.3.6/§3.3.7 / 06 笔记 §五。
> `pet.feed` payload 线格式（delta/reason/ts/sig）由 pet-growth plan（已确认）定稿，本模块拥有合入校验逻辑，pet-growth 发送/接收复用。

**`pet.feed` payload 契约**（与 04 笔记 §增量同步一致）：

```python
{
    "delta": 10,          # int，积分增量（>0；只增不降）
    "reason": "carry",    # str，INTIMACY_REASONS 之一
    "ts": 1786_000_000,   # int，事件 Unix 秒（本地日归属每日上限）
    "sig": "<hex>",       # HMAC-SHA256(mac_key, f"{delta}:{reason}:{ts}")，hex
}
```

- `mac_key = derive_mac_key(my_private, peer_public)`（sync crypto `SUBKEY_MAC`，两端同值）。
- **reason 枚举** `INTIMACY_REASONS = ("carry", "chat", "feed", "gift", "streak")`（与 pet-growth 积分来源表一致）。
- **每日上限** `INTIMACY_DAILY_LIMITS`（按**次数**）：chat=1、feed=5、streak=1（来源表明确）；carry（5min 频率限制）/gift（接受事件为准、发送侧已限）由发送侧约束，本侧不设每日上限，仅验签防伪。计数存 `kv.intimacy_count:{date}:{reason}`，按事件 ts 的本地日归属（重放历史事件不计入今天）。

```python
def sign_intimacy_event(mac_key: bytes, delta: int, reason: str, ts: int) -> str:
    """签名规范化（发送侧复用，两端互验）。"""
def verify_intimacy_signature(mac_key, delta, reason, ts, sig) -> bool:
    """恒定时间比较。"""
def apply_intimacy_event(db, ev: dict, mac_key: bytes) -> bool:
    """亲密度合入唯一入口（pet-growth 复用）。单事务原子：
    熔断检查 → 验签 → 每日 reason 上限 → 累加 pet_state.intimacy。
    返回 True=合入；False=拒绝（签名非法/超上限/已熔断）。
    熔断：连续签名失败 ≥3 次 → kv.intimacy_halted=1，停止自动合入
    （spec §3.4"重大异常停止自动合入并提示手动同步"）；成功清零计数。
    """
def merge_lww(db, table, key_col, ts_col, row, ts, *, once=False) -> bool:
    """通用 LWW 合并。返回 True=写入；False=忽略。
    - 键不存在 → 插入（row 须含 ts_col）
    - 已存在：once=True（一次性事件如 gift.accept 装扮解锁）→ 忽略；
      现有 ts >= 新 ts → 忽略；否则更新全部 row 列
    - ts_col=None 且键已存在 → 忽略（无时间戳不可覆盖）
    table/key_col/ts_col 必须为代码内受控标识符（禁止用户输入拼接）。
    """
```

**衔接点**：`pet_state.intimacy` 累加 + `kv` 计数/熔断状态均走 `Database`；mac_key 由调用方（pet-growth 发送/接收侧，经 SyncManager 会话上下文）派生后传入，本模块不直接接触密钥存储。双端从同一事件流各自重放收敛（spec §3.3.6）。

### 4.4 S5：每日对齐（`src/core/daily_sync.py`）

> 状态：✅ 已落地（批次4）—— 规则见 spec §3.3.8 / 06 笔记 §五兜底。
> `date.sync` payload 定稿 `{"streak": int, "intimacy": int, "ts": int}`（累计数据快照，02 笔记/事件表同步更新）。

```python
class AlignResult(str, Enum):
    OK = "ok"
    REPAIRED = "repaired"
    MANUAL_NEEDED = "manual_needed"   # S4 熔断（kv.intimacy_halted）→ 不自动修复，提示手动
    DEFERRED = "deferred"             # 未连接，推迟到重连后（plan S5 断网推迟语义）

def build_snapshot(db) -> dict:
    """本端累计数据快照：{"streak", "intimacy", "ts"}（date.sync payload）。"""

def daily_align(sync, db, mac_key: bytes | None = None) -> AlignResult:
    """每日零点兜底对齐（后台定时任务调用，同步纯函数不阻塞主线程）。
    - 已熔断 → MANUAL_NEEDED（spec §3.4 重大异常停止自动合入并提示手动同步）
    - 未连接 → DEFERRED（调用方在重连回调重新触发）
    - 已连接 → 发 date.sync 快照 + 亲密度本地重放兜底核对
      （mac_key 为空则仅发快照、跳过重放；用于未建立会话密钥的场景）
    - 重放总和 != pet_state.intimacy → 修复为总和，REPAIRED"""

def handle_peer_snapshot(db, payload: dict) -> AlignResult:
    """收到对端 date.sync 快照：streak 取较大者修复（只增兜底；灰色期降级由
    pet-growth 状态机管理，不在此覆盖）。"""

def replay_intimacy_total(db, mac_key: bytes) -> int:
    """重放 events 表 pet.feed 事件流（验签 + 每日上限，按事件 ts 本地日），
    返回收敛总和；**不写库**，仅用于核对 pet_state.intimacy。
    与 apply_intimacy_event 同构（S4 合入校验逻辑一致）。"""
```

- **亲密度兜底以本端事件流重放为准**，不直接采用对端数字（spec §3.3.6 两端各自重放收敛）；对端快照仅用于 streak 校准。
- **streak 只增兜底**：取两端较大者；自然日降级（灰色期清零）由 pet-growth 每日结算状态机负责，对齐不主动降值。
- **触发/接入**：Core 启动定时器每日零点调 `daily_align`（与 S3 清理共用定时器）；收到 `date.sync` 走 `sync.add_handler(DATE_SYNC, ...)` → `handle_peer_snapshot`；重连回调重触发 DEFERRED 场景。峰值 < 100ms（plan §3.2 S5）。

### 4.5 S6：换机/重装恢复（`src/core/backup_restore.py`）

> 状态：✅ 已落地（批次5）。规则见 spec §3.4/§6 决策；密钥归 sync key_store 单一职责，备份**不含会话密钥**。

```python
MAGIC = b"TUANZI-BACKUP-1\n"
BACKUP_TABLES = ("pet_state", "user_items", "anniversaries")   # 养成 + 装扮解锁 + 纪念日

class BackupError(Exception):
    """备份相关错误（口令错误 / 文件损坏 / 非备份文件）。"""

def export_backup(db, passphrase: str, out_path) -> int:
    """加密导出三表快照到 `.tuanzi.bak`，返回导出行数。
    scrypt 口令派生 32B 密钥 → SecretBox 加密 JSON 快照；文件 = MAGIC + salt + blob，
    salt 随机、随文件保存（解密以同一口令 + salt 重派生）。"""

def import_backup(db, passphrase: str, in_path) -> int:
    """口令解密 → 单事务覆盖写三表，返回导入行数。
    先解密成功才写库；错误口令 / 损坏 / 非备份文件 → 抛 BackupError，**不破坏现有库**。"""
```

- **隐私（spec §3.4/§6 决策）**：备份为密文，中继不可解；口令由用户自行保管，遗忘则无法恢复；口令不入 keyring、不存盘。
- **口令派生独立于配对状态**：换机重新配对（新会话密钥）后旧备份仍可解密（口令不变）。
- **恢复后的对齐**：`import_backup` 只负责写库；调用方（恢复流程）导入后触发一次 `daily_align`（S5）——**须传 `mac_key=None`（仅发 date.sync 快照、跳过重放修复）**：恢复后亲密度以备份快照为准（spec §3.3.9），换机重配对后新会话密钥无法验证旧事件签名、旧事件流不可重放，重放核对会将快照亲密度误修为零。以备份快照 + 对方端近期事件为准对齐，不依赖已清理的历史日志。
- **导入不触碰其他表**：events / carries / gift_offers / kv 原样保留。

### 4.6 C1/C2：设置入口与联调脚本

> 状态：✅ 已落地（批次6）。

**C1 设置服务层 + Qt 对话框**（`src/ui/`，UI 无关编排与 PySide6 薄封装分离）：

- `settings_service.py`（供 Qt 对话框与工具脚本复用的 UI 无关编排）：
  - `passphrase_strength(passphrase) -> "weak"|"medium"|"strong"`：长度（≥8）+ 字符类别（数字/大小写/符号）强度提示（UI 展示用）。
  - `export_backup_flow(db, passphrase, out_path) -> int`：口令校验（≥8 位）→ `export_backup` 加密导出三表快照；空口令/弱口令抛 `BackupError`（避免导出不可恢复的废备份）。
  - `import_backup_flow(db, passphrase, in_path, sync=None) -> ImportResult(rows, align)`：解密导入 → 恢复后对齐；`sync` 为空仅导入（align=None）。对齐以 `mac_key=None` 调 `daily_align`（仅发快照、不做重放修复，见 §4.5）。
  - `manual_sync(sync, db, mac_key) -> AlignResult`："立即同步"按钮——日常对齐（含重放核对修复），结果供 UI 展示。
- `settings_dialog.py`：PySide6 薄封装（try import + HAS_QT 占位，无 Qt 环境导入不崩溃）——备份/恢复双 Tab + 立即同步按钮；口令 `QLineEdit.Password`（不入 keyring、不存盘）、强度标签实时更新、导入前"确认覆盖"勾选（plan C1 验收：避免误覆盖）、耗时操作经 `_TaskThread(QThread)` 后台执行 + Qt 信号队列回 UI 线程（plan C1 验收：不阻塞主线程）。

**C2 联调脚本**（`tools/consistency_demo.py`）：一键验证全链路——

配对 → 在线互发（pet.feed 直发经 `record_sent` 落表）→ 去重（重复 event_id 丢弃）→
断网离线入队 → 重连按 seq 升序补发（无重复）→ 双端 events 事件集一致 → 重放收敛 →
`daily_align` 兜底修复（篡改亲密度 → 修复为重放总和）→ `prune`（pet.feed 豁免、普通
超期事件删除）→ 每日对齐（streak 只增兜底）→ 导出备份 → 模拟换机（新 data_dir + 重新
配对）→ 导入恢复（三表与备份快照一致、events 不被触碰）→ 前向同步（换机后跨会话事件
合入，双端亲密度收敛到新值）。

> 联调中确认的两点实现语义：① 恢复后对齐用 `mac_key=None`（快照为准，见 §4.5）；
> ② B 端今日 feed 已 5 次达日上限（防作弊正确拦截第 6 条），前向同步步骤改用
> `gift` reason（发送侧限次、接收侧无每日上限）验证跨会话传播/合入。

**测试**：`tests/core/test_settings_service.py`（7 用例）——口令强度分级 / 弱口令拒绝导出 /
导出导入 roundtrip（`ImportResult.rows`）/ 连接时导入对齐发 date.sync 快照（mac_key=None，
亲密度保持备份值）/ 未连接 DEFERRED（数据已导入）/ 手动同步 ok + 离线 deferred。
联调脚本为可执行验证（非 pytest），`run_demo` 全绿即验收。

---

## 5. 本次实施范围与测试清单

| 交付 | 文件 | 测试 | 状态 |
|------|------|------|:----:|
| G1 | `src/core/db.py`、`src/core/migrations/0001_init.sql` | test_db.py（18 用例） | ✅ 已落地 |
| G2 | `src/core/migrate.py` | test_migrate.py（7 用例） | ✅ 已落地 |
| S1/S2 | `src/core/migrations/0002_events_queue.sql`、`src/sync/queue.py`（改造）、`src/sync/manager.py`（注入 db） | test_queue.py（收编改造 + 迁移用例） | ✅ 已落地（批次2） |
| S3 | `src/core/prune.py` | test_prune.py（7 用例） | ✅ 已落地（批次3） |
| S4 | `src/core/consistency.py` | test_consistency.py（20 用例） | ✅ 已落地（批次3） |
| S5 | `src/core/daily_sync.py` | test_daily_sync.py（12 用例） | ✅ 已落地（批次4） |
| S6 | `src/core/backup_restore.py` | test_backup_restore.py（6 用例） | ✅ 已落地（批次5） |
| C1/C2 | `src/ui/settings_service.py`、`src/ui/settings_dialog.py`、`tools/consistency_demo.py` | test_settings_service.py（7 用例）+ 联调脚本 | ✅ 已落地（批次6） |

> 落地验证：G1/G2 后 tests/core 25 用例全绿；S1/S2 收编后全量 110 passed（sync 40 + core 70，含 4 个迁移用例）；批次3（S3+S4）后全量 **137 passed**；批次4（S5）后全量 **149 passed**；批次5（S6）后全量 **155 passed**（core 115 + sync 40）无回归；批次6（C1/C2）后全量 **164 passed**（core 119 + sync 45，含 D17 的 queue/prune 用例 + 7 个 settings_service 用例）无回归；`sync_demo` / `carry_demo` 联调通过，收编后不再生成 `sync_queue.db`；`consistency_demo` 全链路联调绿（配对→互发→去重→离线补发→重放收敛→兜底修复→prune→对齐→备份→换机恢复→重配对→前向同步）。

**验收前提**：carry-message 编码前落地；`carries` 表由 0001 基线建表，carry_store 基于 `Database` 实现（carry impl D4 修订）。

---

## 6. 与 carry-message 的衔接（修订 carry impl D4）

carry-message impl §3.1 原方案"carries 内联建表于独立 `carry.db`"**废止**，修订为：

| 项 | 原（独立库） | 新（统一库） |
|----|------------|------------|
| 建表 | `carry_store.py` 内 `CREATE TABLE IF NOT EXISTS` | `migrations/0001_init.sql` 基线表 |
| 库 | `data_dir/carry.db` | `data_dir/core.db` |
| `CarryStore` 构造 | `CarryStore(data_dir)` | `CarryStore(db: Database)` |
| 建表时机 | 首次实例化 | Core 启动 `migrate()` |

> carry impl 文档相应修订（§0 storage 说明、§3.1、§11 D4、§12 变更记录）。

---

## 7. 决策记录与待确认项

| 编号 | 主题 | 决策 | 状态 |
|------|------|------|:----:|
| D6 | events 行号主键改名 | 06 笔记 `seq` → `id`（避免与发送序号混淆） | 已确认 |
| D7 | G1 先行范围 | 本次仅 G1/G2；S 系列契约框架预留 | 已定（路线 A） |
| D8 | 收编前双库并存 | `sync_queue.db` 与 `core.db` 并存至 S1/S2 | 已收编（批次2） |
| D9 | carries 表归属 | 统一库基线表（修订 carry D4） | 已定（路线 A） |
| D10 | pairing 表 session_key | 移除该列，密钥归 sync key_store 单一职责 | 已确认 |
| D11 | 旧库数据迁移方式 | **代码层幂等函数** `migrate_legacy_queue`（偏离 impl §4.1 字面"新增迁移脚本"）——SQLite ATTACH 无法条件判断旧库是否存在（缺失会建空文件）、损坏旧库可降级不阻塞启动、数据迁移与 schema 演进解耦且可单测 | 已确认（用户决策） |
| D12 | 收编接入方式 | `SyncQueue` 基于统一 Database（鸭子类型，避免 core→sync 循环依赖），构造改 `SyncQueue(db, my_peer_id)`；`SyncManager` 构造注入 `db`（必填）；demo/tests 经 `init_core` 提供 db | 已定（批次2） |
| D13 | events 表扩展 | 0002 迁移加 `status`/`expires_at` 列 + 新建 `kv` 表（meta 收编）；发送侧 `event_id` 前缀用本端 id（`"{本端}:{seq}"`），接收侧为 `"{对方}:{seq}"` | 已定（批次2） |
| D14 | 历史 seen_events 迁移 type | 迁移时 `type` 置空串（去重记录无需业务类型，`type NOT NULL` 允许空串） | 已定（批次2） |
| D15 | 备份范围与格式 | scrypt 口令派生 + SecretBox 加密三表快照（pet_state/user_items/anniversaries），**不含会话密钥**（密钥归 key_store 单一职责）；文件 = `MAGIC + salt + blob`；口令独立于配对状态（换机重配对后旧备份仍可解密） | 已定（批次5） |
| D16 | 导入安全策略 | `import_backup` 先解密成功才写库（单事务覆盖写三表）；错误口令/损坏/非备份文件抛 `BackupError` 且**不破坏现有库**；不触碰 events/carries 等其他表 | 已定（批次5） |
| D17 | 在线直发落表 | 在线直发成功经 `SyncQueue.record_sent` 落 events 表（`status='sent'`），events 表为**完整双向事件日志**——双端重放同一事件集收敛（spec §3.3.6），daily_align 兜底核对才成立；prune 豁免 `pet.feed`（重放需要完整历史；feed 受每日 5 次上限约束、事件量有界） | 已定（批次6，用户决策） |
| D18 | 发送序号游标键控本端 | 换机重配对后对方 peer_id 变化不导致序号复位（`event_id="{本端}:{seq}"` 撞车）；`SeqManager` 游标键由 `"{对方}:next_seq"` 改 `"{本端}:next_seq"`（序号对本端全局单调），游标缺失时从已有本端事件 max(seq)+1 初始化（覆盖重配对 + 旧库迁移历史）；`SyncManager.stop` 清 `_ready` 支持 stop→start 重启 | 已定（批次6，联调发现） |

---

## 8. 变更记录

| 日期 | 变更内容 | 变更人 |
|------|---------|--------|
| 2026-08-04 | 初始草稿（G1/G2 本次实施；S/C 系列契约框架） | 项目负责人 |
| 2026-08-04 | 确认 D6/D10：events 行号主键改 `id`、pairing 移除 `session_key`；06 笔记同步；状态置为已确认 | 项目负责人 |
| 2026-08-04 | G1/G2 落地：db.py/migrate.py/0001_init.sql + 16 单测全绿，全量 56 passed 无回归；§5 用例数更新 | 项目负责人 |
| 2026-08-04 | db.py 补强：row_factory=Row + query_all/query_one/table_exists + transaction savepoint 嵌套（v2）+ init_core 启动入口 + busy_timeout + close 幂等；test_db.py 增至 18 用例，全量 65 passed 无回归 | 项目负责人 |
| 2026-08-04 | 批次2（S1/S2）实施前确认 D11：旧库数据迁移走代码层幂等函数；填充 §4.1 契约（0002 迁移 schema + SyncQueue 改造 + migrate_legacy_queue + SyncManager 注入 db + 测试清单）；§1.3 双库状态更新 | 项目负责人 |
| 2026-08-04 | 批次2（S1/S2）落地：0002 迁移脚本（events 加 status/expires_at/seq + kv 表）；SyncQueue 收编 core.db.events（鸭子类型避免循环依赖）；migrate_legacy_queue 代码层幂等迁移；SyncManager 注入 db 并在启动时迁移旧库；sync_demo/carry_demo/test_queue 同步改造。全量 110 passed（sync 40 + core 70，含 4 个迁移用例），两 demo 联调绿；§4.1/§5/§7 同步 | 项目负责人 |
| 2026-08-04 | 批次3（S3）落地：prune.py 事件日志清理（子查询限行分批 DELETE，SQLite 默认不支持 DELETE...LIMIT）；test_prune.py 7 用例（保留期边界固定时钟防跨秒抖动）；全量 117 passed 无回归；§4.2/§5 同步 | 项目负责人 |
| 2026-08-04 | 批次3（S4）落地：consistency.py 冲突合并与亲密度重放——`pet.feed` payload 线格式定稿（delta/reason/ts/sig，依据 pet-growth plan 已确认决策 + 04 笔记）；`apply_intimacy_event` 验签/每日上限/熔断（kv 持久化）/累加 pet_state.intimacy；`merge_lww` LWW+once 通用合并；`sign/verify_intimacy_signature` 两端同源。test_consistency.py 20 用例全绿，全量 137 passed 无回归；§4.3/§5 同步 | 项目负责人 |
| 2026-08-04 | 批次4（S5）落地：daily_sync.py 每日兜底对齐——date.sync payload 定稿 {streak/intimacy/ts}；daily_align（未连接 DEFERRED 推迟/熔断 MANUAL_NEEDED/重放核对修复 REPAIRED）；handle_peer_snapshot（streak 只增兜底）；replay_intimacy_total（与 apply_intimacy_event 同构，纯计算不写库）。AlignResult 增 DEFERRED 枚举（断网推迟语义）。test_daily_sync.py 12 用例全绿，全量 149 passed 无回归；§4.4/§5 同步 | 项目负责人 |
| 2026-08-04 | 批次5（S6）落地：backup_restore.py 换机/重装恢复——scrypt 口令派生 + SecretBox 加密三表快照（pet_state/user_items/anniversaries，不含会话密钥）；import 先解密成功才写库（单事务覆盖写，错误口令/损坏/非备份文件明确报错且不破坏现有库）；口令独立于配对状态（换机重配对后旧备份仍可解密）。test_backup_restore.py 6 用例全绿，全量 155 passed 无回归；§4.5/§5/§7 同步 | 项目负责人 |
| 2026-08-04 | 批次6（C1/C2）落地：settings_service（口令强度/导出导入流程/手动同步，恢复后对齐 `mac_key=None` 仅快照——spec §3.3.9 备份快照为准）；settings_dialog（PySide6 薄封装：备份/恢复 Tab + 确认覆盖 + 后台线程）；consistency_demo 全链路联调脚本。先修 D17 缺口（在线直发落表 + prune 豁免 pet.feed），再经联调暴露并修复 D18（换机重配对序号复位 → 游标键控本端 + stop 清 `_ready` 支持重启）。test_settings_service.py 7 用例全绿，全量 **164 passed**（core 119 + sync 45）无回归；consistency_demo 全链路联调绿；§4.1/§4.2/§4.5/§4.6/§5/§7/§8 同步 | 项目负责人 |
