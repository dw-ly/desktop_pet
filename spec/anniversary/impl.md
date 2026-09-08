# 纪念日 实现规格（impl）

> 关联 Spec：[[7-AI学习/11-AI桌宠情侣互联/spec/anniversary/spec.md|spec/anniversary]]
> 关联 Plan：[[7-AI学习/11-AI桌宠情侣互联/spec/anniversary/plan.md|plan/anniversary]]
> 创建日期：2026-08-04
> 状态：已确认（G1/S1-S5/C1/C2 全部落地）

## 目的

将 plan 的任务级 TODO（G1/S1-S5/C1/C2）细化为**可直接编码的契约**：接口签名、`date.add` / `date.remind` 线格式、日期存储格式与农历换算、每日触发扫描、祝福生成（LLM + 模板兜底）、当天庆祝与限时装扮、LWW 同步合并、`is_anniversary_today()` 钩子注册、断言级测试用例。编码时不再做设计决策；若实现中必须偏离本文档，先更新本文档再编码（文档即契约）。

本模块是 M3 模块：团子记住纪念日并主动参与。核心链路：本地 `anniversaries` 表（CRUD）→ 每日扫描（农历先换算当年公历）→ 距离提醒日等于提前天数 → LLM 祝福预告（模板兜底）→ 距离为 0 → 当天庆祝（限时装扮 + 互赠邀请信号 + `date.remind` 加密发送对方）→ `date.add` / `date.remind` 双向同步 LWW 合并 → 注册 `is_anniversary_today()` 钩子供 pet-growth 积分加成调用。

本模块**不重复实现** data-consistency 已有能力（`anniversaries` 表、离线队列、配对状态、加密传输）与 pet-growth 的积分计算（只注册钩子让 pet-growth 侧 ×2）。互赠邀请通过**可注入钩子**解耦（D35），gift-exchange 落地前钩子为 no-op。

隐私核心（沿用全局约束）：纪念日属敏感信息，全程加密同步、中继不可见；未配对时纯本地，**不外发任何纪念日事件**。

---

## 0. 目录结构与模块归属

```
src/core/
├── anniv_config.py       ← plan G1  纪念日配置（预设/祝福风格与模板/限时装扮）
├── anniv_store.py        ← plan G1  anniversaries 表 CRUD + 字段校验
├── anniv_calendar.py     ← plan S1  农历换算 + 每日触发扫描
├── anniv_blessing.py     ← plan S2  祝福生成（LLM 注入 + 超时/失败模板兜底 + 风格）
├── anniv_celebrate.py    ← plan S3  当天庆祝 + 限时装扮 + date.remind + 互赠邀请信号
├── anniv_sync.py         ← plan S4  date.add LWW 合并同步
├── anniv_integration.py  ← plan S5  is_anniversary_today 钩子注册
src/ui/
└── anniv_panel.py        ← plan C1  纪念日管理 UI（HAS_QT 占位 + 纯函数）
tools/
└── anniv_demo.py         ← plan C2  纪念日联调脚本
tests/core/
├── test_anniv_config.py
├── test_anniv_store.py
├── test_anniv_calendar.py
├── test_anniv_blessing.py
├── test_anniv_celebrate.py
├── test_anniv_sync.py
├── test_anniv_integration.py
└── test_anniv_status_text.py   ← C1 纯函数单测
```

---

## 1. 全局约定

### 1.1 线格式

**`date.add`**（已登记，事件注册表不改）：纪念日增/改/删的同步事件，负载为完整条目 + 墓碑标记——

```json
{
  "id": "a1b2c3d4",            // str，uuid4.hex，两端同一纪念日共享
  "title": "在一起 100 天",     // str，非空
  "date": "2026-11-11",       // once: "YYYY-MM-DD"；yearly: "MM-DD"（公历/农历同，D31）
  "calendar": "solar",        // "solar" | "lunar"
  "repeat": "once",           // "once" | "yearly"
  "notify_days_before": 1,    // int ≥ 0
  "updated_at": 1786000000,   // int，Unix 秒（LWW 依据，D32）
  "deleted": false            // tombstone：true 表示删除该 id（同 LWW）
}
```

