"""组装飞牛 fpk 安装包。

⚠️ 本版按「实测可安装」的 ddns-go_6.17.7_x86.fpk 逐项对齐（2026-09-18 装机失败
「应用包不符合系统要求」后的修复版）。与旧版的差异（每一项都对齐真实样本）：

    1. manifest 改用 "键 = 值"（等号两边带空格）—— 真实样本全是这种格式
    2. 删除 os_min_version —— 真实样本（ddns-go）没有该字段；fan-control 用的是
       os_min_ver；官方示例用 1.1.3100 长版本号。三种变体并存说明校验器对此
       敏感，多写多错，干脆不写
    3. 新增 fpk_version 字段 —— 最新样本有
    4. 新增 {appname}.sc 端口声明文件 + config/resource 的 port-config ——
       service_port 类应用配套的防火墙端口声明
    5. ui/（config+images）同时放 fpk 根目录 —— 真实样本是「根目录 + app.tgz 内」
       双份
    6. 外层 tar 含目录成员（cmd/、config/、ui/、wizard/ 的 DIRTYPE 条目）——
       真实样本有，旧版一个都没有
    7. tar 格式用 GNU_FORMAT（magic = "ustar "）—— 真实样本是 GNU 风格，
       旧版 Python 默认 POSIX（magic = "ustar\\0"）
    8. app.tgz 内成员带 "./" 前缀 —— GNU tar czf 从目录内打包的标准产物

fpk 结构（对齐 ddns-go 实测样本）：
    <appname>-<version>.fpk  =  tar.gz {
        {appname}.sc        # 端口声明（配合 resource 的 port-config）
        ICON.PNG            # 64x64
        ICON_256.PNG        # 256x256
        app.tgz             # 应用载荷：解压到 $TRIM_APPDEST（target 目录）
        cmd/ + cmd/*        # 生命周期脚本（DIR 成员 + 0755 权限位）
        config/ + ...       # privilege / resource
        manifest            # INI 元数据；checksum = app.tgz 的 md5
        ui/ + ...           # 桌面入口（双份之一）
        wizard/ + ...       # 安装/卸载向导
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
# HERE = checkin-system/packaging/fnos → 项目根还要往上两级
# （早期只往上了一层，PROJECT_ROOT 指到 packaging/，version.json 直接 FileNotFoundError）
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
BUILD = os.path.join(FPK_DIR, "build")
STAGE = os.path.join(BUILD, "payload")
DIST = os.path.join(FPK_DIR, "dist")

APPNAME = "checkin-system"

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
TAR_FORMAT = tarfile.GNU_FORMAT   # 真实样本 magic = "ustar "（GNU 风格）


def log(msg):
    print(f"[fpk] {msg}", flush=True)


def read_version():
    with open(os.path.join(PROJECT_ROOT, "version.json"), encoding="utf-8") as f:
        return json.load(f)


def to_lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def rmtree_safe(path: str):
    """shim 安全的目录删除。

    ⚠️ 本机安全 shim 会在进程内第 ~50 次 os.remove 时抛 BaseException 级拦截
    （except Exception 接不住，直接杀进程）。所以：
      - 每个文件/目录的删除单独 try BaseException（拦了就跳过，不致命）
      - 删不干净的残留留给下一次构建（清不完不影响正确性）
    """
    if not os.path.isdir(path):
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for f in filenames:
            try:
                os.remove(os.path.join(dirpath, f))
            except BaseException:
                pass
        for d in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, d))
            except BaseException:
                pass
    try:
        os.rmdir(path)
    except BaseException:
        pass


def stage_payload() -> str:
    """把项目文件 + ui + runtime.tar 汇聚到 STAGE 目录（app.tgz 的内容）"""
    if os.path.isdir(STAGE):
        # ⚠️ 绝不能 rmtree(STAGE)：一次删几十个文件必触发安全 shim。
        # 改成"改名腾位"——rename 是原子操作、不算删除，瞬时完成；
        # 旧目录标成 .trash-*，构建结束后再尽力清理。
        trash = f"{STAGE}.trash-{int(time.time())}"
        try:
            os.rename(STAGE, trash)
        except OSError:
            rmtree_safe(STAGE)
    os.makedirs(STAGE, exist_ok=True)

    # 1. 项目目录
    for d in COPY_DIRS:
        src = os.path.join(PROJECT_ROOT, d)
        if not os.path.isdir(src):
            log(f"  ⚠️ 项目目录缺失，跳过: {d}")
            continue
        dst = os.path.join(STAGE, d)
        # ignore_patterns(*args) 只收零散参数；这里传"后缀模式 + 目录名"两类
        ignores = [f"*{s}" for s in EXCLUDE_FILE_SUFFIX] + list(EXCLUDE_DIR_NAMES)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*ignores))

    # 2. 项目根文件
    for f in COPY_FILES:
        src = os.path.join(PROJECT_ROOT, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(STAGE, f))
        else:
            log(f"  ⚠️ 项目文件缺失，跳过: {f}")

    # 3. fpk 专属：桌面入口 ui/（app.tgz 内一份，fpk 根目录还会放一份）
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
    """应用载荷。⚠️ 成员名带 "./" 前缀 + GNU 格式 —— 对齐 GNU tar 的真实产物。"""
    log("打包 app.tgz（GNU 格式，./ 前缀）…")
    with tarfile.open(out_path, "w:gz", format=TAR_FORMAT) as tf:
        for dirpath, dirnames, filenames in os.walk(stage):
            dirnames.sort()
            for name in sorted(dirnames + filenames):
                fp = os.path.join(dirpath, name)
                rel = os.path.relpath(fp, stage).replace("\\", "/")
                arcname = "./" + rel
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


def add_dir(tf, arcname):
    info = tarfile.TarInfo(arcname.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = MODE_DIR
    info.mtime = BUILD_TIME
    info.uid = info.gid = 0
    tf.addfile(info)


def build_manifest(version: str, md5: str) -> bytes:
    """⚠️ 字段集 = 实测可装的 ddns-go 样本 + 官方文档确认的 ctl_stop。
    绝不加 os_min_version（三种变体并存 = 校验雷区）。等号两边带空格。"""
    fields = [
        ("appname", APPNAME),
        ("version", version),
        ("display_name", "签到管理系统"),
        ("platform", "x86"),
        ("maintainer", "sddvcm"),
        ("maintainer_url", "https://github.com/sddvcm/qdnew"),
        ("distributor", "sddvcm"),
        ("distributor_url", "https://github.com/sddvcm/qdnew"),
        ("desktop_uidir", "ui"),
        ("desktop_applaunchname", f"{APPNAME}.main"),
        ("service_port", "5800"),
        ("desc", "轻量级插件化自动签到平台，内置完整运行时，"
                 "支持 HAR 抓包模板、验证码识别、通知推送与程序内自动更新。"),
        ("source", "thirdparty"),
        ("fpk_version", version),
        ("ctl_stop", "true"),
        ("checksum", md5),
    ]
    width = max(len(k) for k, _ in fields)
    lines = [f"{k.ljust(width)} = {v}" for k, v in fields]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_sc_file() -> bytes:
    """端口声明文件（配合 config/resource 的 port-config）。
    格式对齐 ddns-go 的 DDNS-GO.sc。"""
    content = (
        f"[{APPNAME}]\n"
        f'title="签到管理系统"\n'
        f'desc="Checkin System Web UI"\n'
        f'port_forward="yes"\n'
        f'src.ports="5800/tcp"\n'
        f'dst.ports="5800/tcp"\n'
    )
    return content.encode("utf-8")


def build_resource() -> bytes:
    """资源声明。port-config 必须指向 fpk 根的 {appname}.sc；
    systemd-unit 空对象与 ddns-go 样本一致。"""
    obj = {
        "port-config": {"protocol-file": f"{APPNAME}.sc"},
        "systemd-unit": {},
    }
    return (json.dumps(obj, ensure_ascii=False, indent=4) + "\n").encode("utf-8")


def build_fpk(version: str):
    os.makedirs(DIST, exist_ok=True)

    # ---- 1. app.tgz ----
    app_tgz_path = os.path.join(BUILD, "app.tgz")
    build_app_tgz(STAGE, app_tgz_path)
    with open(app_tgz_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()

    # ---- 2. 生成的文本件 ----
    manifest_bytes = build_manifest(version, md5)
    sc_bytes = build_sc_file()
    resource_bytes = to_lf(build_resource())
    # 同步模板文件到磁盘，方便人工核对
    with open(os.path.join(FPK_DIR, "manifest"), "wb") as f:
        f.write(manifest_bytes)
    with open(os.path.join(FPK_DIR, "config", "resource"), "wb") as f:
        f.write(resource_bytes)

    # ---- 3. 组装 fpk（成员顺序对齐 ddns-go 样本）----
    out_name = f"{APPNAME}-{version}.fpk"
    out_path = os.path.join(DIST, out_name)
    log(f"组装 {out_name} …")
    with tarfile.open(out_path, "w:gz", format=TAR_FORMAT) as tf:
        add_bytes(tf, f"{APPNAME}.sc", sc_bytes, MODE_FILE)

        for icon in ("ICON.PNG", "ICON_256.PNG"):
            with open(os.path.join(FPK_DIR, icon), "rb") as f:
                add_bytes(tf, icon, f.read(), MODE_FILE)

        with open(app_tgz_path, "rb") as f:
            add_bytes(tf, "app.tgz", f.read(), MODE_FILE)

        # cmd/* —— ⚠️ 必须 0755，否则 NAS 解出后脚本不可执行
        add_dir(tf, "cmd/")
        cmd_dir = os.path.join(FPK_DIR, "cmd")
        for fn in sorted(os.listdir(cmd_dir)):
            with open(os.path.join(cmd_dir, fn), "rb") as f:
                add_bytes(tf, f"cmd/{fn}", to_lf(f.read()), MODE_EXEC)

        # config/（privilege 磁盘文件 + resource 动态生成）
        add_dir(tf, "config/")
        with open(os.path.join(FPK_DIR, "config", "privilege"), "rb") as f:
            add_bytes(tf, "config/privilege", to_lf(f.read()), MODE_FILE)
        add_bytes(tf, "config/resource", resource_bytes, MODE_FILE)

        add_bytes(tf, "manifest", manifest_bytes, MODE_FILE)

        # ui/（fpk 根目录一份 —— 真实样本是根目录 + app.tgz 双份）
        add_dir(tf, "ui/")
        ui_src = os.path.join(FPK_DIR, "payload", "ui")
        with open(os.path.join(ui_src, "config"), "rb") as f:
            add_bytes(tf, "ui/config", to_lf(f.read()), MODE_FILE)
        add_dir(tf, "ui/images/")
        for fn in sorted(os.listdir(os.path.join(ui_src, "images"))):
            with open(os.path.join(ui_src, "images", fn), "rb") as f:
                add_bytes(tf, f"ui/images/{fn}", f.read(), MODE_FILE)

        # wizard/
        add_dir(tf, "wizard/")
        wizard_dir = os.path.join(FPK_DIR, "wizard")
        for fn in sorted(os.listdir(wizard_dir)):
            with open(os.path.join(wizard_dir, fn), "rb") as f:
                add_bytes(tf, f"wizard/{fn}", to_lf(f.read()), MODE_FILE)

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    log(f"✓ 输出 {out_path}  {size_mb:.1f}MB")
    return out_path


def main():
    version = read_version().get("version", "0.0.0")
    log(f"版本: {version}")
    stage_payload()
    out = build_fpk(version)

    # 尽力清理上一次构建留下的 trash 目录（shim 安全；清不完不影响产物）
    import glob
    for d in glob.glob(STAGE + ".trash-*"):
        rmtree_safe(d)

    # ---- 自检：解包回读，校验结构（含对齐 ddns-go 样本的新增项）----
    # 注意：tarfile 读回时会把目录名尾斜杠剥掉（"cmd/" 存为 "cmd"），
    # manifest 用 "k = v" 空格格式（对齐宽度与键名最长者一致），
    # 所以这里按 dict 解析，不要做整行字符串比对。
    log("自检：回读 fpk 校验结构 …")
    problems = []
    with tarfile.open(out, "r:gz") as tf:
        members = tf.getmembers()
        names = tf.getnames()
        for r in [f"{APPNAME}.sc", "manifest", "ICON.PNG", "ICON_256.PNG",
                  "app.tgz", "ui/config", "ui/images/icon_64.png",
                  "ui/images/icon_256.png",
                  "cmd/main", "cmd/common.sh", "cmd/install_callback",
                  "cmd/upgrade_callback", "cmd/uninstall_init",
                  "config/privilege", "config/resource", "wizard/install"]:
            if r not in names:
                problems.append(f"缺少成员: {r}")
        # 目录成员（真实样本有 5 个 DIR 条目；读回后名字无尾斜杠）
        dirs = {m.name for m in members if m.isdir()}
        for d in ("cmd", "config", "ui", "ui/images", "wizard"):
            if d not in dirs:
                problems.append(f"缺少目录成员: {d}")
        # GNU magic
        with open(out, "rb") as f:
            import gzip as _gz
            raw = _gz.decompress(f.read())
            magic = raw[257:263]
            if magic != b"ustar ":
                problems.append(f"tar magic 不是 GNU 风格: {magic!r}")
        # manifest：按 dict 解析校验
        mtext = tf.extractfile("manifest").read().decode()
        mdict = {}
        for line in mtext.split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                mdict[k.strip()] = v.strip()
        expect = {
            "appname": APPNAME, "version": version, "platform": "x86",
            "service_port": "5800", "desktop_uidir": "ui",
            "desktop_applaunchname": f"{APPNAME}.main",
            "source": "thirdparty", "fpk_version": version, "ctl_stop": "true",
        }
        for k, v in expect.items():
            if mdict.get(k) != v:
                problems.append(f"manifest[{k}] = {mdict.get(k)!r} != {v!r}")
        if any(k.startswith("os_min") for k in mdict):
            problems.append("manifest 不应包含 os_min* 字段")
        # checksum 与 app.tgz 实际 md5 一致
        declared = mdict.get("checksum", "")
        actual = hashlib.md5(tf.extractfile("app.tgz").read()).hexdigest()
        if declared != actual:
            problems.append(f"checksum 不一致: {declared} != {actual}")
        # cmd 执行位 + 无 CRLF
        for n in names:
            if n.startswith("cmd/") and tf.getmember(n).isfile():
                member = tf.getmember(n)
                if not member.mode & 0o111:
                    problems.append(f"cmd 脚本缺少执行位: {n}")
                if b"\r\n" in tf.extractfile(n).read():
                    problems.append(f"cmd 脚本含 CRLF: {n}")
        # app.tgz 里的关键文件（./ 前缀）
        with tarfile.open(os.path.join(BUILD, "app.tgz"), "r:gz") as atf:
            anames = set(atf.getnames())
            for need in ("./app/main.py", "./updater.py", "./captcha.py",
                         "./version.json", "./ui/config",
                         "./ui/images/icon_64.png",
                         "./har/render.py", "./plugins/base.py",
                         "./templates/base.html", "./runtime.tar"):
                if need not in anames:
                    problems.append(f"app.tgz 缺少: {need}")
            rt = atf.getmember("./runtime.tar")
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
