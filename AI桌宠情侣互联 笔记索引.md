---
tags:
  - AI
  - 桌宠
  - 情侣场景
  - 索引
created: 2026-08-01
---

# AI桌宠情侣互联 笔记索引

> 情侣双端互联桌宠——在单机桌宠（[[7-AI学习/10-AI桌宠实战/AI桌宠实战 笔记索引.md\|10-AI桌宠实战]]）基础上，为情侣关系提供**可选**互联模式。双端各自本地推理，通过端到端加密通道同步五类轻量事件。定位：关系的连接器，不是 AI 恋人。

---

## 项目定位

```text
✅ 做：情侣间的"连接器 / 催化器"
  - 两个人各养一只团子，帮忙传话、传递情绪、一起成长
  - 提供关系中的仪式感与共同记忆

❌ 不做：AI 恋人（替代真人）、人设模仿 TA
数据边界：聊天/语音/模型永不上传；只同步五类密文事件
```

---

## 工作流总览

```text
┌──────────────────────────────────────────────────────────┐
│ 01-总体方案与架构                                          │
│   定位边界 · 系统拓扑 · 设计原则 · 模块划分                │
├──────────────────────────────────────────────────────────┤
│ 02-同步层与端到端加密（基础，先行）                        │
│   配对密钥 · 消息协议 · 中继部署                           │
│   ├─ 03-带话与情绪同步实现                                │
│   ├─ 04-共同养成与纪念日实现                              │
│   └─ 05-互赠与界面交互实现                                │
├──────────────────────────────────────────────────────────┤
│ 06-数据模型与离线一致性                                    │
│ 07-里程碑与可行性验证                                      │
└──────────────────────────────────────────────────────────┘
```

---

## 笔记目录

| 文件 | 内容 |
|------|------|
| [[7-AI学习/11-AI桌宠情侣互联/01-总体方案与架构.md\|01-总体方案与架构]] | 定位边界、系统拓扑、设计原则、模块划分、与主项目衔接 |
| [[7-AI学习/11-AI桌宠情侣互联/02-同步层与端到端加密.md\|02-同步层与端到端加密]] | X25519 配对、secretbox 加密、消息协议、中继/局域网直连 |
| [[7-AI学习/11-AI桌宠情侣互联/03-带话与情绪同步实现.md\|03-带话与情绪同步实现]] | 带话意图检测与流程、情绪标签导出与节流 |
| [[7-AI学习/11-AI桌宠情侣互联/04-共同养成与纪念日实现.md\|04-共同养成与纪念日实现]] | 亲密度积分、成长系统、纪念日提醒与祝福 |
| [[7-AI学习/11-AI桌宠情侣互联/05-互赠与界面交互实现.md\|05-互赠与界面交互实现]] | 虚拟礼物模型、双端 UI、通知与动画 |
| [[7-AI学习/11-AI桌宠情侣互联/06-数据模型与离线一致性.md\|06-数据模型与离线一致性]] | SQLite schema、事件去重、冲突合并、换机恢复 |
| [[7-AI学习/11-AI桌宠情侣互联/07-里程碑与可行性验证.md\|07-里程碑与可行性验证]] | M1-M4 阶段规划、内测方案、风险与边界 |
| [[7-AI学习/11-AI桌宠情侣互联/08-美术资源规划.md\|08-美术资源规划]] | 素材包目录结构、礼物图标占位生成、宠物形象与动画排期 |
| [[7-AI学习/11-AI桌宠情侣互联/环境准备.md\|环境准备]] | Windows 优先开发环境：Python/依赖/模型/联调指南/工程迁移 |

---

## 需求规格（spec）

> 按模块拆解的需求规格说明，供开发实施与验收对照。每个 spec 含概述、用户故事、功能需求、非功能需求、边界、验收标准、开放问题。

