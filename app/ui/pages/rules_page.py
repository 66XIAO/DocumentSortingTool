"""规则管理页。需求 4.3、4.8。

展示与编辑关键词规则与扩展名规则，保存后下一次方案生成即使用修改后的规则。
解析错误时展示含行号与字段名的结构化错误提示。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.rules import Rule, RuleError, RuleSerializer, builtin_rules, render_rules_yaml
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rulesPage")
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
            toast_error(f"规则文件有 {len(errors)} 处错误，请修正后保存")
            return

        # 写入文件
        from app.config.settings import default_app_dir
        import os
        rules_path = default_app_dir() / "rules.yaml"
        os.makedirs(rules_path.parent, exist_ok=True)
        rules_path.write_text(text, encoding="utf-8")

        self._original_text = text
        self._status.setText("")
        toast_info(f"已保存 {len(rules)} 条规则")
        self.rulesSaved.emit()
