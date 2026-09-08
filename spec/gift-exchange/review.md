# 互赠礼物 代码审查报告

> 关联 Spec：`spec/gift-exchange/spec.md`
> 关联 Plan：`spec/gift-exchange/plan.md`
> 审查日期：2026-09-08
> 审查范围：`code/src/core/gift_*.py`、`code/src/ui/gift_dialog.py`、`code/tools/gift_demo.py`、`code/tests/core/test_gift_*.py`、`spec/gift-exchange/impl.md`

## 审查总结

互赠模块按 plan 六批 TODO 完整落地，线格式与既有 sync 测试载荷对齐，解锁凭证复用会话 MAC 键 HMAC，亲密度走 `apply_intimacy_event` 且刻意不发 `pet.feed` 避免双计，数据层复用 0001 基线表。49 单测 + gift_demo 全绿，全量 386 passed 无回归。达到可合入标准；无 P0。遗留主要为 Qt 外壳与记忆系统接入（明确 Out of Scope / 占位）。

## P0 - 必须修复（阻塞性问题）

无。

## P1 - 建议修复（重要但不阻塞）

### [P1-1] GiftReceive 直接访问 `store._db` / kv 签名缓存
- **类型**：封装泄漏 / 潜在维护风险
- **位置**：`code/src/core/gift_receive.py`（`_db_set_sig` / `_db_get_sig`）、`gift_expire.py`（清理 `gift:sig:`）
- **描述**：解锁签名缓存在 kv，经 `GiftStore` 私有 `_db` 读写，未收敛到 store API。
- **建议**：在 `GiftStore` 增加 `set_unlock_sig` / `get_unlock_sig` / `clear_unlock_sig`，接收与过期模块只调 store。

### [P1-2] 发送端彩蛋在 handle_accept 才写入本端记忆库
- **类型**：潜在产品语义差
- **位置**：`code/src/core/gift_send.py`（confirm 不对 custom_egg 写 pending）、`gift_receive.py`（`_persist_unlock`）
- **描述**：spec 称确认后双方解锁；发送端彩蛋在收到 accept 后才 `add_egg`。与装扮乐观解锁路径不一致，但最终一致。
- **建议**：若产品要求发送端也「先看见」彩蛋，可在 confirm 时本地暂存预览（不计入 20 上限），accept 时转正。

## P2 - 可选优化（锦上添花）

### [P2-1] 未默认注册 `set_gift_invite_hook`
- **类型**：文档补充 / 接入提示
- **位置**：`code/src/core/anniv_celebrate.py:25`；gift 模块无反向注册
- **描述**：纪念日庆祝的互赠邀请钩子仍为 no-op，外壳需自行注册打开礼物选择。
- **建议**：在 impl 接入示例中补一段注册样例即可（非功能缺失）。

### [P2-2] gift_dialog 无独立 status_text 单测文件命名
- **类型**：风格建议
- **位置**：`tests/core/test_gift_dialog.py`
- **描述**：anniv/pet 用 `test_*_status_text.py`；本模块合并在 dialog 测试中，可接受。

## 验收标准覆盖检查

| AC 编号 | 描述 | 状态 |
|---------|------|------|
| AC-1 | A 二次确认发送 → B 收礼物盒，A 状态已发送 | ✅ 通过（send+receive+demo） |
| AC-2 | B 打开 → 双方解锁 + accept + 亲密度 | ✅ 通过 |
| AC-3 | 超 24h 未打开 → 过期退回可重发 | ✅ 通过（短 TTL 注入） |
| AC-4 | user_items 到期失效移除 | ✅ 通过 |
| AC-5 | 伪造解锁凭证拒绝 | ✅ 通过 |

## TODO 完成度检查

| TODO | 描述 | 状态 |
|------|------|------|
| TODO-G1 | gift_config.py | ✅ 完成 |
| TODO-S1 | gift_store.py | ✅ 完成 |
| TODO-S2 | gift_send.py | ✅ 完成 |
| TODO-S3 | gift_receive.py | ✅ 完成 |
| TODO-S4 | gift_expire.py | ✅ 完成 |
| TODO-S5 | gift_intimacy.py | ✅ 完成 |
| TODO-C1 | gift_dialog.py | ✅ 完成（纯助手 + Qt 占位） |
| TODO-C2 | gift_demo.py | ✅ 完成 |
