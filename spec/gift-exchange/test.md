# 互赠礼物 测试报告

> 关联 Spec：`spec/gift-exchange/spec.md`
> 关联 Plan：`spec/gift-exchange/plan.md`
> 测试日期：2026-09-08
> 测试环境：Linux 6.12 / Python 3.x / `code/.venv` / pytest / 本机双端 LAN 模拟（SyncManager）

## 测试总结

| 指标           | 数值 |
| -------------- | ---- |
| 单元测试总数   | 49   |
| 单元测试通过   | 49   |
| 单元测试失败   | 0    |
| 集成测试总数   | 7（gift_demo 场景 + 6 既有双端 demo 回归） |
| 集成测试通过   | 7    |
| 集成测试失败   | 0    |
| 总体通过率     | 100% |
| 全量 pytest    | 386 passed |

## 单元测试详情

### ✅ 通过的测试

| 测试文件 | 用例数 | 对应 TODO | 覆盖要点 |
| -------- | ------ | --------- | -------- |
| `tests/core/test_gift_config.py` | 11 | G1 | manifest 17 款、空/损坏兜底、配置覆盖、状态枚举、彩蛋校验、unlock 签验、expire_at |
| `tests/core/test_gift_store.py` | 11 | S1 | CRUD/幂等、sent→accepted/expired、非法流转、pending 乐观解锁、到期移除、彩蛋 20 上限替换 |
| `tests/core/test_gift_send.py` | 7 | S2 | 取消不落库、confirm 发 gift.send+乐观解锁、无 propose、彩蛋长度、非彩蛋带文本拒、未配对 |
| `tests/core/test_gift_receive.py` | 7 | S3 | 收件箱、accept 解锁+accept 事件+亲密度、伪造拒、later、handle_accept、彩蛋上限、幂等 |
| `tests/core/test_gift_expire.py` | 4 | S4 | 24h 边界（严格 <）、远端 expire、items tick、已 accepted 不扫 |
| `tests/core/test_gift_intimacy.py` | 4 | S5 | +5 / 纪念日 +20、双方各一次、本地合入 |
| `tests/core/test_gift_dialog.py` | 5 | C1 | 分组、确认/接收文案、状态标签、彩蛋提示 |

### ❌ 失败的测试

无。

## 集成 / 场景验证详情

### 场景 1：gift_demo 全链路

- **操作步骤**：双端配对 → A 送 outfit-heart-100 → B 打开 → 亲密度 +5 → 短 TTL + 注入时钟过期退回 → 重发 → 纪念日钩子 +20 → 自定义彩蛋 → user_items 到期清理 → propose 后 cancel
- **期望结果**：各步 PASS；双端状态一致；最终亲密度 35/35；礼物库 17 款
- **实际结果**：✅ 符合预期
- **证据**：
```
PASS 配对成功
PASS 双方解锁 + 亲密度 A=5 B=5
PASS 过期退回两端一致
PASS 重发并接受成功
PASS 纪念日 +20（A 10→30, B 10→30）
PASS 彩蛋互送（B 记忆库 1 条）
PASS 到期物品清理
PASS 取消不产生记录
=== 全部断言通过 ===
最终亲密度 A=35 B=35；礼物库 17 款
```

### 场景 2–7：既有双端 demo 回归

| Demo | 结果 |
|------|------|
| `tools/sync_demo.py` | ✅ PASS |
| `tools/carry_demo.py` | ✅ PASS |
| `tools/mood_demo.py` | ✅ PASS |
| `tools/pet_growth_demo.py` | ✅ PASS |
| `tools/anniv_demo.py` | ✅ PASS |
| `tools/consistency_demo.py` | ✅ PASS（对齐前等待传输 connected，消除换机重连竞态） |

### 全量单元

```
cd code && ../code/.venv/bin/python -m pytest -q
# 386 passed
```

## 未覆盖的测试场景

- Qt 真机弹窗动画 / 主项目 07 动画状态机接入（C1 仅纯函数 + HAS_QT 占位，随外壳）
- 离线队列中途 gift.send 补发的专项用例（依赖 sync/data-consistency 既有离线能力，demo 未单独造断网礼物场景）
- 抓包密文断言（与其他模块一致，信任 sync SecretBox）

## 遗留问题

- `anniv_celebrate.set_gift_invite_hook` 尚未由 gift 模块默认注册（外壳可接；plan 允许无纪念日时普通 +5）
- 彩蛋「主项目记忆库」以本地 `user_items` 列表模拟，待外壳接真实记忆系统
- `consistency_demo` 换机后对齐依赖传输连通；已加 connected 等待，极端端口占用仍可能超时
