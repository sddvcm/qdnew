# -*- coding: utf-8 -*-
"""复现「旧 .pyc 导致启动崩溃」并验证清理有效（v1.6.3 的关键修复）

用户实测：1.6.0 在线更新到 1.6.1 后一重启就起不来。

根因：更新用 os.replace() 原子替换 .py，但旧 .pyc 仍在。Python 判断缓存
有效性靠「源文件 mtime + size」，mtime 只有秒级精度 —— 小改动在同一秒内
完成时，新 .py 的 mtime/size 可能与 .pyc 记录一致 → 解释器认为缓存有效
→ 按旧字节码执行、磁盘上是新源码 → ImportError/TypeError → 启动即崩。

这个测试**真的构造这个场景**：先用旧源码编译出 .pyc，再换上"新源码"但
把 mtime 设回同一秒、size 保持一致，验证 Python 确实会加载旧字节码；
然后跑 _purge_pycache() 确认问题消失。
"""
import io
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="pyc_test_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "pyc-key"
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


print("=" * 62)
print("1. 复现：旧 .pyc 让新源码不生效")

proj = os.path.join(TMP, "proj")
os.makedirs(os.path.join(proj, "pkg"), exist_ok=True)

# 旧版本：函数叫 old_name，返回 'OLD'
OLD_SRC = "def old_name():\n    return 'OLD'\n"
# 新版本：函数改名 + 逻辑变化，**保持同样长度**（模拟"改几行"的更新）
NEW_SRC = "def new_name():\n    return 'NEW'\n"
check("1.1 两个源码长度一致（构造成 size 相同）",
      len(OLD_SRC.encode()) == len(NEW_SRC.encode()),
      "%d vs %d" % (len(OLD_SRC.encode()), len(NEW_SRC.encode())))

mod = os.path.join(proj, "pkg", "target.py")
with io.open(mod, "w", encoding="utf-8", newline="") as f:
    f.write(OLD_SRC)

# 编译出 .pyc（模拟旧版本运行过一次）
st = os.stat(mod)
py_compile.compile(mod, doraise=True)
pyc = os.path.join(proj, "pkg", "__pycache__", "target.cpython-%d%d.pyc" % (
    sys.version_info.major, sys.version_info.minor))
check("1.2 .pyc 已生成", os.path.isfile(pyc), pyc)

# 换成新源码，但把 mtime 设回与编译时**同一秒**（关键：模拟秒内更新）
old_mtime = st.st_mtime
with io.open(mod, "w", encoding="utf-8", newline="") as f:
    f.write(NEW_SRC)
os.utime(mod, (old_mtime, old_mtime))
check("1.3 源文件 mtime 已设回同一秒",
      int(os.stat(mod).st_mtime) == int(old_mtime),
      "%s vs %s" % (os.stat(mod).st_mtime, old_mtime))

# 在独立进程里导入，看拿到的是哪个函数（必须新开进程：同一进程会命中
# sys.modules，测不出来）
probe = (
    "import sys; sys.path.insert(0, %r)\n"
    "import pkg.target as t\n"
    "print('HAS_OLD=%%s' %% hasattr(t, 'old_name'))\n"
    "print('HAS_NEW=%%s' %% hasattr(t, 'new_name'))\n"
) % proj
env = os.environ.copy()
env["PYTHONDONTWRITEBYTECODE"] = ""
r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                   env=env, cwd=proj)
has_old = "HAS_OLD=True" in (r.stdout or "")
has_new = "HAS_NEW=True" in (r.stdout or "")
print("   子进程输出:", (r.stdout or "").strip().replace("\n", " | "))
# ★ 这里要断言的是「**能复现这个 bug**」：解释器按旧字节码执行，
#   看到的是已从源码删除的 old_name，而新源码的 new_name 不可见。
#   若某天 Python 改进了缓存校验（比如 mtime 用纳秒）导致这句失败，
#   说明复现条件不再成立 —— 但清理逻辑仍应保留作兜底（改动可能跨秒完成）。
check("1.4 成功复现：解释器加载旧字节码、新源码不生效",
      has_old and not has_new,
      "has_old=%s has_new=%s（这正是「更新后重启崩溃」的成因）" % (has_old, has_new))

print()
print("2. _purge_pycache 清理有效")
import updater  # noqa: E402
_orig_root = updater.ROOT
updater.ROOT = proj
try:
    before = os.path.isdir(os.path.join(proj, "pkg", "__pycache__"))
    n = updater._purge_pycache()
    after = os.path.isdir(os.path.join(proj, "pkg", "__pycache__"))
finally:
    updater.ROOT = _orig_root

check("2.1 清理前 __pycache__ 存在", before)
check("2.2 返回值 >= 1", n >= 1, n)
check("2.3 清理后 __pycache__ 已删", not after)

# 清完再导入，必须拿到新函数
r2 = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                    env=env, cwd=proj)
check("2.4 清理后新源码必定生效",
      "HAS_NEW=True" in (r2.stdout or ""),
      (r2.stdout or "").strip().replace("\n", " | "))

print()
print("3. 清理只删 __pycache__，不动源码")
check("3.1 源码文件仍在", os.path.isfile(mod))
with io.open(mod, encoding="utf-8") as f:
    check("3.2 源码内容正确", f.read() == NEW_SRC)
# 造些别的目录，确认不被误删
for d in ("data", "logs", "runtime"):
    os.makedirs(os.path.join(proj, d), exist_ok=True)
    io.open(os.path.join(proj, d, "keep.txt"), "w").write("x")
updater.ROOT = proj
try:
    updater._purge_pycache()
finally:
    updater.ROOT = _orig_root
for d in ("data", "logs", "runtime"):
    check(f"3.x 未误删 {d}/", os.path.isfile(os.path.join(proj, d, "keep.txt")))

print()
print("4. run_update 会调用清理（阶段 5）")
import re  # noqa: E402
src = io.open(os.path.join(ROOT, "updater.py"), encoding="utf-8").read()
check("4.1 run_update 里调了 _purge_pycache", "purged = _purge_pycache()" in src)
check("4.2 结果里带 pycache_purged 字段", '"pycache_purged"' in src)
check("4.3 有 _purge_pycache 函数定义", "def _purge_pycache" in src)

print()
print("5. cmd/main 启动前也清（双保险）")
main_sh = io.open(os.path.join(ROOT, "packaging", "fnos", "cmd", "main"),
                  encoding="utf-8").read()
common_sh = io.open(os.path.join(ROOT, "packaging", "fnos", "cmd", "common.sh"),
                    encoding="utf-8").read()
check("5.1 main 调用 purge_pycache", "purge_pycache" in main_sh)
check("5.2 common.sh 定义 purge_pycache", "purge_pycache()" in common_sh)
check("5.3 用 find 定位 __pycache__", "__pycache__" in common_sh)

print()
print("=" * 62)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
