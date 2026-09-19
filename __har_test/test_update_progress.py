# -*- coding: utf-8 -*-
"""更新进度条测试（v1.6.2）

验证「后台线程 + 进度轮询」这套机制：
  - run_update 的 progress 回调按阶段推进、百分比单调不减
  - 回调异常不影响更新本身
  - /api/update/run 立即返回（不阻塞到下载完）
  - /api/update/progress 能查到 running/success/percent
  - 并发更新被拒（409）
"""
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from test_auth_helper import login_client
TMP = tempfile.mkdtemp(prefix="prog_test_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "prog-key"
_HERE_FOR_HELPER = os.path.dirname(os.path.abspath(__file__))
if _HERE_FOR_HELPER not in sys.path:
    sys.path.insert(0, _HERE_FOR_HELPER)
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {extra}")


import updater  # noqa: E402

print("=" * 62)
print("1. progress 回调：阶段顺序与百分比单调性")

events = []


def cb(phase, done, total, msg):
    events.append((phase, done, total, msg))


# 伪造一次更新：patch 掉网络与写盘，只跑阶段流程
import hashlib  # noqa: E402

files = {}
staged_content = {}
for i in range(5):
    name = f"app/fake{i}.py"
    body = ("print(%d)\n" % i).encode()
    files[name] = hashlib.sha256(body.replace(b"\r\n", b"\n")).hexdigest()
    staged_content[name] = body

manifest = {"version": "9.9.9", "files": files}

_orig_fetch = updater._fetch_raw
_orig_write_version = None


def fake_fetch(src, rel, proxy=None):
    if rel == updater.MANIFEST_NAME:
        return json.dumps(manifest).encode()
    return staged_content.get(rel)


updater._fetch_raw = fake_fetch
# 让 ROOT 指向临时目录，别真写项目文件
_orig_root = updater.ROOT
updater.ROOT = os.path.join(TMP, "proj")
os.makedirs(updater.ROOT, exist_ok=True)
_orig_backup = updater.BACKUP_DIR
updater.BACKUP_DIR = os.path.join(TMP, "backups")
_orig_vf = updater.VERSION_FILE
updater.VERSION_FILE = os.path.join(TMP, "version.json")
_orig_cv = updater.current_version
updater.current_version = lambda: {"version": "1.0.0"}

try:
    r = updater.run_update("user/repo", progress=cb)
finally:
    updater._fetch_raw = _orig_fetch
    updater.ROOT = _orig_root
    updater.BACKUP_DIR = _orig_backup
    updater.VERSION_FILE = _orig_vf
    updater.current_version = _orig_cv

check("1.1 更新成功", r.get("updated") is True, r.get("error", "")[:120])
check("1.2 回调用到了", len(events) > 0, len(events))

phases = [e[0] for e in events]
for want in ("fetching", "backing_up", "writing", "done"):
    check(f"1.3 阶段含 {want}", want in phases, phases)

order_ok = True
seen = []
for p in phases:
    if not seen or seen[-1] != p:
        seen.append(p)
check("1.4 阶段顺序正确", seen == ["fetching", "backing_up", "writing", "done"], seen)

# fetching 阶段的 done 应递增到 total
fetch_ev = [e for e in events if e[0] == "fetching"]
check("1.5 fetching 总数=文件数", any(e[2] == len(files) for e in fetch_ev),
      [(e[1], e[2]) for e in fetch_ev][:6])
check("1.6 fetching 进度递增",
      [e[1] for e in fetch_ev] == sorted(e[1] for e in fetch_ev),
      [e[1] for e in fetch_ev])

print()
print("2. 回调抛异常不能影响更新")

def bad_cb(phase, done, total, msg):
    raise RuntimeError("故意炸")


updater._fetch_raw = fake_fetch
updater.ROOT = os.path.join(TMP, "proj2")
os.makedirs(updater.ROOT, exist_ok=True)
updater.BACKUP_DIR = os.path.join(TMP, "backups2")
updater.VERSION_FILE = os.path.join(TMP, "version2.json")
updater.current_version = lambda: {"version": "1.0.0"}
try:
    r2 = updater.run_update("user/repo", progress=bad_cb)
finally:
    updater._fetch_raw = _orig_fetch
    updater.ROOT = _orig_root
    updater.BACKUP_DIR = _orig_backup
    updater.VERSION_FILE = _orig_vf
    updater.current_version = _orig_cv

check("2.1 回调炸了但更新仍成功", r2.get("updated") is True, r2.get("error", "")[:120])
check("2.2 文件确实写进去了",
      os.path.isfile(os.path.join(os.path.join(TMP, "proj2"), "app", "fake0.py")))

print()
print("3. 路由：立即返回 + 可轮询")
from app.main import create_app  # noqa: E402
from app.routes import update_api  # noqa: E402
from app.database import get_db  # noqa: E402

app = create_app()
app.config["TESTING"] = True
c = login_client(app)
db = get_db()
db.execute("INSERT INTO system_config (key,value) VALUES ('update_source',"
           "'https://github.com/sddvcm/qdnew') "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
db.commit(); db.close()

# 把真正的 run_update 换成"慢慢跑"的假实现，好观察中间态
_orig_run = update_api.updater.run_update
_steps = [("fetching", 1, 10, "下载校验 1/10"), ("fetching", 5, 10, "下载校验 5/10"),
          ("writing", 10, 10, "写入 10/10"), ("done", 10, 10, "已更新到 9.9.9")]


def slow_run(source, allow_downgrade=False, proxy=None, progress=None):
    for ph, dn, tt, ms in _steps:
        if progress:
            progress(ph, dn, tt, ms)
        time.sleep(0.35)
    return {"updated": True, "version": "9.9.9",
            "files": ["app/a.py", "templates/b.html"], "backup_dir": "data/backups/x",
            "skipped": [], "error": ""}


update_api.updater.run_update = slow_run

r = c.post("/api/update/run", json={})
d = r.get_json()
check("3.1 run 立即返回", r.status_code == 200 and d.get("success") is True, d)
check("3.2 返回里带初始进度", isinstance(d.get("progress"), dict), d.get("progress"))

# 中途查一次进度
time.sleep(0.5)
p1 = c.get("/api/update/progress").get_json()
check("3.3 中途 running=True", p1.get("running") is True, p1)
check("3.4 中途有百分比且 <100", 0 <= (p1.get("percent") or 0) < 100, p1.get("percent"))
check("3.5 中途有阶段文案", bool(p1.get("message")), p1.get("message"))

# 并发请求应被拒
r2 = c.post("/api/update/run", json={})
check("3.6 并发更新被拒（409）", r2.status_code == 409, r2.status_code)

# 等它跑完
final = None
for _ in range(40):
    time.sleep(0.4)
    p = c.get("/api/update/progress").get_json()
    if not p.get("running"):
        final = p
        break
check("3.7 最终 running=False", final is not None and final.get("running") is False,
      (final or {}).get("running"))
check("3.8 最终 success=True", (final or {}).get("success") is True,
      (final or {}).get("error"))
check("3.9 最终 percent=100", (final or {}).get("percent") == 100,
      (final or {}).get("percent"))
res = (final or {}).get("result") or {}
check("3.10 结果含 file_count", res.get("file_count") == 2, res.get("file_count"))
check("3.11 结果含 need_restart（有 app/ 文件→True）",
      res.get("need_restart") is True, res.get("need_restart"))

# 再点一次应能重新开始（running 已复位）
r3 = c.post("/api/update/run", json={})
check("3.12 完成后可再次发起", r3.status_code == 200, r3.status_code)
for _ in range(40):
    time.sleep(0.4)
    if not c.get("/api/update/progress").get_json().get("running"):
        break
update_api.updater.run_update = _orig_run

print()
print("4. 页面含进度条相关代码")
r = c.get("/settings")
html = r.get_data(as_text=True)
for k in ("renderProgress", "pollUpdateProgress", "api/update/progress",
          "NO_RESTART_HINT", "errBox"):
    check(f"4.x 含 {k}", k in html)
check("4.1 不再有旧的 files.length 用法",
      "res.files.length" not in html)

print()
print("=" * 62)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
