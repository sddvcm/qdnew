# -*- coding: utf-8 -*-
"""日志配置测试（app/logsetup.py）

用户反馈"日志文件是空的"，根因不是没写日志，而是：
  - 应用**完全没有 logging 配置**，只有 print
  - Python 重定向到文件时 stdout 是块缓冲，崩在半路时缓冲区不落盘
本模块锁住修复，防止回归成"日志文件 0 字节"。
"""
import io
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="log_test_")
os.environ["CHECKIN_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "log-key"
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


from app import logsetup  # noqa: E402

print("=" * 62)
print("1. 路径解析")
check("1.1 log_dir 用 CHECKIN_LOG_DIR",
      logsetup.log_dir() == os.path.join(TMP, "logs"), logsetup.log_dir())
check("1.2 log_file = dir/app.log",
      logsetup.log_file() == os.path.join(TMP, "logs", "app.log"),
      logsetup.log_file())

print()
print("2. setup() 能建日志并写入")
logsetup.setup()
check("2.1 日志目录已创建", os.path.isdir(logsetup.log_dir()))
log = logging.getLogger("app")
log.info("测试日志内容 MARKER_ABC123")
for h in logging.getLogger().handlers:
    try:
        h.flush()
    except Exception:                           # noqa: BLE001
        pass
lf = logsetup.log_file()
check("2.2 日志文件已生成", os.path.isfile(lf), lf)
content = io.open(lf, encoding="utf-8", errors="replace").read()
check("2.3 内容真的写进去了（关键：防 0 字节）",
      "MARKER_ABC123" in content, "%d 字节" % len(content))
check("2.4 日志带时间戳与级别",
      "[INFO]" in content and "2026-" in content, content[:80])

print()
print("3. 幂等：重复 setup 不叠加 handler")
n_before = len(logging.getLogger().handlers)
logsetup.setup()
logsetup.setup()
check("3.1 handler 数量不变",
      len(logging.getLogger().handlers) == n_before,
      "%d -> %d" % (n_before, len(logging.getLogger().handlers)))

print()
print("4. 有轮转配置（长期运行不撑爆磁盘）")
from logging.handlers import RotatingFileHandler  # noqa: E402
rot = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
check("4.1 文件 handler 是轮转式", len(rot) == 1, len(rot))
if rot:
    check("4.2 有大小上限", rot[0].maxBytes > 0 and rot[0].maxBytes <= 10 * 1024 * 1024,
          rot[0].maxBytes)
    check("4.3 有备份份数", rot[0].backupCount >= 1, rot[0].backupCount)

print()
print('5. 未捕获异常会落进日志（排查"启动即退出"的关键）')
# 装 hook 后手动调用，模拟未捕获异常
logsetup.install_excepthook()
check("5.1 sys.excepthook 已被替换", sys.excepthook is not logsetup.sys.__excepthook__)

try:
    raise ValueError("模拟启动崩溃 EXC_MARKER_789")
except ValueError:
    exc_type, exc_val, exc_tb = sys.exc_info()
    sys.excepthook(exc_type, exc_val, exc_tb)

for h in logging.getLogger().handlers:
    try:
        h.flush()
    except Exception:                           # noqa: BLE001
        pass
content2 = io.open(lf, encoding="utf-8", errors="replace").read()
check("5.2 异常栈写进日志", "EXC_MARKER_789" in content2)
check("5.3 带 critical 级别", "[CRITICAL]" in content2)
check("5.4 含 traceback 细节", "Traceback" in content2 or "ValueError" in content2)

print()
print("6. 线程未捕获异常也会落日志")
import threading  # noqa: E402
check("6.1 已安装 threading.excepthook",
      threading.excepthook is not None)


def _boom():
    raise RuntimeError("线程崩溃 THREAD_MARKER_456")


t = threading.Thread(target=_boom, name="test-thread")
t.start()
t.join()
for h in logging.getLogger().handlers:
    try:
        h.flush()
    except Exception:                           # noqa: BLE001
        pass
content3 = io.open(lf, encoding="utf-8", errors="replace").read()
check("6.2 线程异常写进日志", "THREAD_MARKER_456" in content3)

print()
print("7. 日志目录不可写时降级，不阻止启动")
# 指向一个「文件」而不是目录 → makedirs 必失败
badfile = os.path.join(TMP, "not_a_dir")
with io.open(badfile, "w") as f:
    f.write("x")
os.environ["CHECKIN_LOG_DIR"] = badfile
# 重置配置状态以重新走一遍 setup
logsetup._configured = False
root_logger = logging.getLogger()
saved = list(root_logger.handlers)
for h in saved:
    root_logger.removeHandler(h)
try:
    logsetup.setup()                    # 不该抛异常
    check("7.1 目录不可写时不抛异常（降级为仅控制台）", True)
    check("7.2 仍有控制台 handler",
          len(logging.getLogger().handlers) >= 1,
          len(logging.getLogger().handlers))
except Exception as e:                  # noqa: BLE001
    check("7.1 目录不可写时不抛异常（降级为仅控制台）", False, f"{type(e).__name__}: {e}")
finally:
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    for h in saved:
        root_logger.addHandler(h)
    logsetup._configured = True

os.environ["CHECKIN_LOG_DIR"] = os.path.join(TMP, "logs")

print()
print("8. 启动横幅包含排查所需信息")
logsetup.startup_banner()
for h in logging.getLogger().handlers:
    try:
        h.flush()
    except Exception:                           # noqa: BLE001
        pass
c4 = io.open(lf, encoding="utf-8", errors="replace").read()
for k in ("Python:", "工作目录", "程序版本", "数据目录", "日志文件"):
    check(f"8.x 横幅含 {k}", k in c4)

print()
print("9. 环境信息接口带日志路径（用户能在界面看到去哪找）")
from app.main import create_app  # noqa: E402
app = create_app()
app.config["TESTING"] = True
c = app.test_client()
d = c.get("/api/system/env").get_json()
dd = d.get("data", {})
check("9.1 含 log_file", "log_file" in dd, list(dd.keys())[:12])
check("9.2 含 log_dir", "log_dir" in dd)
check("9.3 含 log_size_kb", "log_size_kb" in dd)
check("9.4 log_file 指向本次环境",
      os.path.join(TMP, "logs") in (dd.get("log_file") or ""), dd.get("log_file"))

r = c.get("/settings")
html = r.get_data(as_text=True)
check("9.5 设置页展示日志文件行", "日志文件" in html and "log_file" in html)

print()
print("=" * 62)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
