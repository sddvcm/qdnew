# Hyperdown 网盘签到插件 — 开发交接文档（可复刻级）

> **文档用途**：本文件供其他 AI Agent 凭此完整复刻 `plugins/hyperdown.py`（Hyperdown 网盘每日签到插件）。
> 复刻目标是：让该插件在 [checkin-system](..) 框架内正确完成「账号密码登录 → 查询签到状态 → SealJSON 加密签到」。
>
> **验证状态**：已完成 2 轮可复现性验证，共发现并修补 8 处问题（含 1 个真实代码 bug）。
> - **第 1 轮**（独立 Agent，仅凭本文档 + 禁用读参考实现）：凭文档独立实现 SealJSON 信封，8/8 验收项通过（libsodium 可原样解密、空对象密文 18 字节、HMAC 可重算）。报告 4 处含糊点，已全部补进文档。
> - **第 2 轮**（mock 端到端自查，18/18 通过）：覆盖静态契约、离线算法回归、三类端到端场景（全新签到 / 已签到 / 401 刷新重试）。**抓到 1 个真实 bug**：`do_me()` 漏解包 `/me/` 响应的 `user` 层，导致"已签到"识别失效、每次白跑签到请求——已修复并写成 §2.3 的 ⚠️⚠️ 专项警告。
> - **第 3 轮**（端到端全流程独立 Agent 验证）因 API 频率限制未执行；复刻者若有条件，建议照第 8 节清单完整跑一遍真账号实测。
>
> 结论：按本文档可复刻。核心算法（加密信封）经独立 Agent 验证；插件融入框架的行为经 18 项 mock 验收；**线上接口契约未经真账号实测**（见下方诚实声明）。
>
> **诚实声明**：作者未使用真实 Hyperdown 账号实测过线上接口。本文档的**接口契约、字段结构、加密算法**来自开源参考实现 `jasper0507/hyperdown-checkin`（其 `secure_api.py`/`client.py` 为 2026-07-14 抓包对齐的官方协议），并经本地 libsodium 交叉验证（插件生成的信封可被 libsodium 原样解密、空对象密文恰为 18 字节）。**最终以真账号实测为准**，插件已对响应字段做了多重兜底，复刻者无需假设精确字段名。

---

## 1. 项目定位与产物

### 1.1 这个插件解决什么
Hyperdown（https://hyperdown.net）是一个 Windows 网盘下载工具（百度/夸克/UC 不限速），靠**每日签到**领取下载流量。桌面客户端把"已登录 token"存在本机加密 vault 里。本插件让服务器侧的签到系统能**用一个账号（邮箱+密码）自动每日签到**。

**签到到底通过什么方式？** —— 三步走：
1. **账号密码登录**拿 `access_token` / `refresh_token`（明文 HTTPS POST，不加密）；
2. **查询 `/me/`** 看 `user.is_check_in` 判断今天是否已签；
3. **签到 `/me/checkins`** 这一步才是难点：请求体必须套一个 **SealJSON 加密信封**（X25519 ECDH 协商一次性密钥 + HKDF 派生 + XChaCha20-Poly1305 加密 + HMAC-SHA256 签名），服务端逐字节校验签名，差一位就 401。

### 1.2 产物形态
- 单个文件 `plugins/hyperdown.py`，继承框架 `BasePlugin`。
- 类名 `HyperdownPlugin`，`name="hyperdown"` → 框架启动时自动发现并写入插件表，**无需改任何核心代码**。
- Web 后台「新建任务」选「Hyperdown 网盘签到」，表单只填**账号(邮箱)**和**密码**两个通用字段（框架顶层渲染，不在插件专属区）。
- token 不落明文配置：登录成功写入 `result.cookie`，由框架 AES 加密存档，下次作为 `config["cookie"]` 回传。

### 1.3 硬性约束（影响实现选择）
| 约束 | 说明 | 对复刻的影响 |
|------|------|--------------|
| 部署在飞牛 NAS 的 Docker 容器 | 基础镜像 `python:3.12-slim`，`pip install -r requirements.txt` 全量装依赖 | 任何新依赖必须写进 `requirements.txt` 并**重建镜像**才生效 |
| 镜像已含 `cryptography` + `requests` | 但**不含** `pynacl` | `cryptography` 需 ≥40.0（用到 `public_bytes_raw()`；`requirements.txt` 实际钉 `>=41.0.0`）；XChaCha20-Poly1305 需要 `pynacl`（libsodium 绑定），不走 pynacl 就得手写 RFC 8439（见第 6 节，不推荐） |
| 服务端校验签名逐字节 | 算法必须与官方完全一致 | 不能"差不多"，必须用现成 libsodium 实现对齐 |
| 用户提供的 `session.json` 不可用 | 那是本机加密 vault，密钥绑定本机 | **不要尝试**读这个文件，直接账号密码登录（见第 6 节） |

