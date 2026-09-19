"""任务导出 / 导入 —— 加密的 .qdpack 交换格式。

为什么需要
----------
换机部署、把配置从测试环境搬到生产、或者给别人一份"能直接导入"的配置包。
直接导 JSON 是不行的：里面**必然包含明文密码和 Cookie**，随手发出去就泄露了。

格式设计
--------
文件后缀 `.qdpack`，内容是：

    ┌────────────────┬──────────────────────────────────────────┐
    │ 明文头（固定）  │ AES-256-GCM 密文                         │
    ├────────────────┼──────────────────────────────────────────┤
    │ magic  QDPACK1 │ nonce(12B) ‖ ciphertext ‖ tag(16B)       │
    │ +ver(2B) + len │                                          │
    └────────────────┴──────────────────────────────────────────┘

- **加密**：AES-256-GCM（认证加密）。密钥由 `CHECKIN_SECRET_KEY` 经
  PBKDF2-HMAC-SHA256 迭代派生（固定盐 + 固定迭代次数），**与站点无关、
  与导出时间无关** —— 所以同一套部署导出的包，在另一套**密钥相同**的部署上
  能直接导入；密钥不同则解不开。
- **防篡改**：GCM 自带 tag 校验。另外把明文头作为 **AAD** 绑定进去，
  改 magic/版本号也会导致校验失败（实测 InvalidTag）。
- **只认自己**：外部程序既不知道 magic，也没有密钥，更无法通过 GCM 校验，
  所以"只有本程序能导入识别"。

⚠️ 安全边界（必读）
------------------
1. 这个密钥**绑定部署**，不是"任意本程序"。
   换机导入时两边的 `CHECKIN_SECRET_KEY` 必须一致 —— 这是特性不是缺陷：
   否则随便谁装一个同款程序就能解开你的密码。
   fpk 模式下密钥在 `etc/app.env` 生成、**升级不丢**，所以同机升级后照常能导入。
2. 导出文件**含明文密码/Cookie**（在加密层内）。虽然文件本身加密了，
   但请当敏感文件对待，别丢在公共网盘。
3. 没有"口令加密"选项是刻意的：让用户自己想密码会带来"密码忘了数据没了"
   的支持负担。要跨密钥迁移，正确做法是同步 `CHECKIN_SECRET_KEY`。

导入策略
--------
- 按 `name` 判断冲突（任务名/模板名/渠道名）
- `skip`：同名保留本地的（默认，最安全）
- `overwrite`：同名用导入包里的覆盖
- `rename`：同名自动加后缀（`xxx (导入)`）
"""
import base64
import hashlib
import json
import os
from typing import Dict, List, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------- 格式常量 ----------
MAGIC = b"QDPACK1"          # 7 字节
FORMAT_VERSION = 1
HEADER_LEN = len(MAGIC) + 2          # magic + 版本号（uint16 LE）
PBKDF2_SALT = b"checkin-system.qdpack.v1"
PBKDF2_ITER = 200_000
NONCE_LEN = 12
TAG_LEN = 16
MAX_PACK_BYTES = 64 * 1024 * 1024    # 导入上限 64MB，防有人传超大文件撑爆内存

# 各种类别的键名
K_META = "meta"
K_TASKS = "tasks"
K_PLUGINS = "plugins"
K_TEMPLATES = "har_templates"
K_NOTIFIES = "notify_configs"
K_TASK_NOTIFY = "task_notify"
K_SETTINGS = "settings"


class TransferError(Exception):
    """导出/导入失败（消息面向用户）"""


# ============================ 密钥 ============================

def _derive_key() -> bytes:
    """从应用密钥派生打包密钥。

    ⚠️ 刻意**不**依赖 CHECKIN_DATA_DIR / 机器名 / 时间 等环境因素 ——
    否则"换机导入"永远失败。只认 CHECKIN_SECRET_KEY。
    """
    secret = os.environ.get("CHECKIN_SECRET_KEY", "checkin-default-key-change-me")
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"),
                             PBKDF2_SALT, PBKDF2_ITER, dklen=32)
    return dk


def _header() -> bytes:
    return MAGIC + FORMAT_VERSION.to_bytes(2, "little")


