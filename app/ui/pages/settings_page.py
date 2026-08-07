"""设置页。需求 6.2、7.1、7.4、8.3、15.5、18.1、18.8。

分组呈现扫描、分类、执行、AI、历史保留、清理各组配置项。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import Settings, SettingsManager
from app.core.models import PrivacyLevel, ProviderKind
from app.ui.theme import (
    SPACE_LG,
    SPACE_MD,
    BodyLabel,
    PrimaryPushButton,
    StrongBodyLabel,
    SwitchButton,
    TitleLabel,
    toast_error,
    toast_info,
)


class _Section(QWidget):
    """设置分组。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_MD)
        layout.addWidget(StrongBodyLabel(title, self))
        self._form = QFormLayout()
        self._form.setSpacing(SPACE_MD)
        layout.addLayout(self._form)
        layout.addStretch()


class SettingsPage(QWidget):
    """设置页。"""

    settingsChanged = Signal()

    def __init__(
        self,
        settings: Settings,
        manager: SettingsManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._settings = settings
        self._manager = manager

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        layout.setSpacing(SPACE_LG)

        header = QHBoxLayout()
        header.addWidget(TitleLabel("设置", self))
        header.addStretch()
        self._save_btn = PrimaryPushButton("保存", self)
        self._save_btn.clicked.connect(self._save)
        header.addWidget(self._save_btn)
        layout.addLayout(header)

        # --- AI 分组 ---
        ai_section = _Section("AI 分类", self)
        self._ai_enabled = SwitchButton(self)
        self._ai_enabled.setChecked(settings.ai.enabled)
        ai_section._form.addRow("启用 AI 分类", self._ai_enabled)

        self._provider_combo = QComboBox(self)
        self._provider_combo.addItem("OpenAI 兼容", "openai_compat")
        self._provider_combo.addItem("Ollama（本地）", "ollama")
        idx = self._provider_combo.findData(settings.ai.provider.value)
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        ai_section._form.addRow("Provider", self._provider_combo)

        self._base_url = QLineEdit(settings.ai.base_url, self)
        ai_section._form.addRow("Base URL", self._base_url)

        self._model = QLineEdit(settings.ai.model, self)
        ai_section._form.addRow("模型", self._model)

        self._privacy_combo = QComboBox(self)
        self._privacy_combo.addItem("仅元数据", "metadata_only")
        self._privacy_combo.addItem("元数据+正文前 500 字", "metadata_plus_head500")
        pidx = self._privacy_combo.findData(settings.ai.privacy_level.value)
        if pidx >= 0:
            self._privacy_combo.setCurrentIndex(pidx)
        ai_section._form.addRow("隐私级别", self._privacy_combo)

        self._test_conn_btn = PrimaryPushButton("测试连通性", self)
        self._test_conn_btn.clicked.connect(self._test_connectivity)
        ai_section._form.addRow("", self._test_conn_btn)

        layout.addWidget(ai_section)

        # --- 历史保留分组 ---
        hist_section = _Section("历史保留", self)
        self._max_runs = QSpinBox(self)
        self._max_runs.setRange(1, 1000)
        self._max_runs.setValue(settings.history.max_runs)
        hist_section._form.addRow("保留条数", self._max_runs)

        self._max_days = QSpinBox(self)
        self._max_days.setRange(1, 3650)
        self._max_days.setValue(settings.history.max_days)
        hist_section._form.addRow("保留天数", self._max_days)

        layout.addWidget(hist_section)
        layout.addStretch()

    def _test_connectivity(self) -> None:
        """需求 7.4：发起一次最小请求确认服务可达。"""
        from app.core.llm.provider import make_provider
        kind = self._provider_combo.currentData()
        try:
            provider = make_provider(
                kind,
                base_url=self._base_url.text(),
                api_key="<test>",
                host=self._manager.base_dir.as_uri(),
                model=self._model.text(),
            )
            result = provider.test_connectivity(timeout_seconds=10.0)
            toast_info(result)
        except Exception as exc:
            toast_error(f"连接失败: {exc}")

    def _save(self) -> None:
        self._settings.ai.enabled = self._ai_enabled.isChecked()
        self._settings.ai.provider = ProviderKind(self._provider_combo.currentData())
        self._settings.ai.base_url = self._base_url.text()
        self._settings.ai.model = self._model.text()
        self._settings.ai.privacy_level = PrivacyLevel(self._privacy_combo.currentData())
        self._settings.history.max_runs = self._max_runs.value()
        self._settings.history.max_days = self._max_days.value()

        self._manager.save(self._settings)
        toast_info("设置已保存")
        self.settingsChanged.emit()
