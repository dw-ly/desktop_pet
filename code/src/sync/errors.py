"""同步层异常体系。

对应 impl §1.3。所有同步层异常以此处类型为准，上层模块按需捕获。
"""


class SyncError(Exception):
    """同步层基类异常。"""


class ProtocolError(SyncError):
    """协议错误：坏 JSON / 未知事件类型 / 协议版本不兼容。"""


class CryptoError(SyncError):
    """密码学错误：解密失败 / 密钥派生失败 / 校验失败。"""


class PairingError(SyncError):
    """配对错误：短码无效 / 已过期 / 超次 / 已一次性消费 / 未发现对方。"""


class KeyStoreError(SyncError):
    """密钥存储错误：keyring 与文件兜底均失败。"""


class TransportError(SyncError):
    """传输错误：连接失败 / 发送失败 / 离线。"""


class ConfigError(SyncError):
    """配置错误：非法字段 / 类型错误 / 取值越界。"""


__all__ = [
    "SyncError",
    "ProtocolError",
    "CryptoError",
    "PairingError",
    "KeyStoreError",
    "TransportError",
    "ConfigError",
]