def looks_like_pack(data: bytes) -> bool:
    """快速判断是不是本程序的包（只看头部，不解密）"""
    return isinstance(data, (bytes, bytearray)) and data[:len(MAGIC)] == MAGIC


def is_encrypted_pack(data: bytes) -> bool:
    return looks_like_pack(data)


# ============================ 打包 / 解包 ============================

def pack(payload: Dict) -> bytes:
    """把数据字典打成加密的 .qdpack 字节"""
    try:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise TransferError(f"内容无法序列化：{e}") from e

    header = _header()
    nonce = os.urandom(NONCE_LEN)
    aes = AESGCM(_derive_key())
    # 明文头作为 AAD → 头被改则解密失败
    ct = aes.encrypt(nonce, raw, header)
    return header + nonce + ct


def unpack(data: bytes) -> Dict:
    """解开 .qdpack，返回数据字典。任何异常都翻译成人话。"""
    if not data:
        raise TransferError("文件内容为空")
    if len(data) > MAX_PACK_BYTES:
        raise TransferError(
            f"文件过大（{len(data)//1024//1024}MB），上限 "
            f"{MAX_PACK_BYTES//1024//1024}MB"
        )
    if not looks_like_pack(data):
        raise TransferError(
            "这不是本程序导出的配置文件。\n"
            f"（缺少文件头标识；正确格式应以后缀 .qdpack 结尾，"
            "且只能由本程序「导出任务」生成）"
        )

    if len(data) < HEADER_LEN + NONCE_LEN + TAG_LEN:
        raise TransferError("文件不完整（长度不足）")

    ver = int.from_bytes(data[len(MAGIC):HEADER_LEN], "little")
    if ver != FORMAT_VERSION:
        raise TransferError(
            f"配置包版本不支持：{ver}（本程序支持 {FORMAT_VERSION}）。"
            "可能是更高版本程序导出的，请先升级本程序。"
        )

    header = data[:HEADER_LEN]
    nonce = data[HEADER_LEN:HEADER_LEN + NONCE_LEN]
    ct = data[HEADER_LEN + NONCE_LEN:]

    aes = AESGCM(_derive_key())
    try:
        raw = aes.decrypt(nonce, ct, header)
    except Exception as e:      # noqa: BLE001 —— InvalidTag 等
        raise TransferError(
            "无法解密该配置包。常见原因：\n"
            "  1. 文件被改动/损坏（校验未通过）\n"
            "  2. 本机与导出机的应用密钥不同\n"
            "     （换机导入时两边的 CHECKIN_SECRET_KEY 必须一致）"
        ) from e

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TransferError(f"内容解析失败（文件可能损坏）：{e}") from e
    if not isinstance(payload, dict):
        raise TransferError("内容结构异常")
    return payload


# ============================ 收集 / 还原数据 ============================

# 会被导出的任务字段（不含 id / 运行状态 / 时间戳 —— 这些是本地属性）
TASK_FIELDS = (
    "name", "site_url", "username", "password", "cookie",
    "cron_expr", "enabled", "plugin_id", "max_fail_alert", "params",
)
# 插件字段（用于把 plugin_id 映射到插件名，导入端再按名字找回本地 id）
PLUGIN_FIELDS = ("name", "display_name", "description", "version",
                 "plugin_type", "author", "builtin", "form_schema", "enabled")
TEMPLATE_FIELDS = ("name", "host", "note", "entries", "variables", "builtin")
NOTIFY_FIELDS = ("notify_type", "name", "config", "enabled")
SETTING_KEYS = ("update_source", "update_proxy", "captcha_backend",
                "captcha_cloud_token", "captcha_cloud_type")


