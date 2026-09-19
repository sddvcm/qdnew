# -*- coding: utf-8 -*-
"""导出/导入测试：加密格式、防篡改、错密钥、冲突策略、端到端往返。

重点覆盖安全性：
  - 非本程序的包必须被拒
  - 篡改任意一个字节必须解密失败
  - 换个密钥必须解不开
  - 导出的文件里**不能出现明文密码**
"""
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
TMP = tempfile.mkdtemp(prefix="transfer_test_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "test-secret-AAA-111"
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"FAIL  {name}  {extra}")


def fresh_db():
    """模拟换机：**删掉库文件**再重建。

    ⚠️ 不能只调 init_db() —— 它用的是 CREATE TABLE IF NOT EXISTS，
    是**增量迁移**（有意为之，绝不清库），残留数据会让"新机器"断言全错。
    """
    from app import database
    import sqlite3
    # 关掉可能的连接（WAL 模式下要连 -wal/-shm 一起删）
    try:
        database.get_db().close()
    except Exception:      # noqa: BLE001
        pass
    for suffix in ("", "-wal", "-shm"):
        p = database.DB_PATH + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    database.init_db()


from app import transfer   # noqa: E402
from app.database import get_db  # noqa: E402
from app.crypto import encrypt, decrypt  # noqa: E402

print("=" * 60)
print("1. 加密格式基本性质")
payload = {
    transfer.K_META: {"app": "checkin-system", "format": 1},
    transfer.K_TASKS: [{"name": "T1", "password": "SUPER_SECRET_PWD",
                        "cookie": "SECRET_COOKIE_VALUE", "params": {"a": 1}}],
    transfer.K_PLUGINS: [], transfer.K_TEMPLATES: [],
    transfer.K_NOTIFIES: [], transfer.K_TASK_NOTIFY: [], transfer.K_SETTINGS: {},
}
blob = transfer.pack(payload)
check("1.1 有固定文件头 QDPACK1", blob[:7] == b"QDPACK1")
check("1.2 头部可识别", transfer.looks_like_pack(blob))
check("1.3 是二进制而非明文", b"SUPER_SECRET_PWD" not in blob
      and b"T1" not in blob[:200])
check("1.4 长度合理（头+nonce+ct+tag）",
      len(blob) >= 9 + 12 + 16, len(blob))
check("1.5 base64 化后也不含明文密码",
      b"SUPER_SECRET_PWD" not in transfer.encode_b64(blob).encode())

print()
print("2. 往返一致性")
back = transfer.unpack(blob)
check("2.1 能解回原结构", back == payload, str(back)[:120])

print()
print("3. 安全：非本程序的文件必须被拒")
for label, data in (
    ("3.1 空文件", b""),
    ("3.2 随机二进制", os.urandom(500)),
    ("3.3 纯 JSON", json.dumps(payload).encode()),
    ("3.4 短头仿冒", b"QDPAC"),
    ("3.5 zip 文件",
     (lambda: (lambda b: (__import__("zipfile").ZipFile(b, "w").close(),
                          b.getvalue())[1])(io.BytesIO()))()),
):
    try:
        transfer.unpack(data)
        check(label + " 被拒", False, "竟然解开了")
    except transfer.TransferError:
        check(label + " 被拒", True)
    except Exception as e:
        check(label + " 被拒", False, f"抛了非 TransferError: {type(e).__name__}")

print()
print("4. 安全：篡改必须被发现")
tampered = bytearray(blob)
tampered[-5] ^= 0x01            # 改密文末尾
try:
    transfer.unpack(bytes(tampered))
    check("4.1 密文篡改被拒", False, "竟然解开了")
except transfer.TransferError as e:
    check("4.1 密文篡改被拒", "解密" in str(e) or "改动" in str(e))

t2 = bytearray(blob)
t2[0] ^= 0x01                   # 改 magic
try:
    transfer.unpack(bytes(t2))
    check("4.2 文件头篡改被拒", False, "竟然解开了")
except transfer.TransferError:
    check("4.2 文件头篡改被拒", True)

t3 = bytearray(blob)
t3[8] ^= 0x01                   # 改版本号（在 AAD 里）
try:
    transfer.unpack(bytes(t3))
    check("4.3 版本字节篡改被拒（AAD 生效）", False, "竟然解开了")
except transfer.TransferError:
    check("4.3 版本字节篡改被拒（AAD 生效）", True)

# 截断
for cut in (5, 20, len(blob) - 3):
    try:
        transfer.unpack(blob[:cut])
        check(f"4.4 截断到 {cut}B 被拒", False, "竟然解开了")
    except transfer.TransferError:
        check(f"4.4 截断到 {cut}B 被拒", True)

