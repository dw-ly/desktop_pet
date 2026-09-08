"""AI 桌宠情侣互联 · 同步层与端到端加密。

独立线程运行的传输底座：配对、端到端加密、协议、局域网直连、离线队列。
对应 spec/sync-security 与 spec/sync-security/impl.md。
"""

from .errors import (
    SyncError,
    ProtocolError,
    CryptoError,
    PairingError,
    KeyStoreError,
    TransportError,
    ConfigError,
)
from .events import EventType, Message, Envelope
from .config import SyncConfig, load_sync_config
from .manager import SyncManager, PairingEvent, PairingStatus

__version__ = "0.1.0"

__all__ = [
    "SyncError",
    "ProtocolError",
    "CryptoError",
    "PairingError",
    "KeyStoreError",
    "TransportError",
    "ConfigError",
    "EventType",
    "Message",
    "Envelope",
    "SyncConfig",
    "load_sync_config",
    "SyncManager",
    "PairingEvent",
    "PairingStatus",
    "__version__",
]