**`date.remind`**（已登记）：当天庆祝通知，接收方本端一同庆祝——

```json
{
  "id": "a1b2c3d4",
  "title": "在一起 100 天",
  "date": "2026-11-11",
  "calendar": "solar"
}
```

### 1.2 存储映射（本端一份）

| 数据 | 位置 | 写入方 |
|------|------|--------|
| 纪念日条目 | `anniversaries` 表（id/title/date/repeat/calendar/notify_days_before/updated_at，0001 基线已建） | `AnnivStore`（唯一入口） |
| 每日已触发标记 | `kv['anniv:triggered:<YYYY-MM-DD>:<id>:<kind>']` | `AnnivCelebrate.daily_check`（D33） |
| 限时装扮 | `kv['anniv:limited_outfit']` = `{"item_id", "expire_at"}` | `AnnivCelebrate._celebrate`（D34） |

> 触发标记键含日期，避免 yearly 纪念日跨年碰撞；限时装扮单槽位（last-write-wins），过期清除即回归原始装扮。

### 1.3 与 pet-growth 的衔接（钩子注册）

- pet-growth 定义模块级 `set_anniversary_hook(fn)` / `is_anniversary_today()`（缺省 False，D27）。本模块 `anniv_integration.register_anniversary_hook(calendar)` 注册实现，pet-growth 积分计算自动 ×2（基础 reason ×2 / 礼物特惠 +20 不叠加由 pet-growth 侧按积分表结算）。
- 两端纪念日数据经 `date.add` 同步，理论上一端为纪念日则两端判定一致；即使个别不一致，积分事件 delta 由发送侧烘焙，接收侧以事件内 delta 为准（同 pet-growth §1.4）。

### 1.4 与 gift-exchange 的衔接（互赠邀请钩子）

```python
_GIFT_HOOK: Callable[[dict], None] | None = None

def set_gift_invite_hook(fn: Callable[[dict], None] | None) -> None: ...
def _fire_gift_invite(entry: dict) -> None: ...
```

- `AnnivCelebrate` 当天庆祝时调用 `_fire_gift_invite(entry)`；gift-exchange 落地后注册，弹出"邀请互赠"，礼物流程由该模块实现，本模块不依赖其落地（D35）。

### 1.5 线程模型

- CRUD / 每日检查 / 同步接收在单线程内使用（UI 线程触发 add/update/delete/daily_check，sync 线程回调 handle_date_add / handle_date_remind）；跨线程临界点仅 UI 回调（`on_remind` / `on_celebrate`），UI 侧自行 marshal 到 UI 线程（Qt 信号队列，同 mood-sync impl §1.2 约定）。
- 时间源注入：`daily_check` / `check_outfit_expiry` / `is_anniversary` 接受 `today: date | None`（测试可控），生产默认 `date.today()`；`updated_at` 默认 `int(time.time())`（测试可传 `now`）。

---

## 2. G1 anniv_config.py + anniv_store.py（批次1）

### 2.1 配置（anniv_config.py）

```python
@dataclass(frozen=True)
class AnnivConfig:
    outfit_item_id: str = "anniv_limited_suit"   # 限时装扮 item_id（D34）
    outfit_duration_days: int = 1                # 限时装扮时长：today + N 日零点失效
    blessing_timeout: float = 3.0                # LLM 生成超时秒数（spec §4.1）
    default_blessing_style: str = "cute"         # 默认祝福风格（cute/deep）
    default_calendar: str = "solar"
    default_repeat: str = "yearly"
    default_notify_days: int = 1                 # 添加纪念日默认提前提醒天数

def load_anniv_config(overrides: dict | None = None) -> AnnivConfig:
    """非法值（outfit_duration_days ≤ 0 / blessing_timeout ≤ 0 / default_notify_days < 0 /
    风格非法）抛 ValueError；缺失键用默认值。"""

# 常用纪念日预设（spec §3.3.1"默认提供常用类型"）
DEFAULT_PRESETS: tuple[dict, ...] = (
    {"title": "在一起纪念日", "calendar": "solar", "repeat": "yearly"},
    {"title": "对方生日",     "calendar": "solar", "repeat": "yearly"},
    {"title": "第一次约会",   "calendar": "solar", "repeat": "yearly"},
    {"title": "七夕",         "calendar": "lunar", "repeat": "yearly"},
)

BLESSING_STYLES: tuple[str, ...] = ("cute", "deep")
BLESSING_TEMPLATES: dict[str, tuple[str, ...]] = {
    "cute": (
        "{title}到啦，团子想和你贴贴！",
        "今天是{title}，抱抱我的团子～",
    ),
    "deep": (
        "{title}，谢谢你陪我走到今天。",
        "{title}，在一起的每一天都值得珍藏。",
    ),
}
```

