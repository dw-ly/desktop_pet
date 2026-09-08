# 共同养成 实现规格（impl）

> 关联 Spec：[[7-AI学习/11-AI桌宠情侣互联/spec/pet-growth/spec.md|spec/pet-growth]]
> 关联 Plan：[[7-AI学习/11-AI桌宠情侣互联/spec/pet-growth/plan.md|plan/pet-growth]]
> 创建日期：2026-08-04
> 状态：已确认（G1/S1-S5/C1/C2 全部落地）

## 目的

将 plan 的任务级 TODO（G1/S1-S5/C1/C2）细化为**可直接编码的契约**：接口签名、`pet.feed` / `pet.profile` 线格式、积分计算规则、等级/解锁判定、灰色期状态机、宠物名/形象管理、断言级测试用例。编码时不再做设计决策；若实现中必须偏离本文档，先更新本文档再编码（文档即契约）。

本模块是 M3 核心模块：两人共同养一只团子。核心链路：本地互动行为 → `PetGrowth.award` 计算 delta（纪念日钩子加成）→ 门控（carry 5min / 每日 reason 上限）→ 签名（MAC 子密钥）→ `pet.feed` 加密同步 → 双方 `apply_intimacy_event` 合入 → `PetLevel.sync` 换算等级与解锁 → 每日零点 `PetStreak.settle` 结算灰色期状态机 → `date.sync` 对齐 → `pet.profile` 同步宠物名。

本模块**不重复实现** data-consistency 已有能力（`pet_state` 表、`apply_intimacy_event` 验签/上限/熔断、`date.sync` 对齐、MAC 签名派生），只做养成的业务计算与状态机。纪念日加成通过**可注入钩子**解耦（D27），anniversary 模块落地前钩子返回 False（无加成）。

---

## 0. 目录结构与模块归属

```
src/core/
├── pet_config.py     ← plan G1  成长配置与解锁映射
├── pet_growth.py     ← plan S1  积分计算与本地合入（award 唯一入口）
├── pet_sync.py       ← plan S2  积分事件接收合入（handle_pet_feed）
├── pet_level.py      ← plan S3  等级与解锁系统
├── pet_streak.py     ← plan S4  每日结算与灰色期状态机
├── pet_profile.py    ← plan S5  宠物名同步与形象选择
src/ui/
└── pet_panel.py      ← plan C1  成长面板 UI（HAS_QT 占位 + 纯函数）
tools/
└── pet_growth_demo.py ← plan C2  养成联调脚本
assets/pets/
├── moon-cat/manifest.json      # 月薪猫
├── line-dog-1/manifest.json    # 线条小狗·款一
└── line-dog-2/manifest.json    # 线条小狗·款二
tests/core/
├── test_pet_config.py
├── test_pet_growth.py
├── test_pet_sync.py
├── test_pet_level.py
├── test_pet_streak.py
├── test_pet_profile.py
└── test_pet_status_text.py
```

---

## 1. 全局约定

### 1.1 线格式

**`pet.feed`**（已登记，事件注册表不改）：负载与 data-consistency `apply_intimacy_event` 契约一致——

```json
{
  "delta": 3,       // int，>0
  "reason": "feed", // str ∈ INTIMACY_REASONS = ("carry","chat","feed","gift","streak")
  "ts": 1786000000, // int，Unix 秒（签名覆盖 + 每日 reason 上限按此本地日）
  "sig": "<hex>"    // HMAC(mac_key, "delta:reason:ts")
}
```

**`pet.profile`**（已登记）：

```json
{
  "name": "小团子"   // str，本端宠物名（改名重发）
}
```

时间戳由 sync 层 `Message.ts` 承载，`pet.profile` 负载不冗余携带；`pet.feed` 的 `ts` 是业务事件时间（签名覆盖对象），不可省略。

### 1.2 存储映射（本端一份）

