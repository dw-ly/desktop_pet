# 带话功能 实现规格（impl）

> 关联 Spec：[[7-AI学习/11-AI桌宠情侣互联/spec/carry-message/spec.md|spec/carry-message]]
> 关联 Plan：[[7-AI学习/11-AI桌宠情侣互联/spec/carry-message/plan.md|plan/carry-message]]
> 创建日期：2026-08-04
> 状态：已确认

## 目的

将 plan 的任务级 TODO（G1/S1-S4/C1-C3）细化为**可直接编码的契约**：接口签名、数据结构字段、线格式 JSON、算法伪代码、断言级测试用例。编码时不再做设计决策；若实现中必须偏离本文档，先更新本文档再编码（文档即契约）。

本模块是**第一个业务模块**，在已落地且联调通过的同步层（impl/sync-security）之上验证其对外 API（`add_handler` / `send` / `Message`）。文档末尾的「决策记录」收录了三处对上游文档的细化/偏差（D1/D2/D3），均已确认；06 笔记 `carries` 表已按 §3.1 落地 schema 同步更新。

---

## 0. 目录结构与模块归属

```
src/core/
├── carry_intent.py    ← plan S1  意图检测 + 正文抽取（纯规则，无外部依赖）
├── carry_store.py     ← plan G1  CarryRecord/CarryStatus + carries 表 CRUD + 状态流转校验
├── carry.py           ← plan S2+S4 发送流程编排、ack/revoke 事件处理、配置、定时扫描
├── carry_receive.py   ← plan S3  接收编排（勿扰判断、播报触发、自动 ack 扫描）
└── ...
src/ui/
├── carry_dialog.py    ← plan C1  发送确认弹窗、状态展示、撤回按钮
└── notify.py          ← plan C2  接收播报弹窗（"TA 说：…"）、稍后看列表
tools/
└── carry_demo.py      ← plan C3  端到端联调脚本
tests/
└── core/
    ├── test_carry_intent.py
    ├── test_carry_store.py
    ├── test_carry_service.py
    └── test_carry_receive.py
```

- **storage**：`carries` 表由 data-consistency 统一库 `core.db` 的基线表提供（`migrations/0001_init.sql`，见 data-consistency impl §2.2）；`CarryStore` 基于统一 `Database` 实现，**不建独立库**（修订见 D4）。
- **sync 层复用**：`msg.carry` / `carry.ack` / `carry.revoke` 三个事件类型已在 events.py 注册（业务类型，分发到 `add_handler`），本模块仅注册 handler 消费。

---

## 1. 全局约定

### 1.1 常量（本模块统一集中定义于 `carry.py`）

| 常量 | 值 | 说明 |
|------|-----|------|
| `REVOKE_WINDOW_SECONDS` | `120` | 撤回窗口（发送后 2 分钟内，spec 决议） |
| `AUTO_ACK_SECONDS` | `180` | 接收端自动确认（3 分钟，延后于撤回窗口，spec 决议） |
| `DEFAULT_EXPIRES_SECONDS` | `86400` | 带话默认过期（24 小时，spec 决议） |
| `SCAN_INTERVAL_SECONDS` | `30` | 过期扫描 / 自动 ack 扫描周期 |
| `TRIGGERS` | 见 §2.1 | 意图触发词表（句首匹配，长词优先） |

### 1.2 事件 payload 键名规范

线格式（密文内明文）统一 **camelCase**，与已落地 `sync_demo` 的 `carryId`/`expireAt` 一致；DB 列名统一 **snake_case**。

**msg.carry**（A→B）：

```json
{
  "carryId": "<uuid4 hex 32 字符>",
  "text": "今晚早点睡",
  "mood": "caring",
  "expireAt": 1764864000
}
```

| 键 | 类型 | 必填 | 说明 |
|----|------|:----:|------|
| `carryId` | string | 是 | `uuid.uuid4().hex`；两端据此关联 ack/revoke |
| `text` | string | 是 | 剥离触发词后的正文；空正文由发送端兜底为原句 |
| `mood` | string \| null | 否 | 可选情绪标签，复用主项目 8 label（happy/excited/calm/neutral/sad/angry/confused/sleepy）；无本地情绪时传 `null` |
| `expireAt` | int | 是 | 业务过期时间 Unix 秒；默认 `now + DEFAULT_EXPIRES_SECONDS` |

**carry.ack**（B→A）：

```json
{
  "carryId": "<uuid4 hex>",
  "ackedAt": 1764864000,
  "auto": false
}
```