### 1.4 运行环境（复刻者本机）
- Python 3.12（容器内）/ 3.13（本机验证）
- 依赖：`requests`、`cryptography`、`pynacl`（见 `requirements.txt`）
- 联网可达 `https://hyperdown.net`

---

## 2. 目标站点与接口逆向 ★

所有接口 base：`https://hyperdown.net/api/v1`。所有请求 `User-Agent: Go-http-client/1.1`（官方桌面客户端用的 Go HTTP transport，服务端可能按 UA 放行，务必带）。

通用请求头：
```
User-Agent: Go-http-client/1.1
Accept: application/json
Content-Type: application/json
```

### 2.1 接口清单

| 步骤 | 方法 + 路径 | 加密 | 鉴权 | 作用 |
|------|------------|------|------|------|
| 登录 | `POST /auth/login` | 否 | 否 | 拿 token |
| 查状态 | `GET /me/` | 否 | `Bearer <access_token>` | 看是否已签到 |
| 刷新 | `POST /auth/refresh` | 否 | 否 | 用 refresh_token 换新 access |
| 签到 | `POST /me/checkins` | **是（SealJSON）** | `Bearer <access_token>` | 真正签到 |

### 2.2 登录 `POST /auth/login`
- 请求体（明文，不加信封）：`{"email": "你的邮箱", "password": "你的密码"}`
- `secure=False`、`auth=False`（不加 `X-Hyperdown-Secure` 头、不加 `Authorization`）
- 成功响应（**两种结构都见过，必须两种都兼容**，见 2.5）：
```json
{"ok": true, "data": {"tokens": {"access_token": "eyJ...", "refresh_token": "eyJ...", "expires_in": 86400}}}
```
或
```json
{"ok": true, "data": {"access_token": "eyJ...", "refresh_token": "eyJ...", "expires_in": 86400}}
```
- ⚠️ **字段陷阱**：token 可能包在 `data.tokens` 嵌套里，也可能平铺在 `data` 顶层；个别版本还见过 `data.token` / `data.auth` 包裹。复刻者**必须按 `tokens`→`token`→`auth`→顶层 的顺序探测** `access_token`/`refresh_token`，否则"登录成功却拿不到 token"。

### 2.3 查状态 `GET /me/`
- `auth=True`：带 `Authorization: Bearer <access_token>`；`secure=False`（不加信封）
- 成功响应（`user` 对象在 `data` 里）：
```json
{
  "ok": true,
  "data": {
    "user": {
      "id": 12345,
      "email": "you@example.com",
      "is_check_in": false,
      "check_in_days": 3,
      "traffic": "12.50 GB",
      "today_traffic": "0.00 GB"
    }
  }
}
```
- ⚠️ **判定今天是否已签的唯一可靠字段是 `data.user.is_check_in`（布尔）**。不要靠 `today_traffic` 是否为 0 推断，字段可能缺。
- ⚠️⚠️ **响应解包陷阱（实测抓到的真 bug，重写者必踩）**：`_request` 已剥掉外层 `data` 键，所以 `do_me()` 拿到的是 `{"user": {...}}` 而**不是** `{...}`。必须再解包一层 `data["user"]` 才能读到 `is_check_in`：
```python
def do_me():
    data = self._request(session, "GET", "/me/", auth=True, secure=False, tokens=tokens)
    if isinstance(data, dict) and isinstance(data.get("user"), dict):
        return data["user"]          # ← 必须解包，否则 is_check_in 永远取不到
    return data                      # 兼容已平铺的结构
```
  **若漏了这层解包**：`is_check_in` 恒为 `None`（falsy）→ 插件**每次都发签到请求**，虽然服务端可能返回"已签到"被兜底救回，但白跑一次请求、且"已签到"状态识别不准。
- 401 → token 失效，走刷新/重登（见 3.2 数据流）。