| 数据 | 位置 | 写入方 |
|------|------|--------|
| 亲密度 `intimacy` | `pet_state.intimacy` | `apply_intimacy_event`（唯一入口） |
| 等级镜像 `level` | `pet_state.level` | `PetLevel.sync`（**只增不降**，D26） |
| 连续共同登录 `streak_days` | `pet_state.streak_days` | `PetStreak.settle` + `daily_align` 对齐兜底 |
| 灰色期剩余容错天数 `grace_left` | `pet_state.grace_left` | `PetStreak.settle` |
| 灰色期连续恢复日 `grace_progress` | `pet_state.grace_progress` | `PetStreak.settle` |
| 灰色期状态 `pet:grace_status` | `kv`（TEXT：normal/grace） | `PetStreak.settle` |
| 带话频率门控 `pet:carry_last_ts` | `kv`（int 秒） | `PetGrowth.award`（D28） |
| 本端宠物名 `pet:my_name` | `kv` | `PetProfile.set_my_pet_name` |
| 本端形象预设 `pet:preset_id` | `kv` | `PetProfile.set_preset` |
| 解锁物品 | `user_items`（`source='unlock:<kind>:<threshold>'`） | `PetLevel.sync`（D23） |
| 已结算日标记 `pet:settled:<day>` | `kv` | `PetStreak.settle`（幂等） |

> `pet_state` 为 INTEGER-only，字符串状态（grace_status、宠物名、预设 id、解锁列表）一律入 `kv` / `user_items`。

### 1.3 与 data-consistency 的衔接

- **亲密度合入唯一入口**：`apply_intimacy_event(db, ev, mac_key)`——熔断检查 → 验签 → 每日 reason 上限 → 累加。本模块**所有**积分事件（含 streak 结算）都经它，杜绝绕过。
- **签名**：`sign_intimacy_event(mac_key, delta, reason, ts)`，mac_key = `derive_mac_key(my_private, peer_public)`。
- **对齐**：`daily_align` / `handle_peer_snapshot` 已在 daily_sync 实现——streak 取较大者修复、亲密度以本端事件流重放为准；本模块 `settle` 与之正交（状态机在前，对齐在后）。
- **重放收敛**：`replay_intimacy_total` 以 events 表 `pet.feed` 事件流核对；pet.feed 已被 S3 prune 豁免（不清理），本模块不新增豁免。

### 1.4 纪念日钩子（D27）

```python
_HOOK: Callable[[], bool] = lambda: False

def set_anniversary_hook(fn: Callable[[], bool]) -> None: ...
def is_anniversary_today() -> bool:
    """anniversary 模块注册后当日返回 True；未注册恒 False。"""
```

- `PetGrowth.delta_for` / `award` 计算时调用；anniversary 模块（M3 后置）落地后 `set_anniversary_hook` 注册，自动生效，不阻塞本模块。
- 两端 anniversary 数据经 `date.add` 同步，理论上两端钩子一致；即使个别不一致，streak 事件由单一生成方烘焙 delta（见 D25），接收侧以事件内 delta 为准。

### 1.5 线程模型

- `award` / `handle_pet_feed` / `settle` 在**单线程内使用**（UI 线程触发 award/settle，sync 线程回调 handle）；跨线程临界点仅 UI 回调（`on_intimacy` / `on_unlock`），UI 侧自行 marshal 到 UI 线程（Qt 信号队列，同 mood-sync impl §1.2 约定）。
- 时间源注入：`award` / `settle` 接受 `now` 参数（测试可控），生产默认 `time.time()`。

---

## 2. G1 pet_config.py（批次1）

### 2.1 配置

```python
@dataclass(frozen=True)
class PetConfig:
    level_k: int = 100            # 等级曲线 k：level = int(sqrt(intimacy / k))（D26）
    feed_delta: int = 3           # 日常喂食/摸头基础积分
    carry_delta: int = 10         # 带话送达基础积分
    chat_delta: int = 5           # 共同对话（当日首次）基础积分
    gift_delta: int = 5           # 礼物接受基础积分
    gift_anniversary_delta: int = 20  # 礼物接受纪念日特惠（D29）
    carry_min_interval: float = 300.0  # 带话频率门控：距上次 ≥ 此秒数才计（D28）
    level_milestones: tuple[int, ...] = (5, 10, 15)          # 等级解锁里程碑
    intimacy_milestones: tuple[int, ...] = (100, 365, 1000)  # 亲密度解锁里程碑
    grace_days: int = 3           # 灰色期容错天数（漏登 -1，耗尽清零）
    grace_recover_days: int = 3   # 灰色期连续共同登录日 ≥ 此值恢复 normal

def load_pet_config(overrides: dict | None = None) -> PetConfig:
    """配置加载：缺失键用默认值；overrides 仅覆盖给定键。
    非法值（delta ≤ 0 / level_k ≤ 0 / carry_min_interval ≤ 0 /
    grace_days < 0 / grace_recover_days ≤ 0 / 里程碑非正或非严格递增）抛 ValueError。"""
```