print()
print("5. 安全：换密钥解不开（换机但密钥不同）")
orig_key = os.environ["CHECKIN_SECRET_KEY"]
os.environ["CHECKIN_SECRET_KEY"] = "different-secret-BBB-222"
try:
    transfer.unpack(blob)
    check("5.1 错密钥被拒", False, "竟然解开了")
except transfer.TransferError as e:
    check("5.1 错密钥被拒", "密钥" in str(e) or "解密" in str(e), str(e))
# 同密钥可解
os.environ["CHECKIN_SECRET_KEY"] = orig_key
check("5.2 同密钥可解", transfer.unpack(blob) == payload)

print()
print("6. 版本号校验")
hdr = b"QDPACK1" + (99).to_bytes(2, "little")
try:
    transfer.unpack(hdr + os.urandom(40))
    check("6.1 不支持的版本被拒", False, "竟然过了")
except transfer.TransferError as e:
    check("6.1 不支持的版本被拒", "版本" in str(e), str(e))

print()
print("7. 端到端：造数据 → 导出 → 清库 → 导入")
fresh_db()
db = get_db()
db.execute("INSERT INTO plugins (name, display_name, plugin_type, form_schema) "
           "VALUES ('demo-plugin','演示插件','http','[]')")
pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
db.execute(
    "INSERT INTO tasks (plugin_id,name,site_url,username,password,cookie,cron_expr,"
    "enabled,params) VALUES (?,?,?,?,?,?,?,?,?)",
    (pid, "任务甲", "https://a.test", "user_a", encrypt("pwd_a"),
     encrypt("ck_a"), "0 9 * * *", 1, json.dumps({"k": "v"})))
db.execute(
    "INSERT INTO tasks (plugin_id,name,site_url,username,password,cookie,cron_expr,"
    "enabled,params) VALUES (?,?,?,?,?,?,?,?,?)",
    (pid, "任务乙", "https://b.test", "user_b", encrypt("pwd_b"),
     encrypt("ck_b"), "0 10 * * *", 0, "{}"))
db.execute("INSERT INTO har_templates (name,host,entries,variables) "
           "VALUES ('模板甲','a.test','[]','[]')")
db.execute("INSERT INTO notify_configs (notify_type,name,config) "
           "VALUES ('pushplus','我的推送','{\"token\":\"tk\"}')")
nid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
tid = db.execute("SELECT id FROM tasks WHERE name='任务甲'").fetchone()[0]
db.execute("INSERT INTO task_notify (task_id,notify_id,on_success,on_failure) "
           "VALUES (?,?,1,1)", (tid, nid))
db.commit()
db.close()

p = transfer.collect(include_tasks=True, include_templates=True,
                     include_notifies=True)
check("7.1 收集到 2 个任务", len(p[transfer.K_TASKS]) == 2, len(p[transfer.K_TASKS]))
check("7.2 收集到 1 个插件", len(p[transfer.K_PLUGINS]) == 1)
check("7.3 收集到 1 个模板", len(p[transfer.K_TEMPLATES]) == 1)
check("7.4 收集到 1 个通知", len(p[transfer.K_NOTIFIES]) == 1)
check("7.5 任务里密码已还原为明文（在加密层内）",
      any(t["password"] == "pwd_a" for t in p[transfer.K_TASKS]))
check("7.6 插件带 _src_id（映射必需）",
      all("_src_id" in x for x in p[transfer.K_PLUGINS]))

blob2 = transfer.pack(p)
check("7.7 包里不含明文密码", b"pwd_a" not in blob2 and b"ck_a" not in blob2)

# 预览
info = transfer.summarize(blob2)
check("7.8 预览列出任务名", set(info["tasks"]) == {"任务甲", "任务乙"},
      info["tasks"])
check("7.9 预览列出模板/通知/插件",
      info["templates"] == ["模板甲"] and info["notifies"] == ["我的推送"]
      and info["plugins"] == ["demo-plugin"])

# 清库模拟换机
fresh_db()
db = get_db()
check("7.10 清库后无任务",
      db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0)
db.close()

res = transfer.apply(transfer.unpack(blob2), on_conflict="skip")
st = res["stat"]
check("7.11 导入新增 2 个任务", st["tasks"]["added"] == 2, st["tasks"])
check("7.12 导入新增 1 个插件", st["plugins"]["added"] == 1)
check("7.13 导入新增 1 个模板", st["templates"]["added"] == 1)
check("7.14 导入新增 1 个通知", st["notifies"]["added"] == 1)
check("7.15 重建了 1 条绑定", st["bindings"] == 1, st["bindings"])

