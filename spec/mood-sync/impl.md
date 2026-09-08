# 情绪同步 实现规格（impl）

> 关联 Spec：[[7-AI学习/11-AI桌宠情侣互联/spec/mood-sync/spec.md|spec/mood-sync]]
> 关联 Plan：[[7-AI学习/11-AI桌宠情侣互联/spec/mood-sync/plan.md|plan/mood-sync]]
> 创建日期：2026-08-04
> 状态：已确认（G1/S1-S4 + C2 全部落地，C1 契约就绪待 Qt 外壳；全量 213 passed + mood_demo 联调绿）

## 目的

将 plan 的任务级 TODO（G1/S1-S4/C1/C2）细化为**可直接编码的契约**：接口签名、`mood.sync` 线格式、节流算法、表现映射表、隐身状态机、断言级测试用例。编码时不再做设计决策；若实现中必须偏离本文档，先更新本文档再编码（文档即契约）。

本模块是**最轻量的互联模块**：本端情绪系统（复用主项目 07）输出连续值 → 汇总为当前标签 → **节流判定**（显著变化 + 最小间隔）→ 加密发送 `mood.sync` → 对方解析后映射为团子空闲表现并更新状态条。隐私核心：**只同步三个数值（valence / arousal / label），绝不携带任何文本**；情绪标签不持久化，仅保留当前值（spec 行为 8）。

---

## 0. 目录结构与模块归属

```
src/core/
├── mood_config.py     ← plan G1  情绪标签枚举 + 表现映射 + 节流配置
├── mood_export.py     ← plan S1  本端情绪汇总与当前值维护（内存态）
├── mood_sender.py     ← plan S2  节流判定 + mood.sync 发送
├── mood_receive.py    ← plan S3  接收 + 表现映射（空闲优先）
├── mood_privacy.py    ← plan S4  隐身模式（最高优先级开关）
src/ui/
└── partner_state.py   ← plan C1  TA 状态条（与 sync-security C1 共用文件）
tools/
└── mood_demo.py       ← plan C2  情绪同步联调脚本
tests/core/
├── test_mood_config.py
├── test_mood_export.py
├── test_mood_sender.py
├── test_mood_receive.py
├── test_mood_privacy.py
└── test_mood_status_text.py
```

---

## 1. 全局约定

### 1.1 `mood.sync` 线格式（唯一外发信息）

**正常负载**（spec §3.2：仅含愉悦度、激活度、标签，不含任何文本）：

```json
{
  "valence": 0.8,     // float，-1.0（消极）~ +1.0（积极）
  "arousal": 0.6,     // float，0（平静）~ 1.0（激动）
  "label": "happy"    // str，MOOD_LABELS 之一
}
```

**隐身通知负载**（决策 D19，`privacy` 布尔字段；不含情绪三值）：

```json
{
  "privacy": true     // bool：开启隐身的瞬间通知；仅此一条，此后停止一切 mood.sync
}
```

- 事件类型 `mood.sync` 已在 sync-security 事件注册表登记（`EventType.MOOD_SYNC`）。
- 时间戳由 sync 层 `Message.ts` 承载，负载不冗余携带。
- 接收侧校验：负载含 `privacy == True` → 隐私通知（跳过情绪字段校验）；否则三字段均须合法（valence ∈ [-1.0, 1.0]、arousal ∈ [0, 1.0]、label ∈ MOOD_LABELS），任一非法 → 丢弃。

### 1.2 内存态与线程模型

- 情绪标签**不写数据库**（spec 行为 8）：本端当前情绪、对方当前情绪、隐身开关均为**进程内内存态**，新值覆盖旧值。
- 模块各实例在**单线程内使用**（UI 线程调 send/开关，sync 线程回调 handle）；跨线程临界点仅 UI 回调（`on_mood_change`），UI 侧自行 marshal 到 UI 线程（Qt 信号队列，同 sync-security impl §1.4 约定）。
- 时间源注入：节流判定接受 `now` 参数（测试可控），生产默认 `time.time()`。

