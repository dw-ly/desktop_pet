# 同步层与端到端加密 实现规格（impl）

> 关联 Spec：[[7-AI学习/11-AI桌宠情侣互联/spec/sync-security/spec.md|spec/sync-security]]
> 关联 Plan：[[7-AI学习/11-AI桌宠情侣互联/spec/sync-security/plan.md|plan/sync-security]]
> 创建日期：2026-08-03
> 状态：已确认（**实现规格试点**：先以本模块验证产出质量，再决定是否推广到其余 6 模块）

## 目的

将 plan 的任务级 TODO 细化为**可直接编码的契约**：接口签名、数据结构字段、线格式 JSON、算法伪代码、断言级测试用例。编码时不再做设计决策；若实现中必须偏离本文档，先更新本文档再编码（文档即契约）。

---

## 0. 目录结构与模块归属

```
src/sync/
├── __init__.py
├── config.py       ← plan G2    配置加载与默认值
├── events.py       ← plan G1    事件类型枚举 + Message/Envelope dataclass + JSON 序列化
├── protocol.py     ← plan G1+S4 信封编解码、单调序号、去重键、协议版本
├── crypto.py       ← plan S1    密钥生成 / 会话派生 / 加解密 / SAS / 配对短码
├── key_store.py    ← plan S2    私钥与会话密钥加密落盘（keyring，兜底文件）
├── pairing.py      ← plan S3    配对会话、临时监听、pair.* 消息
├── queue.py        ← plan S5    离线出站队列表 + 接收去重表（M2 自带 schema）
├── discovery.py    ← plan S6    mDNS 广播与解析（zeroconf）
├── transport.py    ← plan S6    Transport 抽象 + LanTransport + 连接状态机
└── manager.py      ← plan S7    线程生命周期、事件分发、吊销、配对版本校验
```

---

## 1. 全局约定

### 1.1 常量（统一集中定义）

| 常量 | 值 | 说明 |
|------|-----|------|
| `PROTOCOL_VERSION` | `1` | Message.v；不兼容则拒绝连接 |
| `CODE_CHARSET` | `"ABCDEFGHJKLMNPQRSTUVWXYZ23456789"` | 32 字符，去 `0/O/1/I/L` 易混淆字符 |
| `CODE_LEN` | `12` | 配对短码总长（8 位会话 id + 4 位校验） |
| `CODE_TTL_SECONDS` | `600` | 配对码有效期（spec 决议） |
| `CODE_MAX_ATTEMPTS` | `5` | 配对码重试上限（spec 决议） |
| `HEARTBEAT_INTERVAL` | `30` | 心跳/hello 发送间隔（秒） |
| `HEARTBEAT_MISSES` | `3` | 连续无响应次数阈值 → 判定离线 |
| `RECONNECT_BACKOFF` | `[1,2,4,8,16,32,60]` | 重连退避（秒），循环取末值 |
| `WS_PORT_RANGE` | `(47700, 47799)` | 局域网 WS 监听端口范围 |
| `MDNS_SERVICE` | `"_tuanzi._tcp.local."` | 发现服务类型 |
| `MDNS_PAIR_SERVICE` | `"_tuanzi-pair._tcp.local."` | 配对临时监听发现服务 |
| `SUBKEY_ENC_LOWER_TO_HIGHER` | `1` | 派生子键 id：公钥较小方 → 较大方的加密键（方向由两端公钥规范序决定，见 §5.1） |
| `SUBKEY_ENC_HIGHER_TO_LOWER` | `2` | 派生子键 id：公钥较大方 → 较小方的加密键（见 §5.1） |
| `SUBKEY_MAC` | `3` | 派生子键 id：HMAC 签名键（data-consistency 的 pet.feed 消费） |
| `KD_CONTEXT` | `b"tuanzi0"` | 派生 context（固定，勿改；长度不限，两端一致即可） |

### 1.2 标识与编码约定

- **peer_id**：`base32(blake2b(identity_pubkey, digest_size=10))` → 16 字符大写 base32。作为 `Message.from`/`to` 与 peer 记录文件名。
- **instance_id**：由 `data_dir` 派生（`base32(blake2b(data_dir, digest_size=6))` → 9 字符），用于 mDNS 服务实例名，保证同机双实例不冲突。
- **时间**：全部为 Unix 秒（`int(time.time())`）。
- **JSON**：UTF-8；载荷中的二进制一律 base64 字符串。
- **字节序**：序号、子键 id 等整数值在线格式中均为 JSON 数字，无字节序问题。

### 1.3 异常体系（`sync/errors.py`）

```python
class SyncError(Exception): ...          # 基类
class ProtocolError(SyncError): ...      # 坏 JSON / 未知类型 / 版本不兼容
class CryptoError(SyncError): ...        # 解密失败 / 密钥派生失败
class PairingError(SyncError): ...       # 短码无效 / 过期 / 超次 / 已消费
class KeyStoreError(SyncError): ...      # keyring 与文件兜底均失败
class TransportError(SyncError): ...     # 连接 / 发送失败
```

### 1.4 线程与并发模型

- 同步层整体运行在**独立线程**（`manager.start()` 创建），该线程内跑一个 `asyncio` 事件循环（transport 为异步）。
- 对外只暴露**线程安全回调**：事件到达、连接状态变化、配对状态变化。回调由调用方（Core/UI）自行 marshal 到自己的线程（UI 用 Qt 信号队列，见 §11）。
- 数据库（queue）只允许在同步线程内访问（单写者）；跨线程不直接操作。

