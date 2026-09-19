# -*- coding: utf-8 -*-
"""cmd 脚本冒烟测试：模拟 fnOS 注入的 TRIM_* 变量，验证
SHARE_ROOT 推导（不写死卷名）+ 老数据迁移 + 启动路径拼接。

不真正起 Flask —— main start 会被 PY_BIN 缺失挡住，这里只测
common.sh 的路径逻辑（source 后打印变量 + 调 migrate_legacy_data）。
"""
import os
import subprocess
import shutil
import sys
import tempfile
import io

CMD = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmd"))
out = []
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    tag = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    line = f"{tag}  {name}  {extra}"
    out.append(line)
    print(line)


def run_scenario(tmp, pkgvar, extra_env=None, do_migrate=True):
    """source common.sh 后输出 SHARE_ROOT/DATA_DIR 等关键变量"""
    env = os.environ.copy()
    env.update({
        "TRIM_APPDEST": os.path.join(tmp, "appdest", "checkin-system"),
        "TRIM_PKGVAR": pkgvar,
        "TRIM_PKGETC": os.path.join(tmp, "etc"),
        "TRIM_USERNAME": "",          # 模拟非 root：跳过 chown
        "TRIM_GROUPNAME": "",
    })
    if extra_env:
        env.update(extra_env)
    script = (
        'source "%s/common.sh"\n'
        'echo "SHARE_ROOT=$SHARE_ROOT"\n'
        'echo "DATA_DIR=$DATA_DIR"\n'
        'echo "USER_PLUGINS_DIR=$USER_PLUGINS_DIR"\n'
        'echo "BACKUP_DIR=$BACKUP_DIR"\n'
        'echo "LOG_DIR=$LOG_DIR"\n'
    ) % CMD
    if do_migrate:
        script += 'ensure_dirs\nmigrate_legacy_data\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True,
                       text=True, env=env, cwd=CMD,
                       shell=False) if False else subprocess.run(
        [os.environ.get("BASH_BIN", "bash"), "-c", script],
        capture_output=True, text=True, env=env, cwd=CMD)
    return r


def parse(r):
    d = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


tmp = tempfile.mkdtemp(prefix="cmd_smoke_")
BASH = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
if not os.path.exists(BASH):
    BASH = r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\bin\bash.exe"
os.environ["BASH_BIN"] = BASH

print("=" * 60)
print("1. 普通安装：TRIM_PKGVAR=/vol1/@appdata/checkin-system")
r = run_scenario(tmp, "/vol1/@appdata/checkin-system")
d = parse(r)
check("1.1 SHARE_ROOT=/vol1/@appshare/checkin-system",
      d.get("SHARE_ROOT") == "/vol1/@appshare/checkin-system", d)
check("1.2 DATA_DIR 在 SHARE_ROOT/data",
      d.get("DATA_DIR") == "/vol1/@appshare/checkin-system/data", d)
check("1.3 日志留在 var（内部）",
      d.get("LOG_DIR") == "/vol1/@appdata/checkin-system/logs", d)
check("1.4 备份目录与 updater 的 DATA_DIR/backups 一致",
      d.get("BACKUP_DIR") == "/vol1/@appshare/checkin-system/data/backups", d)

print()
print("2. 装在别的卷：/vol2（不写死 vol1）")
r = run_scenario(tmp, "/vol2/@appdata/checkin-system")
d = parse(r)
check("2.1 SHARE_ROOT 跟随安装卷 /vol2",
      d.get("SHARE_ROOT") == "/vol2/@appshare/checkin-system", d)

print()
print("3. root 安装：/usr/local/apps/@appdata/...")
r = run_scenario(tmp, "/usr/local/apps/@appdata/checkin-system")
d = parse(r)
check("3.1 SHARE_ROOT 落系统盘 @appshare",
      d.get("SHARE_ROOT") == "/usr/local/apps/@appshare/checkin-system", d)

print()
print("4. 老数据迁移：var/data 有旧库 → 搬到 @appshare")
old_data = os.path.join(tmp, "legacy_var")
pkgvar = "/vol1/@appdata/checkin-system"
# 构造 Windows 侧目录：把 /vol1 映射到临时目录下模拟
# （脚本是 POSIX 路径，Windows bash 会把 /vol1 解析到盘符根 —— 不可写）
# 因此迁移测试用「真实 bash 能写」的路径跑：用 MSYS 映射不了的路径会失败，
# 改在 Git Bash 根下模拟：用 //c/ 形式? —— 直接用 tmpdir 换算 POSIX 路径。
win_tmp = os.path.abspath(tmp)
posix = "/c/" + win_tmp[3:].replace("\\", "/") if win_tmp[1:3] == ":\\" else win_tmp
pv = posix + "/@appdata/checkin-system"
os.makedirs(os.path.join(win_tmp, "@appdata", "checkin-system", "data"), exist_ok=True)
with open(os.path.join(win_tmp, "@appdata", "checkin-system", "data",
                       "checkin.db"), "w") as f:
    f.write("fake-db")
r = run_scenario(tmp, pv)
d = parse(r)
new_db = os.path.join(win_tmp, "@appshare", "checkin-system", "data", "checkin.db")
check("4.1 旧 checkin.db 已搬到 @appshare", os.path.isfile(new_db),
      new_db)
old_gone = not os.path.exists(os.path.join(
    win_tmp, "@appdata", "checkin-system", "data", "checkin.db"))
check("4.2 原位置已清空（move 而非 copy）", old_gone)
# log_msg 写的是 LOG_DIR/install.log（文件），不在 stdout
_logfile = os.path.join(win_tmp, "@appdata", "checkin-system", "logs",
                        "install.log")
_migrated = os.path.isfile(_logfile) and "migrate:" in io.open(
    _logfile, encoding="utf-8", errors="replace").read()
check("4.3 install.log 记录了迁移", _migrated, _logfile)

print()
print("5. 幂等：再跑一次不报错、不重复搬")
r2 = run_scenario(tmp, pv)
check("5.1 第二次执行 rc=0", r2.returncode == 0, r2.stderr[:200])
check("5.2 数据仍在 @appshare", os.path.isfile(new_db))

print()
print("6. 目标已有内容时不覆盖")
# 在 @appshare 放新数据，@appdata 放旧数据 → 跳过
shutil.rmtree(os.path.join(win_tmp, "@appshare", "checkin-system", "data"),
              ignore_errors=True)
os.makedirs(os.path.join(win_tmp, "@appshare", "checkin-system", "data"))
with open(new_db, "w") as f:
    f.write("NEW")
os.makedirs(os.path.join(win_tmp, "@appdata", "checkin-system", "data"),
            exist_ok=True)
with open(os.path.join(win_tmp, "@appdata", "checkin-system", "data",
                       "checkin.db"), "w") as f:
    f.write("OLD")
run_scenario(tmp, pv)
with open(new_db) as f:
    check("6.1 新数据未被覆盖", f.read() == "NEW")

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