### 1.3 与主项目边界

- **情绪来源**：复用主项目 07 情绪系统输出（`Emotion(valence, arousal, label)` 帧），本模块不实现识别算法。
- **动画表现**：本模块定义**抽象表现标识**（`idle_tired` 等，决策 D20），与主项目动画状态机解耦；主项目外壳接入层负责把抽象标识映射到具体动画状态。主项目动画未就绪时，以文字状态条降级验证。
- **软化文案**：状态条展示"最近标签"而非原文（如 sleepy → "TA 今天有点累"），映射表见 C1。

### 1.4 与 data-consistency 的衔接

- 发送经 `sync.send(EventType.MOOD_SYNC, payload)` 走 SyncManager：在线直发成功落 `events` 表（`status='sent'`，D17），离线入队补发。`mood.sync` 属普通事件，由 S3 prune 按保留期清理（非 pet.feed 不豁免）。
- **不建业务表**：情绪标签不持久化（spec 行为 8），events 表日志仅为 data-consistency 的统一事件审计，非情绪状态存储。

---

## 2. G1 mood_config.py（批次1）

### 2.1 情绪标签枚举

```python
# 8 标签（与主项目 07 EMOTION_MAP 完全一致，spec §3.1）
MOOD_LABELS = ("happy", "excited", "calm", "neutral", "sad", "angry", "confused", "sleepy")
```

### 2.2 标签 → 空闲动画表现映射（人工设计先行，决策 D20）

```python
# 抽象表现标识（主项目外壳接入层映射到具体动画状态机；M2 内测后按反馈调优）
MOOD_ANIMATION: dict[str, str] = {
    "happy":    "idle_happy",     # 摇尾巴、跳跃
    "excited":  "idle_excited",   # 跳跃、欢快
    "calm":     "idle_calm",      # 正常待机
    "neutral":  "idle_neutral",   # 正常待机
    "sad":      "idle_sad",       # 低头、尾巴垂下
    "angry":    "idle_angry",     # 耳朵后贴、警惕姿态
    "confused": "idle_confused",  # 歪头、左右张望
    "sleepy":   "idle_tired",     # 耷拉耳朵、趴着、无精打采
}
assert set(MOOD_ANIMATION) == set(MOOD_LABELS)   # 8 项全覆盖、无多余
```

### 2.3 节流与采样配置

```python
@dataclass(frozen=True)
class MoodConfig:
    threshold: float = 0.3       # 显著变化阈值：|Δvalence| 或 |Δarousal| ≥ 此值触发
    min_interval: float = 600.0  # 最小同步间隔（秒）：距上次发送 ≥ 此值才允许
    sample_period: float = 300.0 # 采样周期（秒）：主项目情绪系统输出频率（本模块不主动计时）
    stealth_default: bool = False  # 隐身默认关闭（spec §2.2 决策）

def load_mood_config(overrides: dict | None = None) -> MoodConfig:
    """配置加载：缺失键用默认值；overrides 仅覆盖给定键。
    非法值（threshold 非 0~1 的有限数 / min_interval ≤ 0）抛 ValueError。"""
```

- `threshold=0.3` / `min_interval=600` / `sample_period=300` 为已决议初始参数（spec 变更记录）。
- `sample_period` 为配置项，供主项目情绪系统接入侧设定输出频率；本模块侧（export/sender）不依赖它主动调度。

### 2.4 断言级测试（test_mood_config.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 标签全集 | MOOD_LABELS | 8 项，与主项目 07 EMOTION_MAP 键集一致 |
| 映射全覆盖 | MOOD_ANIMATION | `set(MOOD_ANIMATION) == set(MOOD_LABELS)`（无缺失/多余） |
| 默认配置 | `load_mood_config()` | threshold=0.3 / min_interval=600 / sample_period=300 / stealth_default=False |
| 部分覆盖 | `load_mood_config({"threshold": 0.5})` | threshold=0.5，其余为默认值 |
| 非法配置 | `load_mood_config({"threshold": 2.0})` | 抛 `ValueError` |