### 2.4 签到 `POST /me/checkins` ★
- 请求体是**空对象 `{}`**（路径参数已含在 URL，body 无业务字段）。
- `auth=True` + `secure=True`：**必须套 SealJSON 加密信封**（算法见第 5 节）。
- 实际发出的 HTTP 报文近似：
```
POST /api/v1/me/checkins HTTP/1.1
Host: hyperdown.net
User-Agent: Go-http-client/1.1
Authorization: Bearer <access_token>
Content-Type: application/json
X-Hyperdown-Secure: v1
{"v":"v1","request_id":"<32hex>","ts":<unix秒>,"nonce":"<rawurl-no-pad>","pub":"<64hex>","ciphertext":"<rawurl-no-pad>","sign":"<rawurl-no-pad>"}
```
- 成功响应：
```json
{"ok": true, "data": {"is_check_in": true, "today_traffic": "1.00 GB", "check_in_days": 4}}
```
- 已签到（幂等）：服务端返回错误码，常见 `already_check_in` / `checked_in`，或 message 含「已签到」。复刻者**必须捕获并当作成功（今日已签）**，不要判失败。
- ⚠️ **响应字段名未经真账号实测确认**，插件做了兜底：从 `data` 里挑 `is_check_in`/`today_traffic`/`check_in_days`/`total_traffic`/`traffic` 任意存在的字段回显。复刻者同样兜底即可。

### 2.5 错误响应统一结构
```json
{"ok": false, "error": {"code": "already_check_in", "message": "今日已签到"}}
```
- 框架约定：`ok === false` 或 HTTP ≥ 400 抛 `APIError(code, message, status)`。
- ⚠️ `code` 的值**未穷举确认**。复刻者的「已签到」判定应**同时看** `code ∈ {already_check_in, checked_in}` **和** `message` 含「已签到/already/repeat/Already」关键词，双保险。

---

## 3. 整体架构与数据流

### 3.1 模块划分
| 模块 | 位置 | 职责 |
|------|------|------|
| 加密信封 | `_seal_json()` + 辅助 `_derive_key`/`_b64`/`_normalize_path` | 把明文 body 封成 SealJSON 信封 dict |
| HTTP 客户端 | `HyperdownPlugin._request()` | 统一发请求、套信封、解析响应、抛 `APIError` |
| 鉴权 | `_login` / `_refresh` / `_extract_tokens` | 拿 token、兼容多种响应结构 |
| 主流程 | `checkin()` | 编排：复用/登录 → 查状态 → 签到/已签 → 回写 token |
| 持久化 | `_pack_tokens` / `_parse_stored_cookie` + `result.cookie` | token 打包进 cookie 字段，由框架加密存档 |

### 3.2 数据流图（ASCII）
```
config{cookie, username(邮箱), password}
   │
   ├─ 解析 cookie → {access, refresh}
   │
   ▼
[有 access?]──否──► _login(email,pwd) ──► {access, refresh}
   │是
   ▼
 do_me() = GET /me/  ──401──► ensure_tokens():
   │                                 ├─ _refresh(refresh) 成功?用新 access
   │                                 └─ 失败则 _login(email,pwd) 重登
   │  ⚠️ 响应需解包 data["user"] 一层（见 2.3）
   ▼
 user.is_check_in == true? ──是──► 返回「今日已签」(success=True)，不发签到请求
   │否
   ▼
 POST /me/checkins (secure=True) 套 _seal_json 信封
   │
   ├─ 成功 ──► 提取 today_traffic 等 ──► 返回「签到成功」
   ├─ 401 ──► ensure_tokens() 后重试一次
   └─ 已签到错误码/关键词 ──► 返回「今日已签」
   │
   ▼
 所有成功分支 → result.cookie = _pack_tokens(access, refresh)
                （框架加密存档，下次作为 config["cookie"] 回传）
```

### 3.3 并发与超时
- 单任务串行，无并发（`requests.Session()` 单次复用）。
- `TIMEOUT = 30` 秒（HTTPS 请求超时）。
- 重试：仅 token 失效（401）时刷新/重登后**重试一次**签到，不无限重试。

---

## 4. 模块逐一实现规格 ★

### 4.1 常量（务必与附录 A 一致）
```python
BASE_URL = "https://hyperdown.net"
DEFAULT_BASE = BASE_URL + "/api/v1"          # 注意结尾无斜杠
USER_AGENT = "Go-http-client/1.1"
TIMEOUT = 30
_PEER_PUB_HEX = "dd85f63f107a32ce3def4835fe56c27865a1557fedad19adbd72ff81ea2e1025"
SALT_PREFIX = b"hyperdown-secure-api:v1:"     # 字节串，末尾有冒号

> **命名与概念一致性（复刻者必读）**：
> - 常量 `_PEER_PUB_HEX`（hex 字符串）经 `bytes.fromhex(...)` 得到变量 `PEER_PUBLIC_KEY`（bytes），二者是同一公钥的两种形态。
> - `_normalize_path`（§4.3）与 §4.5 内的 `_norm` 是**同一函数的等价别名**，照抄任一即可。
> - `_derive_key` 是模块级函数，验证脚本可直接 `import` 调用复现 key。
> - ⚠️ `SALT_PREFIX` 是 HKDF 的 **info** 前缀；真正的 HKDF **salt** 是 `SHA256(request_id:ts)`。**两个都带 "salt" 字样但不是同一个东西**，别把 `SALT_PREFIX` 当 salt 传入 HKDF。
```