---

## 2. events.py（plan G1）

### 2.1 EventType 枚举

```python
class EventType(str, Enum):
    HELLO        = "hello"           # 同步层内部
    HELLO_ACK    = "hello.ack"       # 同步层内部
    PAIRING_REVOKE = "pairing.revoke"# 同步层内部（manager 处理）
    MSG_CARRY    = "msg.carry"
    CARRY_ACK    = "carry.ack"
    CARRY_REVOKE = "carry.revoke"
    MOOD_SYNC    = "mood.sync"
    PET_FEED     = "pet.feed"
    PET_PROFILE  = "pet.profile"
    DATE_ADD     = "date.add"
    DATE_REMIND  = "date.remind"
    DATE_SYNC    = "date.sync"
    GIFT_SEND    = "gift.send"
    GIFT_ACCEPT  = "gift.accept"
    GIFT_EXPIRE  = "gift.expire"

BUSINESS_TYPES = { ... }   # 除 HELLO/HELLO_ACK/PAIRING_REVOKE 外全部
```

- 枚举值**唯一登记处**为 spec §3.3 注册表；本枚举只是落地，任何新增/改名必须先改 spec。
- `pairing.revoke`（吊销）与 `carry.revoke`（撤回）是两个类型，禁止混淆。

### 2.2 Message（密文内明文负载）

```python
@dataclass(frozen=True)
class Message:
    v: int            # == PROTOCOL_VERSION
    type: str         # EventType 值
    from_id: str      # 发送方 peer_id
    seq: int          # 发送方单调递增序号
    ts: int           # 发送方 Unix 秒
    payload: dict     # 各类型自定义负载（见 spec §3.3 注册表 payload 要点）
```

### 2.3 Envelope（传输层信封，中继可见部分）

```python
@dataclass(frozen=True)
class Envelope:
    to: str           # 目标 peer_id（直连时恒为本机记录的伴侣 id，为与中继协议统一保留）
    cipher: bytes     # SecretBox 密文（含 nonce）
    expires: int      # 过期时间 Unix 秒；不设过期用 0（直连模式下不影响投递，中继模式读取）
```

### 2.4 序列化契约

| 函数 | 签名 | 行为 |
|------|------|------|
| `message_to_json(m: Message) -> bytes` | UTF-8 JSON，键序固定 `v,type,from,seq,ts,payload` | 键序固定便于未来跨语言互通与抓包比对 |
| `message_from_json(raw: bytes) -> Message` | 反序列化；`type` 不在 `EventType` 或 `v != PROTOCOL_VERSION` 抛 `ProtocolError` | 未知字段忽略 |
| `envelope_to_json(e: Envelope) -> bytes` | cipher base64 | — |
| `envelope_from_json(raw: bytes) -> Envelope` | 反序列化；坏 JSON 抛 `ProtocolError` | — |

> 往返保证：`message_from_json(message_to_json(m)) == m` 对全部 15 种类型成立。

---

## 3. protocol.py（plan G1 + S4）

### 3.1 序号管理

```python
class SeqManager:
    """发送端单调递增序号，重启不回退。持久化在 queue 的 meta 表。"""
    def __init__(self, peer_id: str, meta_get, meta_set): ...
    def next(self) -> int: ...       # 取当前值并 +1 持久化，再返回旧值
```

- **持久化规则**：`meta` 表存 `{"<peer_id>:next_seq": <int>}`。`next()` 先 `meta_set` 再返回，保证崩溃后不重复分配（宁可跳号，不可回退）。
- 初始化：无记录时从 `1` 开始。
- 跳号允许：`seq` 只需单调递增，不要求连续（去重靠"已见过的最大/全部 seq"判断）。

### 3.2 去重键

```python
def dedup_key(from_id: str, seq: int) -> str:
    return f"{from_id}:{seq}"
```

- 不同发送方同 `seq` 不冲突；同一发送方 `seq` 全局唯一。
- data-consistency 阶段 `event_id = "{from}:{seq}"` 与此**同构**，M2 的 seen 表可直接迁移。

### 3.3 版本兼容校验

```python
def check_version(v: int) -> None:   # v != PROTOCOL_VERSION → ProtocolError
```

- 在 `message_from_json` 内调用；transport 层把解密失败的连接判为协议不兼容并断开。

---

## 4. config.py（plan G2）

```python
@dataclass
class SyncConfig:
    instance_id: str = ""            # 缺省由 data_dir 派生
    mds_service: str = MDNS_SERVICE
    ws_port_range: tuple[int, int] = WS_PORT_RANGE
    heartbeat_interval: int = 30
    heartbeat_misses: int = 3
    reconnect_backoff: list[int] = field(default_factory=lambda: [1,2,4,8,16,32,60])
    code_ttl: int = 600
    code_max_attempts: int = 5
    queue_db_path: str = ""          # 缺省 <data_dir>/sync_queue.db
    data_dir: str = ""               # 运行时数据目录 ~/.tuanzi

def load_sync_config(raw: dict, *, data_dir: str) -> SyncConfig: ...
```

