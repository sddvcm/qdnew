"""生命周期脚本功能自测（Windows 上用 Git Bash 跑真实脚本逻辑）。

能测的部分：
    - install_callback：解压运行时 tar、建数据目录、生成密钥、幂等性
    - main status：未运行 → exit 3
    - main stop：无进程时 no-op 成功
    - main start：伪造一个"假 python"（Windows 可执行脚本）验证 PID/状态自愈
测不了的部分（Linux 专属）：
    - chown（Windows 无意义，脚本里已有 2>/dev/null 兜底）
    - 真 ELF 二进制执行
"""
import os
import shutil
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
GIT_BASH = r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\bin\bash.exe"
CMD_DIR = os.path.join(HERE, "cmd")

TEST_ROOT = os.path.join(HERE, "build", "lifecycle_test")
TARGET = os.path.join(TEST_ROOT, "target")
VAR = os.path.join(TEST_ROOT, "var")
ETC = os.path.join(TEST_ROOT, "etc")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)[:180]) if extra else ""))


def make_fake_runtime_tar():
    """造一个最小 runtime.tar：runtime/bin/python3 + runtime/lib/x.so"""
    p = os.path.join(TARGET, "runtime.tar")
    os.makedirs(TARGET, exist_ok=True)
    with tarfile.open(p, "w") as tf:
        def add(name, mode, content=b""):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            if name.endswith(("/bin", "/lib")):
                info.type = tarfile.DIRTYPE
                info.size = 0
            import io
            tf.addfile(info, io.BytesIO(content) if content else None)
        add("runtime/bin", 0o755)
        add("runtime/lib", 0o755)
        add("runtime/bin/python3", 0o755, b"#!/bin/sh\n")
        add("runtime/lib/core.so", 0o644, b"\x7fELF...")
    return p


def run_script(script, args=()):
    """在 Git Bash 里以伪造的 TRIM_* 环境执行生命周期脚本"""
    env = {
        **os.environ,
        "TRIM_APPDEST": TARGET.replace("\\", "/"),
        "TRIM_PKGVAR": VAR.replace("\\", "/"),
        "TRIM_PKGETC": ETC.replace("\\", "/"),
        "TRIM_USERNAME": "checkin-system",
        "TRIM_GROUPNAME": "checkin-system",
        "TRIM_TEMP_LOGFILE": os.path.join(VAR, "trim_error.log").replace("\\", "/"),
        "TRIM_OLD_APPVER": "1.0.0",
        "TRIM_APPVER": "1.2.1",
        "purge_data": "false",
    }
    script_path = os.path.join(CMD_DIR, script).replace("\\", "/")
    cmd = [GIT_BASH, script_path] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env,
                       timeout=60)
    return r