### 2.2 数据模型（anniv_store.py）

```python
DATE_RE_ONCE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_RE_YEARLY = re.compile(r"^\d{2}-\d{2}$")
CALENDARS = ("solar", "lunar")
REPEATS = ("once", "yearly")

class AnnivStore:
    """anniversaries 表读写封装 + 字段校验（CRUD 唯一入口）。"""

    def __init__(self, db, cfg: AnnivConfig | None = None) -> None: ...

    def validate_entry(self, entry: dict) -> dict:
        """字段校验并返回规范 dict（幂等）。非法抛 ValueError：
        - id 可选（缺省生成 uuid4.hex）；title 非空 str
        - calendar ∈ {solar, lunar}；repeat ∈ {once, yearly}
        - repeat=once → date 匹配 YYYY-MM-DD 且为真实日期（2-30 之类拒绝）
        - repeat=yearly → date 匹配 MM-DD 且月/日合法（2000 为闰年基准，02-29 允许）
        - notify_days_before int ≥ 0；updated_at int > 0（缺省 int(time.time())）"""

    def add(self, entry: dict) -> dict:
        """校验 + INSERT；已存在同 id → 抛 ValueError（新增走 add，覆盖走 update/upsert）。"""
    def update(self, anniv_id: str, patch: dict, *, now: int | None = None) -> dict | None:
        """合并 patch 校验后写回并刷新 updated_at=now；不存在 → None。"""
    def upsert(self, entry: dict) -> dict:
        """校验 + INSERT OR REPLACE（同步 LWW 用，按 id 覆盖整行）。"""
    def delete(self, anniv_id: str) -> bool: ...
    def get(self, anniv_id: str) -> dict | None:
        """行转 dict（含全部列）；不存在 → None。"""
    def list_all(self) -> list[dict]:
        """全量，按 updated_at 升序（稳定序，测试可断言）。"""
```

> 日期真实性校验：`datetime.strptime(s, "%Y-%m-%d")`（once）；`datetime.strptime(f"2000-{s}", "%Y-%m-%d")`（yearly，2000 为闰年）。农历 once 的年份校验走 S1 `lunar_to_solar`（1900-2099），此处仅格式校验。

### 2.3 断言级测试

**test_anniv_config.py**

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 默认配置 | `load_anniv_config()` | outfit_item_id="anniv_limited_suit"/duration=1/timeout=3/style=cute/notify=1 |
| 部分覆盖 | `{"outfit_duration_days": 3}` | duration=3，其余默认 |
| 非法时长 | `{"outfit_duration_days": 0}` | 抛 ValueError |
| 非法风格 | `{"default_blessing_style": "x"}` | 抛 ValueError |
| 预设 | DEFAULT_PRESETS | 含七夕（lunar/yearly）、在一起纪念日 |
| 模板 | BLESSING_TEMPLATES | cute/deep 两风格均有非空模板，含 `{title}` 占位 |

