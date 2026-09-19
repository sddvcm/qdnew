# -*- coding: utf-8 -*-
"""cmd 脚本冒烟测试：验证数据目录落点（官方 data-share 机制）+ 兜底 + 迁移。

覆盖：
  - TRIM_DATA_SHARE_PATHS 可用 → 数据落共享目录（文件管理可见）
  - 共享目录**不可写**（模拟未声明/ACL 未授权）→ 回退私有目录，应用照常可跑
  - 多路径（":" 分隔）取第一个
  - 老数据从私有目录迁到共享目录；幂等；不覆盖已有新数据
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CMD = os.path.join(HERE, "cmd")
BASH = os.environ.get("BASH_BIN") or \
    r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\bin\bash.exe"

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


def posix(p):
    p = os.path.abspath(p)
    return "/" + p[0].lower() + p[2:].replace("\\", "/") if p[1:3] == ":\\" else p


def run_common(tmp, pkgvar, share_paths=None, cwd_probe=""):
    """source common.sh 并输出关键变量"""
    env = os.environ.copy()
    env.update({
        "TRIM_APPDEST": posix(os.path.join(tmp, "appdest", "checkin-system")),
        "TRIM_PKGVAR": pkgvar,
        "TRIM_PKGETC": posix(os.path.join(tmp, "etc")),
        "TRIM_USERNAME": "", "TRIM_GROUPNAME": "",
    })
    env.pop("TRIM_DATA_SHARE_PATHS", None)
    if share_paths is not None:
        env["TRIM_DATA_SHARE_PATHS"] = share_paths
    script = (
        'source "%s/common.sh"\n'
        'echo "DATA_ROOT=$DATA_ROOT"\n'
        'echo "DATA_DIR=$DATA_DIR"\n'
        'echo "USER_PLUGINS_DIR=$USER_PLUGINS_DIR"\n'
        'echo "FALLBACK_ROOT=$FALLBACK_ROOT"\n'
        'ensure_dirs\n'
    ) % CMD
    return subprocess.run([BASH, "-c", script], capture_output=True,
                          text=True, env=env, cwd=CMD)


def parse(r):
    d = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


tmp = tempfile.mkdtemp(prefix="cmd_smoke_")
share_ok = os.path.join(tmp, "vol1", "@appshare", "checkin-system")
pkgvar = posix(os.path.join(tmp, "vol1", "@appdata", "checkin-system"))

print("=" * 62)
print("1. 官方机制：TRIM_DATA_SHARE_PATHS 可用 → 数据落共享目录")
os.makedirs(share_ok, exist_ok=True)
r = run_common(tmp, pkgvar, share_paths=posix(share_ok))
d = parse(r)
check("1.1 DATA_ROOT = 共享目录", d.get("DATA_ROOT") == posix(share_ok), d)
check("1.2 DATA_DIR 在共享目录下",
      d.get("DATA_DIR") == posix(share_ok) + "/data", d)
check("1.3 私有目录作为兜底保留",
      d.get("FALLBACK_ROOT") == pkgvar, d)

print()
print("2. 未声明共享目录（变量为空）→ 回退私有目录，应用仍能跑")
r = run_common(tmp, pkgvar, share_paths=None)
d = parse(r)
check("2.1 DATA_ROOT 回退到私有目录", d.get("DATA_ROOT") == pkgvar, d)
check("2.2 不报错（rc=0）", r.returncode == 0, r.stderr[:200])

print()
print("3. 共享目录存在但**不可写**（模拟 ACL 未授权）→ 回退，不崩")
ro = os.path.join(tmp, "ro_share", "checkin-system")
os.makedirs(ro, exist_ok=True)
# 只读：用 chmod 在 Windows 上不可靠，改为指向一个「文件」而不是目录
notdir = os.path.join(tmp, "a_file")
with open(notdir, "w") as f:
    f.write("x")
r = run_common(tmp, pkgvar, share_paths=posix(notdir))
d = parse(r)
check("3.1 路径不可用时回退私有目录", d.get("DATA_ROOT") == pkgvar, d)
check("3.2 关键：没有崩溃（rc=0）", r.returncode == 0, r.stderr[:200])

print()
print("4. 多路径（':' 分隔）取第一个")
other = os.path.join(tmp, "vol2", "@appshare", "checkin-system")
os.makedirs(other, exist_ok=True)
r = run_common(tmp, pkgvar, share_paths=posix(other) + ":" + posix(share_ok))
d = parse(r)
check("4.1 取第一个路径", d.get("DATA_ROOT") == posix(other), d)

print()
print("5. 老数据迁移：私有目录 → 共享目录")
shutil.rmtree(share_ok, ignore_errors=True)
os.makedirs(share_ok, exist_ok=True)
old_db = os.path.join(tmp, "vol1", "@appdata", "checkin-system", "data",
                      "checkin.db")
os.makedirs(os.path.dirname(old_db), exist_ok=True)
with io.open(old_db, "w") as f:
    f.write("LEGACY")
env = os.environ.copy()
env.update({
    "TRIM_APPDEST": posix(os.path.join(tmp, "appdest", "checkin-system")),
    "TRIM_PKGVAR": pkgvar, "TRIM_PKGETC": posix(os.path.join(tmp, "etc")),
    "TRIM_USERNAME": "", "TRIM_GROUPNAME": "",
    "TRIM_DATA_SHARE_PATHS": posix(share_ok),
})
script = (f'source "{CMD}/common.sh"\nensure_dirs\n'
          f'migrate_legacy_data\n'
          f'echo "AFTER=$(ls {posix(share_ok)}/data 2>/dev/null)"\n')
r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                   env=env, cwd=CMD)
new_db = os.path.join(share_ok, "data", "checkin.db")
check("5.1 旧库已搬到共享目录", os.path.isfile(new_db), new_db)
check("5.2 原位置已清空", not os.path.exists(old_db))
with io.open(new_db) as f:
    check("5.3 内容完整", f.read() == "LEGACY")

print()
print("6. 幂等 + 不覆盖")
r2 = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                    env=env, cwd=CMD)
check("6.1 第二次执行 rc=0", r2.returncode == 0, r2.stderr[:200])
with io.open(new_db, "w") as f:
    f.write("NEW")
os.makedirs(os.path.dirname(old_db), exist_ok=True)
with io.open(old_db, "w") as f:
    f.write("OLD")
subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env,
               cwd=CMD)
with io.open(new_db) as f:
    check("6.2 新数据未被旧数据覆盖", f.read() == "NEW")

print()
print("=" * 62)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
