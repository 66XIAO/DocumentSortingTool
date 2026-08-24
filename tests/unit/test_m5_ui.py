"""M5 规则页与设置页的回归测试。

这些页面此前可以构造，所以普通 smoke test 全绿；但点击「保存」才会因为 toast 参数
错误抛 TypeError。这里必须真的触发按钮背后的槽，防止再次出现“页面能打开、功能不能用”。
"""

from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings, SettingsManager
from app.core.models import PrivacyLevel, ProviderKind, Strategy
from tests.fixtures.doubles import MemoryKeyring


def test_rules_page_loads_existing_user_rules(qtbot, tmp_path: Path) -> None:
    from app.ui.pages.rules_page import RulesPage

    manager = SettingsManager(base_dir=tmp_path)
    manager.ensure_user_files()
    custom = manager.rules_path.read_text(encoding="utf-8") + "\n# 用户自己的注释\n"
    manager.rules_path.write_text(custom, encoding="utf-8")

    page = RulesPage(manager)
    qtbot.addWidget(page)

    assert page._editor.toPlainText() == custom  # noqa: SLF001


def test_rules_page_invalid_yaml_shows_error_without_crashing(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    from app.ui.pages import rules_page as module

    manager = SettingsManager(base_dir=tmp_path)
    page = module.RulesPage(manager)
    qtbot.addWidget(page)
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module, "toast_error", lambda _parent, title, body: messages.append((title, body))
    )
    page._editor.setPlainText("rules: [")  # noqa: SLF001

    page._save()  # noqa: SLF001

    assert "错误" in page._status.text()  # noqa: SLF001
    assert messages and messages[0][0] == "规则无法保存"
    assert not manager.rules_path.exists()