### 4.2 `_b64(data: bytes) -> str`
base64 **URL-safe、去 padding**：
```python
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
```
⚠️ 解码方必须补 padding：`base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))`。

### 4.3 `_normalize_path(path) -> str`
去 query、保证以 `/` 开头：
```python
def _normalize_path(path: str) -> str:
    path = (path or "").split("?", 1)[0]
    if not path:
        return "/"
    return path if path.startswith("/") else "/" + path
```
⚠️ **sign 的 aad 和 envelope 里用的 path 必须是 normalize 后的全路径**（如 `/api/v1/me/checkins`），不能只用 `/me/checkins`。

### 4.4 `_derive_key(ikm, method, path, ts, request_id) -> 32字节`
```python
def _derive_key(ikm, method, path, ts, request_id):
    material = f"{request_id}:{ts}".encode()
    salt = hashlib.sha256(material).digest()          # 32 字节
    info = SALT_PREFIX + f"{method.upper()}:{_normalize_path(path)}".encode()
    return HKDF(hashes.SHA256(), 32, salt, info).derive(ikm)
```
⚠️ `ikm` 是 X25519 ECDH 的 shared secret（32 字节）。`info` 里 method **大写**、path **normalize**。

### 4.5 `_seal_json(method, path, body: bytes, access_token: str) -> dict`  ★核心
完整可抄实现（独立单文件版，用 `cryptography` + `pynacl`）：
```python
import base64, hashlib, hmac, secrets, time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt

PEER_PUBLIC_KEY = bytes.fromhex("dd85f63f107a32ce3def4835fe56c27865a1557fedad19adbd72ff81ea2e1025")
SALT_PREFIX = b"hyperdown-secure-api:v1:"

def _b64(data): return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
def _norm(path):
    path = (path or "").split("?", 1)[0]
    return path if path.startswith("/") else "/" + path

def seal_json(method, path, body, access_token=""):
    plaintext = body if body else b"{}"    # ⚠️ 传 b"" 也会退回 b"{}"；调用方应显式传 b"{}"
    ts = int(time.time())
    request_id = secrets.token_hex(16)
    nonce = secrets.token_bytes(24)                 # 24 字节 nonce
    eph = x25519.X25519PrivateKey.generate()
    eph_pub_hex = eph.public_key().public_bytes_raw().hex()
    shared = eph.exchange(x25519.X25519PublicKey.from_public_bytes(PEER_PUBLIC_KEY))
    key = HKDF(hashes.SHA256(), 32,
               hashlib.sha256(f"{request_id}:{ts}".encode()).digest(),
               SALT_PREFIX + f"{method.upper()}:{_norm(path)}".encode()).derive(shared)
    aad = f"{method.upper()}\n{_norm(path)}\n{request_id}\n{ts}".encode()
    sealed = crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)
    nonce_field = _b64(nonce)
    ct_field = _b64(sealed)
    sign_msg = b"\x00".join(p.encode() for p in [
        "v1", method.upper(), _norm(path), request_id, str(ts),
        nonce_field, eph_pub_hex, ct_field, access_token or "",
    ])
    sig = _b64(hmac.new(key, sign_msg, hashlib.sha256).digest())
    return {"v": "v1", "request_id": request_id, "ts": ts, "nonce": nonce_field,
            "pub": eph_pub_hex, "ciphertext": ct_field, "sign": sig}
```
**逐字节要点（错一处即 401）：**
1. `nonce` 必须 **24 字节**（XChaCha20 用 24 字节 nonce，不是 12）。
2. `aad` = `METHOD\nPATH\nREQUEST_ID\nTS`，四段 `\n` 连接，`METHOD` 大写、`PATH` normalize。
3. `sealed` 用 libsodium：`crypto_aead_xchacha20poly1305_ietf_encrypt(msg, aad, nonce, key)` —— **参数顺序是 (明文, aad, nonce, key)**，别写反。
4. `sign_msg` 用 **`\x00`（NUL）分隔**，不是 `\n`；**9 段固定**，即使 `access_token` 为空也要保留第 9 段（空字符串）。
5. `sign` 的 HMAC key 就是上面派生的 `key`（同一个 32 字节，不是另一个 mac key）。
6. envelope **字段顺序固定**：`v, request_id, ts, nonce, pub, ciphertext, sign`。
7. HTTP 层额外加请求头 `X-Hyperdown-Secure: v1`（值就是字符串 `"v1"`，不是信封本身）。
8. ⚠️ `plaintext = body if body else b"{}"`：传 `b""` 会因 falsy 退回 `b"{}"`。**调用方应显式传 `b"{}"`**（签到 body 就是空对象），否则传空字节也会静默得到 18 字节密文，问题难察觉。