---

## 3. S1 mood_export.py（批次2）

### 3.1 契约

```python
@dataclass(frozen=True)
class Emotion:
    """本端情绪系统输出帧（复用主项目 07）。"""
    valence: float   # -1.0 ~ +1.0
    arousal: float   # 0 ~ 1.0
    label: str       # MOOD_LABELS 之一

class MoodExporter:
    """本端当前情绪（内存态，不持久化）。被动接收情绪系统输出帧，新值覆盖旧值。"""

    def __init__(self, config: MoodConfig | None = None) -> None:
        # self._current: Emotion | None = None

    def update(self, emotion: Emotion) -> bool:
        """接收一帧输出。label 非法或数值越界 → 返回 False，current 不变；
        合法 → 覆盖 current，返回 True。"""
    def current(self) -> Emotion | None:
        """当前情绪；未接收过合法帧返回 None。"""
```

- **不写库、无 db 依赖**（spec 行为 8）。
- 数值范围校验与接收侧一致（valence ∈ [-1.0, 1.0]、arousal ∈ [0, 1.0]），保证本端不会对外发出非法负载。
- 采样周期由主项目情绪系统侧控制（每 `sample_period` 输出一帧汇总）；本类被动接收，不自行计时。

### 3.2 断言级测试（test_mood_export.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 初始为空 | 新实例 | `current() is None` |
| 覆盖更新 | update(happy) → update(sleepy) | current 为 sleepy（新值覆盖旧值） |
| 非法 label | update(Emotion(0.8, 0.6, "love")) | 返回 False；current 不变 |
| 数值越界 | update(Emotion(1.5, 0.6, "happy")) | 返回 False；current 不变 |
| 无持久化 | 全程 | 无任何 DB/文件写入（无 db 依赖，纯内存） |

---

## 4. S2 mood_sender.py（批次3）

### 4.1 节流判定算法（spec §3.3.2 / 03 笔记 §二）

```text
maybe_send(emotion, now):
  1. 隐身中（is_stealth() 为 True）→ 不发送，返回 False      ← 最高优先级（spec §3.4）
  2. now - last_sync_ts < min_interval → 不发送，返回 False    ← 最小间隔
  3. last_sent 存在 且 |Δvalence| < threshold 且 |Δarousal| < threshold
       → 不发送，返回 False                                   ← 无显著变化
  4. 通过 → 发送 payload（仅三字段）+ 记录 last_sent/last_sync_ts → 返回 True
  首次（last_sent 为 None）：跳过第 3 步，直接发送（决策 D21，03 笔记伪代码同源）
```

### 4.2 契约

```python
class MoodSender:
    """节流判定 + mood.sync 发送。内存态记录上次发送值/时间（不持久化）。"""

    def __init__(self, sync, config: MoodConfig | None = None,
                 is_stealth: Callable[[], bool] | None = None) -> None:
        # sync: SyncManager 鸭子类型（仅 send），不依赖具体类型
        # is_stealth: 查询隐身状态的回调（由 S4 MoodPrivacy 提供），缺省恒 False

    def maybe_send(self, emotion: Emotion, now: float | None = None) -> bool:
        """按 4.1 算法节流判定并发送。发送负载 = {"valence", "arousal", "label"} 三字段。"""
    def reset(self) -> None:
        """清空 last_sent / last_sync_ts（换机/重配对场景可选调用，下次发送视为首次）。"""
```

- 发送调用 `sync.send(EventType.MOOD_SYNC, payload)`（`EventType` 自 `sync.events` 导入）。
- **负载断言点**：payload 键集合恰为 `{"valence", "arousal", "label"}`，绝不携带文本/上下文。