def collect(include_tasks=True, include_templates=True, include_notifies=True,
            include_settings=False, task_ids: List[int] = None) -> Dict:
    """从当前库收集要导出的数据。

    Args:
        task_ids: 只导出这些任务 id；None = 全部
        其余开关控制是否带上模板/通知/系统设置
    """
    from app.database import get_db
    from app.crypto import decrypt

    db = get_db()
    payload: Dict = {
        K_META: {
            "app": "checkin-system",
            "format": FORMAT_VERSION,
            "exported_at": _now(),
        }
    }

    plugin_ids = set()
    if include_tasks:
        sql = "SELECT * FROM tasks"
        params: Tuple = ()
        if task_ids:
            marks = ",".join("?" * len(task_ids))
            sql += f" WHERE id IN ({marks})"
            params = tuple(task_ids)
        sql += " ORDER BY id"
        rows = db.execute(sql, params).fetchall()
        tasks = []
        for r in rows:
            d = dict(r)
            t = {k: d.get(k) for k in TASK_FIELDS}
            # 密码/Cookie 在库里是密文，导出前先解成明文 —— 反正整包还会再加密一次，
            # 而且这样"同密钥异机"导入时不会因库层密钥派生差异出问题。
            t["password"] = decrypt(d.get("password") or "")
            t["cookie"] = decrypt(d.get("cookie") or "")
            try:
                t["params"] = json.loads(d.get("params") or "{}")
            except (TypeError, json.JSONDecodeError):
                t["params"] = {}
            if d.get("plugin_id"):
                plugin_ids.add(d["plugin_id"])
            tasks.append(t)
        payload[K_TASKS] = tasks
        payload[K_META]["task_count"] = len(tasks)
    else:
        payload[K_TASKS] = []

    # 只导出被引用到的插件（避免带上无关插件）
    if plugin_ids:
        marks = ",".join("?" * len(plugin_ids))
        rows = db.execute(
            f"SELECT * FROM plugins WHERE id IN ({marks})", tuple(plugin_ids)
        ).fetchall()
        plugins = []
        for r in rows:
            d = dict(r)
            p = {k: d.get(k) for k in PLUGIN_FIELDS}
            # ⚠️ 必须带上导出机的 id：任务里存的 plugin_id 是**导出机**的 id，
            # 导入端要靠 _src_id 反查插件名，再按名字映射到本机的 plugin_id。
            # 少了它，任务会找不到插件而被跳过。
            p["_src_id"] = d.get("id")
            try:
                p["form_schema"] = json.loads(d.get("form_schema") or "[]")
            except (TypeError, json.JSONDecodeError):
                p["form_schema"] = []
            plugins.append(p)
        payload[K_PLUGINS] = plugins
    else:
        payload[K_PLUGINS] = []

    if include_templates:
        rows = db.execute("SELECT * FROM har_templates ORDER BY id").fetchall()
        tpls = []
        for r in rows:
            d = dict(r)
            t = {}
            for k in TEMPLATE_FIELDS:
                v = d.get(k)
                if k in ("entries", "variables"):
                    try:
                        v = json.loads(v or "[]")
                    except (TypeError, json.JSONDecodeError):
                        v = []
                t[k] = v
            tpls.append(t)
        payload[K_TEMPLATES] = tpls
    else:
        payload[K_TEMPLATES] = []

    if include_notifies:
        rows = db.execute("SELECT * FROM notify_configs ORDER BY id").fetchall()
        notifies = []
        for r in rows:
            d = dict(r)
            n = {}
            for k in NOTIFY_FIELDS:
                v = d.get(k)
                if k == "config":
                    try:
                        v = json.loads(v or "{}")
                    except (TypeError, json.JSONDecodeError):
                        v = {}
                n[k] = v
            n["_src_id"] = d.get("id")
            notifies.append(n)
        payload[K_NOTIFIES] = notifies

        # 任务↔通知的绑定关系（靠名字关联，导入端重建）
        binds = []
        rows = db.execute(
            "SELECT tn.task_id, tn.notify_id, tn.on_success, tn.on_failure, "
            "       t.name AS task_name "
            "FROM task_notify tn LEFT JOIN tasks t ON tn.task_id=t.id"
        ).fetchall()
        for r in rows:
            binds.append({
                "task_name": r["task_name"],
                "notify_id": r["notify_id"],
                "task_id": r["task_id"],
                "on_success": r["on_success"],
                "on_failure": r["on_failure"],
            })
        payload[K_TASK_NOTIFY] = binds
    else:
        payload[K_NOTIFIES] = []
        payload[K_TASK_NOTIFY] = []

    if include_settings:
        marks = ",".join("?" * len(SETTING_KEYS))
        rows = db.execute(
            f"SELECT key, value FROM system_config WHERE key IN ({marks})",
            SETTING_KEYS).fetchall()
        payload[K_SETTINGS] = {r["key"]: r["value"] for r in rows}
    else:
        payload[K_SETTINGS] = {}

    db.close()
    payload[K_META]["plugin_count"] = len(payload[K_PLUGINS])
    payload[K_META]["template_count"] = len(payload[K_TEMPLATES])
    payload[K_META]["notify_count"] = len(payload[K_NOTIFIES])
    return payload


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def summarize(data: bytes) -> Dict:
    """只解密 + 汇总，不写库。给前端"导入前预览"用。"""
    p = unpack(data)
    meta = p.get(K_META) or {}
    return {
        "meta": {
            "exported_at": meta.get("exported_at", ""),
            "format": meta.get("format"),
            "app": meta.get("app", ""),
        },
        "tasks": [t.get("name", "") for t in (p.get(K_TASKS) or [])],
        "plugins": [x.get("name", "") for x in (p.get(K_PLUGINS) or [])],
        "templates": [x.get("name", "") for x in (p.get(K_TEMPLATES) or [])],
        "notifies": [x.get("name", "") or x.get("notify_type", "")
                     for x in (p.get(K_NOTIFIES) or [])],
        "settings": sorted((p.get(K_SETTINGS) or {}).keys()),
        "has_settings": bool(p.get(K_SETTINGS)),
    }