def test_rules_page_saves_atomically_to_injected_manager_path(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    from app.core.rules import render_rules_yaml
    from app.ui.pages import rules_page as module

    manager = SettingsManager(base_dir=tmp_path / "portable")
    page = module.RulesPage(manager)
    qtbot.addWidget(page)
    monkeypatch.setattr(module, "toast_info", lambda *_args: None)
    text = render_rules_yaml()
    page._editor.setPlainText(text)  # noqa: SLF001

    with qtbot.waitSignal(page.rulesSaved, timeout=1000):
        page._save()  # noqa: SLF001

    assert manager.rules_path.read_text(encoding="utf-8") == text
    assert not manager.rules_path.with_suffix(".yaml.tmp").exists()


def test_rules_page_switches_preset(qtbot, tmp_path: Path, monkeypatch) -> None:
    """切换分类方案会把对应方案的规则载入编辑器。"""
    from app.core.rules import RULE_PRESETS, RuleSerializer
    from app.ui.pages import rules_page as module

    manager = SettingsManager(base_dir=tmp_path)
    page = module.RulesPage(manager)
    qtbot.addWidget(page)

    dev_idx = [i for i, p in enumerate(RULE_PRESETS) if p.id == "developer"][0]
    page._preset.setCurrentIndex(dev_idx)  # noqa: SLF001

    dev = next(p for p in RULE_PRESETS if p.id == "developer")
    loaded, _ = RuleSerializer.load(page._editor.toPlainText())  # noqa: SLF001
    assert set(loaded) == set(dev.rules)


def test_rules_page_unsaved_change_blocks_preset_switch(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """有未保存修改时切换方案需确认；取消则回弹。"""
    from app.core.rules import RULE_PRESETS
    from app.ui.pages import rules_page as module

    manager = SettingsManager(base_dir=tmp_path)
    page = module.RulesPage(manager)
    qtbot.addWidget(page)
    monkeypatch.setattr(module, "confirm", lambda *_a, **_k: False)

    before = page._editor.toPlainText()  # noqa: SLF001
    page._editor.setPlainText(before + "\n# 未保存改动\n")  # noqa: SLF001

    personal_idx = [i for i, p in enumerate(RULE_PRESETS) if p.id == "personal"][0]
    page._preset.setCurrentIndex(personal_idx)  # noqa: SLF001

    # 取消后：下拉回弹、编辑器内容不变
    assert page.current_preset_id() != "personal"
    assert page._editor.toPlainText() == before + "\n# 未保存改动\n"  # noqa: SLF001


def test_rules_page_confirmed_preset_switch_overwrites(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """确认后切换方案会用新方案覆盖编辑器内容。"""
    from app.core.rules import RULE_PRESETS
    from app.ui.pages import rules_page as module

    manager = SettingsManager(base_dir=tmp_path)
    page = module.RulesPage(manager)
    qtbot.addWidget(page)
    monkeypatch.setattr(module, "confirm", lambda *_a, **_k: True)

    page._editor.setPlainText(page._editor.toPlainText() + "\n# 改动\n")  # noqa: SLF001
    personal_idx = [i for i, p in enumerate(RULE_PRESETS) if p.id == "personal"][0]
    page._preset.setCurrentIndex(personal_idx)  # noqa: SLF001

    assert page.current_preset_id() == "personal"
    assert "# 改动" not in page._editor.toPlainText()  # noqa: SLF001


def test_settings_save_updates_all_visible_groups_and_keyring(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    from app.ui.pages import settings_page as module

    backend = MemoryKeyring()
    manager = SettingsManager(base_dir=tmp_path, keyring_backend=backend)
    settings = Settings()
    page = module.SettingsPage(settings, manager)
    qtbot.addWidget(page)
    monkeypatch.setattr(module, "toast_info", lambda *_args: None)

    page._set_combo(page._strategy, Strategy.SMART.value)  # noqa: SLF001
    page._ai_enabled.setChecked(True)  # noqa: SLF001
    page._base_url.setText("https://api.example.test")  # noqa: SLF001
    page._host.setText("http://localhost:11434")  # noqa: SLF001
    page._model.setText("model-x")  # noqa: SLF001
    page._api_key.setText("secret-value")  # noqa: SLF001
    page._timeout.setValue(45)  # noqa: SLF001
    page._batch_size.setValue(120)  # noqa: SLF001
    page._cleanup.setChecked(True)  # noqa: SLF001

    with qtbot.waitSignal(page.settingsChanged, timeout=1000):
        page._save()  # noqa: SLF001

    restored = manager.load()
    assert restored.classify.strategy is Strategy.SMART
    assert restored.cleanup.remove_empty_dirs is True
    assert restored.ai.enabled is True
    assert restored.ai.base_url == "https://api.example.test"
    assert restored.ai.host == "http://localhost:11434"
    assert restored.ai.model == "model-x"
    assert restored.ai.timeout_seconds == 45
    assert restored.ai.batch_size == 120
    assert restored.ai.api_key_ref == manager.api_key_ref("openai_compat")
    assert manager.get_api_key("openai_compat") == "secret-value"
    assert "secret-value" not in manager.settings_path.read_text(encoding="utf-8")


def test_settings_privacy_upgrade_requires_explicit_confirmation(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    from app.ui.pages import settings_page as module

    manager = SettingsManager(base_dir=tmp_path, keyring_backend=MemoryKeyring())
    settings = Settings()
    page = module.SettingsPage(settings, manager)
    qtbot.addWidget(page)
    page._set_combo(  # noqa: SLF001
        page._privacy_combo, PrivacyLevel.METADATA_PLUS_HEAD500.value
    )
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: False)

    page._save()  # noqa: SLF001

    assert settings.ai.privacy_level is PrivacyLevel.METADATA_ONLY
    assert page._privacy_combo.currentData() == PrivacyLevel.METADATA_ONLY.value  # noqa: SLF001
    assert not manager.settings_path.exists()


def test_connectivity_uses_configured_ollama_host(qtbot, tmp_path: Path, monkeypatch) -> None:
    """需求 7.1、7.4：Ollama 用界面上填的 host，而不是别处推导出来的地址。

    patch 的是 ``runner.make_provider``：``ConnectivityService`` 经
    ``make_provider_for`` 调用它，而那个名字在 runner 的命名空间里已经绑定好了，
    patch ``provider.make_provider`` 不会生效。
    """
    from app.core.llm import runner as runner_module
    from app.ui.pages import settings_page as module

    captured: dict[str, str] = {}

    class FakeProvider:
        def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
            assert timeout_seconds == 10.0
            return "ok"

    def fake_make(kind: str, **kwargs):
        captured["kind"] = kind
        captured.update({key: str(value) for key, value in kwargs.items()})
        return FakeProvider()

    monkeypatch.setattr(runner_module, "make_provider", fake_make)
    monkeypatch.setattr(module, "toast_info", lambda *_args: None)
    manager = SettingsManager(base_dir=tmp_path, keyring_backend=MemoryKeyring())
    settings = Settings()
    settings.ai.provider = ProviderKind.OLLAMA
    page = module.SettingsPage(settings, manager)
    qtbot.addWidget(page)
    page._host.setText("http://127.0.0.1:11434")  # noqa: SLF001
    page._model.setText("qwen-test")  # noqa: SLF001

    # 调用现在是异步的：跑在工作线程里，必须等 finished
    with qtbot.waitSignal(page._connectivity.finished, timeout=15000):  # noqa: SLF001
        page._test_connectivity()  # noqa: SLF001
    page._connectivity.wait(15000)  # noqa: SLF001

    assert captured["kind"] == ProviderKind.OLLAMA.value
    assert captured["host"] == "http://127.0.0.1:11434"
    assert captured["model"] == "qwen-test"
