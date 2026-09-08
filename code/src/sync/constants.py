"""同步层全局常量（对应 impl §1.1）。

所有模块统一从此处导入，避免魔法数字散落。
"""

# 协议
PROTOCOL_VERSION = 1

# 配对短码
CODE_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 字符，去 0/O/1/I/L
CODE_LEN = 12                      # 8 位会话 id + 4 位校验
CODE_TTL_SECONDS = 600             # 10 分钟有效
CODE_MAX_ATTEMPTS = 5              # 重试上限

# 心跳与重连
HEARTBEAT_INTERVAL = 30            # 秒
HEARTBEAT_MISSES = 3               # 连续无响应次数 → 离线
HEARTBEAT_OFFLINE_SECONDS = HEARTBEAT_INTERVAL * HEARTBEAT_MISSES
RECONNECT_BACKOFF = [1, 2, 4, 8, 16, 32, 60]  # 秒，循环取末值

# 传输
WS_PORT_RANGE = (47700, 47799)
MDNS_SERVICE = "_tuanzi._tcp.local."
MDNS_PAIR_SERVICE = "_tuanzi-pair._tcp.local."

# 会话密钥派生（libsodium crypto_kdf 兼容的 blake2b 实现）
# 子键按「两端公钥的规范序」分配方向，保证双方对同一方向使用同一子键：
#   1 = 公钥较小方 → 较大方；2 = 反向（公钥按字节序比较，确定性一致）
SUBKEY_ENC_LOWER_TO_HIGHER = 1    # 方向 A→B（A.pub < B.pub）
SUBKEY_ENC_HIGHER_TO_LOWER = 2    # 方向 B→A
SUBKEY_MAC = 3                    # HMAC 签名键（data-consistency pet.feed 消费）
KD_CONTEXT = b"tuanzi0"            # blake2b 派生 context（固定勿改）

# 配对会话
PAIR_WS_TIMEOUT = 10.0             # 临时通道等待秒数

# 运行时数据目录
DEFAULT_DATA_DIR_NAME = ".tuanzi"