def apply(payload: Dict, on_conflict: str = "skip") -> Dict:
    """把解包后的数据写入本库。

    on_conflict: skip / overwrite / rename
    返回统计 + 逐项明细（哪些新增、哪些跳过、哪些覆盖）。
    """
    from app.database import get_db
    from app.crypto import encrypt

    if on_conflict not in ("skip", "overwrite", "rename"):
        raise TransferError(f"未知的冲突处理方式：{on_conflict}")

    db = get_db()
    stat = {"tasks": {"added": 0, "skipped": 0, "overwritten": 0, "renamed": 0},
            "plugins": {"added": 0, "skipped": 0, "overwritten": 0, "renamed": 0},
            "templates": {"added": 0, "skipped": 0, "overwritten": 0, "renamed": 0},
            "notifies": {"added": 0, "skipped": 0, "overwritten": 0, "renamed": 0},
            "bindings": 0,
            "settings": 0}
    details: List[str] = []

    try:
        # ---------- 1. 插件（任务依赖它，必须先建）----------
        # 名字 → 本地 id
        plugin_map: Dict[str, int] = {}
        existing_plugins = {r["name"]: r["id"] for r in
                            db.execute("SELECT id, name FROM plugins").fetchall()}

        for p in payload.get(K_PLUGINS) or []:
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            if name in existing_plugins:
                pid = existing_plugins[name]
                # ⚠️ 插件按 name 匹配即**复用本地插件**，不做 rename：
                # 插件名对应 plugins/*.py 里的类，改名的插件根本加载不出来。
                # 任务只要挂到同名插件上即可正常工作（插件代码本地已有）。
                if on_conflict == "overwrite":
                    db.execute(
                        "UPDATE plugins SET display_name=?, description=?, version=?, "
                        "plugin_type=?, author=?, form_schema=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (p.get("display_name") or name, p.get("description", ""),
                         p.get("version", "1.0"), p.get("plugin_type", "http"),
                         p.get("author", ""),
                         json.dumps(p.get("form_schema") or [], ensure_ascii=False),
                         pid))
                    stat["plugins"]["overwritten"] += 1
                    details.append(f"插件「{name}」已覆盖")
                else:
                    stat["plugins"]["skipped"] += 1
                plugin_map[name] = pid
                continue

            db.execute(
                "INSERT INTO plugins (name, display_name, description, version, "
                "plugin_type, author, builtin, form_schema) VALUES (?,?,?,?,?,?,?,?)",
                (name, p.get("display_name") or name, p.get("description", ""),
                 p.get("version", "1.0"), p.get("plugin_type", "http"),
                 p.get("author", ""), p.get("builtin", 0),
                 json.dumps(p.get("form_schema") or [], ensure_ascii=False)))
            pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            plugin_map[name] = pid
            existing_plugins[name] = pid
            stat["plugins"]["added"] += 1

        # ---------- 2. 任务 ----------
        # 源 plugin_id → 插件名（靠包内 plugins 列表反查）
        # 因为包里的 task.plugin_id 是**导出机**的 id，不能直接用。
        src_pid_to_name: Dict[int, str] = {}
        for p in payload.get(K_PLUGINS) or []:
            # 导出时没存 id，这里靠顺序对齐 —— 但更稳的是让 collect 带上 _src_id
            if "_src_id" in p:
                src_pid_to_name[p["_src_id"]] = p.get("name")

        existing_tasks = {r["name"]: r["id"] for r in
                          db.execute("SELECT id, name FROM tasks").fetchall()}
        task_map: Dict[str, int] = {}

        for t in payload.get(K_TASKS) or []:
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            # 解析出本地 plugin_id
            local_pid = None
            src_pid = t.get("plugin_id")
            if src_pid in src_pid_to_name:
                local_pid = plugin_map.get(src_pid_to_name[src_pid])
            if local_pid is None and len(plugin_map) == 1:
                local_pid = next(iter(plugin_map.values()))
            if local_pid is None:
                details.append(f"任务「{name}」跳过：找不到对应插件")
                stat["tasks"]["skipped"] += 1
                continue

            params = t.get("params")
            if not isinstance(params, dict):
                params = {}
            pwd = t.get("password") or ""
            ck = t.get("cookie") or ""

            if name in existing_tasks:
                tid = existing_tasks[name]
                if on_conflict == "overwrite":
                    db.execute(
                        "UPDATE tasks SET plugin_id=?, site_url=?, username=?, "
                        "password=?, cookie=?, cron_expr=?, enabled=?, params=?, "
                        "max_fail_alert=COALESCE(?, max_fail_alert), "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (local_pid, t.get("site_url", ""), t.get("username", ""),
                         encrypt(pwd), encrypt(ck),
                         t.get("cron_expr", "0 9 * * *"), t.get("enabled", 1),
                         json.dumps(params, ensure_ascii=False),
                         t.get("max_fail_alert"), tid))
                    stat["tasks"]["overwritten"] += 1
                    details.append(f"任务「{name}」已覆盖")
                    task_map[name] = tid
                    continue
                if on_conflict == "rename":
                    # 落到下面"新增"分支，用改名后的名字插入
                    new_name = _unique_name(name, existing_tasks)
                    stat["tasks"]["renamed"] += 1
                    details.append(f"任务「{name}」重命名为「{new_name}」")
                else:
                    # skip
                    stat["tasks"]["skipped"] += 1
                    details.append(f"任务「{name}」已存在，跳过")
                    task_map[name] = tid
                    continue
            else:
                new_name = name

            db.execute(
                "INSERT INTO tasks (plugin_id, name, site_url, username, password, "
                "cookie, cron_expr, enabled, params, max_fail_alert) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (local_pid, new_name, t.get("site_url", ""), t.get("username", ""),
                 encrypt(pwd), encrypt(ck), t.get("cron_expr", "0 9 * * *"),
                 t.get("enabled", 1), json.dumps(params, ensure_ascii=False),
                 t.get("max_fail_alert") or 3))
            tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            task_map[name] = tid
            existing_tasks[new_name] = tid
            stat["tasks"]["added"] += 1

        # ---------- 3. HAR 模板 ----------
        existing_tpls = {r["name"]: r["id"] for r in
                         db.execute("SELECT id, name FROM har_templates").fetchall()}
        for tp in payload.get(K_TEMPLATES) or []:
            name = str(tp.get("name") or "").strip()
            if not name:
                continue
            entries = json.dumps(tp.get("entries") or [], ensure_ascii=False)
            variables = json.dumps(tp.get("variables") or [], ensure_ascii=False)
            if name in existing_tpls:
                if on_conflict == "overwrite":
                    db.execute(
                        "UPDATE har_templates SET host=?, note=?, entries=?, "
                        "variables=?, updated_at=datetime('now','localtime') "
                        "WHERE id=?",
                        (tp.get("host", ""), tp.get("note", ""), entries,
                         variables, existing_tpls[name]))
                    stat["templates"]["overwritten"] += 1
                    details.append(f"模板「{name}」已覆盖")
                    continue
                if on_conflict == "rename":
                    new_name = _unique_name(name, existing_tpls)
                    stat["templates"]["renamed"] += 1
                    details.append(f"模板「{name}」重命名为「{new_name}」")
                else:
                    stat["templates"]["skipped"] += 1
                    continue
            else:
                new_name = name

            db.execute(
                "INSERT INTO har_templates (name, host, note, entries, variables, "
                "builtin) VALUES (?,?,?,?,?,?)",
                (new_name, tp.get("host", ""), tp.get("note", ""), entries,
                 variables, tp.get("builtin", 0)))
            existing_tpls[new_name] = \
                db.execute("SELECT last_insert_rowid()").fetchone()[0]
            stat["templates"]["added"] += 1

        # ---------- 4. 通知渠道 ----------
        existing_notifies = {r["name"]: r["id"] for r in
                             db.execute("SELECT id, name FROM notify_configs").fetchall()}
        notify_map: Dict[int, int] = {}       # 源 id → 本地 id
        for n in payload.get(K_NOTIFIES) or []:
            name = str(n.get("name") or "").strip() or \
                str(n.get("notify_type") or "渠道")
            src_id = n.get("_src_id")
            cfg = json.dumps(n.get("config") or {}, ensure_ascii=False)
            if name in existing_notifies:
                nid = existing_notifies[name]
                if on_conflict == "overwrite":
                    db.execute(
                        "UPDATE notify_configs SET notify_type=?, config=?, enabled=? "
                        "WHERE id=?",
                        (n.get("notify_type", ""), cfg, n.get("enabled", 1), nid))
                    stat["notifies"]["overwritten"] += 1
                    details.append(f"通知渠道「{name}」已覆盖")
                    if src_id is not None:
                        notify_map[src_id] = nid
                    continue
                if on_conflict == "rename":
                    new_name = _unique_name(name, existing_notifies)
                    stat["notifies"]["renamed"] += 1
                else:
                    stat["notifies"]["skipped"] += 1
                    if src_id is not None:
                        notify_map[src_id] = nid
                    continue
            else:
                new_name = name

            db.execute(
                "INSERT INTO notify_configs (notify_type, name, config, enabled) "
                "VALUES (?,?,?,?)",
                (n.get("notify_type", ""), new_name, cfg, n.get("enabled", 1)))
            nid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            existing_notifies[new_name] = nid
            if src_id is not None:
                notify_map[src_id] = nid
            stat["notifies"]["added"] += 1

        # ---------- 5. 任务↔通知绑定 ----------
        for b in payload.get(K_TASK_NOTIFY) or []:
            tname = b.get("task_name")
            tid = task_map.get(tname)
            nid = notify_map.get(b.get("notify_id"))
            if not tid or not nid:
                continue
            # 已存在的绑定不重复插（同一 (task,notify) 组合）
            dup = db.execute(
                "SELECT id FROM task_notify WHERE task_id=? AND notify_id=?",
                (tid, nid)).fetchone()
            if dup:
                continue
            db.execute(
                "INSERT INTO task_notify (task_id, notify_id, on_success, on_failure) "
                "VALUES (?,?,?,?)",
                (tid, nid, b.get("on_success", 1), b.get("on_failure", 1)))
            stat["bindings"] += 1

        # ---------- 6. 系统设置（只补缺失的，不覆盖本地已有）----------
        for k, v in (payload.get(K_SETTINGS) or {}).items():
            row = db.execute("SELECT value FROM system_config WHERE key=?",
                             (k,)).fetchone()
            if row:
                continue
            db.execute(
                "INSERT INTO system_config (key, value, updated_at) "
                "VALUES (?,?,datetime('now','localtime'))", (k, str(v)))
            stat["settings"] += 1

        db.commit()
    except TransferError:
        db.rollback()
        db.close()
        raise
    except Exception as e:      # noqa: BLE001
        db.rollback()
        db.close()
        raise TransferError(f"导入失败（已回滚，数据未变）：{e}") from e

    db.close()
    return {"stat": stat, "details": details}


def _unique_name(base: str, existing: Dict[str, int]) -> str:
    """生成不冲突的名字：xxx (导入) / xxx (导入2) …"""
    if base not in existing:
        return base
    cand = f"{base} (导入)"
    if cand not in existing:
        return cand
    i = 2
    while f"{base} (导入{i})" in existing:
        i += 1
    return f"{base} (导入{i})"


def export_filename() -> str:
    from datetime import datetime
    return f"checkin-tasks-{datetime.now().strftime('%Y%m%d-%H%M')}.qdpack"


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