- 数值与 spec §3.3.2 积分来源表 / 04 号笔记完全一致；`carry_delta=10`、`chat_delta=5`、`feed_delta=3`、`gift_delta=5`、`gift_anniversary_delta=20`。
- **每日 reason 上限不在此表**：consistency `INTIMACY_DAILY_LIMITS` 已是唯一来源（chat=1、feed=5、streak=1；carry/gift 由发送侧约束），本模块引用之，不重复定义。

### 2.2 解锁映射

```python
# item_id → 元数据（kind: level|intimacy；threshold: 达到即解锁）
UNLOCK_ITEMS: dict[str, dict] = {
    "unlock_level_5":        {"name": "普通装扮",   "type": "outfit", "rarity": "common",     "kind": "level",    "threshold": 5},
    "unlock_level_10":       {"name": "专属动作",   "type": "action", "rarity": "uncommon",   "kind": "level",    "threshold": 10},
    "unlock_level_15":       {"name": "高级装扮",   "type": "outfit", "rarity": "rare",       "kind": "level",    "threshold": 15},
    "unlock_intimacy_100":   {"name": "爱心气泡",   "type": "bubble", "rarity": "event",      "kind": "intimacy", "threshold": 100},
    "unlock_intimacy_365":   {"name": "纪念日套装", "type": "outfit", "rarity": "anniversary","kind": "intimacy", "threshold": 365},
    "unlock_intimacy_1000":  {"name": "羁绊徽章",   "type": "badge",  "rarity": "legend",     "kind": "intimacy", "threshold": 1000},
}

def level_from_intimacy(intimacy: int, k: int | None = None) -> int:
    """level = int(sqrt(intimacy / k))，k 默认 cfg.level_k=100（D26）。intimacy ≤ 0 → 0。"""

def earned_unlock_ids(cfg: PetConfig, level: int, intimacy: int) -> set[str]:
    """返回达到阈值应解锁的全部 item_id（kind=level 取 threshold ≤ level；
    kind=intimacy 取 threshold ≤ intimacy；与 cfg 里程碑解耦：以 UNLOCK_ITEMS 为准）。"""
```

- **等级从 0 起算**（intimacy 0 → level 0）：`exp = k·level²` 在 level 0 时 exp=0，公式自洽；5/10/15 级门槛对应 intimacy 2500/10000/22500（D26 决议）。
- 解锁物品池以 `UNLOCK_ITEMS` 为唯一清单（04 号笔记 L2/L5/L8/L12 为早期清单，spec 决议以 5/10/15 为准，风险已记录）。

### 2.3 活动类型（D24，供 S4 用）

```python
# 参与"共同登录"判定的业务事件类型（date.sync 为对齐自触发，排除避免自我激活）
ACTIVITY_TYPES: frozenset[str] = frozenset({
    "msg.carry", "carry.ack", "carry.revoke",
    "mood.sync", "pet.feed", "pet.profile",
    "date.add", "date.remind",
    "gift.send", "gift.accept", "gift.expire",
})
```

### 2.4 断言级测试（test_pet_config.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 默认配置 | `load_pet_config()` | feed=3/carry=10/chat=5/gift=5/gift_anniv=20/carry_min=300/level_k=100/level_milestones=(5,10,15)/intimacy_milestones=(100,365,1000)/grace=3/grace_recover=3 |
| 部分覆盖 | `{"feed_delta": 4}` | feed=4，其余默认 |
| 非法 delta | `{"feed_delta": 0}` | 抛 ValueError |
| 非法里程碑 | `{"level_milestones": (10, 5)}` | 抛 ValueError（非严格递增） |
| 等级换算边界 | `level_from_intimacy(0/99/100/400/2500/10000)` | 0/0/1/2/5/10 |
| 解锁全集 | `earned_unlock_ids(cfg, 5, 100)` | 含 unlock_level_5 + unlock_intimacy_100，不含 level_10/15/intimacy_365/1000 |
| 越级解锁 | `earned_unlock_ids(cfg, 15, 1000)` | 6 个 item_id 全覆盖 |
| 活动类型 | ACTIVITY_TYPES | 含 pet.feed，不含 date.sync |

---

## 3. S1 pet_growth.py（批次2）

### 3.1 积分计算规则（spec §3.3.2 唯一来源表 + 纪念日统一规则）