### 4.3 断言级测试（test_mood_sender.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 首次发送 | last_sent=None，now=0 | 返回 True；发送 payload 三字段 |
| 无显著变化 | 间隔够、|Δvalence|=0.1、|Δarousal|=0.2 | 返回 False；无发送 |
| 恰好阈值 | |Δvalence|=0.3 | 返回 True（≥ 边界触发） |
| 间隔不足 | 显著变化但 now-last<600 | 返回 False |
| 恰好间隔 | now-last=600 且显著变化 | 返回 True |
| 隐身中 | is_stealth 返回 True | 返回 False；无发送 |
| 负载纯净 | 触发发送 | 断言 payload 键 == {valence, arousal, label}，无任何文本键 |
| 发送后记录 | 触发发送 | 下次同值（Δ<0.3）不再发送；reset 后恢复首次语义 |

---

## 5. S3 mood_receive.py（批次4）

### 5.1 契约

```python
@dataclass(frozen=True)
class MoodEvent:
    """UI 回调负载：对方情绪 + 隐身状态。"""
    emotion: Emotion | None   # 对方当前情绪（最近标签）；privacy 通知时不更新
    stealth: bool             # 对方是否隐身

class MoodReceiver:
    """接收 mood.sync → 更新对方情绪内存态 + 表现覆盖 + UI 回调。"""

    def __init__(self, on_mood_change: Callable[[MoodEvent], None] | None = None) -> None:
        # on_mood_change: UI 回调（Qt 信号队列 marshal 由 UI 侧负责）

    def handle(self, payload: dict) -> bool:
        """处理一条 mood.sync（§1.1 校验规则）。非法 → 返回 False。
        - privacy 通知：partner_stealth=True，**不更新 partner_mood**（保留最近标签，D19）
        - 情绪帧：partner_mood=emotion，partner_stealth=False
        两者均触发 on_mood_change(MoodEvent(...))。"""
    def partner_mood(self) -> Emotion | None: ...
    def partner_stealth(self) -> bool: ...
    def clear_partner_state(self) -> None:
        """清空对方状态（partner_mood=None、partner_stealth=False）并触发回调——
        本端开启隐身时调用（spec §3.3.6：清除本端已展示的对方状态）。"""
    def animation_override(self, local_active: bool = False) -> str | None:
        """返回应覆盖本端团子空闲态的抽象动画标识：
        - 对方隐身 → 保留最近标签映射（MOOD_ANIMATION[last label]），表现稳定不闪烁（D19）
        - 无标签 / 对方无情绪 → None
        - local_active=True（本地交互进行中）→ None（本地交互优先，spec §3.3.4）"""
```

- **隐私通知语义（D19）**：对方隐身不改变已展示的最近标签（避免界面闪烁），仅状态条叠加"TA 开启了隐身"提示；下次收到正常情绪帧时 `partner_stealth` 复位为 False。
- 表现映射仅查 `MOOD_ANIMATION`（G1），本类不依赖主项目动画状态机。

### 5.2 断言级测试（test_mood_receive.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 8 标签映射完整 | 逐 label handle 正常帧 | `animation_override()` 返回对应抽象标识，且非 None |
| 收到 happy | handle({happy 帧}) | partner_mood.label=="happy"；override=="idle_happy" |
| 本地交互优先 | local_active=True | override 返回 None（不覆盖） |
| 无标签初始 | 新实例 | partner_mood is None；override 返回 None |
| privacy 通知 | handle({"privacy": True}) | partner_stealth=True；partner_mood 保留原值 |
| 通知后标签保留 | 先 happy 帧 → privacy | override 仍 == "idle_happy"（保留最近表现） |
| 非法负载 | handle({"valence": "x"}) | 返回 False；状态不变 |
| 清除状态 | clear_partner_state() | partner_mood is None；partner_stealth False；触发回调 |
| 回调负载 | 收到帧 / 通知 | on_mood_change 收到 MoodEvent(emotion, stealth) 正确 |
| 情绪帧复位隐身 | privacy 后接正常帧 | partner_stealth 复位 False |

