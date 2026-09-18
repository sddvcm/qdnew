"""组装飞牛 fpk 安装包。

fpk 格式（经 fan-control 实测样例 + 官方文档双重确认）：
    <appname>-<version>.fpk  =  tar.gz {
        manifest            # INI 元数据；checksum = app.tgz 的 md5
        ICON.PNG            # 64x64
        ICON_256.PNG        # 256x256
        app.tgz             # 应用载荷：解压到 $TRIM_APPDEST（target 目录）
        cmd/*               # 生命周期脚本（需要 +x 权限位！）
        config/privilege    # 运行身份（JSON）
        config/resource     # 资源声明（JSON）
        wizard/install 等   # 安装/卸载向导（JSON）
    }

⚠️ Windows 打包的两个坑（本脚本已处理，改动前先懂）：
    1. 权限位：Windows 文件系统没有 +x。tar 成员的模式由**打包时写入的
       TarInfo.mode 决定**（不是从磁盘读的），所以 cmd/* 必须显式写 0755，
       否则 NAS 解出的脚本不可执行，应用永远起不来。
    2. 换行符：bash 脚本若带 CRLF，Linux 上会报 `/bin/bash^M: bad interpreter`。
       本脚本对 cmd/、wizard/、ui/config、manifest 强制 LF 归一化。
"""
import hashlib
import io
import json
import os
import shutil
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FPK_DIR = HERE
PROJECT_ROOT = os.path.dirname(HERE)
BUILD = os.path.join(FPK_DIR, "build")
STAGE = os.path.join(BUILD, "payload")
DIST = os.path.join(FPK_DIR, "dist")

# 进载荷的项目内容（相对项目根）
COPY_DIRS = ["app", "har", "plugins", "templates", "docs"]
COPY_FILES = [
    "captcha.py", "updater.py", "version.json", "update_manifest.json",
    "requirements.txt", "README.md", "DEVELOPMENT.md",
]
EXCLUDE_DIR_NAMES = {"__pycache__", ".git", "build", "dist"}
EXCLUDE_FILE_SUFFIX = (".pyc", ".pyo", ".log")

MODE_DIR = 0o755
MODE_FILE = 0o644
MODE_EXEC = 0o755
BUILD_TIME = int(time.time())


def log(msg):
    print(f"[fpk] {msg}", flush=True)


def read_version():
    with open(os.path.join(PROJECT_ROOT, "version.json"), encoding="utf-8") as f:
        return json.load(f)


def to_lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def stage_payload() -> str:
    """把项目文件 + ui + runtime.tar 汇聚到 STAGE 目录（app.tgz 的内容）"""
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE, ignore_errors=True)
    os.makedirs(STAGE)

    # 1. 项目目录
    for d in COPY_DIRS:
        src = os.path.join(PROJECT_ROOT, d)
        if not os.path.isdir(src):
            log(f"  ⚠️ 项目目录缺失，跳过: {d}")
            continue
        dst = os.path.join(STAGE, d)
        shutil.copytree(
            src, dst,
            ignore=shutil.ignore_patterns(
                *(f"*{s}" for s in EXCLUDE_FILE_SUFFIX) | tuple(EXCLUDE_DIR_NAMES)))

    # 2. 项目根文件
    for f in COPY_FILES:
        src = os.path.join(PROJECT_ROOT, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(STAGE, f))
        else:
            log(f"  ⚠️ 项目文件缺失，跳过: {f}")

    # 3. fpk 专属：桌面入口 ui/
    ui_src = os.path.join(FPK_DIR, "payload", "ui")
    shutil.copytree(ui_src, os.path.join(STAGE, "ui"))

    # 4. 内置运行时（未压缩 tar，install_callback 在 NAS 上 tar -xf 解压）
    rt = os.path.join(BUILD, "runtime.tar")
    if not os.path.isfile(rt):
        raise SystemExit("缺少 build/runtime.tar —— 请先跑 make_runtime_tgz.py")
    log("复制 runtime.tar（这一步会拷贝数百 MB，稍等）…")
    shutil.copy2(rt, os.path.join(STAGE, "runtime.tar"))

    # 统计
    total = 0
    for dp, _, fs in os.walk(STAGE):
        for f in fs:
            total += os.path.getsize(os.path.join(dp, f))
    log(f"载荷暂存完成：{total // 1024 // 1024}MB")
    return STAGE