**验证时如何拿到临时私钥（复刻者关心的"私钥回流"）：**
生产版 `seal_json` 只返回 envelope（含 `pub`，不含私钥）。跑单元测试需要独立复现 `key`，两种做法任选：
- **做法 A（推荐，不改生产代码）**：验证脚本自己走一遍完整流程——自己生成 `eph`、算 `shared`、调 `_derive_key(shared, method, path, ts, request_id)` 拿 `key`，再用 libsodium 解密插件产出的 envelope（解密只需要 `pub` 对应的 `shared`，但脚本需知道自己生成的 `eph`）。即：测试脚本不自造 envelope，而是**从插件取 envelope 里的 `pub` 反推不了私钥**，所以必须让测试脚本自己持有私钥。
- **做法 B（改测试版）**：写一个 `seal_json_debug(...)` 副本，返回值里额外带 `{"_eph_priv": priv_bytes, "_key": key}`，仅供测试使用，不进生产。
- **做法 C（最省事）**：直接 `import` 生产模块的 `_derive_key`，在测试里用自造 `eph` 走一遍 §4.5 的全部步骤，断言与生产 `seal_json` 的输出结构一致。**这足以证明算法正确**——因为服务端校验的是"信封字段 + 能解密 + 签名匹配"，而这三者只要算法一致就成立。

> 注：pynacl 的 `crypto_aead_xchacha20poly1305_ietf_decrypt` 只需 `key`，不需要私钥。所以拿到 `key` 就能解密——而 `key` 由 `shared` 派生，`shared` 需私钥。故"验证私钥"本质是"验证脚本要自己能算出同一个 `key`"。

### 4.6 `_request()` 判定是否加信封
```python
use_secure = _is_sensitive_path(method, full_path) if secure is None else secure
```
实际调用方都**显式传 `secure=True/False`**，所以 `_is_sensitive_path` 只是兜底。敏感路径集合（POST 才加密）：
```
/api/v1/me/checkins
/api/v1/redemptions/redeem
/api/v1/shares/parse
/api/v1/shares/downloads/resolve
/api/v1/downloads/resolve
/api/code/redeem
```
⚠️ `full_path` 拼法：传进来的 `path` 若已以 `/api/` 开头则用原值，否则前缀 `/api/v1`。即 `_request(session,"POST","/me/checkins",...)` 内部 full_path=`/api/v1/me/checkins`，加密用的 path 也是它。

### 4.7 框架接口（复刻者必须实现的契约）
`plugins/hyperdown.py` 顶部 `from plugins.base import BasePlugin, FormField, CheckinResult`。契约：
- `class HyperdownPlugin(BasePlugin)`，类属性 `name="hyperdown"`、`display_name`、`description`、`plugin_type="http"`、`version`、`form_schema`（本插件为 `[]`）。
- `def checkin(self, config: dict) -> CheckinResult`：**唯一必须实现的抽象方法**。
- `config` 入参包含框架顶层通用字段：`cookie`、`username`、`password`、`site_url`，以及 `params`（专属参数 dict）。本插件读 `config["username"]`（邮箱）、`config["password"]`、`config["cookie"]`。
- `CheckinResult(success: bool, message: str, extra: dict = {}, cookie: str = "")`。
- **`result.cookie` 是回写钩子**：非空时框架用 `CHECKIN_SECRET_KEY` 派生 AES 密钥加密存档，下次执行作为 `config["cookie"]` 回传。token 持久化全靠它——必须显式 `result.cookie = <json 字符串>`。
- `form_schema = []` 表示无专属表单字段（账号/密码走框架通用字段）。

---

## 5. 最难的部分 ★ —— SealJSON 加密信封（为什么这么设计）