---

## 6. S4 mood_privacy.py（批次5）

### 6.1 隐身状态机与通知（最高优先级，spec §3.3.6 / 决策 D19）

```text
开启（set_stealth(True)）：
  1. 置本端 stealth=True            → 一切 mood.sync 发送停止（Sender 查询 is_stealth 拦截）
  2. 发送一条 mood.sync {"privacy": true}   → 对方显示"TA 开启了隐身"（保留最近标签）
  3. receiver.clear_partner_state()  → 清除本端已展示的对方状态（spec：本端也不再看对方状态）

关闭（set_stealth(False)）：
  1. 置本端 stealth=False（恢复节流发送）
  2. 不发任何消息；对方端最近标签保持，直到下一次正常 mood.sync 覆盖（D19：避免界面闪烁）
```

### 6.2 契约

```python
class MoodPrivacy:
    """本端隐身开关（内存态）。开启即停发 + 清本端对方状态 + 发 privacy 通知。"""

    def __init__(self, sync, receiver: MoodReceiver) -> None:
        # sync: 发送 privacy 通知用（SyncManager 鸭子类型）
        # receiver: 清除本端对方状态用

    @property
    def is_stealth(self) -> bool: ...        # 供 MoodSender 查询
    def set_stealth(self, enabled: bool) -> None:
        """按 6.1 状态机执行。开启重复调用幂等；关闭重复调用幂等。"""
```

> 接线提示：`is_stealth` 为**属性**（求值为 bool，非方法）。Sender 构造时以
> `is_stealth=lambda: privacy.is_stealth` 包装为查询回调（C1/C2 接线同此）。

### 6.3 断言级测试（test_mood_privacy.py）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 默认关闭 | 新实例 | is_stealth 为 False（配置 stealth_default） |
| 开启置位 | set_stealth(True) | is_stealth True |
| 发送 privacy 通知 | set_stealth(True) | sync 收到一条 MOOD_SYNC，payload == {"privacy": True}，无情绪键 |
| 清除本端对方状态 | set_stealth(True) | receiver.partner_mood is None；回调已触发 |
| 开启幂等 | 连续两次 set_stealth(True) | 仅发一条 privacy 通知 |
| 关闭复位 | set_stealth(False) | is_stealth False；无任何发送 |
| 与 Sender 联动 | 隐身中 maybe_send | 返回 False 不发送；关闭后恢复正常 |

---

## 7. C1/C2：状态条 UI 与联调脚本

> 状态：C1 已落地（HAS_QT 占位 + 可单测纯函数）；Qt 控件实例化随主项目外壳接入。

### 7.1 C1 partner_state.py

与 sync-security C1 共用文件（sync-security impl 已注明：连接/配对区 vs 情绪区职责不重叠）。本模块负责**情绪标签子区域**：

- **软化文案映射**（只显示标签软化文案，不显示原文）：

```python
MOOD_SOFT_TEXT: dict[str, str] = {
    "happy":    "TA 心情不错",
    "excited":  "TA 有点兴奋",
    "calm":     "TA 很平静",
    "neutral":  "TA 状态一般",
    "sad":      "TA 有点难过",
    "angry":    "TA 好像生气了",
    "confused": "TA 有点困惑",
    "sleepy":   "TA 今天有点累",
}
```

- 展示逻辑：`partner_mood` 非空 → 显示 `MOOD_SOFT_TEXT[label]`；`partner_stealth` → 叠加/替换为"TA 开启了隐身"；两者皆空 → 占位（"—"）。
- 订阅 `MoodReceiver.on_mood_change` → Qt 信号队列 marshal 回 UI 线程（不阻塞主线程，plan C1 验收）。
- PySide6 条件导入 + HAS_QT 占位（同 `settings_dialog.py` 模式）。

### 7.2 C2 tools/mood_demo.py