- `load_sync_config` 接受主项目 config 的 `sync:` 段 dict；缺失字段用默认值；非法字段（类型错误/端口越界/负数）抛 `ConfigError`。
- `instance_id` 为空时用 `base32(blake2b(data_dir, 6))`。

---

## 5. crypto.py（plan S1）

### 5.1 密钥生成与派生

```python
@dataclass(frozen=True)
class IdentityKeyPair:
    private_key: bytes    # 32B
    public_key: bytes     # 32B

@dataclass(frozen=True)
class SessionKeys:
    key_to_peer: bytes    # 32B，本端→对方
    key_from_peer: bytes  # 32B，对方→本端

def generate_identity() -> IdentityKeyPair:
    # nacl.public.PrivateKey.generate(); 私钥 bytes(priv), 公钥 bytes(priv.public_key)

def derive_session(my_private: bytes, peer_public: bytes) -> SessionKeys:
    # 1. shared = Box(PrivateKey(my_private), PublicKey(peer_public)).shared_key()  # 32B，对称
    # 2. 按两端公钥字节序取规范序：pub 较小方记作 L，较大方记作 H
    #    if my_pub < peer_public:   # 本端是 L
    #        key_to_peer   = _kdf_derive(SUBKEY_ENC_LOWER_TO_HIGHER, KD_CONTEXT, shared)
    #        key_from_peer = _kdf_derive(SUBKEY_ENC_HIGHER_TO_LOWER, KD_CONTEXT, shared)
    #    else:                      # 本端是 H
    #        key_to_peer   = _kdf_derive(SUBKEY_ENC_HIGHER_TO_LOWER, KD_CONTEXT, shared)
    #        key_from_peer = _kdf_derive(SUBKEY_ENC_LOWER_TO_HIGHER, KD_CONTEXT, shared)
    # 3. shared 生命周期内释放（不落盘、不入日志）

def derive_mac_key(my_private: bytes, peer_public: bytes) -> bytes:
    # _kdf_derive(SUBKEY_MAC, KD_CONTEXT, shared)  # data-consistency pet.feed 签名用，双方同值

def _kdf_derive(subkey_id: int, context: bytes, master: bytes, size: int = 32) -> bytes:
    # PyNaCl 1.6.2 未绑定 crypto_kdf_derive_from_key，用等价的 blake2b 实现：
    #   h = hashlib.blake2b(key=master, digest_size=size)
    #   h.update(subkey_id.to_bytes(8, "little"))   # subkey_id LE64
    #   h.update(context)
    #   return h.digest()
    # 该构造与 libsodium crypto_kdf 输出字节一致；两端使用同一实现即互通。
```

- **方向不混淆（关键契约）**：方向键由**两端公钥的规范序**决定，双方各自计算得到相同结论，因此 `A.key_to_peer == B.key_from_peer`、`A.key_from_peer == B.key_to_peer`。单测断言两者不相等。
- 子键 id 是**绝对方向**标签（L→H / H→L），与调用方相对视角无关；禁止改回"本端→对方 / 对方→本端"式相对标签，否则两端方向键无法对齐。

### 5.2 加解密

```python
def encrypt(key: bytes, plaintext: bytes) -> bytes:
    return nacl.secret.SecretBox(key).encrypt(plaintext)   # 返回 nonce||ct，自带认证

def decrypt(key: bytes, ciphertext: bytes) -> bytes:
    return nacl.secret.SecretBox(key).decrypt(ciphertext)  # 篡改/错键 → nacl.exceptions.CryptoError
```

- `decrypt` 捕获 `nacl.exceptions.CryptoError` 并重抛 `CryptoError`（统一异常面）。

### 5.3 一次性验证码 SAS（防中间人）

```python
def compute_sas(shared: bytes, pub_a: bytes, pub_b: bytes) -> str:
    # sas_bytes = blake2b(shared + pub_a + pub_b, digest_size=2).digest()  # 16 bit
    # 转 32 字符集 4 位（不足位补 0）：sas 形如 "K7Q2"
```

- 双方用**相同输入**（各自私钥与对方公钥算出的 shared + 双方公钥）计算，结果必须一致。
- SAS 只在配对握手期经临时通道交换并比对，不落盘、不入日志。

### 5.4 配对短码（12 位）

```python
def generate_pairing_code(session_id: bytes) -> str:
    # session_id: 5B(40bit) CSPRNG
    # checksum = blake2b(session_id, digest_size=3)[:3]  # 取前 20 bit → 4 个 base32 字符（高 4 bit 归零对齐）
    # code = base32(session_id[:4]) + base32(checksum)   # 8 + 4 = 12 字符
    # 显示分组 XXXX-XXXX-XXXX

def decode_pairing_code(code: str) -> bytes:
    # 去 '-'、大写化、校验长度 12 与字符集；checksum 不符 → PairingError("短码无效")
    # 返回 session_id(5B)
```

- `CODE_CHARSET` 编码函数：每字符 5 bit，长度 12 → 60 bit，按需补零。
- **10 分钟 / 5 次 / 一次性消费**的判定在 pairing.py 的会话对象内（§7），短码本身只负责承载 session_id 与检错。

---

## 6. key_store.py（plan S2）

