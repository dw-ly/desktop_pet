"""mDNS 局域网发现（对应 impl §9.1 / plan S6）。

- 常规发现：广播 ``tuanzi-{peer_id}``，TXT 含 peer_id
- 配对发现：广播 ``pair-{session_b32}``，配对临时监听用
- **同机兜底**：同一进程内 publish 的服务登记进本地注册表，resolve 优先命中，
  使双实例同机联调不依赖多播（WSL/部分虚拟网卡不支持多播的场景，见 impl §9.4）。
  跨机仍走真实 mDNS。
"""

from __future__ import annotations

import logging
from typing import Optional

from zeroconf import ServiceInfo, Zeroconf, get_all_addresses

log = logging.getLogger(__name__)

# (service, instance_name) → (host, port)；同进程内发布的服务
_local_registry: dict[tuple[str, str], tuple[str, int]] = {}


class DiscoveryHandle:
    """保持 Zeroconf 实例存活；unpublish 时注销并关闭。"""

    def __init__(self, zc: Zeroconf, info: ServiceInfo) -> None:
        self._zc = zc
        self._info = info

    def unpublish(self) -> None:
        _local_registry.pop((self._info.type, self._info.name), None)
        try:
            self._zc.unregister_service(self._info)
        except Exception:  # noqa: BLE001 —— 注销失败无需升级
            pass
        finally:
            self._zc.close()


def publish(service: str, name: str, port: int, txt: dict) -> DiscoveryHandle:
    """注册一条服务并返回句柄。句柄必须由调用方保存，否则服务立即消失。"""
    zc = Zeroconf()
    props = {k.encode("utf-8"): str(v).encode("utf-8") for k, v in txt.items()}
    info = ServiceInfo(
        service,
        f"{name}.{service}",
        addresses=get_all_addresses(),
        port=port,
        properties=props,
        weight=0,
        priority=0,
    )
    try:
        zc.register_service(info)
    except Exception:
        zc.close()
        raise
    _local_registry[(service, info.name)] = ("127.0.0.1", port)
    log.debug("mDNS 已广播 %s.%s", name, service)
    return DiscoveryHandle(zc, info)


def resolve(
    service: str, name: str, timeout_seconds: float = 3.0
) -> Optional[tuple[str, int]]:
    """解析服务实例 → (host, port)；同机注册表优先，否则走 mDNS；超时返回 None。"""
    inst = f"{name}.{service}"
    local = _local_registry.get((service, inst))
    if local is not None:
        return local
    zc = Zeroconf()
    try:
        info = zc.get_service_info(service, inst, timeout=int(timeout_seconds * 1000))
        if not info or not info.addresses:
            return None
        host = _addr_to_str(info.addresses[0])
        return host, int(info.port)
    finally:
        zc.close()


def _addr_to_str(addr: bytes) -> str:
    return ".".join(str(b) for b in addr)
