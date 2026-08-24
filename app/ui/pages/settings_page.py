"""设置页。需求 6.2、7.1、7.4、8.3、15.5、18.1、18.8。

设置对象由 MainWindow 与本页共享；保存成功后发 ``settingsChanged``，主窗口再从磁盘
重载。API key 是唯一例外：明文只进 keyring，不进入 Settings/settings.yaml。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import Settings, SettingsManager
from app.core.models import (
    ActionKind,
    AIOptions,
    ConflictPolicy,
    PrivacyLevel,
    ProviderKind,
    Strategy,
)
from app.services.connectivity_service import (
    ConnectivityResult,
    ConnectivityService,
)
from app.ui.theme import (
    SPACE_LG,
    SPACE_MD,
    PrimaryPushButton,
    StrongBodyLabel,
    SwitchButton,
    TitleLabel,
    confirm,
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
        self.form = QFormLayout()
        self.form.setSpacing(SPACE_MD)
        layout.addLayout(self.form)


class SettingsPage(QWidget):
    """扫描、分类、执行、清理、AI 与历史保留的统一设置页。"""

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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_LG)

        # 标题与「保存」留在滚动区**外**：设置项可以滚，但提交动作必须始终可点，
        # 否则用户改完最后一项还得往上滚才能保存。
        header = QHBoxLayout()
        header.addWidget(TitleLabel("设置", self))
        header.addStretch()
        self._save_btn = PrimaryPushButton("保存", self)
        self._save_btn.clicked.connect(self._save)
        header.addWidget(self._save_btn)
        outer.addLayout(header)

        # 六组设置必须放进滚动区。它们叠起来约 940px 高，而所有页面共用一个
        # StackedWidget——栈的最小高度取各页最大值，本页不滚就会把整个主窗口顶到
        # 1000px 以上。在 1536x864（可用高 816）的屏幕上，窗口随后被居中，标题栏
        # 直接被推到屏幕上方之外，用户连拖动窗口都做不到。
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.viewport().setAutoFillBackground(False)
        body = QWidget(self._scroll)
        self._scroll.setWidget(body)
        outer.addWidget(self._scroll, 1)

        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, SPACE_MD, 0)
        layout.setSpacing(SPACE_LG)

        # --- 扫描 ---
        scan = _Section("扫描", self)
        self._include_hidden = SwitchButton(self)
        self._include_hidden.setChecked(settings.scan.include_hidden)
        scan.form.addRow("包含隐藏文件", self._include_hidden)
        self._follow_symlinks = SwitchButton(self)
        self._follow_symlinks.setChecked(settings.scan.follow_symlinks)
        scan.form.addRow("跟随符号链接", self._follow_symlinks)
        layout.addWidget(scan)

        # --- 分类 ---
        classify = _Section("分类", self)
        self._strategy = QComboBox(self)
        for label, value in (
            ("按类型", Strategy.BY_TYPE),
            ("按时间", Strategy.BY_DATE),
            ("类型 + 时间", Strategy.TYPE_AND_DATE),
            ("智能", Strategy.SMART),
        ):
            self._strategy.addItem(label, value.value)
        self._set_combo(self._strategy, settings.classify.strategy.value)
        classify.form.addRow("默认策略", self._strategy)

        self._min_confidence = QDoubleSpinBox(self)
        self._min_confidence.setRange(0.0, 1.0)
        self._min_confidence.setSingleStep(0.05)
        self._min_confidence.setDecimals(2)
        self._min_confidence.setValue(settings.classify.min_confidence)
        classify.form.addRow("最低置信度", self._min_confidence)

        self._merge_small = SwitchButton(self)
        self._merge_small.setChecked(settings.classify.merge_small_categories)
        classify.form.addRow("合并小类目", self._merge_small)
        self._small_threshold = QSpinBox(self)
        self._small_threshold.setRange(1, 1000)
        self._small_threshold.setValue(settings.classify.small_category_threshold)
        classify.form.addRow("小类目阈值", self._small_threshold)
        layout.addWidget(classify)

        # --- 执行与清理 ---
        execution = _Section("执行与清理", self)
        self._conflict = QComboBox(self)
        for label, value in (
            ("自动改名", ConflictPolicy.AUTO_RENAME),
            ("跳过", ConflictPolicy.SKIP),
            ("覆盖（危险）", ConflictPolicy.OVERWRITE),
        ):
            self._conflict.addItem(label, value.value)
        self._set_combo(self._conflict, settings.conflict.policy.value)
        execution.form.addRow("同名冲突", self._conflict)

        self._default_action = QComboBox(self)
        self._default_action.addItem("移动", ActionKind.MOVE.value)
        self._default_action.addItem("复制", ActionKind.COPY.value)
        self._set_combo(self._default_action, settings.conflict.default_action.value)
        execution.form.addRow("默认动作", self._default_action)

        self._verify_hash = SwitchButton(self)
        self._verify_hash.setChecked(settings.conflict.verify_hash)
        execution.form.addRow("跨卷校验哈希", self._verify_hash)
        self._cleanup = SwitchButton(self)
        self._cleanup.setChecked(settings.cleanup.remove_empty_dirs)
        execution.form.addRow("清理整理后变空的子文件夹", self._cleanup)
        layout.addWidget(execution)

        # --- AI ---
        ai = _Section("AI 分类", self)
        self._ai_enabled = SwitchButton(self)
        self._ai_enabled.setChecked(settings.ai.enabled)
        ai.form.addRow("启用 AI 分类", self._ai_enabled)

        self._provider_combo = QComboBox(self)
        self._provider_combo.addItem("OpenAI 兼容", ProviderKind.OPENAI_COMPAT.value)
        self._provider_combo.addItem("Ollama（本地）", ProviderKind.OLLAMA.value)
        self._set_combo(self._provider_combo, settings.ai.provider.value)
        ai.form.addRow("Provider", self._provider_combo)

        self._base_url = QLineEdit(settings.ai.base_url, self)
        self._base_url.setPlaceholderText("https://api.example.com")
        ai.form.addRow("Base URL", self._base_url)
        self._host = QLineEdit(settings.ai.host, self)
        self._host.setPlaceholderText("http://localhost:11434")
        ai.form.addRow("Ollama Host", self._host)
        self._model = QLineEdit(settings.ai.model, self)
        ai.form.addRow("模型", self._model)

        self._api_key = QLineEdit(self)
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        if manager.get_api_key(ProviderKind.OPENAI_COMPAT.value):
            self._api_key.setPlaceholderText("已保存在 Windows 凭据管理器；留空则不修改")
        else:
            self._api_key.setPlaceholderText("仅保存到 Windows 凭据管理器")
        ai.form.addRow("API Key", self._api_key)

        self._timeout = QSpinBox(self)
        self._timeout.setRange(1, 600)
        self._timeout.setSuffix(" 秒")
        self._timeout.setValue(settings.ai.timeout_seconds)
        ai.form.addRow("请求超时", self._timeout)
        self._batch_size = QSpinBox(self)
        self._batch_size.setRange(80, 120)
        self._batch_size.setValue(settings.ai.batch_size)
        ai.form.addRow("批大小", self._batch_size)

        self._privacy_combo = QComboBox(self)
        self._privacy_combo.addItem("仅元数据", PrivacyLevel.METADATA_ONLY.value)
        self._privacy_combo.addItem(
            "元数据 + 正文前 500 字", PrivacyLevel.METADATA_PLUS_HEAD500.value
        )
        self._set_combo(self._privacy_combo, settings.ai.privacy_level.value)
        ai.form.addRow("隐私级别", self._privacy_combo)

        self._test_conn_btn = PrimaryPushButton("测试连通性", self)
        self._test_conn_btn.clicked.connect(self._test_connectivity)
        ai.form.addRow("", self._test_conn_btn)
        layout.addWidget(ai)

        # 连通性测试跑在工作线程里，见 ConnectivityService 的说明
        self._connectivity = ConnectivityService(parent=self)
        self._connectivity.busyChanged.connect(self._on_connectivity_busy)
        self._connectivity.finished.connect(self._on_connectivity_done)
        self._connectivity.failed.connect(self._on_connectivity_failed)

        # --- 历史保留 ---
        history = _Section("历史保留", self)
        self._max_runs = QSpinBox(self)
        self._max_runs.setRange(1, 1000)
        self._max_runs.setValue(settings.history.max_runs)
        history.form.addRow("保留条数", self._max_runs)
        self._max_days = QSpinBox(self)
        self._max_days.setRange(1, 3650)
        self._max_days.setValue(settings.history.max_days)
        history.form.addRow("保留天数", self._max_days)
        layout.addWidget(history)
        layout.addStretch()

    @staticmethod
    def _set_combo(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @property
    def connectivity_service(self) -> ConnectivityService:
        """供主窗口关窗时收尾：网络等待可能还没结束。"""
        return self._connectivity

    def current_ai_options(self) -> AIOptions:
        """按界面上**当前填着**的值构造 AIOptions。

        不读 ``self._settings``：测试连通性的意义就是在保存之前先试一下，读已保存的
        配置会让用户测的是旧参数。api_key 不进这个对象（需求 7.3），单独传。
        """
        return AIOptions(
            enabled=self._ai_enabled.isChecked(),
            provider=ProviderKind(str(self._provider_combo.currentData())),
            base_url=self._base_url.text().strip(),
            host=self._host.text().strip(),
            model=self._model.text().strip(),
            timeout_seconds=self._timeout.value(),
            privacy_level=PrivacyLevel(str(self._privacy_combo.currentData())),
            batch_size=self._batch_size.value(),
        )

    def current_api_key(self) -> str:
        """优先用刚输入的，其次用 keyring 里已存的。"""
        kind = str(self._provider_combo.currentData())
        return self._api_key.text() or self._manager.get_api_key(kind) or ""

    def _test_connectivity(self) -> None:
        """发起一次最小请求确认服务可达。需求 7.4。

        调用走 ``ConnectivityService``，在工作线程里跑。同步调用那 10 秒硬超时会
        整整冻住窗口，用户看不到进展也没法取消，只会以为程序崩了。
        """
        kind = str(self._provider_combo.currentData())
        if kind == ProviderKind.OPENAI_COMPAT.value and not self.current_api_key():
            toast_error(self, "无法测试连接", "请先输入 API Key。")
            return
        if not self._model.text().strip():
            toast_error(self, "无法测试连接", "请先填写模型名称。")
            return

        if not self._connectivity.start_test(
            self.current_ai_options(), self.current_api_key()
        ):
            toast_info(self, "正在测试", "上一次连通性测试还没结束，请稍候。")

    def _on_connectivity_busy(self, busy: bool) -> None:
        self._test_conn_btn.setEnabled(not busy)
        self._test_conn_btn.setText("测试中…" if busy else "测试连通性")

    def _on_connectivity_done(self, payload: object) -> None:
        if not isinstance(payload, ConnectivityResult):
            return
        if payload.ok:
            toast_info(self, "连接成功", payload.message)
        else:
            toast_error(self, "连接失败", payload.message)

    def _on_connectivity_failed(self, detail: str) -> None:
        """作业本身崩了才走到这里；连不上服务走的是 finished。"""
        toast_error(self, "连接失败", detail)

    def _save(self) -> None:
        new_privacy = PrivacyLevel(str(self._privacy_combo.currentData()))
        if (
            new_privacy is PrivacyLevel.METADATA_PLUS_HEAD500
            and self._settings.ai.privacy_level is not new_privacy
            and not confirm(
                self,
                "允许发送正文片段？",
                "启用后，AI 请求会额外包含每个受支持文档正文的前 500 个字符。"
                "绝对路径仍不会发送。",
                ok_text="我同意",
            )
        ):
            self._set_combo(self._privacy_combo, self._settings.ai.privacy_level.value)
            return

        provider_id = str(self._provider_combo.currentData())
        typed_key = self._api_key.text()
        if typed_key:
            if not self._manager.set_api_key(provider_id, typed_key):
                toast_error(self, "设置未保存", "API Key 无法写入凭据管理器。")
                return
            self._settings.ai.api_key_ref = self._manager.api_key_ref(provider_id)

        self._settings.scan.include_hidden = self._include_hidden.isChecked()
        self._settings.scan.follow_symlinks = self._follow_symlinks.isChecked()
        self._settings.classify.strategy = Strategy(str(self._strategy.currentData()))
        self._settings.classify.min_confidence = self._min_confidence.value()
        self._settings.classify.merge_small_categories = self._merge_small.isChecked()
        self._settings.classify.small_category_threshold = self._small_threshold.value()
        self._settings.conflict.policy = ConflictPolicy(str(self._conflict.currentData()))
        self._settings.conflict.default_action = ActionKind(
            str(self._default_action.currentData())
        )
        self._settings.conflict.verify_hash = self._verify_hash.isChecked()
        self._settings.cleanup.remove_empty_dirs = self._cleanup.isChecked()
        self._settings.ai.enabled = self._ai_enabled.isChecked()
        self._settings.ai.provider = ProviderKind(provider_id)
        self._settings.ai.base_url = self._base_url.text().strip()
        self._settings.ai.host = self._host.text().strip()
        self._settings.ai.model = self._model.text().strip()
        self._settings.ai.timeout_seconds = self._timeout.value()
        self._settings.ai.batch_size = self._batch_size.value()
        self._settings.ai.privacy_level = new_privacy
        self._settings.history.max_runs = self._max_runs.value()
        self._settings.history.max_days = self._max_days.value()

        try:
            self._manager.save(self._settings)
        except OSError as exc:
            toast_error(self, "设置保存失败", str(exc))
            return
        self._api_key.clear()
        toast_info(self, "设置已保存", "新设置将在下一次扫描或方案生成时生效。")
        self.settingsChanged.emit()
