"""事件日志清理（对应 data-consistency impl §4.2 / plan S3）。

- 仅动 events 表；carries / gift_offers / user_items / pet_state / anniversaries 不参与
- 分批删除（每批 batch 行）防长事务锁库；启动时 + 每日零点各执行一次
- 业务失败标记衔接：带话 failed / 礼物 expired 由各业务模块在状态变更时
  把对应 events 行标记为 status='failed'，纳入下一轮清理
- **pet.feed 豁免**（D17）：亲密度重放收敛依赖完整事件流，积分事件不清理
"""

from __future__ import annotations

import time


def prune_event_log(db, retention_days: int = 90, batch: int = 1000) -> int:
    """清理 events 表超过保留期的记录，分批删除，返回删除总行数。

    可删（满足其一，且 created_at < cutoff，且 **type != 'pet.feed'**）：
      - status='received'：接收侧插入即处理完，超期即可删
      - acked_at 非空：发送侧已收到送达确认
      - status='failed'：补发过期 / 业务失败标记
      - expires_at 非空且 < now：未确认但超期，视为失败
    保留：90 天内记录；或未确认未过期（仍在去重/补发窗口内）。
    **pet.feed 豁免**（data-consistency impl D17）：亲密度经带签名事件流重放
    收敛（spec §3.3.6），需保留完整历史；pet.feed 受每日 reason 上限约束
    （feed 5/天 等），长期规模有界，豁免不破坏"events 表规模可控"目标。
    """
    now = int(time.time())
    cutoff = now - retention_days * 86400
    total = 0
    while True:
        # SQLite 默认不支持 DELETE ... LIMIT，用子查询限行保证每批 ≤ batch 行
        cur = db.execute(
            "DELETE FROM events WHERE id IN ("
            " SELECT id FROM events WHERE created_at < ? AND type != 'pet.feed' AND ("
            "   status = 'received'"
            "   OR acked_at IS NOT NULL"
            "   OR status = 'failed'"
            "   OR (expires_at IS NOT NULL AND expires_at < ?)"
            " ) ORDER BY id LIMIT ?"
            ")",
            (cutoff, now, batch),
        )
        deleted = cur.rowcount
        if deleted == 0:
            break
        total += deleted
    return total
