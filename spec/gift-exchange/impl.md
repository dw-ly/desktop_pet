# 互赠礼物 实现规格（impl）

> 关联 Spec：[[7-AI学习/11-AI桌宠情侣互联/spec/gift-exchange/spec.md|spec/gift-exchange]]
> 关联 Plan：[[7-AI学习/11-AI桌宠情侣互联/spec/gift-exchange/plan.md|plan/gift-exchange]]
> 创建日期：2026-09-08
> 状态：已确认（G1/S1-S5/C1/C2 全部落地；全量 386 passed + gift_demo 联调绿）

## 目的

将 plan 的任务级 TODO（G1/S1–S5/C1/C2）细化为**可直接编码的契约**：礼物库加载、`gift.send` / `gift.accept` / `gift.expire` 线格式、解锁凭证 HMAC、状态机、亲密度合入、UI 纯助手与联调脚本。编码时不再做设计决策；若实现中必须偏离本文档，先更新本文档再编码（文档即契约）。

核心链路：manifest 17 款 + 自定义彩蛋 → 二次确认 → `gift.send`（含 `unlockSig`）→ 本端乐观解锁（pending）→ 对方打开验签 → `user_items` 持久化 → `gift.accept` → 双方各计亲密度（+5 / 纪念日 +20）→ 超 24h 未打开 `gift.expire` 可重发；`expire_at` 到期移除物品。

---

## 0. 目录结构与模块归属

```
src/core/
├── gift_config.py     ← plan G1  礼物库 + 配置 + 解锁签名
├── gift_store.py      ← plan S1  gift_offers / user_items / 彩蛋上限
├── gift_send.py       ← plan S2  propose / cancel / confirm
├── gift_receive.py    ← plan S3  handle_send / accept / handle_accept
├── gift_expire.py     ← plan S4  过期退回 + 物品到期
├── gift_intimacy.py   ← plan S5  亲密度合入（本地，不发 pet.feed）
src/ui/
└── gift_dialog.py     ← plan C1  UI 纯助手 + HAS_QT 占位
tools/
└── gift_demo.py       ← plan C2  双端联调
tests/core/
├── test_gift_config.py
├── test_gift_store.py
├── test_gift_send.py
├── test_gift_receive.py
├── test_gift_expire.py
├── test_gift_intimacy.py
└── test_gift_dialog.py
```

---

## 1. 全局约定

### 1.1 线格式（载荷纯净）

**`gift.send`**

```json
{
  "giftId": "uuid4hex",
  "item": "outfit-heart-100",
  "to": "<partner peer_id>",
  "expireAt": 1754126400,
  "unlockSig": "<hmac hex>",
  "eggText": "可选，仅 custom_egg"
}
```

键集合：必含 `{giftId, item, to, expireAt, unlockSig}`；彩蛋时另含 `eggText`。

**`gift.accept`**

```json
{
  "giftId": "uuid4hex",
  "item": "outfit-heart-100",
  "unlockSig": "<hmac hex>"
}
```

**`gift.expire`**

```json
{
  "giftId": "uuid4hex"
}
```

### 1.2 解锁凭证

```text
unlockSig = HMAC-SHA256(mac_key, f"{giftId}:{item}:{expireAt}").hexdigest()
```

- `mac_key` 与亲密度同源：`derive_mac_key(my_private, peer_public)`
- 伪造签名 → `accept` / `handle_accept` 拒绝，不解锁、不合入亲密度

### 1.3 状态机

```text
sent → accepted
sent → expired
（其余非法，transition 返回 False）
```

### 1.4 亲密度

- 接受后双方**各计一次**（接收端 `accept`、发送端 `handle_accept`）
- 走 `apply_intimacy_event`（reason=`gift`）；**不**另发 `pet.feed`（避免对端再合入双倍）
- 普通 +5；纪念日钩子为真时 +20（不叠加 ×2）

### 1.5 乐观解锁

- 发送端 confirm 后：`user_items.source = pending:{giftId}`（彩蛋除外）
- `gift.accept` 到达：`finalize_pending` → `gift:{from_peer}`
- `gift.expire`：`remove_pending` 回退

### 1.6 自定义彩蛋