**test_anniv_store.py**

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 新增默认补全 | add({title, date, calendar, repeat}) | id/updated_at 自动生成；get 返回一致 |
| 重复 id 拒绝 | add 同 id ×2 | 第 2 次抛 ValueError |
| 非法 title | title="" | 抛 ValueError |
| 非法 calendar | calendar="hebrew" | 抛 ValueError |
| 非法 repeat | repeat="weekly" | 抛 ValueError |
| once 非法日期 | repeat=once, date="2026-02-30" | 抛 ValueError |
| once 格式错误 | repeat=once, date="02-14" | 抛 ValueError |
| yearly 合法 | repeat=yearly, date="02-29" | 通过（2000 闰年基准） |
| yearly 非法日期 | repeat=yearly, date="13-40" | 抛 ValueError |
| notify 非法 | notify_days_before=-1 | 抛 ValueError |
| 更新刷新 ts | update(id, {title}) | title 变、updated_at 刷新、其余列保留 |
| 更新不存在 | update("nope", {}) | None |
| 删除 | delete(id) 存在/不存在 | True / False |
| 排序 | 插入 3 条 | list_all 按 updated_at 升序 |
| upsert 覆盖 | upsert 同 id 不同 title | 整行覆盖，无重复行 |

---

## 3. S1 anniv_calendar.py（批次2）

### 3.1 农历换算（lunarcalendar 封装）

```python
def lunar_to_solar(year: int, month: int, day: int) -> date:
    """农历 → 公历（lunarcalendar Converter.Lunar2Solar）。
    - year 越界 1900-2099 / 月/日非法 → ValueError
    - lunarcalendar 缺失 → ImportError（调用方降级：solar-only，测试恒装有）"""

def occurrence_date(entry: dict, year: int) -> date | None:
    """该纪念日在指定年份的公历发生日：
    - repeat=once：date 自带年份；year 不匹配 → None
    - repeat=yearly：solar → date(year, m, d)；lunar → lunar_to_solar(year, m, d)
      （lunar 换算可能落入 year+1，属正常，next_occurrence 负责与 today 比较）"""
```

### 3.2 每日触发扫描

```python
@dataclass(frozen=True)
class AnnivTrigger:
    entry: dict
    kind: str          # 'remind' | 'celebrate'
    occurrence: date   # 本次触发对应公历日
    distance: int      # (occurrence - today).days

class AnnivCalendar:
    """每日触发扫描（纯查询 + 标记读；不写除标记外的业务数据）。"""

    def __init__(self, db, cfg: AnnivConfig | None = None) -> None: ...

    def next_occurrence(self, entry: dict, today: date) -> date | None:
        """下一个未来（≥today）发生日（D37 跨年提醒）：
        - once：固定日期；< today → None
        - yearly：occurrence_date(entry, today.year)；< today → occurrence_date(entry, today.year+1)"""

    def daily_triggers(self, today: date | None = None) -> list[AnnivTrigger]:
        """扫描全部纪念日返回本日待触发列表：
        - distance = (next_occurrence - today).days（next_occurrence None → 跳过）
        - distance == 0 → celebrate（notify_days_before 无论几都只 celebrate）
        - 0 < distance == notify_days_before → remind
        - 已标记 kv['anniv:triggered:<iso>:<id>:<kind>'] → 跳过（同一纪念日每日仅触发一次，D33）
        - 列表按 id 稳定排序"""

    def is_anniversary(self, today: date | None = None) -> bool:
        """任一纪念日 today 为发生日（next_occurrence == today），供 S5 钩子调用。"""

    def mark_triggered(self, anniv_id: str, kind: str, today: date) -> None: ...
```

- `daily_triggers` 只读不写标记；`mark_triggered` 由处理方（AnnivCelebrate）在成功处理后调用——处理中途失败不落标记，下轮重试。
- `is_anniversary` 与 celebrate 判定同源（都走 `next_occurrence == today`），保证钩子加成与庆祝触发一致。

