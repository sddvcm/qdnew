"""构建两个微型"探针" fpk，用于装机失败时的二分定位。

背景：正式包安装报「应用包不符合系统要求」，无法远程看到 fnOS 校验器的具体
拒绝原因。探针 = 与正式包**同结构**（manifest 字段/格式、GNU tar、目录成员、
根级 ui/、.sc）但体积极小（几 KB、无运行时）的最小包，一次装机即可分层定位：

    probe-a（无 os_min_version）装得上 + probe-b（带 os_min_version=1.1.8）装不上
        → 实锤 os_min_version 是死因
    两个都装得上
        → 问题出在正式包的内容（体积/runtime.tar/某文件）
    两个都装不上
        → 系统层面（架构不对 / fnOS 版本过旧 / 校验器有别的硬性要求），
          与正式包内容无关

探针应用本身是个空壳（cmd/main 全部 exit 0，无端口无 UI 服务），
装上后无任何行为，卸载即可。

用法：
    python packaging/fnos/build_probe.py
输出：
    packaging/fnos/dist/fnprobe-a-1.0.0.fpk
    packaging/fnos/dist/fnprobe-b-1.0.0.fpk
"""
import hashlib
import io
import json
import os
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
APPNAME = "fnprobe"
VERSION = "1.0.0"
BUILD_TIME = int(time.time())
MODE_FILE = 0o644
MODE_EXEC = 0o755
MODE_DIR = 0o755

# 1x1 透明 PNG（占位图标，合法 PNG 头）
ICON_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

MAIN_SCRIPT = (
    b"#!/bin/bash\n"
    b"# probe stub: every lifecycle command succeeds\n"
    b'case "$1" in\n'
    b"    start|stop) exit 0 ;;\n"
    b"    status)     exit 0 ;;\n"
    b"    *)          exit 0 ;;\n"
    b"esac\n"
)

PRIVILEGE = json.dumps({
    "defaults": {"run-as": "package"},
    "username": APPNAME,
    "groupname": APPNAME,
}, indent=2).encode() + b"\n"

RESOURCE_EMPTY = b"{}\n"

UI_CONFIG = json.dumps({
    ".url": {
        f"{APPNAME}.main": {
            "title": "FPK Probe",
            "icon": "images/icon_{0}.png",
            "type": "url",
            "protocol": "http",
            "port": "1",
            "url": "/",
            "allUsers": True,
        }
    }
}, indent=2).encode() + b"\n"

WIZARD_UNINSTALL = json.dumps([{
    "stepTitle": "卸载探针",
    "items": [{"type": "tips", "helpText": "这是诊断用探针应用，直接卸载即可。"}],
}], indent=2).encode() + b"\n"


def add_bytes(tf, arcname, data, mode=MODE_FILE):
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = BUILD_TIME
    info.uid = info.gid = 0
    tf.addfile(info, io.BytesIO(data))


def add_dir(tf, arcname):
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.DIRTYPE
    info.mode = MODE_DIR
    info.mtime = BUILD_TIME
    info.uid = info.gid = 0
    tf.addfile(info)


def build_manifest(md5: str, with_os_min: bool) -> bytes:
    fields = [
        ("appname", APPNAME),
        ("version", VERSION),
        ("display_name", "FPK Probe"),
        ("platform", "x86"),
        ("maintainer", "sddvcm"),
        ("maintainer_url", "https://github.com/sddvcm/qdnew"),
        ("distributor", "sddvcm"),
        ("distributor_url", "https://github.com/sddvcm/qdnew"),
        ("desktop_uidir", "ui"),
        ("desktop_applaunchname", f"{APPNAME}.main"),
        ("desc", "fnOS fpk structure probe. Safe to uninstall."),
        ("source", "thirdparty"),
        ("fpk_version", VERSION),
        ("ctl_stop", "false"),
    ]
    if with_os_min:
        # 探针 B：唯一变量 —— 正式包 v1 曾用且被疑死的字段
        fields.insert(-1, ("os_min_version", "1.1.8"))
    fields.append(("checksum", md5))
    width = max(len(k) for k, _ in fields)
    return ("\n".join(f"{k.ljust(width)} = {v}" for k, v in fields) + "\n").encode()


def build_probe(with_os_min: bool) -> str:
    tag = "b" if with_os_min else "a"
    out_path = os.path.join(DIST, f"{APPNAME}-{tag}-{VERSION}.fpk")

    # ---- app.tgz（极小：README + ui/）----
    app_buf = io.BytesIO()
    with tarfile.open(fileobj=app_buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        add_bytes(tf, "./README.txt", b"fpk structure probe\n", MODE_FILE)
        add_dir(tf, "./ui/")
        add_bytes(tf, "./ui/config", UI_CONFIG, MODE_FILE)
        add_dir(tf, "./ui/images/")
        add_bytes(tf, "./ui/images/icon_64.png", ICON_1PX, MODE_FILE)
        add_bytes(tf, "./ui/images/icon_256.png", ICON_1PX, MODE_FILE)
    app_tgz = app_buf.getvalue()
    md5 = hashlib.md5(app_tgz).hexdigest()

    # ---- 组装 fpk（结构与正式包一致）----
    os.makedirs(DIST, exist_ok=True)
    out_buf = io.BytesIO()
    with tarfile.open(fileobj=out_buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        add_bytes(tf, "ICON.PNG", ICON_1PX, MODE_FILE)
        add_bytes(tf, "ICON_256.PNG", ICON_1PX, MODE_FILE)
        add_bytes(tf, "app.tgz", app_tgz, MODE_FILE)
        add_dir(tf, "cmd/")
        add_bytes(tf, "cmd/main", MAIN_SCRIPT, MODE_EXEC)
        add_dir(tf, "config/")
        add_bytes(tf, "config/privilege", PRIVILEGE, MODE_FILE)
        add_bytes(tf, "config/resource", RESOURCE_EMPTY, MODE_FILE)
        add_bytes(tf, "manifest", build_manifest(md5, with_os_min), MODE_FILE)
        add_dir(tf, "ui/")
        add_bytes(tf, "ui/config", UI_CONFIG, MODE_FILE)
        add_dir(tf, "ui/images/")
        add_bytes(tf, "ui/images/icon_64.png", ICON_1PX, MODE_FILE)
        add_bytes(tf, "ui/images/icon_256.png", ICON_1PX, MODE_FILE)
        add_dir(tf, "wizard/")
        add_bytes(tf, "wizard/uninstall", WIZARD_UNINSTALL, MODE_FILE)

    with open(out_path, "wb") as f:
        f.write(out_buf.getvalue())
    return out_path


def main():
    a = build_probe(with_os_min=False)
    b = build_probe(with_os_min=True)
    for p in (a, b):
        print(f"OK {os.path.getsize(p):>7}B  {p}")


if __name__ == "__main__":
    main()