| 键 | 类型 | 必填 | 说明 |
|----|------|:----:|------|
| `carryId` | string | 是 | 被确认带话的 id |
| `ackedAt` | int | 是 | 确认时间 Unix 秒（默认 `msg.ts`） |
| `auto` | bool | 是 | `true`=3 分钟自动确认，`false`=用户手动点击"知道了" |

**carry.revoke**（A→B）：

```json
{
  "carryId": "<uuid4 hex>",
  "sentAt": 1764864000
}
```

| 键 | 类型 | 必填 | 说明 |
|----|------|:----:|------|
| `carryId` | string | 是 | 被撤回带话的 id |
| `sentAt` | int | 是 | 发送方发送时间 Unix 秒（撤回窗口基准） |

> `expireAt` 同时作为 `sync.send(..., expires_at=expireAt)` 的传输层过期参数：接收方同步层在密文过期后丢弃（manager `_on_envelope`），发送方离线队列 flush 时对过期项置 failed——两层过期语义一致。

### 1.3 异常体系

复用 `sync/errors.py`（`SyncError` 基类）。本模块新增：

```python
class CarryError(Exception): ...     # 业务基类（本模块内）
class InvalidStateTransition(CarryError): ...  # 非法状态流转（store 层，按约定返回 False 时通常不抛）
```

> 存储层优先用「返回 False / None」表达可预期的拒绝（幂等友好，调用方可忽略）；仅编程错误（如缺字段）抛异常。

### 1.4 线程与并发模型

- `CarryService` 的方法会在**多个线程**被调用：UI 线程发起 `propose`/`confirm_send`/`revoke`；sync 线程经 `add_handler` 触发 `on_ack`/`on_revoke`；定时器线程触发扫描。
- **`CarryStore` 内置 `threading.Lock` 串行化所有 DB 操作**；sqlite 连接以 `check_same_thread=False` 建立（配合锁使用，禁止无锁跨线程）。
- 对外只暴露**线程安全回调**：`on_confirm`（请求 UI 弹确认框）、`on_status`（记录状态变更通知 UI）。UI 侧自行 marshal 到 UI 线程（Qt 信号队列，同 sync-security impl §1.4 约定）。
- 定时扫描：`start_timer()`/`stop_timer()` 由调用方（Core 启动/退出）驱动，内部 `threading.Timer` 每 `SCAN_INTERVAL_SECONDS` 执行 `scan_expired()` + `scan_auto_ack()`。

---

## 2. carry_intent.py（plan S1）

### 2.1 意图检测与正文抽取

```python
@dataclass(frozen=True)
class IntentResult:
    is_carry: bool
    text: str                 # 规范化正文（小写、压缩空白、剥离前导称呼与尾部语气词）；非带话为空串
    trigger: str | None       # 命中的触发词；未命中 None

# 长词在前（"帮我告诉" 优先于 "告诉"），否则短词会截断长词。
# "跟 TA 说"（带空格）与 "跟TA说"（无空格）经 _norm 归一为 "跟 ta 说"/"跟ta说"，
# 故 "跟/对 + ta" 类触发词同时收录带空格与不带空格两个变体。
TRIGGERS: tuple[str, ...] = (
    "帮我告诉", "帮我转告", "帮我传话", "帮我带句话",
    "跟 ta 说", "跟ta说", "对 ta 说", "对ta说", "跟他说", "跟她说",
    "告诉", "转告", "传话",
)

_TRAILERS = ("吧", "呀", "哦", "呢", "哟", "啊")

def _norm(s: str) -> str:
    """小写 + 压缩空白 + 去首尾空白。TA/ta 大小写经 lower 统一，不必特判。"""
    return re.sub(r"\s+", " ", s.strip().lower())

def _strip_leading_ta(s: str) -> str:
    """剥离正文开头的对端称呼 "ta"（"告诉TA今晚早点睡"→"今晚早点睡"）。

    仅精确匹配前导 2 字符 "ta"；"ta们" 等特殊形态罕见，不特判。
    """
    return s[2:].strip() if s.startswith("ta") else s

def detect_carry(text: str) -> IntentResult:
    t = _norm(text)
    if not t:
        return IntentResult(False, "", None)
    for trig in TRIGGERS:
        if t.startswith(trig):
            body = _strip_leading_ta(t[len(trig):].strip())
            while body and body[-1] in _TRAILERS:
                body = body[:-1].strip()
            # 空正文兜底：返回触发词本身（避免发送空内容）
            return IntentResult(True, body or trig, trig)
    return IntentResult(False, "", None)
```

### 2.2 行为决策（决策记录 D1）