### 3.3 断言级测试（test_anniv_calendar.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 农历换算 | `lunar_to_solar(2024, 7, 7)` | date(2024, 8, 10)（七夕） |
| 换算边界 | `lunar_to_solar(1900, 1, 1)` / `(2099, 12, 1)` | date(1900,1,31) / date(2100,1,10) |
| 换算越界 | `lunar_to_solar(1899, 1, 1)` | 抛 ValueError |
| once 年份匹配 | occurrence_date(once "2026-11-11", 2026) | date(2026,11,11) |
| once 年份不匹配 | occurrence_date(once, 2027) | None |
| yearly 农历 | occurrence_date(七夕, 2024) | date(2024, 8, 10) |
| next_occurrence once 过去 | today=2026-12-01, once 2026-11-11 | None |
| next_occurrence yearly 跨年 | today=2026-12-30, yearly "01-01" | date(2027,1,1) |
| next_occurrence yearly 今年 | today=2026-01-01, yearly "01-01" | date(2026,1,1) |
| daily_triggers celebrate | today=发生日, notify=1 | 1 条 kind=celebrate distance=0 |
| daily_triggers remind | today=提前 1 天, notify=1 | 1 条 kind=remind distance=1 |
| daily_triggers 已标记 | 预置触发标记 | 返回空（跳过） |
| notify=0 | today=发生日, notify=0 | 仅 celebrate，无 remind |
| is_anniversary | 有/无当日纪念日 | True / False |

---

## 4. S2 anniv_blessing.py（批次3）

```python
class AnnivBlessing:
    """祝福生成：主项目 LLM 注入 + 超时/失败模板兜底 + 风格（spec §3.3.3）。"""

    def __init__(self, cfg: AnnivConfig | None = None,
                 llm: Callable[[str], str] | None = None) -> None:
        # llm: prompt → 文案 的同步生成函数（主项目外壳注入）；None → 恒模板（测试/无模型）

    def build_prompt(self, title: str, style: str, today: date) -> str:
        """含"团子口吻、≤20 字"与风格约束（卖萌/深情）的提示词。"""

    def generate(self, title: str, style: str | None = None,
                 today: date | None = None) -> str:
        """返回非空祝福文案：
        - style 缺省 cfg.default_blessing_style；非法 → 回退 cute
        - llm None → 直接模板
        - llm 抛异常 / 超时（cfg.blessing_timeout 秒）/ 返回空白 → 模板兜底
        - 模板：BLESSING_TEMPLATES[style] 随机取一 .format(title=title)"""
```

- 超时实现：`ThreadPoolExecutor(max_workers=1)` + `fut.result(timeout)`，`finally: ex.shutdown(wait=False)`（慢线程后台结束，不阻塞调用方）。

### 4.1 断言级测试（test_anniv_blessing.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 无 LLM 模板 | `AnnivBlessing()` | generate 非空且含 title；等于某个模板渲染 |
| LLM 成功 | llm 返回"小团子爱你～" | generate 原样返回 |
| LLM 异常 | llm 抛 RuntimeError | 模板兜底，非空含 title |
| LLM 超时 | llm sleep(0.5)，cfg timeout=0.1 | 模板兜底（<2s 返回） |
| 风格切换 | cute / deep | 文案来自对应风格模板 |
| 非法风格 | style="x" | 回退 cute 模板 |
| 空返回 | llm 返回 "" | 模板兜底 |

---

## 5. S3 anniv_celebrate.py（批次3）

```python
# 互赠邀请钩子（§1.4，D35）
_GIFT_HOOK: Callable[[dict], None] | None = None
def set_gift_invite_hook(fn: Callable[[dict], None] | None) -> None: ...
def _fire_gift_invite(entry: dict) -> None: ...

class AnnivCelebrate:
    """当天庆祝 + 限时装扮 + date.remind 发送 + 互赠邀请信号。"""

    def __init__(self, db, sync, calendar: AnnivCalendar,
                 cfg: AnnivConfig | None = None,
                 blessing: AnnivBlessing | None = None,
                 on_remind: Callable[[dict, str], None] | None = None,
                 on_celebrate: Callable[[dict], None] | None = None) -> None:
        # sync: SyncManager 鸭子类型（仅 send / pairing_status / peer_id）
        # on_remind(entry, text)：预告回调（UI 弹窗）；on_celebrate(entry)：庆祝回调

    def daily_check(self, today: date | None = None) -> list[str]:
        """每日零点后调用。先 check_outfit_expiry(today)（过期回归，动作记 'outfit_expired'），
        再逐条处理 calendar.daily_triggers(today)：
        - remind  → text=blessing.generate(title)；on_remind(entry, text)；mark('remind')
        - celebrate → self._celebrate(entry, today, send_remind=True)；mark('celebrate')
        返回动作列表 ['outfit_expired'?, 'remind:<id>', 'celebrate:<id>', ...]（按处理顺序）。"""

    def _celebrate(self, entry: dict, today: date, *, send_remind: bool) -> None:
        """本端庆祝：
        1. 解锁限时装扮：kv['anniv:limited_outfit'] = {item_id: cfg.outfit_item_id,
           expire_at: _day_start(today + outfit_duration_days)}（D34，last-write-wins）
        2. on_celebrate(entry)
        3. _fire_gift_invite(entry)
        4. send_remind 且已配对 → sync.send(DATE_REMIND, {id,title,date,calendar})"""

    def handle_date_remind(self, m: Message) -> bool:
        """收到对方 date.remind → 本端一同庆祝（_celebrate(send_remind=False)，
        不反向发送）；payload 缺 id/title → False。"""

    def check_outfit_expiry(self, today: date | None = None) -> str | None:
        """限时装扮未过期/无 → None；过期 → 清除并返回原 item_id。"""
    def current_outfit(self) -> str | None:
        """kv anniv:limited_outfit 的 item_id；无 → None。"""
```

