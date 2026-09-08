"""同步层配置加载与默认值（对应 impl §4 / plan G2）。

接受主项目 config 的 ``sync:`` 段 dict；缺失字段用默认值，非法字段抛 ConfigError。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import hashlib

from .constants import (
    CODE_MAX_ATTEMPTS,
    CODE_TTL_SECONDS,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_MISSES,
    MDNS_PAIR_SERVICE,
    MDNS_SERVICE,
    RECONNECT_BACKOFF,
    WS_PORT_RANGE,
)
from .errors import ConfigError


def _b32(text: str, size: int) -> str:
    return (
        hashlib.blake2b(text.encode("utf-8"), digest_size=size)
        .digest()
        .hex()
        .upper()[: size * 2]
    )


@dataclass
class SyncConfig:
    """同步层配置。所有字段带默认值，保证零配置可运行。"""

    instance_id: str = ""                  # 缺省由 data_dir 派生
    data_dir: str = ""                     # 运行时数据目录 ~/.tuanzi
    mds_service: str = MDNS_SERVICE
    mds_pair_service: str = MDNS_PAIR_SERVICE
    ws_port_range: tuple[int, int] = WS_PORT_RANGE
    heartbeat_interval: int = HEARTBEAT_INTERVAL
    heartbeat_misses: int = HEARTBEAT_MISSES
    reconnect_backoff: list[int] = field(default_factory=lambda: list(RECONNECT_BACKOFF))
    code_ttl: int = CODE_TTL_SECONDS
    code_max_attempts: int = CODE_MAX_ATTEMPTS
    queue_db_path: str = ""                # 缺省 <data_dir>/sync_queue.db

    def resolve(self) -> None:
        """补齐派生字段；调用后不可再修改。"""
        if not self.data_dir:
            self.data_dir = str(Path.home() / ".tuanzi")
        if not self.instance_id:
            self.instance_id = _b32(self.data_dir, 6)
        if not self.queue_db_path:
            self.queue_db_path = str(Path(self.data_dir) / "sync_queue.db")
        if not (47700 <= self.ws_port_range[0] <= self.ws_port_range[1] <= 65535):
            raise ConfigError(f"非法端口范围: {self.ws_port_range}")
        if self.heartbeat_interval <= 0 or self.heartbeat_misses <= 0:
            raise ConfigError("心跳参数必须为正")
        if self.code_ttl <= 0 or self.code_max_attempts <= 0:
            raise ConfigError("配对参数必须为正")
        if not self.reconnect_backoff:
            raise ConfigError("重连退避表不能为空")

    def ensure_data_dir(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def instance_data_dir(self) -> str:
        """同机多实例联调用：按实例隔离运行时子目录。"""
        return str(Path(self.data_dir) / self.instance_id)


def load_sync_config(raw: dict, *, data_dir: str = "") -> SyncConfig:
    """从 ``sync:`` 配置段 dict 构建 SyncConfig。

    合法字段见 SyncConfig 字段；未知字段忽略；类型错误 / 取值越界抛 ConfigError。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("sync 配置段必须是对象")

    cfg = SyncConfig(data_dir=data_dir)

    # 仅接受已知字段
    known = set(SyncConfig.__dataclass_fields__)
    for key in raw:
        if key not in known:
            raise ConfigError(f"未知同步配置字段: {key!r}")

    try:
        if "instance_id" in raw:
            cfg.instance_id = str(raw["instance_id"])
        if "data_dir" in raw:
            cfg.data_dir = str(raw["data_dir"])
        if "mds_service" in raw:
            cfg.mds_service = str(raw["mds_service"])
        if "mds_pair_service" in raw:
            cfg.mds_pair_service = str(raw["mds_pair_service"])
        if "ws_port_range" in raw:
            r = raw["ws_port_range"]
            cfg.ws_port_range = (int(r[0]), int(r[1]))
        if "heartbeat_interval" in raw:
            cfg.heartbeat_interval = int(raw["heartbeat_interval"])
        if "heartbeat_misses" in raw:
            cfg.heartbeat_misses = int(raw["heartbeat_misses"])
        if "reconnect_backoff" in raw:
            cfg.reconnect_backoff = [int(x) for x in raw["reconnect_backoff"]]
        if "code_ttl" in raw:
            cfg.code_ttl = int(raw["code_ttl"])
        if "code_max_attempts" in raw:
            cfg.code_max_attempts = int(raw["code_max_attempts"])
        if "queue_db_path" in raw:
            cfg.queue_db_path = str(raw["queue_db_path"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"同步配置字段类型非法: {exc}") from exc

    cfg.resolve()
    return cfg