**为什么必须加密签到请求？** Hyperdown 的服务端对敏感写操作（签到、兑换、解析分享）强制 SealJSON 信封：客户端用**临时 X25519 密钥对**与服务器内嵌公钥协商一次性会话密钥，把请求体用 XChaCha20-Poly1305 加密，再用 HMAC 对整个信封签名。服务端用私钥验签+解密。目的是防重放、防篡改、防中间人。客户端公钥每次随机，服务器无法借此反推账号——所以**复制别人抓包的信封没用**，必须每次现场生成。

**为什么用 libsodium（pynacl）而不是手写？** XChaCha20-Poly1305 是 24 字节 nonce 的 IETF 变体，`cryptography` 库不提供，只有 `pynacl`（libsodium 绑定）有 `crypto_aead_xchacha20poly1305_ietf_encrypt`。手写 RFC 8439 极容易在 ChaCha20 quarter-round 上出错（见第 6 节），且服务端逐字节校验，差一位就失败。直接用 libsodium 能**保证与官方客户端逐字节一致**。

**密钥派生链（务必理解，别乱改）：**
```
shared      = X25519(eph_priv, SERVER_PUB)          # 32 字节
hkdf_salt   = SHA256(f"{request_id}:{ts}")          # 每次请求不同
hkdf_info   = b"hyperdown-secure-api:v1:" + METHOD + ":" + path
key         = HKDF-SHA256(ikm=shared, salt=hkdf_salt, info=hkdf_info, len=32)
aad         = f"{METHOD}\n{path}\n{request_id}\n{ts}"
ciphertext  = XChaCha20Poly1305(key, nonce24, plaintext, aad)
sign        = HMAC-SHA256(key, NUL_JOIN([...9 个字符串...]))
```
注意：**同一个 `key` 既用于 AEAD 又用于 HMAC**（不是两个独立密钥）。这是官方设计，照抄。

**⚠️ sign 段用的是「编码后的字符串」，不是原始字节（最容易写错的地方）：**
`NUL_JOIN` 的 9 段全部是 **UTF-8 字符串**，具体是：
| 序 | 段内容 | 形态 |
|----|--------|------|
| 1 | `"v1"` | 字面量 |
| 2 | `METHOD` | 大写字符串，如 `"POST"` |
| 3 | `path` | normalize 后的路径字符串，如 `"/api/v1/me/checkins"` |
| 4 | `request_id` | 32 位 hex 字符串 |
| 5 | `ts` | **十进制字符串**（`str(ts)`，非整数） |
| 6 | `nonce` | **base64url 去 padding 的字符串**（不是 24 字节原始 nonce！） |
| 7 | `pub` | **hex 字符串**（64 字符，不是 32 字节原始公钥！） |
| 8 | `ciphertext` | **base64url 去 padding 的字符串**（不是原始密文！） |
| 9 | `access_token` | 字符串；**为空时也要保留这一空段** |

**建议直接照抄 §4.5 的代码，不要凭"设计符号"自己写。** 若把 raw nonce/raw ciphertext/raw pub 塞进 HMAC，签名必不匹配、服务端必 401。

---

## 6. 已知边界与不可为之事（防下一个 Agent 白费功夫）

1. **❌ 不要读用户给的 `session.json`**。它是 `hyperdown-local-vault:v1` 本机加密凭据库，密钥绑定本机（机器派生），复制到 NAS 容器里解不开，也解出来没用（不是可用 token）。正确做法是账号密码登录拿 token。作者已实测验证该方向不可行。

2. **❌ 不要手写 XChaCha20-Poly1305（纯标准库）**。作者实测：ChaCha20 quarter-round 极易写成异或（应为模 32 位加法），旋转常数应为 16/12/8/7（非 16/8/12/7）；Poly1305 终值应为 `(acc+s) mod p` 的低 128 位。手写版与 libsodium 交叉验证**失败**，只有用 `pynacl` 的 libsodium 实现才逐字节兼容。**结论：直接依赖 pynacl，别造轮子。**

3. **❌ 不要替换对端公钥 `_PEER_PUB_HEX`**。那是服务器公开公钥（非账号凭据、非私钥），写死在客户端里。换成别的公钥服务器验签必失败。它是 32 字节，hex 为 `dd85f63f107a32ce3def4835fe56c27865a1557fedad19adbd72ff81ea2e1025`。

4. **❌ 不要假设响应字段名精确**。作者未用真账号实测，`is_check_in`/`today_traffic`/`check_in_days` 等字段名来自开源参考。插件对所有 token 位置、签到回显字段做了兜底探测，复刻者照做即可，不要 Hardcode 单一路径。