| 模块 | spec | 里程碑 | 状态 |
|------|------|:------:|:----:|
| 同步层与端到端加密 | [[7-AI学习/11-AI桌宠情侣互联/spec/sync-security/spec.md\|spec/sync-security]] | M2 | 已确认 |
| 带话 | [[7-AI学习/11-AI桌宠情侣互联/spec/carry-message/spec.md\|spec/carry-message]] | M2 | 已确认 |
| 情绪同步 | [[7-AI学习/11-AI桌宠情侣互联/spec/mood-sync/spec.md\|spec/mood-sync]] | M2 | 已确认 |
| 共同养成 | [[7-AI学习/11-AI桌宠情侣互联/spec/pet-growth/spec.md\|spec/pet-growth]] | M3 | 已确认 |
| 纪念日 | [[7-AI学习/11-AI桌宠情侣互联/spec/anniversary/spec.md\|spec/anniversary]] | M3 | 已确认 |
| 互赠礼物 | [[7-AI学习/11-AI桌宠情侣互联/spec/gift-exchange/spec.md\|spec/gift-exchange]] | M3 | 已确认 |
| 数据模型与离线一致性 | [[7-AI学习/11-AI桌宠情侣互联/spec/data-consistency/spec.md\|spec/data-consistency]] | M2-M3 | 已确认 |

> 开发顺序建议：sync-security → carry-message → mood-sync → data-consistency → pet-growth → anniversary → gift-exchange。

---

## 实现规格（impl）

> 将已确认 plan 细化为**可直接编码的契约**（接口签名、数据结构、线格式、伪代码、断言级测试）。试点验证产出质量后再推广。

| 模块 | impl | 状态 |
|------|------|:----:|
| 同步层与端到端加密 | [[7-AI学习/11-AI桌宠情侣互联/spec/sync-security/impl.md\|impl/sync-security]] | 已确认（试点） |
| 带话 | [[7-AI学习/11-AI桌宠情侣互联/spec/carry-message/impl.md\|impl/carry-message]] | 已确认 |
| 数据模型与离线一致性 | [[7-AI学习/11-AI桌宠情侣互联/spec/data-consistency/impl.md\|impl/data-consistency]] | 已确认（G1/G2+S1-S6+C1/C2 全部落地） |
| 情绪同步 | [[7-AI学习/11-AI桌宠情侣互联/spec/mood-sync/impl.md\|impl/mood-sync]] | 已确认（G1/S1-S4+C2 落地，C1 待 Qt 外壳） |
| 共同养成 | [[7-AI学习/11-AI桌宠情侣互联/spec/pet-growth/impl.md\|impl/pet-growth]] | 已确认（G1/S1-S5/C1/C2 全部落地） |
| 纪念日 | [[7-AI学习/11-AI桌宠情侣互联/spec/anniversary/impl.md\|impl/anniversary]] | 已确认（G1/S1-S5/C1/C2 全部落地） |
| 互赠礼物 | [[7-AI学习/11-AI桌宠情侣互联/spec/gift-exchange/impl.md\|impl/gift-exchange]] | 已确认（G1/S1-S5/C1/C2 全部落地） |

---

## 实施计划（plan）

> 基于已确认 spec 的可执行实施计划，含技术决策、任务拆解（TODO）、测试标准与风险。按开发顺序逐份推进。

| 模块 | plan | 状态 |
|------|------|:----:|
| 同步层与端到端加密 | [[7-AI学习/11-AI桌宠情侣互联/spec/sync-security/plan.md\|plan/sync-security]] | 已确认 |
| 带话 | [[7-AI学习/11-AI桌宠情侣互联/spec/carry-message/plan.md\|plan/carry-message]] | 已确认 |
| 情绪同步 | [[7-AI学习/11-AI桌宠情侣互联/spec/mood-sync/plan.md\|plan/mood-sync]] | 已确认 |
| 数据模型与离线一致性 | [[7-AI学习/11-AI桌宠情侣互联/spec/data-consistency/plan.md\|plan/data-consistency]] | 已确认 |
| 共同养成 | [[7-AI学习/11-AI桌宠情侣互联/spec/pet-growth/plan.md\|plan/pet-growth]] | 已确认 |
| 纪念日 | [[7-AI学习/11-AI桌宠情侣互联/spec/anniversary/plan.md\|plan/anniversary]] | 已确认 |
| 互赠礼物 | [[7-AI学习/11-AI桌宠情侣互联/spec/gift-exchange/plan.md\|plan/gift-exchange]] | 已确认 |

---

## 状态速览

