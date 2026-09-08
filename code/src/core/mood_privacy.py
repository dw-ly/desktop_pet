"""本端隐身模式（对应 mood-sync impl §6 / plan S4）。

最高优先级开关（spec §3.3.6 / 决策 D19）。状态机（impl §6.1）：

    开启（set_stealth(True)）：
      1. 置本端 stealth=True            → 一切 mood.sync 发送停止（Sender 查询 is_stealth 拦截）
      2. 发送一条 mood.sync {"privacy": true}  → 对方显示"TA 开启了隐身"（保留最近标签）
      3. receiver.clear_partner_state() → 清除本端已展示的对方状态（本端也不再看对方状态）

    关闭（set_stealth(False)）：
      1. 置本端 stealth=False（恢复节流发送）
      2. 不发任何消息；对方端最近标签保持，直到下一次正常 mood.sync 覆盖（D19：避免界面闪烁）

开启/关闭重复调用均幂等。纯内存态，不持久化。
"""

from __future__ import annotations

from core.mood_receive import MoodReceiver
from sync.events import EventType


class MoodPrivacy:
    """本端隐身开关（内存态）。开启即停发 + 清本端对方状态 + 发 privacy 通知。"""

    def __init__(self, sync, receiver: MoodReceiver) -> None:
        # sync: 发送 privacy 通知用（SyncManager 鸭子类型）
        # receiver: 清除本端对方状态用
        self._sync = sync
        self._receiver = receiver
        self._stealth = False

    @property
    def is_stealth(self) -> bool:
        """供 MoodSender 查询。"""
        return self._stealth

    def set_stealth(self, enabled: bool) -> None:
        """按 6.1 状态机执行。开启重复调用幂等；关闭重复调用幂等。"""
        if enabled:
            if self._stealth:
                return  # 已开启：幂等，不重复发通知
            self._stealth = True
            self._sync.send(EventType.MOOD_SYNC, {"privacy": True})
            self._receiver.clear_partner_state()
        else:
            if not self._stealth:
                return  # 已关闭：幂等
            self._stealth = False  # 关闭不发任何消息（D19）
