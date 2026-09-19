# -*- coding: utf-8 -*-
"""下载 jfbym 官方示例验证码 → base64 → 程序化注入 extras_api.py。

⚠️ 上次踩的坑：手写 base64 字符串当测试图，PNG 魔数对但 chunk 结构坏，
导致「测试本地识别」永远失败。所以这次**绝不手抄**——下载真实图片后
由本脚本直接生成源码片段并用 PIL 逐张验证（魔数/解码/chunk CRC）。
"""
import base64
import io
import os
import re
import struct
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # 项目根
TARGET = os.path.join(ROOT, "app", "routes", "extras_api.py")
UA = {"User-Agent": "Mozilla/5.0",
      "Referer": "https://www.jfbym.com/test/100.html"}

SAMPLES = [
    # (文件名, 内容描述——用于页面展示)
    ("1.png", "数英4位·白底（PNG）"),
    ("2.png", "数英4位·红字（PNG）"),
    ("3.jpg", "数英4位·红字（JPEG）"),
    ("4.jpg", "数英4位·噪点背景（JPEG）"),
]
BASE = "https://www.jfbym.com/static/pic/sy/c1-4/"


def fetch(name):
    req = urllib.request.Request(BASE + name, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read()


def verify_png_crc(data):
    """逐 chunk 校验 PNG CRC（假图最容易坏在这）"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return "非 PNG"
    pos, n = 8, 0
    while pos < len(data):
        ln, = struct.unpack_from(">I", data, pos)
        typ = data[pos + 4:pos + 8]
        crc, = struct.unpack_from(">I", data, pos + 8 + ln)
        if zlib.crc32(typ + data[pos + 8:pos + 8 + ln]) & 0xFFFFFFFF != crc:
            return "chunk %s CRC 错" % typ.decode("latin-1")
        pos += 12 + ln
        n += 1
        if typ == b"IEND":
            return None
    return "缺 IEND"


from PIL import Image  # noqa: E402

blocks = []
for name, desc in SAMPLES:
    raw = fetch(name)
    # ---- 三重验证 ----
    im = Image.open(io.BytesIO(raw))
    im.load()                                    # 1) 完整解码
    err = verify_png_crc(raw) if name.endswith(".png") else None
    if err:                                      # 2) PNG chunk CRC
        raise SystemExit("%s CRC 校验失败: %s" % (name, err))
    b64 = base64.b64encode(raw).decode("ascii")
    # 3) base64 往返后再解码（验证转码没坏）
    again = Image.open(io.BytesIO(base64.b64decode(b64)))
    again.load()
    # 按行切好，生成源码
    lines = "\n".join('    "%s"' % s for s in
                      [b64[i:i + 72] for i in range(0, len(b64), 72)])
    blocks.append(
        "    # %s —— %d×%d %s，%d bytes（来源：云码官网示例图）\n"
        '    {"name": "%s", "desc": "%s", "b64": (\n%s\n    )},\n'
        % (name, im.size[0], im.size[1], im.format, len(raw),
           name, desc, lines))
    print("✓ %s  %s %s  %d bytes  base64 %d chars"
          % (name, im.format, im.size, len(raw), len(b64)))

new_block = (
    "# 内置测试图：云码官网（www.jfbym.com）公开的示例验证码，与真实登录验证码\n"
    "# 同风格（fuliba 用的 10110=通用数英1~4位 就是这类）。含 PNG 与 JPEG 两种\n"
    "# 格式、含噪点背景，比自绘图更能代表真实识别场景。\n"
    "#\n"
    "# ⚠️ 这些 base64 由 packaging/fnos/fetch_test_captchas.py **程序化生成**，\n"
    "# 且经过三重验证（PIL 完整解码 / PNG chunk CRC / base64 往返）。\n"
    "# **绝不要手写或手工修改 base64** —— 上版本手写的假 PNG（魔数对、结构坏）\n"
    "# 曾导致「测试本地识别」必报 cannot identify image file。\n"
    "_TEST_IMAGES = [\n"
    + "".join(blocks)
    + "]\n"
)

src = open(TARGET, encoding="utf-8").read()
# 替换旧的单图常量（含注释块）为新的多图列表
pat = re.compile(
    r"# 一张 100x32 的白底黑块图.*?_TEST_IMG_B64 = \(\n(?:.*?\n)*?\)",
    re.S)
if pat.search(src):
    src = pat.sub(new_block.rstrip(), src, count=1)
    print("已替换旧 _TEST_IMG_B64 块")
else:
    raise SystemExit("没找到旧的 _TEST_IMG_B64 块，中止（不写入）")

open(TARGET, "w", encoding="utf-8", newline="\n").write(src)
print("写入", TARGET)

# 最终验证：import 目标模块并解码每张图
import importlib.util, sys
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("extras_api_check", TARGET)
mod = importlib.util.module_from_spec(spec)
# 避免触发 flask 依赖：只读源码里的常量
import ast
tree = ast.parse(open(TARGET, encoding="utf-8").read())
imgs = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and \
            getattr(node.targets[0], "id", "") == "_TEST_IMAGES":
        imgs = ast.literal_eval(node.value)
        break
assert imgs and len(imgs) == 4, "解析 _TEST_IMAGES 失败"
for it in imgs:
    raw = base64.b64decode(it["b64"])
    im = Image.open(io.BytesIO(raw))
    im.load()
    print("final ✓ %-6s %s %s  %s" % (it["name"], im.format, im.size, it["desc"]))
print("ALL_OK")