- `_day_start(d: date) -> int`：当日 00:00 本地时间戳（mktime，同 pet_streak 模式）。
- `date.remind` 接收端 `handle_date_remind` 直接挂 `mgr.add_handler(DATE_REMIND, ...)`，**不经 anniv_sync**（date.remind 属庆祝事件，归 celebrate 管辖；date.add 归 sync 管辖）。
- 每年同日限一次由触发标记保证（daily_check 路径）；对方 date.remind 为显式事件，重复处理幂等（重新解锁限时装扮，无害）。

### 5.1 断言级测试（test_anniv_celebrate.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 当天庆祝 | daily_check(today=发生日) | 返回含 celebrate:<id>；on_celebrate 触发；kv 限时装扮写入；date.remind 发送 1 条 |
| 限时装扮解锁 | 庆祝后 current_outfit() | == cfg.outfit_item_id |
| 预告 | daily_check(today=提前1天) | 返回含 remind:<id>；on_remind(entry, 非空文案)；不发送 date.remind |
| 每日仅一次 | 同日 daily_check ×2 | 第 2 次返回空列表（标记生效） |
| 接收 date.remind | handle_date_remind(合法) | 本端庆祝（限时装扮解锁 + on_celebrate）；sync 无新发送 |
| 非法 payload | handle_date_remind({}) | False；无副作用 |
| 未过期保持 | 庆祝后 check_outfit_expiry(当日) | None；current_outfit 仍值 |
| 过期回归 | 庆祝后 check_outfit_expiry(today+duration) | 返回 item_id；current_outfit()==None |
| gift 钩子 | set_gift_invite_hook 记录 | 庆祝时收到 entry 回调 |
| 未配对不发 | pairing_status()=="unpaired" | 庆祝不发 date.remind（限时装扮照常解锁） |

---

## 6. S4 anniv_sync.py（批次4）

```python
class AnnivSync:
    """date.add 双向同步（LWW）。未配对不外发。date.remind 归 AnnivCelebrate 管辖。"""

    def __init__(self, db, sync, store: AnnivStore, cfg: AnnivConfig | None = None) -> None: ...

    def add(self, entry: dict) -> dict:
        """store.add + 已配对 → 发送 date.add（全量，deleted=false）。返回规范 entry。"""
    def update(self, anniv_id: str, patch: dict, *, now: int | None = None) -> dict | None:
        """store.update（刷新 updated_at）+ 已配对发送 date.add。"""
    def delete(self, anniv_id: str, *, now: int | None = None) -> bool:
        """store.delete + 已配对发送 date.add（仅 id/deleted=true/updated_at，最小墓碑）。"""

    def handle_date_add(self, m: Message) -> bool:
        """接收 date.add 合并（LWW，D32）：
        - payload 缺 id / updated_at 非 int → False
        - payload.updated_at < 本地该 id updated_at → 忽略（旧事件）
        - deleted=true → store.delete（本地无该 id 则 no-op）
        - 否则 store.upsert(payload)（字段非法 → False，不落库）"""
```

