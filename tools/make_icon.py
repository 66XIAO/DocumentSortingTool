"""生成应用图标 ``resources/icon.ico``。

图标是**构建产物**，但它同时被 PyInstaller 的 ``icon=`` 与运行期窗口图标读取，
所以要提交进仓库。留下这个生成脚本而不是只丢一个二进制文件，是为了让「图标为什么
长这样、想改怎么改」这件事有可复现的答案。

设计取舍：
- 主色沿用 tokens 里的 ``#2563EB``，与界面一致。
- 图形是三条递减的白色横条，读作「归好档的文件」。刻意不用写实的文件夹造型：
  16x16 下任何细节都会糊成一团，只有粗横条还能辨认。
- 多尺寸打包 16/24/32/48/64/128/256：Windows 在任务栏、Alt-Tab、资源管理器
  各处取不同尺寸，只给 256 会让小尺寸由系统缩放，边缘发虚。

跑法：python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

#: 与 app/ui/theme/tokens.py 的 PRIMARY 保持一致
PRIMARY = (37, 99, 235, 255)  # #2563EB
BAR = (255, 255, 255, 255)

#: Windows 各处会用到的尺寸
SIZES = [16, 24, 32, 48, 64, 128, 256]

#: 以 256 为基准绘制，再缩放到各尺寸
BASE = 256

OUTPUT = Path(__file__).resolve().parent.parent / "resources" / "icon.ico"


def draw_base() -> Image.Image:
    """在 256x256 画布上绘制图标。"""
    image = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 圆角底板。留 8px 边距，避免贴边后在圆形裁切的场景里被切掉笔画
    margin = 8
    draw.rounded_rectangle(
        [margin, margin, BASE - margin, BASE - margin],
        radius=48,
        fill=PRIMARY,
    )

    # 三条递减横条：宽度依次收窄，读作「分好类、对齐归档」
    bar_height = 26
    gap = 28
    left = 56
    widths = (144, 112, 80)
    top = (BASE - (bar_height * 3 + gap * 2)) // 2
    for index, width in enumerate(widths):
        y = top + index * (bar_height + gap)
        draw.rounded_rectangle(
            [left, y, left + width, y + bar_height],
            radius=bar_height // 2,
            fill=BAR,
        )

    return image


def main() -> int:
    base = draw_base()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Pillow 的 ico 保存会按 sizes 自行下采样，用 LANCZOS 质量最好
    base.save(
        OUTPUT,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
    )
    print(f"WROTE {OUTPUT} ({OUTPUT.stat().st_size} bytes)")

    with Image.open(OUTPUT) as written:
        print(f"SIZES {sorted(written.ico.sizes())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