- id=`custom-egg`；纯文本 ≤200；每端 `user_items.type=custom_egg` 上限 20（超出删最旧 rowid）
- 文本暂存在 `kv[gift:egg:{giftId}]`，接受后写入 `user_items.name`

---

## 2. G1 gift_config.py

| 符号 | 说明 |
|------|------|
| `GIFT_TYPES` | outfit\|action\|emoji\|custom_egg |
| `GiftState` | sent\|accepted\|expired |
| `GiftItem` / `GiftConfig` | 条目与配置（TTL=86400、egg_max=20、egg_max_len=200） |
| `load_gift_catalog(path?)` | 读仓库根 `assets/manifest.json`；缺失/损坏→空 |
| `load_gift_config(overrides?, manifest_path?)` | 可覆盖 TTL/上限/路径 |
| `sign_unlock` / `verify_unlock` | 解锁凭证 |
| `validate_egg_text` / `item_expire_at` | 彩蛋校验；expire_days≤0→永久 |

---

## 3. S1 gift_store.py

`GiftStore(db)`：

- offers：`create_offer`（幂等）/ `get_offer` / `list_offers` / `pending_expired_offers` / `transition`
- items：`unlock_item`（once）/ `list_available_items` / `set_pending_unlock` / `finalize_pending` / `remove_pending` / `expire_items`
- eggs：`add_egg` / `list_eggs` / `egg_count`；`set/get/clear_egg_text`

---

## 4. S2 gift_send.py

`GiftSend(store, sync, mac_key, *, cfg, my_peer_id, partner_id, on_optimistic)`：

- `propose(item_id, egg_text=None)` → 内存 `GiftProposal`（不落库）
- `cancel()` → 清空 pending
- `confirm(now=None)` → 写 offer、签 unlockSig、`sync.send(GIFT_SEND)`、乐观解锁；未配对/无 pending→None
- 发送后不可撤回

---

## 5. S3 gift_receive.py

`GiftReceive(store, sync, mac_key, *, cfg, on_inbox, on_unlocked, on_accepted, on_forged)`：

- `handle_send(m)` → 收件箱 offer + 缓存 sig
- `accept(gift_id, unlock_sig=None, now=None)` → 验签 → 解锁 → `GIFT_ACCEPT` → 亲密度
- `later(gift_id)` → 保留待打开
- `handle_accept(m)` → 发送端验签 → finalize/解锁 → 亲密度

---

## 6. S4 gift_expire.py

`GiftExpire(store, sync, *, cfg, on_offer_expired, on_items_expired)`：

- `scan_offers(now)`：`expire_at < now` 且 sent → expired + 发 `GIFT_EXPIRE` + 回退 pending
- `handle_expire(m)`：对端过期（幂等）
- `expire_items(now)` / `tick(now)`：物品到期清理

边界：`expire_at == now` 不触发（严格小于）。

---

## 7. S5 gift_intimacy.py

- `gift_intimacy_delta(cfg?)` → 5 或 20
- `apply_gift_intimacy(db, mac_key, *, cfg, now)` → 本地合入，返回实际 delta

---

## 8. C1 gift_dialog.py

纯函数：`group_catalog` / `confirm_copy` / `receive_prompt` / `status_label` / `egg_validation_hint` / `unlock_success_text` / `resend_hint`；`GiftDialog` HAS_QT 占位。

---

## 9. C2 gift_demo.py

双端：配对 → 送 outfit 接受 +5 → 短 TTL 过期退回 → 重发 → 纪念日 +20 → 彩蛋 → item 到期清理 → 取消不落库。日志以 `PASS` 标记。

---

## 10. 与既有模块衔接

| 依赖 | 用法 |
|------|------|
| sync-security | `EventType.GIFT_*`、`SyncManager.send`、配对 peer_id |
| data-consistency | `gift_offers` / `user_items` / `kv`；`apply_intimacy_event` |
| pet-growth | `is_anniversary_today` 钩子；积分数值来自 `PetConfig` |
| anniversary | `set_gift_invite_hook` 已预留（本模块未强制注册，外壳可接） |

---

## 11. 明确不在本实现

- Qt 完整礼物对话框（仅纯助手 + 占位）
- 真实货币/内购、礼物转赠、发送后撤回
- 美术资源生产（使用 assets 占位）
- 主项目记忆系统深度接入（彩蛋以 `user_items` 本地列表模拟）