```python
class KeyStore:
    def __init__(self, data_dir: str): ...      # keyring service "tuanzi-pet"

    def save_identity(self, priv: bytes) -> None: ...
    def load_identity(self) -> bytes | None: ...
    def delete_identity(self) -> None: ...

    def save_peer_session(self, peer_id: str, session_keys: SessionKeys) -> None: ...
    def load_peer_session(self, peer_id: str) -> SessionKeys | None: ...
    def delete_peer(self, peer_id: str) -> None: ...
```

- keyring 条目：`identity` 与 `peer:{peer_id}`，值为 base64（私钥 / 两个 32B 键拼串）。
- **降级路径**：keyring 不可用（无桌面后端 / 抛异常）→ 写入 `<data_dir>/keys/<name>.key`，`os.chmod(0600)`，并打印一次警示日志；读取时若文件权限非 0600 也警告。
- `delete_identity` 与 `delete_peer` 同时清除 keyring 与兜底文件，成功与否以"两处都删干净"为准。
- 吊销配对调 `delete_peer`；清除身份调 `delete_identity`（仅"重置全部数据"时用）。

---

## 7. pairing.py（plan S3）

### 7.1 配对会话状态

```python
class PairingSession:
    """发起方（A）持有。管理短码、临时监听、尝试次数、一次性消费。"""
    # 状态: active(等待请求) → confirmed(完成/一次性消费) | expired | revoked_by_attempts
    session_id: bytes
    code: str
    expires_at: int
    attempts: int
```

### 7.2 发起配对（A 端）

```text
A.start_pairing():
  1. session_id = random(5B)          # CSPRNG
  2. code = generate_pairing_code(session_id)
  3. 在 ws_port_range 内选空闲端口，启动临时 WS 监听，仅接受本次会话
  4. discovery.publish_pair(session_id, port)   # mDNS _tuanzi-pair 实例名 pair-{session_id_b32}
  5. 会话 TTL = now + CODE_TTL_SECONDS；到期自动关闭监听
  6. 返回 code 给 UI 显示
```

### 7.3 加入配对（B 端）

```text
B.confirm_pairing(code):
  1. session_id = decode_pairing_code(code)     # 校验失败 → PairingError
  2. discovery.resolve_pair(session_id)          # 找不到 → PairingError("未发现对方")
  3. 连 A 临时 WS，发送 pair.request（见 §7.4 消息格式）
  4. 等待 pair.accept → 校验 SAS → 配对完成
  5. 对端超时（>10s）无响应 → PairingError("配对超时")
```

### 7.4 配对消息格式（临时通道明文）

> 明文可接受：内容仅为双方公钥与 SAS；SAS 比对可检出中间人替换。共享秘密永不走线。

| 消息 | 方向 | JSON |
|------|:----:|------|
| `pair.request` | B→A | `{"type":"pair.request","pub":"<b64 PubB>"}` |
| `pair.accept` | A→B | `{"type":"pair.accept","pub":"<b64 PubA>","sas":"<A 计算值>","version":<int>}` |
| `pair.error` | A→B | `{"type":"pair.error","reason":"expired\|rejected"}` |

### 7.5 双方校验与持久化（单边 SAS）

> 设计说明：B 在收到 A 公钥前无法计算 SAS，因此 SAS 只在 A→B 方向单向校验；
> A 端的安全由 **UI 确认核对 peer_id** 承担（见 §11.1）。共享秘密永不走线。

```text
A 收到 pair.request:
  1. 会话已消费/过期 → 回 pair.error，忽略
  2. attempts += 1；attempts > CODE_MAX_ATTEMPTS → 关闭会话（超次）
  3. 校验 pub 为 32B → 记录 pending_pub，触发 UI 确认回调 PEER_REQUEST
  4. 用户确认 → 计算 shared、sas = compute_sas(shared, pubA, pubB)
     → 发送 pair.accept{pubA, sas, version}；version 为 A 随机生成的 pairing_version
  5. 会话一次性消费（code 作废）→ 完成配对

B 收到 pair.accept:
  1. 计算 shared, sas = compute_sas(shared, pubA, pubB)
  2. sas != accept.sas → PairingError（中止，疑似中间人）
  3. 记录 accept.version → 完成配对
```

**完成配对（两端）**：

```text
1. session_keys = derive_session(my_priv, peer_pub)
2. pairing_version = random 4B（两端同一值，写入本地 peer 记录）
3. peer 记录（<data_dir>/peers/<peer_id>.json）：
     { peer_id, pairing_version, paired_at, session_keys_ref }
4. session_keys 经 key_store.save_peer_session 加密落盘
5. 身份私钥已由 key_store.save_identity 落盘
```

### 7.6 配对版本号语义

- `pairing_version` 在配对完成时生成，两端各自存储**同一随机值**。
- 握手/重连时在 `hello` 中携带；与本地记录比对不一致 → 视为配对已被对方吊销/重配 → 清除本地配对并提示重新配对（覆盖 revoke 丢失场景，见 spec §3.3.6）。

---

## 8. queue.py（plan S5，M2 自带 schema）

### 8.1 SQLite schema（`sync_queue.db`）

```sql
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  peer_id     TEXT NOT NULL,          -- 目标伴侣 peer_id
  seq         INTEGER NOT NULL,
  type        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  expires_at  INTEGER,                -- NULL=不过期
  created_at  INTEGER NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending'   -- pending | sent | failed
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_seq ON outbox(peer_id, seq);

CREATE TABLE IF NOT EXISTS seen_events (
  from_peer   TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  PRIMARY KEY (from_peer, seq)
);
```