```text
技术可行性：高（≈80%，无未知技术瓶颈）
产品可行性：中（需求真实性待验证）
当前阶段：需求规格 7/7 已确认；实施计划 7/7 已确认；实现规格 7/7 已确认（sync-security 试点 + carry-message + data-consistency + mood-sync + pet-growth + anniversary + gift-exchange 全部落地）
代码：sync-security 全部落地（code/src/sync）——40 单测通过 + sync_demo 全流程联调绿（配对→互发→离线补发→吊销）；data-consistency 全部落地（G1/G2 + S1-S6 + C1/C2：core.db 基线 8 表 + 迁移框架 + 数据库操作层 + queue 收编：events 表统一承载发送队列/接收去重、在线直发落表（D17）、seq 游标键控本端（D18）、旧 sync_queue.db 代码层幂等迁移、SyncManager 注入 db + 事件日志清理 prune.py（pet.feed 豁免）+ 冲突合并/亲密度重放 consistency.py：pet.feed 线格式定稿 delta/reason/ts/sig、apply_intimacy_event 验签/每日上限/熔断、merge_lww LWW + 每日兜底对齐 daily_sync.py：date.sync 快照 streak/intimacy/ts、重放核对修复、断网推迟 + 换机/重装恢复 backup_restore.py：scrypt 口令派生 + SecretBox 加密三表快照、先解密成功才写库 + 设置服务层 settings_service/settings_dialog：口令强度、导出/导入流程（恢复后对齐仅快照）、手动同步）——全量 164 passed（core 119 + sync 45）无回归 + consistency_demo 全链路联调绿（配对→互发→去重→离线补发→重放收敛→兜底修复→prune→对齐→备份→换机恢复→重配对→前向同步）；carry-message 全部落地（carry_store/carry_intent/carry/carry_receive + carry_demo）——40 单测 + 双端联调绿；mood-sync 全部落地（mood_config/mood_export/mood_sender/mood_receive/mood_privacy + partner_state 情绪子区域 + mood_demo）——49 单测（全量 213 passed 无回归）+ 双端联调绿（happy 首次→sleepy 显著变化→节流拦截→开启隐身→关闭恢复，payload 仅三字段纯净断言）；pet-growth 全部落地（pet_config/pet_growth/pet_sync/pet_level/pet_streak/pet_profile + pet_panel 成长面板纯函数 + pet_growth_demo）——59 单测（G1=10/S1=11/S2=7/S3=9/S4=10/S5=9/C1=3）+ 双端联调绿（喂食×5+超上限拒绝→带话 5min 门控→伪造签名拒绝→批量经验升级 10 级解锁 5 项→每日结算 streak 单生成方→灰色期转灰与恢复→改名同步→形象预设仅本端生效→payload 四键纯净+双端收敛）；anniversary 全部落地（anniv_config/anniv_store/anniv_calendar/anniv_blessing/anniv_celebrate/anniv_sync/anniv_integration + anniv_panel 状态行纯函数 + anniv_demo）——65 单测（G1=18/S1=14/S2=8/S3=9/S4=9/S5=4/C1=3）+ 双端联调绿（配对→date.add 同步 B 一致→提前 1 天双端预告→当天双端庆祝 + date.remind 双向送达→限时装扮解锁与过期回归→钩子×2 喂食 +6→农历七夕换算当年公历触发→改名 LWW 覆盖→墓碑删除→date.add/date.remind payload 键纯净 + 双端 LWW 收敛）；gift-exchange 全部落地（gift_config/gift_store/gift_send/gift_receive/gift_expire/gift_intimacy + gift_dialog 纯助手 + gift_demo）——49 单测（G1=11/S1=11/S2=7/S3=7/S4=4/S5=4/C1=5） + 双端联调绿（配对→送 outfit 接受解锁+亲密度+5→短 TTL 过期退回→重发→纪念日+20→彩蛋→item 到期清理→取消不落库）；全量 386 passed 无回归；待 carry C1/C2 与 mood/gift C1 Qt 控件（随主项目外壳接入）；M3 模块已全部落地

前置依赖：主线单机桌宠 MVP 完成（10-AI桌宠实战 01-10）
启动条件：周留存 > 30%（M2 内测验证后）
```

---

## 相关笔记

| 主题 | 笔记 |
|------|------|
| 主项目 | [[7-AI学习/10-AI桌宠实战/AI桌宠实战 笔记索引.md\|AI桌宠实战 索引]] |
| 场景调研 | [[7-AI学习/10-AI桌宠实战/13-竞品与应用场景调研.md\|竞品与应用场景调研]] |
| 系统架构 | [[7-AI学习/10-AI桌宠实战/04-系统架构设计.md\|系统架构设计]] |
| 情绪系统 | [[7-AI学习/10-AI桌宠实战/07-角色动画与情绪系统.md\|角色动画与情绪系统]] |
| 语音交互 | [[7-AI学习/10-AI桌宠实战/05-语音交互与感知.md\|语音交互与感知]] |
