"""补充样式表。

只写 qfluentwidgets 覆盖不到的部分（自定义容器的圆角与留白、状态色文本）。
所有数值都从 tokens 取，不写字面量——属性 41 会扫描样式表里的间距数值，要求
它们落在 {4, 8, 12, 16, 24} 之内。
"""

from __future__ import annotations

from app.ui.theme import tokens


def app_stylesheet() -> str:
    t = tokens
    return f"""
QWidget {{
    font-family: {t.FONT_FAMILY_CSS};
    font-size: {t.FONT_SIZE_BODY}px;
}}

#docsorterCard {{
    border-radius: {t.RADIUS}px;
    padding: {t.SPACE_MD}px;
}}

#docsorterDropZone {{
    border: 2px dashed rgba(128, 128, 128, 0.55);
    border-radius: {t.RADIUS}px;
    padding: {t.SPACE_XL}px;
}}

#docsorterDropZoneActive {{
    border: 2px dashed {t.PRIMARY};
    border-radius: {t.RADIUS}px;
    padding: {t.SPACE_XL}px;
}}

#statValue {{
    font-size: {t.FONT_SIZE_STAT}px;
    font-weight: 600;
}}

#statLabel {{
    font-size: {t.FONT_SIZE_CAPTION}px;
}}

.successText {{ color: {t.SUCCESS}; }}
.warningText {{ color: {t.WARNING}; }}
.dangerText  {{ color: {t.DANGER}; }}
"""