def main():
    # 清理测试区
    if os.path.isdir(TEST_ROOT):
        shutil.rmtree(TEST_ROOT, ignore_errors=True)
    os.makedirs(TARGET, exist_ok=True)
    make_fake_runtime_tar()

    # ---- 1. install_callback：首次安装 ----
    r = run_script("install_callback")
    check("1.1 install_callback 退出码 0", r.returncode == 0, r.stderr[:200])
    check("1.2 运行时已解压",
          os.path.isfile(os.path.join(TARGET, "runtime", "bin", "python3")))
    env_file = os.path.join(ETC, "app.env")
    check("1.3 app.env 已生成", os.path.isfile(env_file))
    secret1 = ""
    if os.path.isfile(env_file):
        content = open(env_file, encoding="utf-8").read()
        check("1.4 env 含 CHECKIN_SECRET_KEY", "CHECKIN_SECRET_KEY=" in content, content[:80])
        check("1.5 密钥为 64 位十六进制",
              "CHECKIN_SECRET_KEY=" in content and
              len(content.split("CHECKIN_SECRET_KEY=")[1].split("\n")[0]) == 64)
        secret1 = content.split("CHECKIN_SECRET_KEY=")[1].split("\n")[0]
    for d in ("data", "logs", "user_plugins", "backups"):
        check(f"1.6 数据目录已建: {d}", os.path.isdir(os.path.join(VAR, d)))

    # ---- 2. 幂等：重跑 install_callback，密钥不变 ----
    # 先删掉解压产物，验证重解压也幂等
    shutil.rmtree(os.path.join(TARGET, "runtime"), ignore_errors=True)
    r = run_script("install_callback")
    check("2.1 重复安装退出码 0", r.returncode == 0, r.stderr[:150])
    check("2.2 运行时再次解压",
          os.path.isfile(os.path.join(TARGET, "runtime", "bin", "python3")))
    content = open(env_file, encoding="utf-8").read()
    secret2 = content.split("CHECKIN_SECRET_KEY=")[1].split("\n")[0]
    check("2.3 密钥保持不变（幂等）", secret1 == secret2, f"{secret1[:8]}… vs {secret2[:8]}…")

    # ---- 3. main status：未运行 → 3 ----
    r = run_script("main", ["status"])
    check("3.1 未运行时 status=3", r.returncode == 3, f"rc={r.returncode}")

    # ---- 4. main stop：无进程 → 0（no-op）----
    r = run_script("main", ["stop"])
    check("4.1 无进程 stop 成功", r.returncode == 0, f"rc={r.returncode}")

    # ---- 5. main start：伪造可执行"python"验证启动/状态/停止闭环 ----
    # Git Bash 下 .sh 文件可直接执行；给 fake python 打 echo 日志 + sleep
    fake_py = os.path.join(TARGET, "runtime", "bin", "python3")
    with open(fake_py, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\nwhile true; do sleep 5; done\n")
    os.chmod(fake_py, 0o755)

    r = run_script("main", ["start"])
    check("5.1 start 退出码 0", r.returncode == 0, r.stderr[:200])

    pid_file = os.path.join(VAR, "app.pid")
    pid = ""
    if os.path.isfile(pid_file):
        pid = open(pid_file).read().strip()
    check("5.2 PID 文件已写", bool(pid), pid)

    r = run_script("main", ["status"])
    check("5.3 运行中 status=0", r.returncode == 0, f"rc={r.returncode}")

    # 数据目录由 start 幂等重建
    check("5.4 日志文件在写", os.path.isfile(os.path.join(VAR, "logs", "app.log")))

    r = run_script("main", ["stop"])
    check("5.5 stop 后进程退出", r.returncode == 0, f"rc={r.returncode}")

    r = run_script("main", ["status"])
    check("5.6 停止后 status=3", r.returncode == 3, f"rc={r.returncode}")

    # ---- 6. status 自愈：进程活着但 PID 文件丢失 ----
    # ⚠️ 自愈走 /proc 扫描（Linux 专属）；Git Bash 无 /proc 无法复现，
    #    真机（fnOS/Debian）上该路径由 /proc/cmdline 保证。
    if os.path.isdir("/proc"):
        subprocess.run([GIT_BASH, "-c",
                        f'"{fake_py.replace(chr(92), "/")}" >/dev/null 2>&1 & echo $!'],
                       capture_output=True, text=True)
        ps = subprocess.run([GIT_BASH, "-c",
                             "ps -ef | grep 'sleep 5' | grep -v grep | head -n1 | awk '{print $2}'"],
                            capture_output=True, text=True)
        live_pid = ps.stdout.strip()
        if live_pid:
            r = run_script("main", ["status"])
            check("6.1 PID 文件丢失时 status 自愈=0", r.returncode == 0, f"rc={r.returncode}")
            subprocess.run([GIT_BASH, "-c", f"kill {live_pid} 2>/dev/null"])
        else:
            print("SKIP  6.1 （无法定位孤儿进程）")
    else:
        print("SKIP  6.1 （本环境无 /proc，自愈逻辑属 Linux 专属路径）")

    # ---- 7. upgrade_callback ----
    r = run_script("upgrade_callback")
    check("7.1 upgrade_callback 退出码 0", r.returncode == 0, r.stderr[:150])
    check("7.2 升级后运行时重解压",
          os.path.isfile(os.path.join(TARGET, "runtime", "bin", "python3")))
    content = open(env_file, encoding="utf-8").read()
    secret3 = content.split("CHECKIN_SECRET_KEY=")[1].split("\n")[0]
    check("7.3 升级后密钥不变", secret3 == secret1)

    # ---- 8. uninstall（保留数据）----
    r = run_script("uninstall_callback")
    check("8.1 默认卸载保留数据", os.path.isdir(VAR) and os.path.isfile(env_file))

    # ---- 9. uninstall（清除数据）----
    r = run_script("uninstall_callback")
    # purge_data=true 的场景单独跑
    env = {
        **os.environ,
        "TRIM_APPDEST": TARGET.replace("\\", "/"),
        "TRIM_PKGVAR": VAR.replace("\\", "/"),
        "TRIM_PKGETC": ETC.replace("\\", "/"),
        "purge_data": "true",
    }
    r = subprocess.run([GIT_BASH,
                        os.path.join(CMD_DIR, "uninstall_callback").replace("\\", "/")],
                       capture_output=True, text=True, env=env, timeout=30)
    check("9.1 purge_data=true 时删除数据",
          r.returncode == 0 and not os.path.exists(env_file), f"rc={r.returncode}")

    # 清理
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
    print("LIFECYCLE_OK" if not FAIL else "LIFECYCLE_FAILED")


if __name__ == "__main__":
    main()