- **删除墓碑最小化**：`delete` 发送 `{id, deleted:true, updated_at}` 三键即可，接收方不依赖 title/date 也能删。
- 未配对时 `add/update/delete` 照常写本地（spec §3.3.5"未配对时为纯本地"），仅不发送。

### 6.1 断言级测试（test_anniv_sync.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 配对新增发送 | pairing_status=paired, add | 本地有行 + 发送 date.add 全量 deleted=false |
| 未配对不外发 | pairing_status=unpaired, add | 本地有行 + 无发送 |
| 更新发送 | update(id, {title}) | 本地更新 + 发送 date.add 含新 updated_at |
| 删除墓碑 | delete(id) | 本地删除 + 发送 date.add deleted=true |
| LWW 新覆盖旧 | 本地 updated_at=100, 收到 200 | upsert 生效 |
| LWW 旧忽略 | 本地 updated_at=200, 收到 100 | 忽略，本地不变 |
| 墓碑 LWW | 本地 updated_at=100, 收到 deleted=true 200 | 本地删除 |
| 墓碑旧忽略 | 本地 updated_at=200, 收到 deleted=true 100 | 忽略，本地保留 |
| 非法 payload | 缺 id / updated_at 非 int / 字段非法 | False；本地不变 |

---

## 7. S5 anniv_integration.py（批次4）

```python
def register_anniversary_hook(calendar: AnnivCalendar) -> None:
    """注册 is_anniversary_today 到 pet-growth 钩子（D27 注入点）：
    _HOOK = lambda: calendar.is_anniversary(date.today())。重复注册以最后一次为准。"""

def unregister_anniversary_hook() -> None:
    """恢复缺省（lambda: False）。测试隔离 / demo 收尾用。"""
```

### 7.1 断言级测试（test_anniv_integration.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 注册生效 | 日历含当日纪念日，register | pet_growth.is_anniversary_today()==True |
| 无纪念日 | 空表，register | False |
| 注销恢复 | unregister | False |
| 加成联动 | 注册后 growth.award("feed") | 返回 6（3×2）；事件 delta=6 |

> 测试需 autouse fixture 每例后 `unregister_anniversary_hook()`，避免污染 pet-growth 全局钩子。

---

## 8. C1/C2：纪念日管理 UI 与联调脚本

### 8.1 C1 src/ui/anniv_panel.py

PySide6 条件导入 + HAS_QT 占位（同 `pet_panel.py` 模式）。**纯函数先行**（可单测）：

```python
def anniv_status_text(title: str, occurrence: date, days_left: int) -> str:
    """纪念日状态行（纯函数）：
    - days_left == 0 → f"{title}：就是今天 🎉"
    - days_left >  0 → f"{title}：还有 {days_left} 天"
    - occurrence 为 None → f"{title}：今年已过"""
```

`AnnivPanel(QWidget)`（HAS_QT 下）：纪念日列表 + 添加/编辑/删除表单（标题/日期/公历农历/重复/提前天数）、预告弹窗（祝福文案）、当天庆祝弹窗（限时装扮 + 互赠邀请按钮）。订阅 `on_remind` / `on_celebrate` 回调，Qt 信号队列 marshal 回 UI 线程。

### 8.2 C2 tools/anniv_demo.py

复用 pet_growth_demo 双端骨架（prewrite_identity + derive_mac_key + 配对）。一键演示并断言：

```text
双端配对 → A 添加"在一起 100 天"（solar/yearly/notify=1）
  → date.add 同步 → B list_all 一致
→ 模拟提前 1 天（daily_check(today=day_before)）→ 双端 remind 预告（on_remind 收到文案）
→ 模拟当天（daily_check(today=day0)）→ A 本端 celebrate + date.remind 同步 → B handle 一同庆祝
  → 双端限时装扮解锁；gift 钩子收到 entry
→ 钩子加成：register_anniversary_hook 后 growth_a.award("feed")==6（纪念日 ×2）
→ 添加农历"七夕"（lunar/yearly）→ lunar_to_solar 触发路径
→ 过期回归：daily_check(today=day0+duration) → check_outfit_expiry → current_outfit()==None
全程断言：date.add/date.remind payload 键纯净、双端纪念日列表 LWW 收敛一致
```