```text
delta 计算（award 内部）：
  reason == 'gift':
      delta = cfg.gift_anniversary_delta (20)  若 is_anniversary_today()
             否则 cfg.gift_delta (5)                     （D29：特惠不叠加 ×2）
  其它 reason：
      delta = 显式 delta 参数（streak 场景 = streak×2，D30）
             否则 cfg 基础值（carry=10 / chat=5 / feed=3）
      delta ×= 2  若 is_anniversary_today()      （纪念日统一 ×2，取整）

门控（顺序）：
  1. 隐身无关；carry 频率：reason=='carry' 且 now - kv['pet:carry_last_ts'] < carry_min_interval
       → 拒绝返回 0（D28，发送侧门控）
  2. 其余 reason 由 apply_intimacy_event 的每日 reason 上限兜底（chat=1/feed=5/streak=1；
       carry/gift 无上限仅验签防伪）
```

### 3.2 契约

```python
class PetGrowth:
    """本地互动行为入口 → delta 计算 → 门控 → 签名 → 本地乐观合入 → 发送。"""

    def __init__(self, db, sync, mac_key: bytes, cfg: PetConfig | None = None) -> None:
        # db: Database；sync: SyncManager 鸭子类型（仅 send）；mac_key: derive_mac_key 产物

    def award(self, reason: str, *, delta: int | None = None,
              now: float | None = None) -> int:
        """合入一次积分。返回实际合入 delta（0 = 拒绝）。
        - reason ∈ INTIMACY_REASONS，否则 ValueError
        - delta 显式传入用于 streak（= streak×2，D30）；其余由 3.1 规则计算
        - 步骤：delta 计算 → carry 门控 → sign → apply_intimacy_event（拒绝则返回 0 不发）
          → sync.send(PET_FEED, payload) → carry 门控记录更新 → 返回 delta
        - 负载键集合恰为 {delta, reason, ts, sig}（载荷纯净断言点）"""
    def delta_for(self, reason: str, *, delta: int | None = None) -> int:
        """3.1 规则计算 delta（不落库、不发；纯函数，供测试/UI 预演）。"""
```

- `sync.send` 经 SyncManager：在线直发落 events（status='sent'，D17），离线入队补发；`pet.feed` 为普通事件按保留期 prune 清理（**非** pet.feed 豁免——即 pet.feed 事件本身被 prune 豁免，见 §1.3）。
- 签名失败/超上限/熔断（`kv.intimacy_halted`）→ `apply_intimacy_event` 返回 False → `award` 返回 0，**不发送**（本端与对方都不会看到该事件，防作弊一致性）。

### 3.3 断言级测试（test_pet_growth.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 喂食合入 | award("feed") | 返回 3；本地 intimacy=3；sync 收到一条 PET_FEED，payload 键 == {delta,reason,ts,sig}，sig 可验证 |
| 签名有效 | award 后验签 | `verify_intimacy_signature(mac_key, 3, "feed", ts, sig)` 为 True |
| 带话 5min 门控 | award("carry") → 再 award("carry")（now+60） | 第 1 次返回 10；第 2 次返回 0（间隔 <300s） |
| 带话间隔放行 | 第 1 次后 now+300 | 返回 10（恰好 300s 边界放行） |
| chat 每日上限 | award("chat") 同 ts 日再 award("chat") | 第 1 次返回 5；第 2 次返回 0 |
| feed 每日上限 | 连续 award("feed") ×6 | 前 5 次返回 3；第 6 次返回 0 |
| 纪念日 ×2 | 钩子 True，award("feed") | 返回 6；事件 delta=6 |
| 礼物特惠 | 钩子 True，award("gift") | 返回 20（不 ×2） |
| 礼物普通 | 钩子 False，award("gift") | 返回 5 |
| 熔断拒绝 | 置 kv.intimacy_halted=1 | award("feed") 返回 0；无发送 |
| 非法 reason | award("hack") | 抛 ValueError |
| delta_for 预演 | delta_for("feed") | 3（钩子 False）；钩子 True 为 6；delta_for("gift") 钩子 True 为 20 |

---

## 4. S2 pet_sync.py（批次3）

### 4.1 契约

```python
class PetSync:
    """pet.feed 接收合入。发送侧由 PetGrowth.award 承担；本类只管接收侧。"""

    def __init__(self, db, sync, mac_key: bytes, cfg: PetConfig | None = None,
                 on_intimacy: Callable[[int], None] | None = None) -> None:
        # on_intimacy: 合入成功后回调(delta)，UI 订阅 → PetLevel.sync（跨线程 marshal 由 UI 侧负责）

    def handle_pet_feed(self, m: Message) -> bool:
        """处理一条收到的 pet.feed（m.payload 为 §1.1 线格式）。
        - 直接委托 apply_intimacy_event(db, payload, mac_key)：验签 + 上限 + 熔断
        - 返回 True=已合入（delta>0，on_intimacy(delta) 触发）；False=拒绝（丢弃/告警）"""
    def set_mac_key(self, mac_key: bytes) -> None:
        """换机重配对后更新校验密钥（替换不可变容器的替代方案）。"""
```