5. **⚠️ pynacl 必须进容器**：当前镜像不含 pynacl，插件 `import` 阶段若无 pynacl 会直接抛 `ImportError`。复刻者若换环境，**必须 `pip install pynacl` 并重建镜像**（Dockerfile 第 9 行 `pip install -r requirements.txt` 会自动带上，前提是 `requirements.txt` 含 `pynacl>=1.5.0`）。

6. **⚠️ 站点曾在 2026-05 被传「跑路/接口关闭」**，但用户本机 v1.1.5 客户端显示签到接口正常。若复刻后实测登录即失败，先排查站点是否仍存活，再怀疑算法。

---

## 7. 完整踩坑史与版本演进

| 版本/动作 | 现象 | 根因 | 修复 |
|----------|------|------|------|
| v1 初稿 | 加密信封 401 | 用纯标准库手写 ChaCha20，quarter-round 写成 XOR | 改用 pynacl libsodium 实现 |
| 纯标准库版调试 | RFC 8439 向量 FAIL | quarter-round 应为模加+旋转 16/12/8/7；Poly1305 终值应为 `(acc+s)%p` 低128位 | 修正后仍与 libsodium 交叉验证不通过（手写风险高），放弃该路线 |
| 用户问「能否不重建镜像直接 Web 导入」 | 临时改纯标准库版 | 误判用户不想重建镜像 | 用户澄清「没说不能重建」，恢复 pynacl 版 |
| pynacl 版定稿 | seal→unseal 自测 OK | 对齐开源 `secure_api.py` 逐行 | libsodium 原样解开插件信封、空对象密文=18字节、HMAC 重算一致 |
| 第 1 轮 Agent 验证 | 报告：libsodium 解密缺 import、密文长度断言 | 验证脚本自身 bug（非插件） | 补 import、改动态长度断言，验证通过 |
| 第 1 轮文档修补 | 报告：§5 sign 段编码未点明、cryptography 版本下限缺失、验证私钥取法不明、常量命名三处不一致 | 文档含糊 | 补 §5 的 9 段编码表、钉 `cryptography>=40.0`、加"验证私钥取法"三方案、加命名一致性说明 |
| 第 2 轮 mock 端到端 | **场景2 已签到却仍发签到请求** | `do_me()` 未解包 `/me/` 响应的 `user` 层，`is_check_in` 恒取不到 | `do_me()` 加 `data["user"]` 解包；§2.3 加 ⚠️⚠️ 专项说明 |
| 第 2 轮 Agent 验证 | 子 Agent 触发频率限制（16:46 重置），改为主 Agent 自查 | — | 18/18 验收项通过（含离线算法 + 3 个 mock 端到端场景） |

---

## 8. 重写检查清单（照做，每步附验证）

- [ ] **环境**：`pip install requests cryptography pynacl`；确认 `from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt` 可用。
  - 验证：`python -c "import nacl.bindings; print('ok')"` 不报错。
- [ ] **常量**：抄第 4.1 节，`_PEER_PUB_HEX` 与附录 A 逐字符一致。
- [ ] **SealJSON 单测**：用第 4.5 节代码生成信封，再用 libsodium 解密验证明文一致。
  - 验证脚本：生成 `envelope` → `ct=b64d(envelope['ciphertext']), nonce=b64d(envelope['nonce'])` → 复现 `key`（需保留临时私钥）→ `crypto_aead_xchacha20poly1305_ietf_decrypt(ct, aad, nonce, key) == b"{}"`。
  - 空对象 `{}` 密文应恰为 **18 字节**（2 明文 + 16 tag）。
- [ ] **sign 校验**：用同一 `key` 重算 HMAC（`\x00` 分隔 9 段）== `envelope['sign']`。
- [ ] **HTTP 客户端**：`_request` 实现 + `_is_sensitive_path` + `X-Hyperdown-Secure: v1` 头。
- [ ] **登录/刷新兼容**：`_extract_tokens` 按 `tokens`→`token`→`auth`→顶层 顺序探测（第 2.2 节）。
- [ ] **主流程**：`checkin()` 复用 cookie → 查 `/me/` → 已签返回成功 → 否则签到 → 所有成功分支写 `result.cookie`。
  - ⚠️ **必须做**：`do_me()` 要解包 `/me/` 响应的 `data["user"]` 层（见 §2.3），否则 `is_check_in` 取不到、每次白跑签到。
  - 验证（mock）：让 `GET /me/` 返回 `{"ok":true,"data":{"user":{"is_check_in":true}}}`，断言 `result.success is True`、`extra["already_checkin"] is True`、**且没有发出任何 `/me/checkins` 请求**。