- 本 schema 为 **M2 内联基线**；data-consistency 阶段将 outbox/seen 并入统一 `events` 表（`event_id = "{from}:{seq}"`）并做版本迁移。本文件即迁移前事实标准。

### 8.2 发送侧操作

| 方法 | 行为 |
|------|------|
| `enqueue(peer_id, seq, type, payload_json, expires_at) -> None` | 插入 `status='pending'`；网络不可用时调用 |
| `flush(peer_id, send_cb) -> list[Exception]` | 取 `status='pending'` 按 `seq` 升序，逐条 `send_cb(envelope)`；发送成功置 `sent`，失败保留 `pending`；`expires_at < now` 置 `failed` |
| `mark_failed(id) -> None` | 超期/发送失败提示后置 `failed` |
| `next_seq(peer_id) -> int` | 读 meta，`{peer_id}:next_seq`；见 §3.1 |

- 补发保证：`flush` 只推进 `sent`/`failed`，未成功的留在 `pending`，下次重连再次 `flush`，天然幂等。

### 8.3 接收侧去重（单事务）

```text
receive(Message m):
  if exists seen_events(from_peer=m.from_id, seq=m.seq):  # 已见 → 丢弃
      return
  begin tx:
      insert into seen_events(...)
      deliver(m)          # 回调 Core（同一事务内投递，避免"已记录未投递"竞态）
  commit
```

- 不同发送方同 `seq` 不冲突（PK 含 from_peer）。
- `deliver` 抛异常 → 回滚（不留下半插入），事件进入失败日志。

---

## 9. discovery.py + transport.py（plan S6）

### 9.1 discovery.py

```python
class DiscoveryHandle:
    def __init__(self, zc, info): ...
    def unpublish(self) -> None: ...   # 注销本地注册表项 + 关闭 Zeroconf

def publish(service: str, name: str, port: int, txt: dict) -> DiscoveryHandle: ...
def resolve(service: str, name: str, timeout: float = 3.0) -> tuple[str, int] | None: ...
```

- 常规发现：`publish(MDNS_SERVICE, f"tuanzi-{instance_id}", port, {"peer": peer_id})` 返回句柄（**句柄必须由调用方保存**，否则服务立即消失）；`resolve(MDNS_SERVICE, f"tuanzi-{instance_id}", ...)` 拿 `(host, port)`。
- 配对发现：`publish(MDNS_PAIR_SERVICE, f"pair-{session_id_b32}", port, {})`；`resolve(MDNS_PAIR_SERVICE, f"pair-{session_id_b32}", ...)`。
- **同机兜底（本地注册表）**：`publish` 把 `(service, instance_name) → (127.0.0.1, port)` 记入进程内 `_local_registry`；`resolve` **优先命中注册表**，未命中才走真实 mDNS。作用：双实例同机联调不依赖多播（WSL/部分虚拟网卡不支持多播，见下方 §9.4）。跨机仍走真实 mDNS。
- **阻塞隔离**：zeroconf 的 `register/get_service_info` 是阻塞调用（内部自带事件循环），在 sync 的 asyncio loop 内同步调用会抛 `EventLoopBlocked`。所有 `publish`/`resolve`/`unpublish` 一律经 `asyncio.to_thread` 挪到工作线程执行。

### 9.2 Transport 抽象（中继预留）

```python
class Transport(ABC):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, peer_id: str, envelope: Envelope) -> None: ...  # 离线抛 TransportError
    def set_receiver(self, cb: Callable[[Envelope], None]) -> None: ...
    def set_state_cb(self, cb: Callable[["ConnState"], None]) -> None: ...
```

- `RelayTransport`：M3 实现，当前 `raise NotImplementedError`（占位类存在，保证接口稳定）。

### 9.3 连接状态机

```python
class ConnState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING   = "connecting"
    CONNECTED    = "connected"
    RECONNECTING = "reconnecting"
```

```text
paired 后启动:
  disconnected ──start──► connecting ──连接建立+hello 校验通过──► connected
  connecting ──失败/超时──► (按 backoff 退避后重试 connecting)
  connected ──心跳超时(3×30s) 或 socket 断开──► reconnecting
  reconnecting ──退避重试──► connecting；连续失败保持 reconnecting
  revoke/停止 ──► disconnected
```

### 9.4 LanTransport 实现要点

```python
class LanTransport(Transport):
    def __init__(self, cfg: SyncConfig, my_peer_id: str): ...
    # - 自身：asyncio 启动 WS 服务器（监听 cfg.ws_port_range 内首个可用端口）
    # - 对外：discovery.publish 常规服务（含 peer_id、port）
    # - 连接判定：peer_id 字典序小者主动 connect，大者等待（确定性避免双向双连接）
    # - 每条 WS 连接：连接建立后立刻 hello 交换（manager 提供 payload），校验配对版本
    # - 心跳：每 heartbeat_interval 发 hello；now - last_rx > interval*misses → 离线
    # - 收到密文 Envelope → set_receiver 回调（密文不解密，解密归 manager）
    # - send(): 无连接 → TransportError（调用方决定入队）
```