- **载荷校验**复用 `apply_intimacy_event`（delta 非 int/≤0、reason 非法、sig 非 hex 均返回 False），本类不重复实现。
- 本端自身 award 的本地合入也走 `apply_intimacy_event`（同源），因此**发送侧无需经 handle_pet_feed**——两端从同一事件流收敛。

### 4.2 断言级测试（test_pet_sync.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 合法事件合入 | handle({delta:3,reason:feed,ts,sig}) | 返回 True；intimacy+3；on_intimacy 收到 3 |
| 伪造签名 | 篡改 delta 后 handle | 返回 False；intimacy 不变；on_intimacy 未触发 |
| 错密钥 | 用不同 mac_key 构造 | 返回 False |
| 超上限 | chat 当日第 2 条 | 返回 False |
| 非法负载 | handle({delta:"x",...}) | 返回 False；状态不变 |
| 熔断 | 置 intimacy_halted=1 | 返回 False |
| set_mac_key | 更新密钥后旧密钥事件 | 旧密钥事件返回 False；新密钥事件 True |

---

## 5. S3 pet_level.py（批次4）

### 5.1 等级与解锁（D26 / D23）

```python
class PetLevel:
    """等级换算（镜像只增）+ 解锁持久化（user_items，source='unlock:...'）。"""

    def __init__(self, db, cfg: PetConfig | None = None) -> None: ...

    def current_level(self) -> int:
        """pet_state.level 镜像；缺失按 intimacy 计算（启动时未 sync 也正确）。"""

    def sync(self, intimacy: int | None = None) -> list[str]:
        """亲密度变化后调用：重算等级并补发解锁。返回**本次新增**解锁 item_id 列表。
        - intimacy 缺省读 pet_state.intimacy
        - level_new = level_from_intimacy(intimacy)；level_new > 当前镜像 → 更新（只增）
        - 对 UNLOCK_ITEMS 中 threshold ≤ level_new（kind=level）或 ≤ intimacy（kind=intimacy）
          且 user_items 不存在的行 INSERT OR IGNORE（source='unlock:<kind>:<threshold>'）
        - 越级（批量经验一次跨多里程碑）一次补发全部新增解锁，返回完整新增列表"""

    def unlocked_items(self) -> list[dict]:
        """已解锁列表（user_items WHERE source LIKE 'unlock:%'），按 threshold 升序。"""
```

- **只增不降**：等级镜像取 max（daily_align 兜底修复 intimacy 瞬时回退不触发降级）；解锁 INSERT OR IGNORE 永不删除。
- `user_items` 在备份三表范围内（backup_restore §BACKUP_TABLES），解锁随密文备份恢复。

### 5.2 断言级测试（test_pet_level.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 初始等级 | 新实例 | current_level()==0 |
| 等级换算 | sync(intimacy=2500) | 返回含 unlock_level_5；pet_state.level==5 |
| 亲密度解锁 | sync(intimacy=100) | 返回含 unlock_intimacy_100 |
| 越级补发 | sync(intimacy=10000) | 返回 5 项：level_5+level_10+intimacy_100/365/1000 |
| 只增不降 | sync(2500) → sync(2000) | level 仍 5；解锁列表不变 |
| 幂等 | sync(2500) ×2 | 第 2 次返回空列表；user_items 无重复行 |
| unlocked_items | 解锁后 | 列表含对应 item，source='unlock:...'，升序 |
| 无持久化依赖 | sync(0) | 返回空；不写任何行 |

---

## 6. S4 pet_streak.py（批次4）

### 6.1 灰色期状态机（spec §3.3.2 / 04 号笔记）