| 点 | 决策 | 理由 |
|----|------|------|
| 匹配位置 | **句首触发词**（`startswith`），句中触发词不命中 | 03 笔记伪代码用全句 `contains`，但"想告诉TA这个秘密"会剥错正文（残留"想你…"）。句首规则剥离结果确定、可测；未命中按普通对话（spec 已定） |
| 正文规范化 | 返回小写 + 压缩空白的正文 | 中文影响极小；保证可测性与确定性 |
| 前导称呼剥离 | 剥离触发词后，正文开头若为 "ta"（对端称呼）一并剥离 | "告诉TA今晚早点睡"→"今晚早点睡"；正文中部 "ta" 保留（"跟TA说我想TA了"→"我想ta了"） |
| 触发词变体 | "跟/对 + ta" 类同时收录带空格与不带空格两变体 | `_norm` 压缩空白后，"跟TA说"/"跟 TA 说" 归一为 "跟ta说"/"跟 ta 说"，两种输入习惯都要命中 |
| 空正文 | 兜底返回触发词本身 | 避免发送空 `text`；"告诉吧"→"告诉" |

> 对齐动作：03 笔记 §一 的 `detect_carry` 伪代码为简化示意，本 impl 为落地契约（句首规则）；03 笔记保持产品级描述不改。

### 2.3 断言级测试（test_carry_intent.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 前缀命中-短触发词 | `"告诉TA今晚早点睡"` | `is_carry=True`, `text="今晚早点睡"` |
| 前缀命中-长触发词优先 | `"帮我告诉TA我加班"` | `is_carry=True`, `trigger="帮我告诉"`, `text="我加班"` |
| 跟TA说-大小写空格 | `"跟TA说我想TA了"` | `is_carry=True`, `text="我想ta了"` |
| 对TA说 | `"对ta说晚安吧"` | `is_carry=True`, `text="晚安"`（剥"吧"） |
| 尾部语气词剥离 | `"告诉TA快睡觉呀"` | `is_carry=True`, `text="快睡觉"` |
| 未命中-普通闲聊 | `"今天天气不错"` | `is_carry=False` |
| 句中触发词不命中 | `"我想告诉TA这个秘密"` | `is_carry=False`（句首规则） |
| 空输入 | `""` / `"   "` | `is_carry=False` |
| 空正文兜底 | `"告诉"` | `is_carry=True`, `text="告诉"`（原句） |
| 纯触发词含语气词 | `"告诉吧"` | `is_carry=True`, `text="告诉"`（兜底原句） |

---

## 3. carry_store.py（plan G1）

### 3.1 carries 表（`core.db` 基线表，data-consistency G1）

`carries` 表由 data-consistency `migrations/0001_init.sql` 建表（`data_dir/core.db`），**本模块不建表**；落地基线即：

```sql
-- 0001_init.sql 中的 carries 表（落地基线，与 06 笔记一致）
CREATE TABLE carries (
  id         TEXT PRIMARY KEY,    -- carryId（uuid4 hex）
  direction  TEXT NOT NULL,       -- 'out' 发送方记录 / 'in' 接收方记录
  text       TEXT NOT NULL,       -- 带话正文
  status     TEXT NOT NULL,       -- draft/sent/delivered/revoked/failed
  expires_at INTEGER NOT NULL,    -- 业务过期时间 Unix 秒
  created_at INTEGER NOT NULL,    -- 本端落库时间
  sent_at    INTEGER,             -- out: 发送方发送时间（撤回窗口基准）
  read_at    INTEGER,             -- 送达确认时间（ack）
  revoked_at INTEGER              -- 撤回时间
);
CREATE INDEX idx_carries_out_status ON carries(direction, status);
```

> 建表与迁移由 data-consistency G2 在 Core 启动时统一执行；`CarryStore` 依赖已迁移完成的 `core.db`。

### 3.2 状态机与合法流转

```text
draft ──确认发送──► sent ──收 ack──► delivered
                       │
                       ├──2 分钟窗口内撤回──► revoked
                       └──超 expires_at 未送达──► failed
```

| from | to | 触发方 | 触发条件 |
|------|-----|--------|---------|
| draft | sent | 本端确认 | `confirm_send` |
| sent | delivered | 收 ack | 对端 `carry.ack` |
| sent | revoked | 本端撤回 | `revoke`（窗口内） |
| sent | failed | 本端扫描 | `expires_at < now` |
| sent | delivered | 收 ack 回滚 | 见决策记录 D3（本端已 revoked 后 ack 后到） |

非法流转一律拒绝：`delivered→revoked`、`revoked→delivered`（除非回滚规则）、`failed→*`、`draft→delivered` 等。

### 3.3 接口契约

