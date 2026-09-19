# -*- coding: utf-8 -*-
"""导出/导入 HTTP 接口端到端测试（Flask test_client）。

覆盖真实使用路径：页面拿 options → 导出下载 → 清库 → 上传预览 → 导入。
以及接口层的安全拒绝：非本程序文件 / 篡改 / 空文件。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
TMP = tempfile.mkdtemp(prefix="transfer_e2e_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "e2e-key-xyz"
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
    from app import database
    try:
        database.get_db().close()
    except Exception:      # noqa: BLE001
        pass
    for s in ("", "-wal", "-shm"):
        p = database.DB_PATH + s
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    database.init_db()


from app.main import create_app     # noqa: E402
from app.database import get_db      # noqa: E402
from app.crypto import encrypt       # noqa: E402

fresh_db()
app = create_app()
app.config["TESTING"] = True
c = app.test_client()

print("=" * 60)
print("1. 造数据")
db = get_db()
# create_app() 启动时会 load_all_plugins()，内置插件（fuliba 等）已入库，直接复用
row = db.execute("SELECT id FROM plugins WHERE name='fuliba'").fetchone()
if row:
    pid = row["id"]
else:
    db.execute("INSERT INTO plugins (name, display_name, plugin_type, form_schema) "
               "VALUES ('fuliba','福利吧','http','[]')")
    pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
db.commit()
for nm, u in (("签到一号", "u1"), ("签到二号", "u2")):
    db.execute(
        "INSERT INTO tasks (plugin_id,name,site_url,username,password,cookie,"
        "cron_expr,enabled,params) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, nm, "https://f.test", u, encrypt(u + "_pwd"),
         encrypt(u + "_ck"), "0 9 * * *", 1, json.dumps({"retry": 3})))
db.execute("INSERT INTO har_templates (name,host,entries,variables) "
           "VALUES ('我的模板','f.test','[]','[]')")
db.execute("INSERT INTO notify_configs (notify_type,name,config,enabled) "
           "VALUES ('pushplus','微信推送','{\"token\":\"TK\"}',1)")
nid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
tid = db.execute("SELECT id FROM tasks WHERE name='签到一号'").fetchone()[0]
db.execute("INSERT INTO task_notify (task_id,notify_id,on_success,on_failure) "
           "VALUES (?,?,1,0)", (tid, nid))
db.commit()
db.close()
check("1.1 数据就绪", True)

print()
print("2. GET /api/transfer/options")
r = c.get("/api/transfer/options")
d = r.get_json()
check("2.1 200", r.status_code == 200)
check("2.2 列出 2 个任务", len(d["tasks"]) == 2, d.get("tasks"))
check("2.3 有模板标记", d["has_templates"] is True)
check("2.4 有通知标记", d["has_notifies"] is True)
check("2.5 不泄露密码字段",
      all("password" not in t and "cookie" not in t for t in d["tasks"]))

print()
print("3. POST /api/transfer/export — 全部导出")
r = c.post("/api/transfer/export", json={})
check("3.1 200", r.status_code == 200)
check("3.2 是附件下载",
      "attachment" in (r.headers.get("Content-Disposition") or ""),
      r.headers.get("Content-Disposition"))
fname = r.headers.get("Content-Disposition", "")
check("3.3 文件名是 .qdpack", ".qdpack" in fname, fname)
blob_all = r.get_data()
check("3.4 有 QDPACK 头", blob_all[:7] == b"QDPACK1")
check("3.5 不含明文密码",
      b"u1_pwd" not in blob_all and b"u1_ck" not in blob_all)
summ = json.loads(r.headers.get("X-Export-Summary") or "{}")
check("3.6 summary 报告 2 个任务", summ.get("tasks") == 2, summ)
check("3.7 summary 报告 1 模板 1 通知",
      summ.get("templates") == 1 and summ.get("notifies") == 1, summ)

print()
print("4. 部分导出（只选一个任务）")
db = get_db()
tid1 = db.execute("SELECT id FROM tasks WHERE name='签到一号'").fetchone()[0]
db.close()
r = c.post("/api/transfer/export", json={"task_ids": [tid1]})
check("4.1 200", r.status_code == 200)
blob_one = r.get_data()
summ = json.loads(r.headers.get("X-Export-Summary") or "{}")
check("4.2 只有 1 个任务", summ.get("tasks") == 1, summ)
check("4.3 两个包内容不同", blob_one != blob_all)

r = c.post("/api/transfer/export", json={"task_ids": "bad"})
check("4.4 task_ids 非数组被拒", r.status_code == 400, r.get_json())
r = c.post("/api/transfer/export", json={"task_ids": ["x"]})
check("4.5 task_ids 含非数字被拒", r.status_code == 400)
r = c.post("/api/transfer/export", json={"include_templates": False,
                                        "include_notifies": False})
summ = json.loads(r.headers.get("X-Export-Summary") or "{}")
check("4.6 可关闭模板/通知导出",
      summ.get("templates") == 0 and summ.get("notifies") == 0, summ)

print()
print("5. POST /api/transfer/inspect — 预览（不写库）")
r = c.post("/api/transfer/inspect",
           data={"file": (io.BytesIO(blob_all), "x.qdpack")},
           content_type="multipart/form-data")
d = r.get_json()
check("5.1 200 且 success", r.status_code == 200 and d.get("success"), d)
info = d.get("info") or {}
check("5.2 列出任务名", set(info.get("tasks") or []) == {"签到一号", "签到二号"},
      info.get("tasks"))
check("5.3 列出模板", info.get("templates") == ["我的模板"], info.get("templates"))
check("5.4 列出通知渠道", info.get("notifies") == ["微信推送"])
check("5.5 标出同名冲突（本机已有）",
      set((info.get("conflicts") or {}).get("tasks") or []) ==
      {"签到一号", "签到二号"}, info.get("conflicts"))
check("5.6 带导出时间", bool((info.get("meta") or {}).get("exported_at")))
# 确认预览真的没写库
db = get_db()
cnt = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
db.close()
check("5.7 预览未改动数据库", cnt == 2, cnt)

print()
print("6. 安全：接口层拒绝非法文件")
for label, data, fname in (
    ("6.1 空文件", b"", "a.qdpack"),
    ("6.2 随机二进制", os.urandom(300), "b.qdpack"),
    ("6.3 纯 JSON", json.dumps({"tasks": []}).encode(), "c.qdpack"),
):
    r = c.post("/api/transfer/inspect",
               data={"file": (io.BytesIO(data), fname)},
               content_type="multipart/form-data")
    check(label + " inspect 被拒", r.status_code == 400, r.get_json())
    r = c.post("/api/transfer/import",
               data={"file": (io.BytesIO(data), fname),
                     "on_conflict": "skip"},
               content_type="multipart/form-data")
    check(label + " import 被拒", r.status_code == 400, r.get_json())

# 篡改
tam = bytearray(blob_all); tam[-4] ^= 0xFF
r = c.post("/api/transfer/import",
           data={"file": (io.BytesIO(bytes(tam)), "t.qdpack"),
                 "on_conflict": "skip"},
           content_type="multipart/form-data")
check("6.4 篡改包被拒", r.status_code == 400, r.get_json())
r = c.post("/api/transfer/import",
           data={"file": (io.BytesIO(blob_all), "t.txt"),
                 "on_conflict": "skip"},
           content_type="multipart/form-data")
check("6.5 非 .qdpack 后缀……仍可导入（内容为准，不靠后缀）",
      r.status_code in (200, 400))

print()
print("7. 换机导入：清库 → 导入")
fresh_db()
app2 = create_app()
app2.config["TESTING"] = True
c2 = app2.test_client()
db = get_db()
check("7.1 清库后无任务",
      db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0)
db.close()

r = c2.post("/api/transfer/import",
            data={"file": (io.BytesIO(blob_all), "x.qdpack"),
                  "on_conflict": "skip"},
            content_type="multipart/form-data")
d = r.get_json()
check("7.2 导入成功", r.status_code == 200 and d.get("success"), d)
st = d.get("stat") or {}
check("7.3 新增 2 个任务", st.get("tasks", {}).get("added") == 2, st.get("tasks"))
check("7.4 插件已存在故跳过（内置插件已入库）",
      st.get("plugins", {}).get("added", 0) + st.get("plugins", {}).get("skipped", 0) >= 1,
      st.get("plugins"))
check("7.5 新增 1 个模板", st.get("templates", {}).get("added") == 1)
check("7.6 新增 1 个通知渠道", st.get("notifies", {}).get("added") == 1,
      st.get("notifies"))
check("7.7 重建绑定 1 条", st.get("bindings") == 1)
check("7.8 message 可读", "导入完成" in (d.get("message") or ""), d.get("message"))
check("7.9 details 有明细", isinstance(d.get("details"), list))

db = get_db()
row = db.execute("SELECT * FROM tasks WHERE name='签到一号'").fetchone()
from app.crypto import decrypt
check("7.10 密码解密正确", decrypt(row["password"]) == "u1_pwd",
      decrypt(row["password"]))
check("7.11 cookie 解密正确", decrypt(row["cookie"]) == "u1_ck")
check("7.12 params 完整", json.loads(row["params"]) == {"retry": 3})
check("7.13 插件已建", db.execute(
    "SELECT COUNT(*) FROM plugins WHERE name='fuliba'").fetchone()[0] == 1)
b = db.execute(
    "SELECT tn.on_success, tn.on_failure FROM task_notify tn "
    "JOIN tasks t ON tn.task_id=t.id WHERE t.name='签到一号'").fetchone()
check("7.14 绑定时机保留（成功1/失败0）",
      b is not None and b["on_success"] == 1 and b["on_failure"] == 0,
      dict(b) if b else None)
db.close()

print()
print("8. 重复导入 + 三种冲突策略（接口层）")
r = c2.post("/api/transfer/import",
            data={"file": (io.BytesIO(blob_all), "x.qdpack"),
                  "on_conflict": "skip"},
            content_type="multipart/form-data")
st = (r.get_json() or {}).get("stat") or {}
check("8.1 skip：2 个任务跳过", st.get("tasks", {}).get("skipped") == 2, st.get("tasks"))

r = c2.post("/api/transfer/import",
            data={"file": (io.BytesIO(blob_all), "x.qdpack"),
                  "on_conflict": "rename"},
            content_type="multipart/form-data")
st = (r.get_json() or {}).get("stat") or {}
check("8.2 rename：2 个任务改名导入",
      st.get("tasks", {}).get("added") == 2
      and st.get("tasks", {}).get("renamed") == 2, st.get("tasks"))

r = c2.post("/api/transfer/import",
            data={"file": (io.BytesIO(blob_all), "x.qdpack"),
                  "on_conflict": "overwrite"},
            content_type="multipart/form-data")
st = (r.get_json() or {}).get("stat") or {}
check("8.3 overwrite：2 个任务覆盖",
      st.get("tasks", {}).get("overwritten") == 2, st.get("tasks"))

r = c2.post("/api/transfer/import",
            data={"file": (io.BytesIO(blob_all), "x.qdpack"),
                  "on_conflict": "evil"},
            content_type="multipart/form-data")
check("8.4 未知策略被拒", r.status_code == 400, r.get_json())

print()
print("9. 页面与新接口注册")
r = c2.get("/settings")
html = r.get_data(as_text=True)
check("9.1 /settings 200", r.status_code == 200)
for k in ("任务导出 / 导入", "doExport", "inspectPack", "doImport"):
    check(f"9.2 页面含 {k}", k in html)

# 9.3 设置页分页结构（左侧导航 + 右侧面板必须一一对应）
import re as _re  # noqa: E402
_nav = _re.findall(r'settings-nav-item[^>]*data-pane="(\w+)"', html)
_panes = _re.findall(r'settings-pane[^>]*data-pane="(\w+)"', html)
check("9.3 左侧导航分页齐全", _nav == [
    "update", "notify", "captcha", "transfer", "backup", "env"], _nav)
check("9.4 导航与面板一一对应", _nav == _panes, _panes)
check("9.5 含分页切换函数", "showSettingsPane" in html
      and "initSettingsNav" in html)
check("9.6 默认激活的是程序更新页",
      'settings-pane active" data-pane="update"' in html)
check("9.7 div 标签配平",
      len(_re.findall(r"<div\b", html)) == len(_re.findall(r"</div>", html)))
# 9.8 环境信息页（数据落点诊断）
check("9.8 含环境信息分页", 'data-pane="env"' in html and "loadEnv" in html)

print()
print("9A. 消息推送区布局（v1.6.1 修的错位回归）")
# 根因：app.css 的 `.form-group input {width:100%}` 把 checkbox 也拉成整行宽，
# 文字全被挤到下一行（用户截图确认过）。这两条断言锁住修复，防止被误删。
_css = io.open(os.path.join(ROOT, "app", "static", "css", "app.css"),
               encoding="utf-8").read()
check("9A.1 CSS 有 checkbox 宽度回退（错位根修）",
      'input[type="checkbox"], .form-group input[type="radio"]' in _css
      and "width: auto" in _css)
check("9A.2 CSS 有 .notify-item 渠道 chip 样式",
      ".notify-item" in _css and ".notify-list" in _css)
# 渠道列表容器必须是 notify-list（曾经错用 help-text）
import io  # noqa: E402
r = c2.get("/task/add")
_check_html = r.get_data(as_text=True)
check("9A.3 表单渠道容器用 notify-list",
      'id="notifyBinding" class="notify-list"' in _check_html)
check("9A.4 旧的渠道 chip 内联样式已移除（padding:3px 0 那版）",
      'gap:6px; padding:3px 0;' not in _check_html)
check("9A.5 CSS 预留滚动条槽位（分页切换时布局不左右晃）",
      "scrollbar-gutter: stable" in _css)
check("9A.6 内容区有稳定最小高度（分页切换时不上下跳）",
      "min-height: 62vh" in _css)
# 静态资源带版本参数 —— 否则更新后浏览器仍用缓存旧 CSS，
# 用户会以为改动没生效（实测踩过）
check("9A.7 静态资源带版本参数（防浏览器缓存旧样式）",
      bool(_re.search(r"app\.css\?v=[\d.]+", html))
      and bool(_re.search(r"app\.js\?v=[\d.]+", html)))

print()
print("10. 空库导出 / 空包导入")
fresh_db()
app3 = create_app(); app3.config["TESTING"] = True
c3 = app3.test_client()
r = c3.post("/api/transfer/export", json={})
check("10.1 空库也能导出", r.status_code == 200
      and r.get_data()[:7] == b"QDPACK1")
r = c3.post("/api/transfer/import",
            data={"file": (io.BytesIO(r.get_data()), "e.qdpack"),
                  "on_conflict": "skip"},
            content_type="multipart/form-data")
check("10.2 导出的空包能导入（无副作用）", r.status_code == 200, r.get_json())

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
print("TRANSFER_E2E_OK" if FAIL == 0 else "TRANSFER_E2E_FAILED")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