- **连接状态机与 runner**：`_runner` 是唯一连接主循环（active 重连 / passive 等待），两个关键守卫：
  1. **未配对分支吞 TimeoutError**：`asyncio.wait_for(peer_event.wait(), 2.0)` 超时抛 `TimeoutError`，若不在 runner 内捕获会杀死任务 → 主动方永远不连接。配对全程可能长于 2s（尤其 A 端 UI 确认），必须先配对完成再触发连接决策。
  2. **已连接分支保持 CONNECTED**：若 `_ws` 已绑定（被动方可能由 `_incoming_handler` 在配对落定**之前**就抢先 `_attach`，此时 runner 才刚得知 peer_id），runner **不得**置 CONNECTING 覆盖 CONNECTED，而应阻塞至连接断开（`_conn_lost.wait()`）。否则状态抖动会让 `manager._on_transport_state` 的 DISCONNECTED→CONNECTED 转换丢失 → `_on_connected` 不再触发 → 在线/离线消息全部失灵。
- **连接断开通知**：`_recv_loop` 的 `finally` 中（`self._ws is ws` 守卫通过后）执行三件事：`_ws=None`、`_set_state(DISCONNECTED)`、`_conn_lost.set()`。`_set_state(DISCONNECTED)` 缺失会吞掉重连后的 CONNECTED 回调（outbox 不 flush）；`_conn_lost` 供被动方 runner 与已连接分支阻塞。
- **stop() 先关连接再取消 runner**：若先 `cancel()` runner，`CancelledError` 会穿透 `_recv_loop` 的 `finally` 把 `self._ws` 清成 None，随后 `if self._ws` 关不到 → 对端收不到 CLOSE、其 `recv_loop` 永不退出、永远以为在线。因此 `stop()`/`disconnect_connection()` 均**先捕获并 `ws.close()`（限时 2s）再 cancel**。
- 双实例同机联调：`publish` 登记的本地注册表使 `resolve` 直接命中 `127.0.0.1:port`，不依赖多播（WSL/部分虚拟网卡不支持多播时 mDNS 解析恒为 None）。**注意**：实例 `stop()` 时 `unpublish` 会移除注册表项，对端随后 `resolve` 回落真实 mDNS（同机同样不可用）→ 表现为"无法发现已停止的实例"，属预期离线行为。
- 防火墙放行与手动 `IP:端口` 兜底见 [[7-AI学习/11-AI桌宠情侣互联/环境准备.md|环境准备]] 第八节。

---

## 10. manager.py（plan S7）

### 10.1 公开接口

```python
class PairingStatus(str, Enum):
    UNPAIRED = "unpaired"
    PAIRED   = "paired"

class PairingEvent(str, Enum):
    PEER_REQUEST = "peer_request"     # A 收到 pair.request，UI 应弹确认框
    PAIRED       = "paired"
    REVOKED      = "revoked"
    REVOKE_RECEIVED = "revoke_received"

class SyncManager:
    def __init__(self, cfg: SyncConfig, *,
                 on_event: Callable[[Message], None],
                 on_state: Callable[[ConnState], None],
                 on_pairing: Callable[[PairingEvent, dict], None]): ...

    def start(self) -> None: ...          # 起同步线程 + asyncio loop
    def stop(self) -> None: ...           # 优雅停止（关闭连接、落盘 queue）

    def add_handler(self, type: EventType, handler: Callable[[Message], None]) -> None: ...
    def send(self, type: EventType, payload: dict, *, expires_at: int | None = None) -> None: ...

    def pairing_status(self) -> PairingStatus: ...
    def connection_status(self) -> ConnState: ...
    def peer_id(self) -> str | None: ...

    # 配对
    def start_pairing(self) -> str: ...           # 返回短码
    def confirm_pairing(self, code: str) -> None: ...
    def confirm_peer(self, peer_id: str) -> None: ...   # A 侧 UI 确认后的落定
    def reject_peer(self, peer_id: str) -> None: ...
    def revoke_pairing(self) -> None: ...         # 发起吊销
```

### 10.2 send 流程（未配对禁止外发）

```text
send(type, payload, expires_at):
  if pairing_status != PAIRED: raise PairingError("未配对，禁止外发")
  if connection_status == CONNECTED:
      seq = next_seq(peer_id)
      msg = Message(v, type, my_id, seq, now, payload)
      env = Envelope(to=peer_id, cipher=encrypt(key_to_peer, message_to_json(msg)), expires=expires_at or 0)
      try: await transport.send(peer_id, env)
      except TransportError: queue.enqueue(...)     # 断线回退入队
  else:
      seq = next_seq(peer_id); queue.enqueue(...)   # 离线直接入队
```

### 10.3 接收流程

```text
transport 密文到达 → manager:
  1. env 校验（expires 且已过期 → 丢弃）
  2. msg = message_from_json(decrypt(key_from_peer, env.cipher))   # 解密失败 → CryptoError，记录并断开
  3. queue.receive(msg)（去重 + 单事务投递）:
       - 同步层内部类型（hello/hello.ack/pairing.revoke）在 manager 内处理，不投 Core
       - 业务类型 → 按 type 分发给 add_handler 注册的 handler（无 handler → 日志 + 忽略）
```

### 10.4 心跳与握手

```text
连接建立后（含重连）:
  1. 发送 hello{ pairing_version, device, instance_id }
  2. 收 hello.ack{ pairing_version, ... }
  3. 比对 pairing_version：
       一致 → connected 状态（通知 on_state）
       不一致 → 视为已吊销 → 清除本地配对 + on_pairing(REVOKED) + 断开（提示重新配对）
  心跳: 每 30s 发 hello；now - last_rx > 90s → 离线 → reconnecting
```