双端装配复用 consistency_demo 模式（init_core + SyncManager + 配对）。一键演示并断言：

```text
A happy（首次发送）→ B 收到，partner_mood=happy，override="idle_happy"
A sleepy（显著变化，间隔满足）→ B 收到，override="idle_tired"，状态条软化文案
A 无显著变化（节流）→ 不发送
A 开启隐身 → B 收到 {"privacy": true}，partner_stealth=True，标签保留
A 关闭隐身 → B 保持最近标签；A 再次发送 happy → B partner_stealth 复位
全程断言：payload 仅三字段、节流边界（0.3 / 600s）正确、隐身停发生效
```

---

## 8. 本次实施范围与测试清单

| 交付 | 文件 | 测试 | 状态 |
|------|------|------|:----:|
| G1 | `src/core/mood_config.py` | test_mood_config.py（10 用例） | ✅ 已实施（批次1） |
| S1 | `src/core/mood_export.py` | test_mood_export.py（7 用例） | ✅ 已实施（批次2） |
| S2 | `src/core/mood_sender.py` | test_mood_sender.py（10 用例） | ✅ 已实施（批次3） |
| S3 | `src/core/mood_receive.py` | test_mood_receive.py（11 用例） | ✅ 已实施（批次4） |
| S4 | `src/core/mood_privacy.py` | test_mood_privacy.py（7 用例） | ✅ 已实施（批次5） |
| C1 | `src/ui/partner_state.py` | test_mood_status_text.py（4 用例） | 已实施；Qt 控件随外壳接入 |
| C2 | `tools/mood_demo.py` | mood_demo 联调绿 | ✅ 已实施（批次6） |

- 全量 213 passed（core 119 + sync 45 + mood 49）无回归；`mood_demo` 双端联调绿
  （happy 首次 → sleepy 显著变化 → 节流拦截 → 开启隐身 → 关闭恢复，payload 纯净断言通过）。

---

## 9. 决策记录与待确认项

| 编号 | 主题 | 决策 | 状态 |
|------|------|------|:----:|
| D19 | 隐身通知机制 | 开启隐身瞬间发一条 `mood.sync {"privacy": true}`（不含情绪三值）；对方显示"TA 开启了隐身"并**保留最近标签**（表现稳定不闪烁）；关闭隐身不发，下次正常 mood.sync 覆盖并复位对方隐身标记。同时按 spec 清除本端已展示的对方状态 | 已确认（用户决策） |
| D20 | 表现标识抽象化 | 本模块定义独立**抽象动画标识**（`idle_tired` 等），与主项目动画状态机解耦；主项目外壳接入层负责映射到具体动画；主项目动画未就绪时以文字状态条降级 | 已确认（用户决策） |
| D21 | 首次发送语义 | 配对后首次（last_sent=None）跳过显著变化判定直接发送（03 笔记伪代码同源：`if LAST_SENT and ...`） | 已定 |
| D22 | mood.sync 落 events 表 | 经 sync 层发送/接收，在线直发落 events 表日志（data-consistency D17 机制）；**不建业务表**，情绪标签仅内存当前值（spec 行为 8）；普通事件按保留期 prune 清理 | 已定 |

---

## 10. 变更记录

| 日期 | 变更内容 | 变更人 |
|------|---------|--------|
| 2026-08-04 | 初始草稿：G1/S1-S4/C1/C2 契约定稿；确认 D19（privacy 通知）/D20（抽象表现标识）；mood.sync 线格式（正常负载 + privacy 通知）与节流算法对齐 03 笔记/plan 决策 | 项目负责人 |
| 2026-08-04 | 实施落地：G1/S1-S4 + C2 全部实现（49 新增单测，全量 213 passed）；C1 partner_state 契约落地（HAS_QT 占位 + `mood_status_text` 纯函数）；`mood_demo` 双端联调绿；记录接线提示（`is_stealth` 属性需 `lambda` 包装） | 项目负责人 |