```python
class CarryStatus(str, Enum):
    DRAFT = "draft"; SENT = "sent"; DELIVERED = "delivered"
    REVOKED = "revoked"; FAILED = "failed"

@dataclass
class CarryRecord:
    id: str
    direction: str               # 'out' | 'in'
    text: str
    status: CarryStatus
    expires_at: int
    created_at: int
    sent_at: int | None = None
    read_at: int | None = None
    revoked_at: int | None = None

class CarryStore:
    def __init__(self, db: Database): ...      # 使用统一库 core.db（表已由 0001_init.sql 建好）

    # 写入（自动分配 id/时间戳）
    def create_outgoing(self, text: str, expires_at: int) -> CarryRecord: ...  # status=DRAFT, direction='out'
    def insert_incoming(self, carry_id: str, text: str, expires_at: int) -> CarryRecord | None: ...
        # status=SENT, direction='in'；carry_id 已存在（重复投递）→ 返回 None（幂等，不抛）

    # 读取
    def get(self, carry_id: str) -> CarryRecord | None: ...
    def unacked_incoming(self, before_ts: int) -> list[CarryRecord]: ...
        # WHERE direction='in' AND status='sent' AND created_at <= before_ts
    def pending_expired(self, now: int) -> list[CarryRecord]: ...
        # WHERE direction='out' AND status='sent' AND expires_at < now

    # 状态流转（非法返回 False，不抛）
    def mark_sent(self, carry_id: str, sent_at: int) -> bool: ...       # draft→sent
    def mark_delivered(self, carry_id: str, read_at: int) -> bool: ...  # sent→delivered（及 D3 回滚）
    def mark_revoked(self, carry_id: str, revoked_at: int) -> bool: ... # sent→revoked
    def mark_failed(self, carry_id: str) -> bool: ...                   # sent→failed

    # 内部：_set_status(carry_id, from_statuses, to, **ts) 统一校验并发更新，全程持锁
```

### 3.4 断言级测试（test_carry_store.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 建库建表 | 新 data_dir | `carry.db` 存在、`carries` 表存在、索引存在 |
| 新建 outgoing | `create_outgoing("晚安", t)` | status=draft, direction=out, id 非空 |
| 插入 incoming 幂等 | 同 id 插两次 | 第一次返回记录、第二次返回 None，不覆盖 |
| 合法流转链 | draft→sent→delivered | 各步返回 True，末态 delivered |
| 非法流转-已读撤回 | delivered→revoked | 返回 False，状态不变 |
| 非法流转-终态 | failed→sent | 返回 False |
| 过期扫描 | 两条 out+sent，一条 expires_at 已过 | `pending_expired` 仅返回过期那条 |
| 未确认 incoming 扫描 | 3 条 in，2 条 created_at 超 cutoff | `unacked_incoming` 返回 2 条 |
| 并发写 | 2 线程各 50 次 mark | 无 `sqlite3.ProgrammingError`，最终状态正确 |
| 半插入不残留 | `insert_incoming` 后事务回滚模拟 | 表中无半条记录 |

---

## 4. carry.py（plan S2 + S4）

### 4.1 CarryConfig

```python
@dataclass(frozen=True)
class CarryConfig:
    revoke_window_seconds: int = REVOKE_WINDOW_SECONDS   # 120
    auto_ack_seconds: int = AUTO_ACK_SECONDS             # 180
    default_expires_seconds: int = DEFAULT_EXPIRES_SECONDS  # 86400
    scan_interval_seconds: int = SCAN_INTERVAL_SECONDS   # 30
    dnd_start: str = ""        # "22:00" 或空（不启用勿扰）
    dnd_end: str = ""          # "08:00"
```

`is_dnd(now) -> bool`：解析 `dnd_start`/`dnd_end` 为当日分钟数；跨午夜区间（start > end）按"今日 22:00 → 次日 08:00"判断。两者皆空恒返回 False。

### 4.2 CarryService 接口