**握手触发条件**：`manager._on_connected` 由 transport 状态 `DISCONNECTED→CONNECTED` 转换触发（发 hello + flush outbox）。两个关键守卫：
- **未配对早退**：若连接建立先于配对落定（主动方先连上被动方、被动方尚未 `_apply_pairing`），`_on_connected` 在 `_peer_id` 为空时直接 return——否则 `_send_hello → _send_async` 会抛 `PairingError` 让任务静默死亡。
- **配对后补触发**：`_apply_pairing` 完成 `set_peer` 后，若 transport 状态已是 `CONNECTED`（连接早于配对建立），需手动再调一次 `_on_connected`，补发 hello 并 flush 本地 outbox。

**直发窗口限制**：`send()` 判定 `connection_status == CONNECTED` 即直发。TCP 半死连接（对端已停但本端尚未处理 CLOSE）下 `ws.send` 可能不抛异常而静默丢弃——该窗口内消息不保证送达（属 at-most-once 限制，最终由心跳超时兜底断开）。离线补发测试须先确认对端状态脱离 `CONNECTED` 再发，否则直发进半死 socket 导致消息丢失。

### 10.5 吊销配对

```text
发起方 revoke_pairing():
  1. 尽力 send(pairing.revoke, {})（若在线）
  2. 清除本地：key_store.delete_peer(peer_id) + 删除 peers/<peer_id>.json
  3. 状态 → UNPAIRED，断开连接

接收方收到 pairing.revoke:
  1. 清除本地 peer 记录与会话密钥
  2. 状态 → UNPAIRED，on_pairing(REVOKE_RECEIVED) 提示
```

---

## 11. UI（plan C1）

### 11.1 pairing_dialog.py

- 入口：托盘/设置菜单"添加伴侣 / 管理配对"。
- 三个视图：`生成短码`（A）/ `输入短码`（B）/ `配对状态`（已配对展示伴侣 peer_id 与"解除配对"按钮）。
- A 生成后倒计时显示剩余有效时间；B 输入错误给"剩余尝试次数"提示。
- 收到 `PEER_REQUEST` → 弹确认框：`与 {peer_id} 配对？[确认][拒绝]`。
- 所有同步线程回调经 **Qt 信号队列**（`Signal`）转发，不跨线程直接操作 UI。
- "解除配对" → `manager.revoke_pairing()`，确认弹窗防误触。

### 11.2 partner_state.py

- 展示：配对状态（未配对/已配对）、连接状态（disconnected/connecting/connected/reconnecting，中文文案）、伴侣 peer_id。
- 本文件为**共享文件**：mood-sync C1 后续在相同文件内补充情绪标签子区域，职责不重叠（连接/配对区 vs 情绪区）。

---

## 12. tools/sync_demo.py（plan C2）

```text
cd code
.venv\Scripts\python tools\sync_demo.py            # 常规（INFO）
.venv\Scripts\python tools\sync_demo.py --debug   # 含帧级日志
```

双端数据目录用临时目录（`tempfile.mkdtemp`），同机双实例经 discovery 本地注册表互通。执行序列：
1. 起实例 A、B（不同临时 data_dir → 不同 instance_id / 端口 / queue 文件）
2. A `start_pairing()` 取码 → B `confirm_pairing(code)`；A 收到 `PEER_REQUEST` 自动确认
3. 双方 `PAIRED` → 等待 `connected` → A 发 `msg.carry(c1)`、B 回 `msg.carry(c2)`，断言互达
4. 停 B → **等 A 状态脱离 `connected`**（主动方重连循环会反复置 connecting/reconnecting，不能只判 `== disconnected`）→ A 入队 3 条离线消息
5. 重启 B → 断言 3 条按 `seq` 升序补齐且不重复
6. 吊销：A `revoke_pairing()` → 双方 `UNPAIRED`
7. 全程 cipher 为密文（信封仅含 `{to, cipher, expires}`，无明文可读）

**多跑几次是必要的**：active/passive 角色由随机 peer_id 字典序决定，两种接线路径都要覆盖。

---

## 13. 端到端时序

### 13.1 首次配对（局域网）

```text
A: start_pairing → code=XXXX-XXXX-XXXX，临时 WS + mDNS 广播 pair-{sid}
B: confirm_pairing(code) → decode → resolve → 连接 A
B ──pair.request{pubB}──► A
A: attempts+1 → on_pairing(PEER_REQUEST)
A ──(用户确认) pair.accept{pubA, sasA, version}──► B
B: 算 sas 比对 → 通过 → derive_session → 持久化 → PAIRED
A: derive_session → 持久化 → code 一次性消费 → PAIRED
A/B: 各自保存 pairing_version → 连接状态机进入 connected
```

### 13.2 日常收发与离线补发

```text
A send(msg.carry) → seq=N → 在线 → 加密 → transport → B
B: 解密 → seen 去重 → 分发 carry 模块
B 离线: A 入队 pending → B 上线 → hello 校验 → connected → queue.flush 按 seq 升序补发
```

### 13.3 吊销后重连（离线吊销覆盖）