def tar_filter_transform(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """统一的成员规范化：属主 root、合理时间、稳定权限"""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = BUILD_TIME
    if info.isdir():
        info.mode = MODE_DIR
    elif info.isfile() and info.mode & 0o111:
        info.mode = MODE_EXEC
    elif info.isfile():
        info.mode = MODE_FILE
    return info


def build_app_tgz(stage: str, out_path: str):
    log("打包 app.tgz …")
    with tarfile.open(out_path, "w:gz") as tf:
        # 先放 runtime.tar 之外的小文件？无所谓顺序，直接 walk
        for dirpath, dirnames, filenames in os.walk(stage):
            dirnames.sort()
            for name in sorted(dirnames + filenames):
                fp = os.path.join(dirpath, name)
                arcname = os.path.relpath(fp, stage).replace("\\", "/")
                if os.path.isdir(fp):
                    info = tarfile.TarInfo(arcname)
                    info.type = tarfile.DIRTYPE
                    info.mode = MODE_DIR
                    info.mtime = BUILD_TIME
                    info.uid = info.gid = 0
                    tf.addfile(info)
                else:
                    info = tf.gettarinfo(fp, arcname=arcname)
                    info = tar_filter_transform(info)
                    with open(fp, "rb") as f:
                        tf.addfile(info, f)
    size = os.path.getsize(out_path) // 1024 // 1024
    log(f"app.tgz 完成：{size}MB")


def add_bytes(tf, arcname, data: bytes, mode=MODE_FILE):
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = BUILD_TIME
    info.uid = info.gid = 0
    tf.addfile(info, io.BytesIO(data))


def build_fpk(version: str):
    os.makedirs(DIST, exist_ok=True)

    # ---- 1. app.tgz ----
    app_tgz_path = os.path.join(BUILD, "app.tgz")
    build_app_tgz(STAGE, app_tgz_path)
    with open(app_tgz_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()

    # ---- 2. manifest（填版本 + checksum）----
    with open(os.path.join(FPK_DIR, "manifest"), encoding="utf-8") as f:
        manifest_text = f.read()
    manifest_text = to_lf(manifest_text.encode()).decode()
    lines = []
    for line in manifest_text.split("\n"):
        if line.startswith("version="):
            line = f"version={version}"
        elif line.startswith("checksum="):
            line = f"checksum={md5}"
        lines.append(line)
    manifest_bytes = "\n".join(lines).encode()

    # ---- 3. 组装 fpk ----
    out_name = f"checkin-system-{version}.fpk"
    out_path = os.path.join(DIST, out_name)
    log(f"组装 {out_name} …")
    with tarfile.open(out_path, "w:gz") as tf:
        add_bytes(tf, "manifest", manifest_bytes, MODE_FILE)

        for icon in ("ICON.PNG", "ICON_256.PNG"):
            with open(os.path.join(FPK_DIR, icon), "rb") as f:
                add_bytes(tf, icon, f.read(), MODE_FILE)

        # app.tgz 以文件形式放入（不再二次压缩内容本身 —— 外层 w:gz 只压一遍 tar 头
        # 与已压缩的 app.tgz，性价比低但格式要求如此）
        with open(app_tgz_path, "rb") as f:
            add_bytes(tf, "app.tgz", f.read(), MODE_FILE)

        # cmd/* —— ⚠️ 必须 0755，否则 NAS 解出后脚本不可执行
        cmd_dir = os.path.join(FPK_DIR, "cmd")
        for fn in sorted(os.listdir(cmd_dir)):
            with open(os.path.join(cmd_dir, fn), "rb") as f:
                add_bytes(tf, f"cmd/{fn}", to_lf(f.read()), MODE_EXEC)

        # config/ 与 wizard/（JSON，归一化 LF）
        for sub in ("config", "wizard"):
            sub_dir = os.path.join(FPK_DIR, sub)
            for fn in sorted(os.listdir(sub_dir)):
                with open(os.path.join(sub_dir, fn), "rb") as f:
                    add_bytes(tf, f"{sub}/{fn}", to_lf(f.read()), MODE_FILE)

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    log(f"✓ 输出 {out_path}  {size_mb:.1f}MB")
    return out_path


def main():
    version = read_version().get("version", "0.0.0")
    log(f"版本: {version}")
    stage_payload()
    out = build_fpk(version)

    # ---- 自检：解包回读，校验结构 ----
    log("自检：回读 fpk 校验结构 …")
    problems = []
    with tarfile.open(out, "r:gz") as tf:
        names = tf.getnames()
        required = ["manifest", "ICON.PNG", "ICON_256.PNG", "app.tgz",
                    "cmd/main", "cmd/common.sh", "cmd/install_callback",
                    "cmd/upgrade_callback", "cmd/uninstall_init",
                    "config/privilege", "config/resource", "wizard/install"]
        for r in required:
            if r not in names:
                problems.append(f"缺少成员: {r}")
        # manifest 字段
        m = tf.extractfile("manifest").read().decode()
        for field in ("appname=checkin-system", f"version={version}", "checksum=",
                      "service_port=5800", "desktop_uidir=ui"):
            if field not in m:
                problems.append(f"manifest 缺字段: {field}")
        # cmd 执行位
        for n in names:
            if n.startswith("cmd/") and not n.endswith("/"):
                member = tf.getmember(n)
                if not member.mode & 0o111:
                    problems.append(f"cmd 脚本缺少执行位: {n} mode={oct(member.mode)}")
        # app.tgz 里的关键文件
        with tarfile.open(os.path.join(BUILD, "app.tgz"), "r:gz") as atf:
            anames = set(atf.getnames())
            for need in ("app/main.py", "updater.py", "captcha.py", "version.json",
                         "ui/config", "ui/images/icon_64.png",
                         "har/render.py", "plugins/base.py",
                         "templates/base.html", "runtime.tar"):
                if need not in anames:
                    problems.append(f"app.tgz 缺少: {need}")
            # runtime.tar 大小合理性（> 100MB 才算带上了运行时）
            rt = atf.getmember("runtime.tar")
            if rt.size < 100 * 1024 * 1024:
                problems.append(f"runtime.tar 疑似不完整: {rt.size} bytes")

    if problems:
        for p in problems:
            log(f"  ✗ {p}")
        raise SystemExit("FPK_VERIFY_FAILED")
    log("✓ 结构自检通过")
    print("FPK_BUILD_OK")


if __name__ == "__main__":
    main()