```python
class CarryService:
    def __init__(self, store: CarryStore, sync: SyncManager, cfg: CarryConfig, *,
                 on_confirm: Callable[[CarryRecord], None],       # UI：弹发送确认框
                 on_status: Callable[[CarryRecord], None],        # UI：状态变更通知
                 on_hide: Callable[[str], None] | None = None,   # 接收端 revoke 生效时即时隐藏（对应 notify.hide_carry）
                 get_mood: Callable[[], str | None] | None = None): ...  # 本地情绪读取；无情绪返回 None（主项目接入后注入）

    # 发送侧（UI 线程）
    def propose(self, text: str) -> bool: ...
        # detect_carry；命中 → create_outgoing(正文, now+default_expires) → on_confirm(record) → True
        # 未命中 → False（UI 走普通对话，不进入带话流程）
    def confirm_send(self, carry_id: str) -> None: ...
        # 仅 DRAFT 有效；mark_sent(now) → sync.send(MSG_CARRY, payload, expires_at=expires_at)
        # payload.mood = get_mood()（无本地情绪时 null）
    def revoke(self, carry_id: str) -> None: ...
        # 仅 out + SENT 有效；now-sent_at <= revoke_window 才允许
        # mark_revoked(now) → sync.send(CARRY_REVOKE, {carryId, sentAt})
    def send_ack(self, carry_id: str, *, auto: bool) -> None: ...  # 接收端调用（手动/自动）

    # 接收侧（sync 线程 add_handler 回调）
    def on_ack(self, msg: Message) -> None: ...
    def on_revoke(self, msg: Message) -> None: ...

    # 定时器（调用方驱动）
    def start_timer(self) -> None: ...
    def stop_timer(self) -> None: ...
    def scan_expired(self, now: int) -> None: ...   # 同步实现（可在专用线程）；对每条 mark_failed + on_status
```

> `propose` 的返回语义：`True` 表示已进入带话确认流程；`False` 表示未命中触发词。UI 依据返回值决定是否继续普通对话。

### 4.3 发送流程伪代码

```text
propose(text):
  r = detect_carry(text)
  if not r.is_carry: return False
  rec = store.create_outgoing(r.text, now + cfg.default_expires_seconds)
  on_confirm(rec)                      # UI 弹"要把这句话带给 TA 吗？"
  return True

confirm_send(id):
  rec = store.get(id)
  if rec is None or rec.status != DRAFT: return
  if store.mark_sent(id, now):
      sync.send(EventType.MSG_CARRY, {
          "carryId": rec.id, "text": rec.text,
          "mood": current_mood() or None,      # 读本地情绪（缺失传 None）
          "expireAt": rec.expires_at,
      }, expires_at=rec.expires_at)
      on_status(rec 更新后)
```

### 4.4 ack / revoke 处理（含竞态收敛）

```text
on_ack(msg):
  rid = msg.payload["carryId"]
  rec = store.get(rid)
  if rec is None or rec.direction != 'out': return
  read_at = msg.payload.get("ackedAt", msg.ts)
  if store.mark_delivered(rid, read_at):        # sent→delivered 正常路径
      on_status(...)
  elif rec.status == REVOKED:
      # D3：撤回后 ack 后到 → 对方已确认，撤回失败，回滚为 delivered（已读优先）
      store.rollback_revoked_to_delivered(rid, read_at)
      on_status(...)

on_revoke(msg):
  rid = msg.payload["carryId"]
  rec = store.get(rid)
  if rec is None or rec.direction != 'in': return
  if rec.read_at is not None: return            # 已手动确认，撤回无效（保持 delivered）
  # D2：不校验 now-sent_at 窗口——发送方 revoke() 已保证撤回发生在窗口内；
  #     接收端离线导致的迟到不改变"对方尚未确认"这一事实，仅以 read_at 为准保证两端收敛。
  store.mark_revoked(rid, msg.ts)
  if on_hide: on_hide(rid)                      # 即时隐藏 + 清理"稍后看"（对应 notify.hide_carry）
  on_status(store.get(rid))
```

### 4.5 决策记录 D2 / D3（对 plan S4 的细化）

| 决策 | 内容 | 理由 |
|------|------|------|
| **D2 接收端撤回校验** | 接收端接受 revoke 仅要求"未手动确认（read_at 为空）"，**不做 `now - sentAt ≤ 120` 校验** | plan 字面要求"按携带的 sent_at 校验"，但接收端离线 >2 分钟会迟收 revoke，拒绝将导致两端不一致（A 已 revoked、B 仍显示）。发送方 `revoke()` 本端已校验窗口，revoke 事件必为窗口内产物；read_at 为空已隐含"撤回发生时对方未确认"。**偏差点，需确认** |
| **D3 ack 后到回滚** | 发送端 revoke 后若收到 ack（对方已手动确认），回滚 `revoked → delivered` | 竞态：B 确认的同时 A 撤回。spec"对方已手动确认后不可再撤回"应优先，故已读方胜。回滚使两端都收敛为 delivered |

