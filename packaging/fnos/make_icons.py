"""生成 fpk 所需的图标（ICON.PNG 64x64 / ICON_256.PNG 256x256）。

官方设计要求（developer.fnnas.com/docs/core-concepts/icon/）：
    - 格式 PNG，sRGB，≤1024KB
    - 完整正方形画布
    - **主体必须是圆角矩形风格**，不要直角满铺（否则与系统图标不协调）
    - 64px 下仍要清晰可辨，重要内容不要贴边

设计思路：「签到」= 打勾 + 日历格子。
    - 底色：蓝紫渐变圆角方（与系统蓝色系一致，且在深浅背景下都醒目）
    - 主体：白色对勾，粗线条，缩到 64px 依然认得出
    - 点缀：右上角一个小圆点（表示「今日已签」的角标）
不用文字 —— 文字在 64px 下必糊，这是官方明确提醒的。
"""
import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# 品牌色（蓝紫渐变，两端色）
C_TOP = (79, 110, 247)      # #4F6EF7
C_BOTTOM = (99, 91, 255)    # #635BFF
C_CHECK = (255, 255, 255)
C_DOT = (74, 222, 128)      # 绿色角标（与"签到成功"语义一致）


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def make_icon(size: int) -> Image.Image:
    """按目标尺寸绘制，所有几何量都按比例算 —— 保证 64 与 256 视觉一致"""
    SS = 4                                   # 超采样倍数，边缘更干净
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ---- 1. 圆角方形底 + 垂直渐变 ----
    radius = int(S * 0.22)                   # 圆角比例：约 22%，接近系统图标
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        gd.line([(0, y), (S, y)], fill=_lerp(C_TOP, C_BOTTOM, y / max(1, S - 1)) + (255,))

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)

    # ---- 2. 白色对勾（签到符号）----
    # 三个关键点，按画布比例定位，避免贴边
    lw = int(S * 0.105)                      # 线宽
    p1 = (S * 0.265, S * 0.525)              # 起笔
    p2 = (S * 0.435, S * 0.685)              # 转折（低谷）
    p3 = (S * 0.745, S * 0.330)              # 收笔（右上）

    for a, b in ((p1, p2), (p2, p3)):
        d.line([a, b], fill=C_CHECK, width=lw)
        # 圆头：在端点补圆，避免出现方头
        for pt in (a, b):
            r = lw / 2
            d.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r], fill=C_CHECK)

    # ---- 3. 右上角状态圆点 ----
    # 64px 下这个点直径约 7px，仍可见；位置贴在圆角内侧，不越界
    dot_r = S * 0.105
    dot_c = (S * 0.775, S * 0.240)
    # 先描一圈底色，让点与对勾分离（避免视觉粘连）
    halo = dot_r + S * 0.028
    d.ellipse([dot_c[0] - halo, dot_c[1] - halo, dot_c[0] + halo, dot_c[1] + halo],
              fill=(255, 255, 255))
    d.ellipse([dot_c[0] - dot_r, dot_c[1] - dot_r, dot_c[0] + dot_r, dot_c[1] + dot_r],
              fill=C_DOT)

    return img.resize((size, size), Image.LANCZOS)


def main():
    for size, name in ((64, "ICON.PNG"), (256, "ICON_256.PNG")):
        img = make_icon(size)
        path = os.path.join(OUT_DIR, name)
        img.save(path, "PNG", optimize=True)
        kb = os.path.getsize(path) / 1024
        flag = "✓" if kb <= 1024 else "✗ 超过 1024KB"
        print(f"{name:16s} {size}x{size}  {kb:7.1f}KB  {flag}")

    # 入口图标（ui/images/icon_{0}.png）
    ui_dir = os.path.join(OUT_DIR, "app", "ui", "images")
    os.makedirs(ui_dir, exist_ok=True)
    for size in (64, 256):
        img = make_icon(size)
        p = os.path.join(ui_dir, f"icon_{size}.png")
        img.save(p, "PNG", optimize=True)
        print(f"ui/images/icon_{size}.png  {os.path.getsize(p)/1024:7.1f}KB")
    print("ICONS_OK")


if __name__ == "__main__":
    main()
