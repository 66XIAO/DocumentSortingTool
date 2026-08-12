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
from app.core.rules import (
    DEFAULT_PRESET_ID,
    RULE_PRESETS,
    RuleSerializer,
    preset_by_id,
    render_rules_yaml,
)
from app.ui.theme import (
    DANGER,
    SPACE_LG,
    SPACE_MD,
    BodyLabel,
    CaptionLabel,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TitleLabel,
    confirm,
    toast_error,
    toast_info,
)


def _preset_index(preset_id: str) -> int:
    """方案 id -> 在 RULE_PRESETS 中的下标；找不到回落到默认方案。"""
    for index, preset in enumerate(RULE_PRESETS):
        if preset.id == preset_id:
            return index
    return 0


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
        # 编辑器当前内容对应的方案 id（用户自定义时可能不在 RULE_PRESETS 中）
        self._loaded_preset_id = DEFAULT_PRESET_ID

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

        # —— 分类方案预设选择 ——
        # qfluentwidgets 的 ComboBox.addItem 不保存 userData，这里用下标直接映射
        # 到 RULE_PRESETS（RULE_PRESETS 是固定元组，下标即方案序）。
        preset_row = QHBoxLayout()
        preset_row.setSpacing(SPACE_MD)
        preset_row.addWidget(StrongBodyLabel("分类方案", self))
        self._preset = ComboBox(self)
        for preset in RULE_PRESETS:
            self._preset.addItem(preset.name)
        self._preset.setCurrentIndex(_preset_index(DEFAULT_PRESET_ID))
        self._preset.currentIndexChanged.connect(self._on_preset_changed)
        preset_row.addWidget(self._preset)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        self._preset_desc = BodyLabel(self)
        self._preset_desc.setWordWrap(True)
        layout.addWidget(self._preset_desc)
        self._update_preset_desc()

        self._status = CaptionLabel("", self)
        self._status.setStyleSheet(f"color: {DANGER};")
        layout.addWidget(self._status)

        self._editor = QPlainTextEdit(self)
        self._editor.setPlainText(self._original_text)
        self._editor.setObjectName("rulesEditor")
        layout.addWidget(self._editor)

    # -- 分类方案预设 -----------------------------------------------------

    def current_preset_id(self) -> str:
        """当前下拉选中的方案 id。"""
        return RULE_PRESETS[self._preset.currentIndex()].id

    def _update_preset_desc(self) -> None:
        preset = preset_by_id(self.current_preset_id())
        self._preset_desc.setText(preset.description if preset else "")

    def _on_preset_changed(self, index: int) -> None:
        """切换方案：若编辑器有未保存改动则先确认，确认后载入该方案规则。"""
        if 0 <= index < len(RULE_PRESETS) and self._editor.toPlainText() != self._original_text:
            ok = confirm(
                self,
                "切换分类方案",
                "当前规则有未保存的修改，切换方案会丢失这些修改。继续吗？",
                "继续切换",
            )
            if not ok:
                # 取消：把下拉回弹到当前编辑器对应的方案
                self._preset.blockSignals(True)
                self._preset.setCurrentIndex(_preset_index(self._loaded_preset_id))
                self._preset.blockSignals(False)
                return
        preset = preset_by_id(self.current_preset_id())
        if preset is not None:
            self._editor.setPlainText(RuleSerializer.dump(preset.rules))
            self._original_text = self._editor.toPlainText()
            self._loaded_preset_id = preset.id
            self._status.setText("")
        self._update_preset_desc()

    def _reset_default(self) -> None:
        preset = preset_by_id(DEFAULT_PRESET_ID)
        self._editor.setPlainText(
            RuleSerializer.dump(preset.rules) if preset else render_rules_yaml()
        )
        self._original_text = self._editor.toPlainText()
        self._loaded_preset_id = DEFAULT_PRESET_ID
        self._status.setText("")
        self._preset.blockSignals(True)
        self._preset.setCurrentIndex(_preset_index(DEFAULT_PRESET_ID))
        self._preset.blockSignals(False)
        self._update_preset_desc()

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