### 4.6 断言级测试（test_carry_service.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 命中进入确认 | `propose("告诉TA晚安")` | 返回 True、store 有 draft 记录、on_confirm 回调 1 次 |
| 未命中不建记录 | `propose("今天好热")` | 返回 False、store 无新增、on_confirm 0 次 |
| 确认后才发送 | `confirm_send(未确认记录)` | 无 sync.send 调用 |
| 确认发送 | `propose`→`confirm_send` | status=sent、sync.send 收到 MSG_CARRY 且 payload 含 carryId/text/expireAt |
| 撤回窗口内 | sent 后 <120s `revoke` | status=revoked、发出 CARRY_REVOKE 且 payload.sentAt=原 sent_at |
| 撤回超窗拒绝 | sent 后 >120s `revoke` | 状态不变、无 CARRY_REVOKE 发出 |
| 撤回已确认拒绝 | delivered 后 `revoke` | 状态不变 |
| ack 幂等 | 同一 carryId 两次 `on_ack` | delivered 一次成功、二次无变化、无重复事件 |
| ack 后到回滚 | sent→revoke→on_ack | 最终 delivered（D3） |
| revoke 窗口外忽略 | 收 revoke 时已 delivered（read_at 非空） | 状态不变（已读优先） |
| revoke 收端生效 | 收 revoke 时 in+sent 且未确认 | 状态 revoked、notify.hide 被调 |
| 过期扫描 | sent 记录 expires_at 已过 | status=failed、on_status 通知 |
| 过期边界未到 | expires_at 恰等于 now | 不置 failed |
| 勿扰时段判断 | cfg dnd 22:00-08:00，now=23:00 | `is_dnd` 返回 True；08:00 与 21:59 返回 False |

---

## 5. carry_receive.py（plan S3）

### 5.1 接口契约

```python
class CarryReceiver:
    def __init__(self, store: CarryStore, service: CarryService, notify, *,
                 is_dnd: Callable[[], bool], on_ack_sent: Callable[[CarryRecord], None] | None = None): ...
    # 注册到 sync：add_handler(MSG_CARRY, self.on_msg)
    def on_msg(self, msg: Message) -> None: ...
    def scan_auto_ack(self) -> None: ...   # 由 service 定时器驱动
```

> `scan_auto_ack` 经 `service.config.auto_ack_seconds` 读取自动确认时长（`CarryService` 暴露只读 `config` 属性）。`is_dnd` 为无参回调，Core 接线时传 `lambda: cfg.is_dnd(int(time.time()))`。

### 5.2 接收编排伪代码

```text
on_msg(msg):
  rid = msg.payload["carryId"]; text = msg.payload["text"]; exp = msg.payload["expireAt"]
  rec = store.insert_incoming(rid, text, exp)     # 幂等：重复投递返回 None
  if rec is None: return                          # 已处理过，不重复播报（spec：仅播报一次）
  if is_dnd():
      return                                      # 勿扰：静默入库，不播报（"稍后看"由 UI 查 store 呈现）
  notify.show_carry(rec, on_ack=lambda: service.send_ack(rid, auto=False))
  # 3 分钟未手动确认 → scan_auto_ack 兜底发 auto ack

scan_auto_ack():
  cutoff = now - cfg.auto_ack_seconds
  for rec in store.unacked_incoming(cutoff):
      service.send_ack(rec.id, auto=True)
```

- **"稍后看"列表**：不建独立表，由 UI 查询 `store` 中 `direction='in' AND status='sent' AND read_at IS NULL` 呈现；revoke 后该条 `status=revoked` 自然移出列表（与 spec"同步清理稍后看"一致）。
- **TTS/动画降级**：`notify.show_carry` 内部若主项目 TTS/动画未就绪，降级为纯文字弹窗，不影响送达与 ack 流程（plan §6 风险缓解）。

### 5.3 断言级测试（test_carry_receive.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 正常播报 | 收 msg.carry | 入库 status=sent、`show_carry` 1 次 |
| 重复投递幂等 | 同 carryId 两次 on_msg | 仅第一次播报、库中单条 |
| 勿扰静默 | `is_dnd` 返回 True | 不入播报、记录 status=sent（稍后看可查） |
| 自动 ack | created_at 超 180s 未确认 | 发出 CARRY_ACK(auto=true)、status=delivered |
| 手动 ack 优先 | 已手动确认 | 自动扫描跳过该条 |
| revoke 后移出稍后看 | 收 revoke 后查列表 | 状态 revoked、不再出现在"未确认"查询 |

---

## 6. UI 契约（plan C1 / C2）

> UI 层为 Qt 实现，本 impl 仅定义**接口与回调契约**，不写 UI 内部代码。所有回调经 Qt 信号队列 marshal。

### 6.1 carry_dialog.py（C1）