```text
B 离线期间 A revoke → A 清本地、重配（pairing_version 变化）
B 上线连接 A → hello 交换 pairing_version 不一致 → 双方判为已吊销 → 清本地 → 提示重新配对
```

---

## 14. 测试清单（断言级）

> 每个用例给出"输入 → 期望断言"。文件名对应 `tests/sync/test_<module>.py`。当前 **40 passed**。
> transport / manager 无独立单测文件，其端到端行为由 §12 `sync_demo` 全流程覆盖（配对→互发→离线补发→吊销）。

### 14.1 crypto（test_crypto.py，13 用例）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 加解密往返 | `encrypt(key, b"hello")` → `decrypt` | 返回 `b"hello"` |
| 篡改密文失败 | 密文改 1 字节 | `decrypt` 抛 `CryptoError` |
| 错键失败 | `decrypt(os.urandom(32), 密文)`（真随机异键） | 抛 `CryptoError` |
| 双向密钥分离 | A、B 各 `derive_session` | `A.key_to_peer == B.key_from_peer`；`A.key_from_peer == B.key_to_peer`；`A.key_to_peer != A.key_from_peer` |
| 公钥序列化稳定 | 同一私钥两次导出公钥 | bytes 相等 |
| SAS 一致 | 双方相同 shared+pub | 返回相同 4 字符 |
| SAS 随对方不同 | 换 peer pub | 返回不同 SAS |
| 短码往返 | 随机 5B → 生成码 → decode | 还原出原 session_id |
| 短码检错 | 码改 1 字符 | `decode` 抛 `PairingError` |
| 短码字符集 | 生成码全字符 | 均在 `CODE_CHARSET` 内 |
| 短码长度非法 | 长度不符 | 抛 `PairingError` |
| MAC 键独立 | `derive_mac_key` vs `key_to_peer` | 不等 |
| peer_id 派生稳定 | 同 pub 两次 derive | 等长且一致（32 位 base32） |

### 14.2 events（test_events.py，10 用例）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 事件注册表完备 | EventType 枚举 | 15 类型全收录、无重复值 |
| 内部/业务不相交 | 内部类型（hello/hello.ack/pairing.revoke） | 与业务类型集合无交集 |
| 消息往返全类型 | 各类型典型 payload → to_json → from_json | 全等 |
| 键序稳定 | 序列化两次 | JSON 键顺序一致（确定性输出） |
| 信封往返 | 任意 Envelope → to_json → from_json | 字段全等（cipher 字节一致） |
| 坏 JSON | `b"{"` | 抛 `ProtocolError` |
| 未知类型 | `{"v":1,"type":"x.y",...}` | 抛 `ProtocolError` |
| 版本不兼容 | `v=2` | 抛 `ProtocolError` |
| 缺字段 | 缺 `type`/`payload` | 抛 `ProtocolError` |
| 信封坏 cipher | 非 base64 | 抛 `ProtocolError` |

### 14.3 protocol（test_protocol.py，6 用例）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 序号递增 | next_seq ×3 | 1,2,3 |
| 重启不回退 | next_seq ×3 → 重建 SeqManager → next | 第 4 次 ≥ 4 |
| 每 peer 独立 | A、B 各自 seq | 互不干扰 |
| 去重键唯一 | (A,42) vs (B,42) | 键不等 |
| 版本兼容通过 | `check_version(v=1)` | 不抛 |
| 版本不兼容拒绝 | `v=2` | 抛 `ProtocolError` |

### 14.4 pairing（test_pairing.py，4 用例）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 握手成功 | 双端真实 WS 直连（monkeypatch mDNS 为 127.0.0.1:port） | 双方 PAIRED、B 得 A peer_id、session_keys 互通 |
| 非法短码 | 校验和错误 | `PairingError` |
| SAS 不一致 | 伪造 accept 携带错误 sas | B 端抛 `PairingError` 中止 |
| 一次性消费 | confirm 后再次 confirm | `PairingError`（会话已消费） |

### 14.5 queue（test_queue.py，7 用例）

| 用例 | 输入 | 期望断言 |
|------|------|---------|
| 入队/补发顺序 | 断网入队 seq 1,3,2 → flush | 按 1,2,3 升序发送 |
| 补发幂等 | flush 后未清 → 再 flush | 不重复发送（status=sent 跳过） |
| 过期失败 | expires_at 已过 → flush | 置 failed 且不发 |
| 发送失败保持 pending | send_cb 抛异常 → flush | 该项仍 pending、留待下次 |
| 去重 | 重复 (from,seq) receive | 仅投递 1 次 |
| 同 seq 不同 peer | (A,42) 与 (B,42) | 均投递 |
| 半插入回滚 | deliver 抛异常 | seen_events 无残留行 |

---

## 15. 集成验证（与 plan §5.2 对齐）

- 同机双实例（不同 `data_dir`）跑通：配对 → 互发 → 离线 → 重连补发 → 吊销。
- 抓包断言：局域网流量仅见 `Envelope` 三字段，无 `type/from/payload` 明文。
- 全部通过后，将本文档状态维持"已确认"，并把 `env 校验` 等实现细节作为 data-consistency 事件表迁移的输入。

---

## 16. 开放问题

| 问题 | 负责人 | 期望解决日期 |
|------|--------|:------------:|
| 无（实现规格试点，开放问题沿用 spec §7 两项） | — | — |