```text
每日零点 settle()：
  本端活动 local_active  = events 表 status ∈ {sent, pending} 且 type ∈ ACTIVITY_TYPES
                           且 created_at ∈ [day_start, day_end)
  对方活动 peer_active   = events 表 status == 'received' 且 type ∈ ACTIVITY_TYPES 同范围
  both_active = local_active AND peer_active          （D24）

状态（kv['pet:grace_status']，'normal' | 'grace'）：
  normal：
    both_active → streak_days +1；生成方（D25）发 streak 积分（delta = streak×2，D30）
    !both_active → 转 grace：grace_status=grace，grace_left=grace_days(3)，grace_progress=0
  grace：
    both_active → streak_days +1，grace_progress +1；生成方发 streak 积分
                  grace_progress ≥ grace_recover_days(3) → 恢复 normal（grace_status=normal，
                  grace_left=grace_days(3) 重置，grace_progress=0；视同未中断继续累计）
    !both_active → grace_left -1，grace_progress=0
                  grace_left ≤ 0 → streak_days 清零重来，恢复 normal（grace_left=3）

幂等：settle 成功后在 kv['pet:settled:<day>'] 写标记，同日重复 settle 直接返回当前态（no-op）。
断网推迟：!both_active 且当前未连接（connected=False）→ 返回 DEFERRED，不写结算标记、
  不转变状态；重连后由调用方重新触发（与 daily_align DEFERRED 语义一致）。
每日 reason 计数：INTIMACY_DAILY_LIMITS 计数键含日期（intimacy_count:<day>:<reason>），
  跨日自动失效，无需显式"重置每日计数"。
```

### 6.2 契约

```python
class SettleStatus(str, Enum):
    OK = "ok"                # 已结算
    DEFERRED = "deferred"    # 断网且无对方证据，推迟

@dataclass(frozen=True)
class SettleOutcome:
    status: SettleStatus
    streak: int              # 结算后 streak_days
    grace_status: str        # 'normal' | 'grace'
    grace_left: int
    grace_progress: int
    awarded_delta: int       # 本次 streak 积分（0 = 无/非生成方/被拒）
    event_sent: bool         # 本端是否为 streak 事件生成方且已发
    both_active: bool

class PetStreak:
    """每日结算 + 灰色期状态机（依赖 events 表活动判定）。"""

    def __init__(self, db, growth: PetGrowth, cfg: PetConfig | None = None) -> None:
        # growth: 生成方 streak 积分经 PetGrowth.award('streak', delta=streak×2)（D25/D30）
        #   （避免重复实现签名/合入/发送路径）

    def settle(self, my_peer_id: str, partner_id: str, *, now: float | None = None,
               connected: bool | None = None) -> SettleOutcome:
        """按 6.1 状态机结算。connected 缺省查 sync.connection_status()（经 growth._sync）。"""
```

- **streak 单一生成方（D25）**：`generator = my_peer_id if my_peer_id > partner_id else partner_id`（peer_id 为派生 id，确定性排序，无相等可能）；仅生成方 `growth.award('streak', delta=new_streak*2, now=settle_ts)`，配合 `INTIMACY_DAILY_LIMITS['streak']=1` 兜底防双计。
- 状态写入在 `db.transaction()` 内原子完成；award 在其后（生成方），失败（熔断/上限）不影响状态机结果（streak 计数与 intimacy 是两个正交值）。

### 6.3 断言级测试（test_pet_streak.py）

> 测试通过直接向 events 表插入活动事件行（created_at 落在目标日窗口），并预置 pet_state。

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 正常双活跃 | 本端+对方各有事件 | streak 0→1；生成方发 streak 事件 delta=2；grace_status=normal |
| 单活跃转灰 | 仅本端事件 | streak 不变；grace=grace；grace_left=3 |
| 灰色双活跃恢复 | 连续 3 日双活跃 | 每日 streak+1；第 3 日 grace_progress=3 → 恢复 normal |
| 灰色漏登 | 灰色态下无活动 | grace_left 3→2；grace_progress=0 |
| 耗尽清零 | grace_left 3 日内连续漏登 3 日 | 第 3 日 streak=0、恢复 normal |
| 单一生成方 | 双端各自 settle | 仅 max(peer_id) 端 event_sent=True；另一端 False |
| 幂等 | 同日 settle ×2 | 第 2 次返回当前态，streak 不重复 +1 |
| 断网推迟 | 无对方事件且 connected=False | 返回 DEFERRED；状态不变；无结算标记 |
| 纪念日 streak ×2 | 钩子 True，生成方 | streak 事件 delta = (streak×2)×2 |
| 对齐衔接 | settle 后 handle_peer_snapshot | streak 取较大者修复（复用 daily_sync） |

---

## 7. S5 pet_profile.py（批次4）

### 7.1 契约