| 元素 | 行为 |
|------|------|
| 发送确认弹窗 | `show_confirm(text) -> (确认, 取消)`；确认 → `service.confirm_send(carry_id)` |
| 状态展示气泡 | `update_status(record)`：draft"待确认"/sent"已交给团子"/delivered"已转达 ✓"/revoked"已撤回"/failed"发送失败" |
| 撤回按钮 | sent 状态显示，120 秒倒计时；`now - sent_at > 120` 或 status≠sent 或已确认 → 禁用/隐藏；点击 → `service.revoke(carry_id)` |

### 6.2 notify.py（C2）

| 元素 | 行为 |
|------|------|
| 播报弹窗 | "TA 说：{text}" + "知道了"按钮 + TTS 朗读 + 开口动画（`mood` 匹配表情；降级为纯文字） |
| "知道了" | `service.send_ack(carry_id, auto=False)` → 隐藏弹窗 |
| 稍后看列表 | 入口展示 store 未确认 `in` 记录；点击回看可补点"知道了" |
| 即时隐藏 | `hide_carry(carry_id)`：revoke 收到时移除弹窗与稍后看条目 |

### 6.3 回调接线

```text
Core 启动:
  store = CarryStore(data_dir)
  svc = CarryService(store, sync, cfg, on_confirm=ui.show_confirm, on_status=ui.update_status)
  recv = CarryReceiver(store, svc, notify, is_dnd=cfg.is_dnd)
  sync.add_handler(MSG_CARRY, recv.on_msg)
  sync.add_handler(CARRY_ACK, svc.on_ack)
  sync.add_handler(CARRY_REVOKE, svc.on_revoke)
  svc.start_timer()
```

---

## 7. tools/carry_demo.py（plan C3）

```text
cd code
.venv\Scripts\python tools\carry_demo.py            # 常规（INFO）
.venv\Scripts\python tools\carry_demo.py --debug   # 含帧级日志
```

> 落地状态：✅ 已落地（批次5），运行全流程绿——配对 → 发送/播报/确认 → delivered → 撤回（B 即时隐藏）→ 过期 failed，全程断言通过。

结构复用 `sync_demo`（临时 data_dir 双实例 + discovery 本地注册表 + 配对），扩展带话场景：

1. 双端配对 → 等待 connected
2. A `propose("告诉TA今晚早点睡")` → 断言命中 → 自动 `confirm_send`
3. B 收 `msg.carry` → `show_carry` 回调记录 → 模拟点击"知道了" → 回 ack
4. A 收 ack → 断言状态 `delivered`、on_status 文案"已转达"
5. **撤回分支**：A 再发一条 → 立即 `revoke` → 断言 B 端 `hide_carry` 被调、状态 `revoked`、两端均 revoked
6. **过期分支**：构造 `expires_at = now - 1` 的 sent 记录 → 触发 `scan_expired` → 断言 `failed`
7. 全程断言：无重复播报、两端状态收敛一致、cipher 为密文

---

## 8. 端到端时序

### 8.1 正常送达

```text
A: propose("告诉TA今晚早点睡") → detect_carry 命中 → draft → on_confirm 弹窗
A: 用户确认 → confirm_send → sent → msg.carry{carryId,text,mood:null,expireAt} ──密文──► B
B: 解密去重 → on_msg → insert_incoming(sent) → 非勿扰 → show_carry("TA 说：今晚早点睡")
B: 点"知道了" → send_ack(auto=false) → carry.ack ──密文──► A
A: on_ack → delivered → UI"已转达 ✓"
```

### 8.2 撤回（窗口内、已读优先）

```text
A 撤回（<2min，未确认）: revoke → 本地 revoked → carry.revoke{sentAt} ──► B
B: read_at 为空 → revoked → hide_carry

A 撤回与 B 确认竞态: B 先确认并发 ack；A revoke → 本地 revoked；A 后收 ack → D3 回滚 delivered
B 已确认 → 拒绝 revoke → 保持 delivered → 两端一致
```

### 8.3 过期失败

```text
A sent 记录 expires_at 超过 → 定时 scan_expired → failed → UI"发送失败"提示
```

---

## 9. 测试清单汇总

> 文件名对应 `tests/core/test_*.py`。单测合计约 **36 用例**；端到端由 §7 `carry_demo` 覆盖（复用 sync_security 的 40 单测 + demo 作为回归基线）。

| 文件 | 用例数 | 覆盖 | 状态 |
|------|:------:|------|:----:|
| test_carry_intent.py | 10 | §2.3 触发词/边界/剥离 | ✅ 已落地（批次2 S1） |
| test_carry_store.py | 10 | §3.4 CRUD/状态流转/扫描/并发 | ✅ 已落地（批次1 G1） |
| test_carry_service.py | 14 | §4.6 发送/撤回/ack/过期/勿扰 | ✅ 已落地（批次3 S2/S4） |
| test_carry_receive.py | 6 | §5.3 播报/幂等/勿扰/自动 ack | ✅ 已落地（批次4 S3） |

