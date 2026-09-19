# -*- coding: utf-8 -*-
"""cmd 脚本冒烟测试：验证数据目录落点（官方 data-share 机制）+ 兜底 + 迁移。

覆盖：
  - 官方软链入口 /var/apps/<app>/shares/<app> → 数据落共享目录
  - TRIM_DATA_SHARE_PATHS 可用 → 数据落共享目录（文件管理可见）
  - 共享目录**不可写**（模拟未声明/ACL 未授权）→ 回退私有目录，应用照常可跑
  - 多路径（":" 分隔）取第一个
  - 老数据从私有目录迁到共享目录；幂等；不覆盖已有新数据

⚠️ 官方文档给出两个取径（「也可以通过 /var/apps/myapp/share/ 下的软链访问」+
   环境变量 TRIM_DATA_SHARE_PATHS）。**两个都必须探**：环境变量只注入到生命周期
   脚本的环境里，常驻进程不一定继承得到 —— 只认变量会导致探测落空、数据无声地
   退回私有目录（用户在文件管理里又找不到了）。
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


def _msys_to_win(p):
    """把 MSYS 风格路径（/c/Users/...、/tmp/...）转成 Windows 路径。

    仅用于**测试断言**：bash 的 readlink -f 返回 MSYS 视角（/tmp/...），
    而环境变量里给的是 /c/Users/... —— Python 的 os.path 认不出这两种写法。
    """
    if os.name != "nt" or not p.startswith("/"):
        return p
    # /tmp 在 Git Bash 里映射到 %TEMP%
    if p == "/tmp" or p.startswith("/tmp/"):
        return p.replace("/tmp", tempfile.gettempdir().replace("\\", "/"), 1)
    # /c/foo → C:/foo
    if len(p) > 2 and p[1].isalpha() and p[2] == "/":
        return p[1].upper() + ":/" + p[3:]
    return p


def _norm(p):
    """规范化路径用于比较（消除 MSYS/Windows 写法的差异）"""
    if not p:
        return ""
    return os.path.normcase(
        os.path.realpath(_msys_to_win(p)).replace("\\", "/").rstrip("/"))


def _same(a, b):
    """两个路径是否指向同一位置。

    必须做规范化：软链经 bash 的 readlink -f 后是 MSYS 视角（/tmp/...），
    而环境变量给的是 /c/...；两者字符串不相等，实际是同一个目录。
    """
    if not a or not b:
        return False
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True
    # 目标可能还没被创建（realpath 对不存在路径不解析软链），退回纯字符串比
    return na.lower() == nb.lower()


def run_common(tmp, pkgvar, share_paths=None, apps_dir=None):
    """source common.sh 并输出关键变量。

    apps_dir: 覆盖 APP_SHARES_DIR 指向的 /var/apps 根，用于测软链入口
              （真实路径 /var/apps 在本机不存在，也无法创建）。
    """
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
    # 覆盖软链根：脚本里 APP_SHARES_DIR="/var/apps/${APP_NAME}"
    override = ""
    if apps_dir:
        override = (
            'APP_SHARES_DIR="%s"\n'
            'SHARE_LINK_CANDIDATES="%s/shares/%s %s/share/%s"\n'
        ) % (posix(apps_dir), posix(apps_dir), "checkin-system",
             posix(apps_dir), "checkin-system")
    script = (
        'source "%s/common.sh"\n'
        '%s'
        'ensure_data_dir\n'
        'echo "DATA_ROOT=$DATA_ROOT"\n'
        'echo "DATA_ROOT_SRC=$DATA_ROOT_SRC"\n'
        'echo "DATA_DIR=$DATA_DIR"\n'
        'echo "USER_PLUGINS_DIR=$USER_PLUGINS_DIR"\n'
        'echo "LOG_DIR=$LOG_DIR"\n'
        'echo "LOG_FILE=$LOG_FILE"\n'
        'echo "PID_FILE=$PID_FILE"\n'
        'echo "FALLBACK_ROOT=$FALLBACK_ROOT"\n'
        'ensure_dirs\n'
    ) % (CMD, override)
    return subprocess.run([BASH, "-c", script], capture_output=True,
                          text=True, env=env, cwd=CMD)


def parse(r):
    d = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


def _run_all():
    tmp = tempfile.mkdtemp(prefix="cmd_smoke_")
    share_ok = os.path.join(tmp, "vol1", "@appshare", "checkin-system")
    pkgvar = posix(os.path.join(tmp, "vol1", "@appdata", "checkin-system"))
    fallback = os.path.join(tmp, "vol1", "@appdata", "checkin-system")

    print("=" * 62)
    print("1. 官方机制：TRIM_DATA_SHARE_PATHS 可用 → 数据落共享目录")
    os.makedirs(share_ok, exist_ok=True)
    r = run_common(tmp, pkgvar, share_paths=posix(share_ok))
    d = parse(r)
    check("1.1 DATA_ROOT = 共享目录", _same(d.get("DATA_ROOT"), share_ok),
          d.get("DATA_ROOT"))
    check("1.2 DATA_DIR 在共享目录下",
          d.get("DATA_DIR") == posix(share_ok) + "/data", d.get("DATA_DIR"))
    check("1.3 私有目录作为兜底保留",
          d.get("FALLBACK_ROOT") == pkgvar, d.get("FALLBACK_ROOT"))

    print()
    print("2. 未声明共享目录（变量为空）→ 回退私有目录，应用仍能跑")
    r = run_common(tmp, pkgvar, share_paths=None)
    d = parse(r)
    check("2.1 DATA_ROOT 回退到私有目录",
          _same(d.get("DATA_ROOT"), fallback), d.get("DATA_ROOT"))
    check("2.2 不报错（rc=0）", r.returncode == 0, r.stderr[:200])

    print()
    print("3. 共享目录存在但**不可写**（模拟 ACL 未授权）→ 回退，不崩")
    # 指向一个「文件」而不是目录 → mkdir 必失败
    notdir = os.path.join(tmp, "a_file")
    with open(notdir, "w") as f:
        f.write("x")
    r = run_common(tmp, pkgvar, share_paths=posix(notdir))
    d = parse(r)
    check("3.1 路径不可用时回退私有目录",
          _same(d.get("DATA_ROOT"), fallback), d.get("DATA_ROOT"))
    check("3.2 关键：没有崩溃（rc=0）", r.returncode == 0, r.stderr[:200])

    print()
    print("4. 多路径（':' 分隔）取第一个")
    other = os.path.join(tmp, "vol2", "@appshare", "checkin-system")
    os.makedirs(other, exist_ok=True)
    r = run_common(tmp, pkgvar, share_paths=posix(other) + ":" + posix(share_ok))
    d = parse(r)
    check("4.1 取第一个路径", _same(d.get("DATA_ROOT"), other), d.get("DATA_ROOT"))
    check("4.2 来源标记为环境变量",
          "TRIM_DATA_SHARE_PATHS" in d.get("DATA_ROOT_SRC", ""),
          d.get("DATA_ROOT_SRC"))

    print()
    print("5. 官方软链入口 → 数据落共享目录（不依赖环境变量）")
    # 模拟真实布局：/var/apps/<app>/shares/<app> 是指向 @appshare 的**软链**
    apps_root = os.path.join(tmp, "var_apps")
    link_true = os.path.join(tmp, "vol1", "@appshare", "checkin-system")
    os.makedirs(link_true, exist_ok=True)
    link_dir = os.path.join(apps_root, "shares")
    os.makedirs(link_dir, exist_ok=True)
    os.symlink(link_true, os.path.join(link_dir, "checkin-system"))
    r = run_common(tmp, pkgvar, share_paths=None, apps_dir=apps_root)
    d = parse(r)
    check("5.1 环境变量为空时，靠软链找到共享目录",
          _same(d.get("DATA_ROOT"), link_true), d.get("DATA_ROOT"))
    check("5.2 来源标记为软链", "软链" in d.get("DATA_ROOT_SRC", ""),
          d.get("DATA_ROOT_SRC"))
    check("5.3 关键：不再无声退回私有目录",
          not _same(d.get("DATA_ROOT"), fallback), d.get("DATA_ROOT"))
    check("5.4 落点是真实路径而非软链本身",
          "/shares/" not in d.get("DATA_ROOT", ""), d.get("DATA_ROOT"))

    print()
    print("6. 软链 + 环境变量同时存在 → 软链优先（文件系统层更可靠）")
    r = run_common(tmp, pkgvar, share_paths=posix(other), apps_dir=apps_root)
    d = parse(r)
    check("6.1 软链优先于环境变量", _same(d.get("DATA_ROOT"), link_true),
          d.get("DATA_ROOT"))

    print()
    print("7. 老数据迁移：私有目录 → 共享目录")
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
    subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                   env=env, cwd=CMD)
    new_db = os.path.join(share_ok, "data", "checkin.db")
    check("7.1 旧库已搬到共享目录", os.path.isfile(new_db), new_db)
    check("7.2 原位置已清空", not os.path.exists(old_db))
    with io.open(new_db) as f:
        check("7.3 内容完整", f.read() == "LEGACY")

    print()
    print("8. 幂等 + 不覆盖")
    r2 = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                        env=env, cwd=CMD)
    check("8.1 第二次执行 rc=0", r2.returncode == 0, r2.stderr[:200])
    with io.open(new_db, "w") as f:
        f.write("NEW")
    os.makedirs(os.path.dirname(old_db), exist_ok=True)
    with io.open(old_db, "w") as f:
        f.write("OLD")
    subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env,
                   cwd=CMD)
    with io.open(new_db) as f:
        check("8.2 新数据未被旧数据覆盖", f.read() == "NEW")

    print()
    print("9. 日志与 PID 落在共享目录（用户可见）")
    # 用户反馈：日志原来在 @appdata（文件管理器看不到），且文件是空的
    # ⚠️ 必须**重新跑一次**拿干净结果：上面的 d 是第 4 步（共享目录=vol2）
    #    留下的，拿它跟 share_ok(vol1) 比会误报。这类"变量串用"是测试老坑。
    r9 = run_common(tmp, pkgvar, share_paths=posix(share_ok))
    d9 = parse(r9)
    check("9.1 LOG_DIR 在共享目录下",
          _same(d9.get("LOG_DIR"), os.path.join(share_ok, "logs")),
          d9.get("LOG_DIR"))
    check("9.2 LOG_FILE = LOG_DIR/app.log",
          (d9.get("LOG_FILE") or "").endswith("/logs/app.log"),
          d9.get("LOG_FILE"))
    check("9.3 PID_FILE 也在共享目录",
          _same(d9.get("PID_FILE"), os.path.join(share_ok, "app.pid")),
          d9.get("PID_FILE"))
    check("9.4 不再落在 @appdata",
          "@appdata" not in (d9.get("LOG_DIR") or ""), d9.get("LOG_DIR"))

    print()
    print("10. 共享目录不可用时，日志跟着回退私有目录（不能丢日志）")
    r = run_common(tmp, pkgvar, share_paths=None)
    d2 = parse(r)
    check("10.1 LOG_DIR 回退到私有目录",
          _same(d2.get("LOG_DIR"), os.path.join(fallback, "logs")),
          d2.get("LOG_DIR"))
    check("10.2 rc=0 不崩", r.returncode == 0, r.stderr[:150])

    print()
    print("11. fix_ownership 空 SHARE_ROOT 不误伤（关键：chown 空参数会指向当前目录）")
    # 构造 SHARE_ROOT 为空的环境，跑一遍 fix_ownership，确认没报错
    env2 = os.environ.copy()
    env2.update({
        "TRIM_APPDEST": posix(os.path.join(tmp, "appdest", "checkin-system")),
        "TRIM_PKGVAR": pkgvar, "TRIM_PKGETC": posix(os.path.join(tmp, "etc")),
        "TRIM_USERNAME": "", "TRIM_GROUPNAME": "",
    })
    env2.pop("TRIM_DATA_SHARE_PATHS", None)
    s2 = (f'source "{CMD}/common.sh"\n'
          f'echo "SHARE_ROOT=[$SHARE_ROOT]"\n'
          f'fix_ownership\n'
          f'echo "RC=$?"\n')
    r2 = subprocess.run([BASH, "-c", s2], capture_output=True, text=True,
                        env=env2, cwd=CMD)
    check("11.1 空 SHARE_ROOT 时 fix_ownership 正常返回",
          "RC=0" in (r2.stdout or ""), (r2.stdout or "")[:150])
    check("11.2 无异常输出",
          "unbound" not in (r2.stderr or "").lower(), (r2.stderr or "")[:150])

    print()
    print("=" * 62)
    print(f"PASS {PASS}  FAIL {FAIL}")
    if FAILS:
        for f in FAILS:
            print("  -", f)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