```python
class PetProfile:
    """宠物名同步（pet.profile）+ 形象预设选择（assets/pets/<id>/manifest.json）。"""

    def __init__(self, db, sync, cfg: PetConfig | None = None,
                 assets_dir: str | Path | None = None) -> None:
        # assets_dir 缺省为 code 目录下 assets/（调用方注入便于测试用临时目录）

    # -- 宠物名 -- #
    def set_my_pet_name(self, name: str) -> None:
        """写 kv['pet:my_name']；已配对则发送 pet.profile（改名重发，spec §3.3.7）。"""
    def my_pet_name(self) -> str: ...
    def peer_pet_name(self) -> str | None:
        """pairing.peer_pet_name（pet.profile 写入）；无 → None。"""
    def handle_pet_profile(self, m: Message) -> bool:
        """接收 pet.profile → upsert pairing(pairing_id=m.from_id, peer_id=m.from_id,
        peer_pet_name=m.payload['name'])。payload 缺 name/非 str → False。"""
    def send_profile(self) -> None:
        """配对成功后发送本端宠物名（无名字时不发）。"""

    # -- 形象 -- #
    def list_presets(self) -> list[dict]:
        """扫描 assets/pets/*/manifest.json：按文件名排序返回 [{id, name, states}]；
        缺失/损坏清单跳过；目录不存在 → []。"""
    def current_preset(self) -> str | None: ...   # kv['pet:preset_id']
    def set_preset(self, preset_id: str) -> None:
        """写 kv['pet:preset_id']（仅本端生效，不同步；preset_id 非法抛 ValueError）。"""
```

**pet 形象 manifest 结构**（08 号笔记 §一 约定，本 impl 定稿）：

```json
{
  "version": 1,
  "id": "moon-cat",
  "name": "月薪猫",
  "states": {"idle": "assets/pets/moon-cat/idle.svg"}
}
```

> 占位 manifest 随本模块落地（`assets/pets/{moon-cat,line-dog-1,line-dog-2}/manifest.json`），states 仅含 `idle` 占位；真实动画帧按 08 号笔记排期替换，程序不硬编码。

### 7.2 断言级测试（test_pet_profile.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 设名持久化 | set_my_pet_name("小团子") | my_pet_name()=="小团子" |
| 改名重发 | 已配对 set_my_pet_name | sync 收到一条 PET_PROFILE payload=={"name": ...} |
| 配对发送 | send_profile | 同上（无名字不发送） |
| 接收存名 | handle_pet_profile | peer_pet_name()==名字；pairing 表有行 |
| 非法负载 | handle_pet_profile({}) | 返回 False；peer_pet_name 不变 |
| 形象清单 | 临时 assets 3 款 | list_presets() 3 项按 id 排序，字段齐 |
| 缺失兜底 | 空 assets 目录 | list_presets()==[]；set_preset 非法 id 抛 ValueError |
| 本端生效 | set_preset("moon-cat") | current_preset()=="moon-cat"；无任何发送 |

---

## 8. C1/C2：成长面板 UI 与联调脚本

### 8.1 C1 src/ui/pet_panel.py

PySide6 条件导入 + HAS_QT 占位（同 `partner_state.py` / `settings_dialog.py` 模式）。**纯函数先行**（可单测）：

```python
def pet_status_text(level: int, streak: int, grace_status: str) -> str:
    """成长面板摘要行（纯函数）：
    - 灰色期 → f"{level} 级 · 连续 {streak} 天 · 灰色期"
    - 否则 → f"{level} 级 · 连续 {streak} 天"
    （等级/亲密度进度条、解锁列表、弹窗由 Qt 控件承载，随主项目外壳接入）"""
```

`PetPanel(QWidget)`（HAS_QT 下）：等级/经验条、亲密度进度条、streak 与灰色标签、已解锁列表、升级/解锁弹窗、宠物名与形象入口。订阅 `on_intimacy` / `on_unlock` 回调，Qt 信号队列 marshal 回 UI 线程。

### 8.2 C2 tools/pet_growth_demo.py

复用 consistency_demo / mood_demo 双端骨架（prewrite_identity + derive_mac_key + 配对）。一键演示并断言：

