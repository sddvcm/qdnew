"""构建内置运行时 runtime.tar（供 fpk 的 install_callback 在 NAS 上解压）。

为什么这样做（重要，别"优化"掉）
--------------------------------
fpk 要求安装时零联网，所以 Python 解释器和全部依赖必须进包。难点是打包机是
Windows，而 Linux 的执行位（bin/python3 +x）和软链（python3 → python3.12）
在 Windows 上无法正确生成。

解法：**不重新打包目录树，而是"流式改造"原始 tar**：
    1. 打开 python-build-standalone 原始 tar.gz，逐成员读出 —— 每个成员的
       mode / type / linkname 元数据在 tar 头里，Windows 上照样能读
    2. 符号链接成员 → 原样写入新 tar（元数据复制，零内容）
    3. 普通文件 → 从原始 tar 流式拷贝（内容 + 原权限位）
    4. 目录 → 强制 755
    5. 顶层名 `python/` 改成 `runtime/`（NAS 上解压为 $TRIM_APPDEST/runtime）
    6. 把 wheels 解包出的 site-packages 作为**追加成员**写进去
       （wheel 是纯 zip，C 扩展 .so 已预编译，目标机无需编译）

NAS 上 `tar -xf runtime.tar` 一步还原出可直接运行的解释器。

输出：build/runtime.tar（不压缩 —— 它会被打进 app.tgz 由 fpk 的 gzip 统一压缩，
避免三层 gzip 浪费时间；install_callback 用 `tar -xf` 自动识别格式）。
"""
import os
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
ORIG_TGZ = os.path.join(BUILD, "python-runtime.tar.gz")
WHEEL_DIR = os.path.join(BUILD, "wheels")
OUT_TAR = os.path.join(BUILD, "runtime.tar")

# 原始 tar 的顶层目录名 → 目标名
SRC_PREFIX = "python/"
DST_PREFIX = "runtime/"
# wheel 内容在运行时树里的落点（posix 风格，用于 tar 内路径）
SP_REL = "lib/python3.12/site-packages"


def log(msg):
    print(f"[runtime] {msg}", flush=True)


def stage_wheels() -> str:
    """把全部 wheel 解包到干净的暂存目录，返回其路径"""
    import shutil

    sp = os.path.join(BUILD, "site-packages-staging")
    if os.path.isdir(sp):
        shutil.rmtree(sp, ignore_errors=True)   # 增量易留脏文件，直接重建
    os.makedirs(sp, exist_ok=True)

    whls = sorted(f for f in os.listdir(WHEEL_DIR) if f.endswith(".whl"))
    if not whls:
        raise SystemExit("build/wheels 下没有 wheel，请先跑 prepare_runtime.py")
    log(f"解包 {len(whls)} 个 wheel → 暂存目录")
    for w in whls:
        with zipfile.ZipFile(os.path.join(WHEEL_DIR, w)) as zf:
            zf.extractall(sp)
        log(f"  ✓ {w}")

    # 清理 wheel 的 .data 残留目录（import 不需要；scripts 类本包不依赖）
    for d in list(os.listdir(sp)):
        if d.endswith(".data"):
            shutil.rmtree(os.path.join(sp, d), ignore_errors=True)
    return sp


def main():
    if not os.path.exists(ORIG_TGZ):
        raise SystemExit(f"缺少 {ORIG_TGZ}，请先跑 prepare_runtime.py")
    sp = stage_wheels()

    log("流式改造原始运行时 tar …")
    n_file = n_link = n_added = 0

    with tarfile.open(ORIG_TGZ, "r:gz") as src, \
         tarfile.open(OUT_TAR, "w") as out:

        # ---- 第一部分：原始成员流式拷贝（改名 python/ → runtime/）----
        for member in src:
            if member.name.startswith(SRC_PREFIX):
                member.name = DST_PREFIX + member.name[len(SRC_PREFIX):]
            elif member.name.rstrip("/") == SRC_PREFIX.rstrip("/"):
                member.name = DST_PREFIX.rstrip("/")

            if member.issym() or member.islnk():
                out.addfile(member)                 # 元数据即全部（linkname 在头里）
                n_link += 1
            elif member.isdir():
                member.mode = 0o755
                out.addfile(member)
            elif member.isfile():
                out.addfile(member, src.extractfile(member))
                n_file += 1
            else:
                log(f"  ⚠️ 跳过特殊成员 {member.name} (type={member.type})")

        # ---- 第二部分：追加 wheel 解包出的 site-packages ----
        # tar 内路径：runtime/lib/python3.12/site-packages/<rel>
        for dirpath, dirnames, filenames in os.walk(sp):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, sp).replace("\\", "/")
            tar_dir = f"{DST_PREFIX}{SP_REL}" + ("" if rel_dir == "." else "/" + rel_dir)

            dir_info = tarfile.TarInfo(tar_dir)
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            out.addfile(dir_info)
            n_added += 1

            for fn in sorted(filenames):
                fp = os.path.join(dirpath, fn)
                info = tarfile.TarInfo(f"{tar_dir}/{fn}")
                info.size = os.path.getsize(fp)
                # .so 给执行位是惯例（dlopen 只需读，但保持与发行包一致更稳）
                info.mode = 0o755 if fn.endswith(".so") else 0o644
                with open(fp, "rb") as f:
                    out.addfile(info, f)
                n_added += 1

    size_mb = os.path.getsize(OUT_TAR) // 1024 // 1024
    log(f"完成：原始文件 {n_file}，软链 {n_link}，追加 {n_added}")
    log(f"输出 {OUT_TAR}  {size_mb}MB")
    print("RUNTIME_TAR_OK")


if __name__ == "__main__":
    main()