db = get_db()
row = db.execute("SELECT * FROM tasks WHERE name='任务甲'").fetchone()
check("7.16 密码可正确解出", decrypt(row["password"]) == "pwd_a",
      decrypt(row["password"]))
check("7.17 Cookie 可正确解出", decrypt(row["cookie"]) == "ck_a")
check("7.18 params 保留", json.loads(row["params"]) == {"k": "v"})
check("7.19 cron 保留", row["cron_expr"] == "0 9 * * *")
row2 = db.execute("SELECT * FROM tasks WHERE name='任务乙'").fetchone()
check("7.20 enabled=0 保留", row2["enabled"] == 0)
row3 = db.execute("SELECT * FROM har_templates WHERE name='模板甲'").fetchone()
check("7.21 模板导入", row3 is not None)
row4 = db.execute("SELECT * FROM notify_configs WHERE name='我的推送'").fetchone()
check("7.22 通知导入且 config 完整",
      json.loads(row4["config"]) == {"token": "tk"})
# 任务确实绑上了通知
bind = db.execute(
    "SELECT tn.* FROM task_notify tn JOIN tasks t ON tn.task_id=t.id "
    "WHERE t.name='任务甲'").fetchone()
check("7.23 绑定关系正确指向本地 id",
      bind is not None and bind["notify_id"] == row4["id"])
db.close()

print()
print("8. 冲突策略")
res = transfer.apply(transfer.unpack(blob2), on_conflict="skip")
check("8.1 skip：全部跳过", res["stat"]["tasks"]["skipped"] == 2
      and res["stat"]["tasks"]["added"] == 0, res["stat"]["tasks"])

res = transfer.apply(transfer.unpack(blob2), on_conflict="rename")
check("8.2 rename：重命名新增", res["stat"]["tasks"]["added"] == 2
      and res["stat"]["tasks"]["renamed"] == 2, res["stat"]["tasks"])
db = get_db()
names = {r["name"] for r in db.execute("SELECT name FROM tasks").fetchall()}
check("8.3 重命名后名字带 (导入)", any("(导入)" in n for n in names), names)
db.close()

# overwrite：改掉本地任务再导入覆盖
db = get_db()
db.execute("UPDATE tasks SET username='LOCAL_CHANGED' WHERE name='任务甲'")
db.commit()
db.close()
res = transfer.apply(transfer.unpack(blob2), on_conflict="overwrite")
check("8.4 overwrite：覆盖 2 个任务",
      res["stat"]["tasks"]["overwritten"] == 2, res["stat"]["tasks"])
db = get_db()
row = db.execute("SELECT username FROM tasks WHERE name='任务甲'").fetchone()
check("8.5 覆盖后 username 恢复为包内值", row["username"] == "user_a",
      row["username"])
db.close()

print()
print("9. 部分导出（只导指定任务）")
db = get_db()
tid = db.execute("SELECT id FROM tasks WHERE name='任务甲'").fetchone()[0]
db.close()
p2 = transfer.collect(task_ids=[tid])
check("9.1 只导出 1 个任务", len(p2[transfer.K_TASKS]) == 1,
      [t["name"] for t in p2[transfer.K_TASKS]])
check("9.2 只带上被引用的插件", len(p2[transfer.K_PLUGINS]) == 1)

p3 = transfer.collect(include_templates=False, include_notifies=False)
check("9.3 可关闭模板导出", p3[transfer.K_TEMPLATES] == [])
check("9.4 可关闭通知导出", p3[transfer.K_NOTIFIES] == [])

print()
print("10. 边界与健壮性")
# 超长名字/特殊字符
weird = {"name": "任务<>&\"'中文🎉", "password": "p", "cookie": "c",
         "params": {"nested": {"deep": [1, 2, {"x": None}]}}}
pw = dict(payload)
pw[transfer.K_TASKS] = [weird]
b = transfer.pack(pw)
check("10.1 特殊字符往返 OK",
      transfer.unpack(b)[transfer.K_TASKS][0]["name"] == weird["name"])
check("10.2 嵌套结构保留",
      transfer.unpack(b)[transfer.K_TASKS][0]["params"] == weird["params"])

# 超过大小上限
try:
    transfer.unpack(b"\x00" * (transfer.MAX_PACK_BYTES + 1))
    check("10.3 超大文件被拒", False, "竟然过了")
except transfer.TransferError as e:
    check("10.3 超大文件被拒", "过大" in str(e), str(e))

# 未知冲突策略
try:
    transfer.apply(payload, on_conflict="hack")
    check("10.4 未知冲突策略被拒", False, "竟然过了")
except transfer.TransferError:
    check("10.4 未知冲突策略被拒", True)

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
print("TRANSFER_TEST_OK" if FAIL == 0 else "TRANSFER_TEST_FAILED")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