```text
A award('feed') → 双方 intimacy=3（B handle 合入）
A award('feed') ×4 → 当日 feed 5 次满；第 6 次 → 0（上限拒绝）
A award('carry') → +10；A 再 award('carry')（间隔<300s）→ 0（5min 门控）
伪造签名 pet.feed → B 拒绝（intimacy 不变）
批量大额事件（delta=10000, reason='gift', 签名）→ 双端 intimacy 收敛 → PetLevel.sync
  双端解锁 5 项（level5/10 + intimacy100/365/1000），新增解锁列表一致
每日结算：settle(A) / settle(B) 双活跃 → 生成方发 streak 事件，streak=1，双端 intimacy 增加
灰色期：次日仅 A 活跃 → settle → grace；连续 3 日双活跃 → 恢复 normal
改名：A set_my_pet_name("小团子") → B peer_pet_name()=="小团子"
形象：list_presets() 3 款；A set_preset("moon-cat") → current_preset()=="moon-cat"（不同步 B）
全程断言：payload 键 == {delta, reason, ts, sig}；上限/门控/验签/状态机/改名全链路正确
```

---

## 9. 本次实施范围与测试清单

| 交付 | 文件 | 测试 | 状态 |
|------|------|------|:----:|
| G1 | `src/core/pet_config.py` | test_pet_config.py（10 通过） | ✅ 批次1 |
| S1 | `src/core/pet_growth.py` | test_pet_growth.py（11 通过） | ✅ 批次2 |
| S2 | `src/core/pet_sync.py` | test_pet_sync.py（7 通过） | ✅ 批次3 |
| S3 | `src/core/pet_level.py` | test_pet_level.py（9 通过） | ✅ 批次4 |
| S4 | `src/core/pet_streak.py` | test_pet_streak.py（10 通过） | ✅ 批次4 |
| S5 | `src/core/pet_profile.py` | test_pet_profile.py（9 通过） | ✅ 批次4 |
| C1 | `src/ui/pet_panel.py` | `pet_status_text` 纯函数单测（3 通过） | ✅ 批次5 |
| C2 | `tools/pet_growth_demo.py` + `assets/pets/*/manifest.json` | pet_growth_demo 联调绿（exit=0） | ✅ 批次5 |

---

## 10. 决策记录

| 编号 | 主题 | 决策 | 状态 |
|------|------|------|:----:|
| D23 | 解锁持久化 | 解锁物品入 `user_items` 表（`source='unlock:<kind>:<threshold>'`）；备份三表之一，换机随密文恢复；解锁可随时从 level/intimacy 重算补发 | 已定 |
| D24 | 灰色期活动判定 | 以 events 表当日事件为基准：本端=status∈{sent,pending}，对方=status='received'；ACTIVITY_TYPES 排除 date.sync（防对齐自触发） | 已定 |
| D25 | streak 积分单一生成方 | 每日 streak×2 积分由 peer_id 较大一侧生成发送（确定性），配合 INTIMACY_DAILY_LIMITS['streak']=1 兜底防双计 | 已定 |
| D26 | 经验镜像亲密度 | 不单独存 exp：level = int(sqrt(intimacy/k))（k=100），level 从 0 起算；pet_state.level 为镜像只增。事件流单一来源、天然收敛且只增不降 | 已定 |
| D27 | 纪念日钩子 | 模块级 `set_anniversary_hook` / `is_anniversary_today()`（缺省 False）；anniversary 落地后注册自动生效，不阻塞本模块 | 已定 |
| D28 | carry 5min 门控 | 带话积分 1 次/5min 在发送侧门控（kv['pet:carry_last_ts'] 持久化）；carry 无每日上限，仅验签防伪 | 已定 |
| D29 | 礼物积分接入 | 礼物积分由 gift-exchange 在 accept 时调用 award('gift')；delta=普通 5 / 纪念日特惠 20（不叠加 ×2） | 已定 |
| D30 | streak 走统一 award | 结算的 streak×2 积分复用 `PetGrowth.award('streak', delta=streak×2)`（签名/合入/发送单一路径）；纪念日对 streak 也 ×2（基础积分统一规则） | 已定 |

---

## 11. 变更记录

| 日期 | 变更内容 | 变更人 |
|------|---------|--------|
| 2026-08-04 | 初始草稿：G1/S1-S5/C1/C2 契约定稿；确认 D23-D30；pet.feed/pet.profile 线格式；存储映射（pet_state/kv/user_items）；灰色期状态机、等级解锁、积分计算规则对齐 spec/plan/04 号笔记 | 项目负责人 |
| 2026-08-04 | 全部落地：G1 pet_config（10）+ S1 pet_growth（11）+ S2 pet_sync（7）+ S3 pet_level（9）+ S4 pet_streak（10）+ S5 pet_profile（9）+ C1 pet_panel（3）+ C2 pet_growth_demo 联调绿；core 导出 pet 模块；pet-growth 59 单测全绿 | 项目负责人 |
