"""Core 数据层（对应 data-consistency impl §2/§3/§4 / plan G1/G2/S1-S6）。

统一数据库 `core.db` + 迁移框架。业务模块（carry 等）基于本层持久化；
pet-growth 复用亲密度合入唯一入口 `apply_intimacy_event`；
换机/重装经 `export_backup` / `import_backup` 加密备份恢复。

启动入口：`init_core(data_dir)` —— 建目录 → 连接 → 迁移到最新版本。
"""

from .backup_restore import BackupError, export_backup, import_backup
from .consistency import (
    INTIMACY_DAILY_LIMITS,
    INTIMACY_REASONS,
    apply_intimacy_event,
    merge_lww,
    sign_intimacy_event,
    verify_intimacy_signature,
)
from .daily_sync import AlignResult, build_snapshot, daily_align, handle_peer_snapshot, replay_intimacy_total
from .db import Database, DB_NAME, init_core
from .migrate import MIGRATIONS_DIR, migrate
from .mood_config import MOOD_ANIMATION, MOOD_LABELS, MoodConfig, load_mood_config
from .mood_export import Emotion, MoodExporter
from .mood_privacy import MoodPrivacy
from .mood_receive import MoodEvent, MoodReceiver
from .mood_sender import MoodSender
from .pet_config import (
    ACTIVITY_TYPES,
    UNLOCK_ITEMS,
    PetConfig,
    earned_unlock_ids,
    level_from_intimacy,
    load_pet_config,
)
from .pet_growth import PetGrowth, is_anniversary_today, set_anniversary_hook
from .pet_level import PetLevel
from .pet_profile import PetProfile
from .pet_streak import PetStreak, SettleOutcome, SettleStatus
from .pet_sync import PetSync
from .anniv_blessing import AnnivBlessing
from .anniv_calendar import AnnivCalendar, lunar_to_solar
from .anniv_celebrate import AnnivCelebrate
from .anniv_config import AnnivConfig, load_anniv_config
from .anniv_integration import register_anniversary_hook, unregister_anniversary_hook
from .anniv_store import AnnivStore
from .anniv_sync import AnnivSync
from .gift_config import (
    CUSTOM_EGG_ID,
    GIFT_TYPES,
    GiftConfig,
    GiftItem,
    GiftState,
    item_expire_at,
    load_gift_catalog,
    load_gift_config,
    sign_unlock,
    validate_egg_text,
    verify_unlock,
)
from .gift_store import GiftOffer, GiftStore, UserItem
from .gift_send import GiftProposal, GiftSend
from .gift_receive import GiftReceive
from .gift_expire import GiftExpire
from .gift_intimacy import apply_gift_intimacy, gift_intimacy_delta

__all__ = [
    "Database",
    "DB_NAME",
    "init_core",
    "MIGRATIONS_DIR",
    "migrate",
    "MOOD_LABELS",
    "MOOD_ANIMATION",
    "MoodConfig",
    "load_mood_config",
    "Emotion",
    "MoodExporter",
    "MoodSender",
    "MoodEvent",
    "MoodReceiver",
    "MoodPrivacy",
    "INTIMACY_REASONS",
    "INTIMACY_DAILY_LIMITS",
    "apply_intimacy_event",
    "merge_lww",
    "sign_intimacy_event",
    "verify_intimacy_signature",
    "AlignResult",
    "build_snapshot",
    "daily_align",
    "handle_peer_snapshot",
    "replay_intimacy_total",
    "BackupError",
    "export_backup",
    "import_backup",
    "PetConfig",
    "load_pet_config",
    "UNLOCK_ITEMS",
    "ACTIVITY_TYPES",
    "level_from_intimacy",
    "earned_unlock_ids",
    "PetGrowth",
    "set_anniversary_hook",
    "is_anniversary_today",
    "PetSync",
    "PetLevel",
    "PetStreak",
    "SettleStatus",
    "SettleOutcome",
    "PetProfile",
    "AnnivConfig",
    "load_anniv_config",
    "AnnivStore",
    "AnnivCalendar",
    "lunar_to_solar",
    "AnnivBlessing",
    "AnnivCelebrate",
    "AnnivSync",
    "register_anniversary_hook",
    "unregister_anniversary_hook",
    "CUSTOM_EGG_ID",
    "GIFT_TYPES",
    "GiftConfig",
    "GiftItem",
    "GiftState",
    "item_expire_at",
    "load_gift_catalog",
    "load_gift_config",
    "sign_unlock",
    "validate_egg_text",
    "verify_unlock",
    "GiftOffer",
    "GiftStore",
    "UserItem",
    "GiftProposal",
    "GiftSend",
    "GiftReceive",
    "GiftExpire",
    "apply_gift_intimacy",
    "gift_intimacy_delta",
]
