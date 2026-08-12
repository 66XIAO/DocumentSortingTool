"""规则管理页。需求 4.3、4.8。

展示与编辑关键词规则与扩展名规则，保存后下一次方案生成即使用修改后的规则。
解析错误时展示含行号与字段名的结构化错误提示。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import SettingsManager
from app.core.rules import RuleSerializer, render_rules_yaml
from app.ui.theme import (
    DANGER,
    SPACE_LG,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TitleLabel,
    toast_error,
    toast_info,
)


class RulesPage(QWidget):
    """规则管理页。需求 4.3、4.8。"""

    rulesSaved = Signal()

    def __init__(
        self,
        manager: SettingsManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("rulesPage")
        self._manager = manager
        try:
            self._original_text = manager.rules_path.read_text(encoding="utf-8")
        except OSError:
            # 首次运行或规则文件不可读时才回落到内置规则。不能每次打开页面都从
            # 内置规则开始，否则用户点一次保存就会覆盖之前的自定义规则。
            self._original_text = render_rules_yaml()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        layout.setSpacing(SPACE_LG)

        header = QHBoxLayout()
        header.addWidget(TitleLabel("规则管理", self))
        header.addStretch()
        self._reset_btn = PushButton("恢复默认", self)
        self._reset_btn.clicked.connect(self._reset_default)
        header.addWidget(self._reset_btn)
        self._save_btn = PrimaryPushButton("保存", self)
        self._save_btn.clicked.connect(self._save)
        header.addWidget(self._save_btn)
        layout.addLayout(header)

        hint = CaptionLabel(
            "编辑关键词规则与扩展名规则。保存后下一次方案生成即生效。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._status = CaptionLabel("", self)
        self._status.setStyleSheet(f"color: {DANGER};")
        layout.addWidget(self._status)

        self._editor = QPlainTextEdit(self)
        self._editor.setPlainText(self._original_text)
        self._editor.setObjectName("rulesEditor")
        layout.addWidget(self._editor)

    def _reset_default(self) -> None:
        self._editor.setPlainText(render_rules_yaml())
        self._status.setText("")

    def _save(self) -> None:
        text = self._editor.toPlainText()
        rules, errors = RuleSerializer.load(text)
        if errors:
            # 需求 4.3：解析错误时展示含行号与字段名的结构化错误提示
            messages = [e.describe() for e in errors]
            self._status.setText("\n".join(messages))
            toast_error(
                self,
                "规则无法保存",
                f"规则文件有 {len(errors)} 处错误，请修正后保存。",
            )
            return

        # 原子替换：写到一半崩溃时保留上一份可用规则，不留下半截 YAML。
        rules_path = self._manager.rules_path
        os.makedirs(rules_path.parent, exist_ok=True)
        tmp = rules_path.with_suffix(".yaml.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, rules_path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            toast_error(self, "规则保存失败", str(exc))
            return

        self._original_text = text
        self._status.setText("")
        toast_info(self, "规则已保存", f"已保存 {len(rules)} 条规则。")
        self.rulesSaved.emit()