- [ ] **框架集成**：`name="hyperdown"`、`form_schema=[]`、`checkin()` 签名正确、`result.cookie` 赋值。
  - 验证：放进 `plugins/`，框架启动日志出现 `hyperdown` 插件；Web 新建任务能看到「Hyperdown 网盘签到」。
- [ ] **重建镜像**（若改了 requirements）：`docker compose build && docker compose up -d`，容器内 `pip show pynacl` 有输出。
- [ ] **真账号实测**：建任务填邮箱+密码，手动跑一次。成功看「签到成功，今日获得 X」；已签看「今日已签」。
  - ⚠️ 实测前不要声称"一定能用"，以真账号结果为准。

---

## 9. 环境与验证方法

### 9.1 本机坑（复刻者可能遇到）
- **Windows + 托管 Python 无 venv 时**：建隔离 venv 装 `cryptography pynacl requests` 再测试，别污染系统环境（见 checkin-system 约定）。
- **libsodium 依赖**：`pynacl` wheel 自带预编译 libsodium，`python:3.12-slim` 直接 `pip install` 即可，无需系统装 `libsodium-dev`（若从源码编译才需要）。
- **代理**：若运行环境设了 `HTTP_PROXY`/`HTTPS_PROXY` 且指向 Clash，访问 `hyperdown.net` 可能走代理失败；测试时按需 `env -u HTTP_PROXY -u HTTPS_PROXY` 规避。

### 9.2 推荐验证手法
1. **离线算法验证（最重要）**：seal→unseal 往返 + 空对象密文 18 字节。不依赖真账号，先确认算法对。
2. **导入验证**：`python -c "import plugins.hyperdown as m; print(m.HyperdownPlugin().name)"` → `hyperdown`。
3. **真账号端到端**：Web 建任务实测（唯一能确认接口契约的环节）。

### 9.3 回归测试基准数据
- 加信封后 `ciphertext` 对 `{}` 必为 18 字节；`nonce` base64 解码必为 24 字节。
- `sign` 重算与 envelope 一致（HMAC 用同一 `key`）。
- 用同一 `key` + `aad` + `nonce` 解密必得原 plaintext。

---

## 附录 A：关键参数速查

| 参数 | 值 | 位置 | 说明 |
|------|-----|------|------|
| Base URL | `https://hyperdown.net` | 常量 `BASE_URL` | API 前缀 `/api/v1` |
| API base | `https://hyperdown.net/api/v1` | `DEFAULT_BASE` | 结尾无斜杠 |
| User-Agent | `Go-http-client/1.1` | `USER_AGENT` | 必须带 |
| 超时 | `30` 秒 | `TIMEOUT` | HTTPS 超时 |
| 服务器公钥 | `dd85f63f107a32ce3def4835fe56c27865a1557fedad19adbd72ff81ea2e1025` | `_PEER_PUB_HEX` | 32 字节，不可替换 |
| HKDF salt 前缀 | `hyperdown-secure-api:v1:` | `SALT_PREFIX` | 字节串，末尾冒号 |
| HKDF 算法 | SHA-256，输出 32 字节 | `_derive_key` | salt=`SHA256(req_id:ts)` |
| AEAD | XChaCha20-Poly1305 (ietf, 24B nonce) | libsodium | `crypto_aead_xchacha20poly1305_ietf_encrypt` |
| HMAC | HMAC-SHA256，key=派生 key，sep=`\x00` | `_seal_json` | 9 段固定 |
| 信封头 | `X-Hyperdown-Secure: v1` | HTTP 头 | 值字符串 `"v1"` |
| 登录路径 | `POST /auth/login` | `_login` | body `{email,password}` |
| 状态路径 | `GET /me/` | `do_me` | 看 `data.user.is_check_in` |
| 签到路径 | `POST /me/checkins` | `checkin` | body `{}`，secure=True |

## 附录 B：错误信息对照表

| 场景 | 识别方式 | 插件处理 |
|------|---------|---------|
| 登录失败 | `ok=false` 或 HTTP≥400 | 返回失败 `login_failed` |
| token 失效 | `GET /me/` 或签到返回 401 | `ensure_tokens()` 刷新/重登后重试 |
| 今日已签 | `code ∈ {already_check_in, checked_in}` 或 message 含「已签到/already/repeat/Already」 | 当作成功，返回「今日已签」 |
| 网络错误 | `requests.RequestException` | 抛 `network_error` |
| 响应非 JSON | `json.JSONDecodeError` | 抛 `invalid_json` |