> 落地验证：核心 4 文件共 40 用例全绿；全量 106 passed（sync 40 + core 66 无回归）；C3 `carry_demo` 双端联调全流程绿（配对 → 送达 → 撤回即时隐藏 → 过期 failed）。

---

## 10. 集成验证（与 plan §5.2 对齐）

- **前置**：sync-security 40 单测 + sync_demo 全流程保持绿（本模块不得引入回归）。
- 双端配对在线：A 发带话 → B 弹窗 + 朗读 → B 点"知道了" → A"已转达"。
- 撤回：2 分钟内撤回 → B 端即时隐藏；B 已确认后撤回被忽略。
- 勿扰：B 处于勿扰时段 → 静默入"稍后看"，退出勿扰后列表可见。
- 离线补发：B 离线时 A 发带话 → B 重连后收到并自动 ack → A delivered。
- 过期：超 24h 未送达 → 标记 failed 并提示。
- 抓包：局域网流量仅见 Envelope 三字段，无明文。

---

## 11. 决策记录与待确认项

| 编号 | 主题 | 决策 | 状态 |
|------|------|------|:----:|
| D1 | 意图匹配位置 | 句首触发词（偏离 03 笔记 contains 简化伪代码） | 已确认 |
| D2 | 接收端 revoke 校验 | 仅校验未手动确认，不校验时间窗（偏离 plan S4 字面） | 已确认 |
| D3 | ack 后到竞态 | 发送端 revoked 后收 ack → 回滚 delivered（已读优先） | 已确认 |
| D4 | 存储策略 | `carries` 表为 data-consistency `core.db` 基线表；`CarryStore` 基于统一 `Database`（修订自"内联建表于独立 carry.db"） | 已确认（路线 A） |
| D5 | 自动 ack 实现 | 定时扫描而非每记录定时器（可测、无泄漏） | 与 plan 一致 |

**确认动作**：D1/D2/D3 已确认；06 笔记 `carries` 表 schema 已同步更新（补 `created_at`/`sent_at`/`revoked_at`、status 统一 `draft/sent/delivered/revoked/failed`）。本文档定稿，进入编码。

---

## 12. 变更记录

| 日期 | 变更内容 | 变更人 |
|------|---------|--------|
| 2026-08-04 | 初始草稿（待确认 D1/D2/D3） | 项目负责人 |
| 2026-08-04 | 确认 D1/D2/D3 决策；06 笔记 `carries` 表 schema 同步；状态置为已确认 | 项目负责人 |
| 2026-08-04 | 路线 A 修订（D4）：`carries` 表改用 data-consistency 统一库基线表，`CarryStore` 基于 `Database` | 项目负责人 |
| 2026-08-04 | 批次1（G1）落地：`carry_store.py`（CarryStatus/CarryRecord/CarryStore + `_set_status` 状态流转 + D3 `rollback_revoked_to_delivered`）+ `test_carry_store.py` 10 用例全绿，全量 76 passed 无回归；§9 状态列 | 项目负责人 |
| 2026-08-04 | 批次2（S1）实施前澄清：§2.1 与 §2.3 三处不一致（触发词带空格变体、前导 ta 剥离、空正文兜底语义）经确认修正 §2.1/§2.2；实现 `carry_intent.py` + `test_carry_intent.py` 10 用例全绿，全量 86 passed 无回归 | 项目负责人 |
| 2026-08-04 | 批次3（S2/S4）实施前澄清：§4.2 补 `on_hide` 回调（接收端 revoke 生效即时隐藏）+ `get_mood` 注入（主项目情绪接入点），`scan_expired` 同步化；实现 `carry.py` + `test_carry_service.py` 14 用例全绿，全量 100 passed 无回归 | 项目负责人 |
| 2026-08-04 | 批次4（S3）落地：`CarryService` 补只读 `config` 属性；实现 `carry_receive.py` + `test_carry_receive.py` 6 用例全绿，全量 106 passed 无回归；§5.1 补充 config/is_dnd 接线说明 | 项目负责人 |
| 2026-08-04 | 批次5（C3）落地：`tools/carry_demo.py` 双端联调脚本（配对 → 发送/播报/确认 → delivered → 撤回即时隐藏 → 过期 failed，全程断言无重复播报），运行全流程绿；§7/§9 标注落地状态。carry-message 核心 5 文件全部落地 | 项目负责人 |