---

## 9. 本次实施范围与测试清单

| 交付 | 文件 | 测试 | 状态 |
|------|------|------|:----:|
| G1 | `src/core/anniv_config.py` + `anniv_store.py` | test_anniv_config.py + test_anniv_store.py | 已落地（批次1，18 单测） |
| S1 | `src/core/anniv_calendar.py` | test_anniv_calendar.py | 已落地（批次2，14 单测） |
| S2 | `src/core/anniv_blessing.py` | test_anniv_blessing.py | 已落地（批次3，8 单测） |
| S3 | `src/core/anniv_celebrate.py` | test_anniv_celebrate.py | 已落地（批次3，9 单测） |
| S4 | `src/core/anniv_sync.py` | test_anniv_sync.py | 已落地（批次4，9 单测） |
| S5 | `src/core/anniv_integration.py` | test_anniv_integration.py | 已落地（批次4，4 单测） |
| C1 | `src/ui/anniv_panel.py` | `anniv_status_text` 纯函数单测 | 已落地（批次5，3 单测） |
| C2 | `tools/anniv_demo.py` | anniv_demo 联调绿 | 已落地（批次5，全链路通过） |

---

## 10. 决策记录

| 编号 | 主题 | 决策 | 状态 |
|------|------|------|:----:|
| D31 | 日期存储格式 | once=`YYYY-MM-DD` / yearly=`MM-DD`（公历/农历同格式，由 calendar 字段区分）；yearly 农历按当年换算，换算可落次年（next_occurrence 与 today 比较兜底） | 已定 |
| D32 | 同步合并 | `date.add` 携带全量 + `deleted` 墓碑；LWW 以 `updated_at` 为准；删除发送最小墓碑 {id, deleted, updated_at} | 已定 |
| D33 | 每日触发标记 | kv `anniv:triggered:<YYYY-MM-DD>:<id>:<kind>`；daily_triggers 只读跳过，处理成功后才 mark（失败可重试）；notify=0 时仅 celebrate | 已定 |
| D34 | 限时装扮 | kv `anniv:limited_outfit` 单槽位 {item_id, expire_at=today+duration 日零点}；过期清除回归原始装扮 | 已定 |
| D35 | 互赠邀请 | 模块级 `set_gift_invite_hook`（gift-exchange 落地后注册）；缺省 no-op，不阻塞本模块 | 已定 |
| D36 | 祝福生成 | LLM 注入 Callable + 3s 超时（ThreadPoolExecutor）异常/超时/空白 → 风格模板兜底 | 已定 |
| D37 | 跨年提醒 | `next_occurrence` 取 ≥today 的最近发生日（yearly 当年已过取次年），自动覆盖 12-30 提醒 01-01 的跨年窗口 | 已定 |
| D38 | date.remind 归属 | date.remind 为庆祝事件归 `AnnivCelebrate` 管辖（发送/接收均在 celebrate），`AnnivSync` 只管 date.add；避免循环依赖 | 已定 |

---

## 11. 变更记录

| 日期 | 变更内容 | 变更人 |
|------|---------|--------|
| 2026-08-04 | 初始草稿：G1/S1-S5/C1/C2 契约定稿；确认 D31-D38；date.add/date.remind 线格式、日期格式与农历换算、每日触发扫描、祝福生成、限时装扮、LWW 同步、钩子注册对齐 spec/plan/04 号笔记 | 项目负责人 |
| 2026-08-04 | 全部落地：G1=18/S1=14/S2=8/S3=9/S4=9/S5=4/C1=3 单测 + C2 联调绿；anniv_demo 全链路（配对→date.add 同步→预告→当天庆祝双向→限时装扮过期→钩子×2→七夕农历换算→改名 LWW→墓碑删除→payload 纯净）通过；全量 337 passed 无回归 | 项目负责人 |
