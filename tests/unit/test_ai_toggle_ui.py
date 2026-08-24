"""预览页 AI 开关与主窗口接线测试。

覆盖需求 6.2（预览页开关与设置页作用于同一配置项）、6.3（未配置 Provider 时置灰）、
6.7（切换后按需求 19 重算并保留 override）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config.settings import Settings, SettingsManager
from app.core.models import ItemOverride, ProviderKind
from app.core.safety import SafetyGuard
from app.services.scan_service import ScanService
from app.ui.main_window import SortFlowPage
from tests.fixtures.doubles import MemoryKeyring

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")


@pytest.fixture
def manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(
        base_dir=tmp_path / "cfg", keyring_backend=MemoryKeyring()
    )


@pytest.fixture
def page(
    qtbot, manager: SettingsManager, tmp_path: Path
) -> Iterator[SortFlowPage]:
    guard = SafetyGuard(system_roots=(), denied_segments=())
    scan = ScanService(guard=guard)
    settings = Settings()
    flow = SortFlowPage(
        scan,
        settings,
        history_dir=tmp_path / "history",
        settings_manager=manager,
    )
    qtbot.addWidget(flow)
    yield flow
    for svc in (scan, flow.plan_service, flow.execute_service, flow.undo_service):
        svc.cancel()
        svc.wait(10000)


def _switch(page: SortFlowPage) -> object:
    return page.preview_page._ai_switch


def _configure_openai(settings: Settings, manager: SettingsManager) -> None:
    settings.ai.provider = ProviderKind.OPENAI_COMPAT
    settings.ai.base_url = "https://api.example.com"
    settings.ai.model = "gpt-test"
    manager.set_api_key(str(ProviderKind.OPENAI_COMPAT), "sk-test")


# ---------------------------------------------------------------------------
# 需求 6.3：未配置 Provider 时置灰
# ---------------------------------------------------------------------------


def test_switch_disabled_without_provider(page: SortFlowPage) -> None:
    assert _switch(page).isEnabled() is False
    assert page.plan_service.ai_available() is False


def test_switch_enabled_once_provider_configured(
    page: SortFlowPage, manager: SettingsManager
) -> None:
    settings = Settings()
    _configure_openai(settings, manager)

    page.update_settings(settings)

    assert page.plan_service.ai_available() is True
    assert _switch(page).isEnabled() is True


def test_ollama_needs_only_host_and_model(
    page: SortFlowPage, manager: SettingsManager
) -> None:
    """本地 Ollama 无鉴权，不该因为没有 API key 就被判定为不可用。"""
    settings = Settings()
    settings.ai.provider = ProviderKind.OLLAMA
    settings.ai.host = "http://localhost:11434"
    settings.ai.model = "qwen2.5"

    page.update_settings(settings)

    assert page.plan_service.ai_available() is True


def test_enabled_but_unconfigured_renders_off(
    page: SortFlowPage, manager: SettingsManager
) -> None:
    """配置里开着但没有可用 Provider 时，开关不该显示成已开启。"""
    settings = Settings()
    settings.ai.enabled = True  # 开着，但 model / base_url / key 都没填

    page.update_settings(settings)

    assert _switch(page).isEnabled() is False
    assert _switch(page).isChecked() is False


# ---------------------------------------------------------------------------
# 需求 6.2：与设置页作用于同一配置项
# ---------------------------------------------------------------------------


def test_toggle_writes_same_setting(
    page: SortFlowPage, manager: SettingsManager
) -> None:
    settings = Settings()
    _configure_openai(settings, manager)
    page.update_settings(settings)
    assert settings.ai.enabled is False

    page.preview_page.aiToggled.emit(True)

    assert settings.ai.enabled is True
    assert page.plan_service.ai_options.enabled is True

    page.preview_page.aiToggled.emit(False)

    assert settings.ai.enabled is False
    assert page.plan_service.ai_options.enabled is False


def test_settings_page_change_propagates_to_preview(
    page: SortFlowPage, manager: SettingsManager
) -> None:
    """设置页保存后整理流程必须换用新配置树，否则会长期用着旧值。"""
    settings = Settings()
    _configure_openai(settings, manager)
    settings.ai.enabled = True

    page.update_settings(settings)

    assert _switch(page).isChecked() is True
    assert page.plan_service.ai_options.model == "gpt-test"


# ---------------------------------------------------------------------------
# 需求 6.7：切换保留 override
# ---------------------------------------------------------------------------


def test_toggle_keeps_overrides(
    page: SortFlowPage, manager: SettingsManager
) -> None:
    """override 由 PlanService 独家持有，切 AI 开关不该冲掉手工调整。"""
    settings = Settings()
    _configure_openai(settings, manager)
    page.update_settings(settings)
    page.plan_service.overrides.items["C:/x/a.pdf"] = ItemOverride(included=False)

    page.preview_page.aiToggled.emit(True)

    assert "C:/x/a.pdf" in page.plan_service.overrides.items


def test_toggle_is_idempotent(page: SortFlowPage, manager: SettingsManager) -> None:
    """重复设成同一个值不该反复触发重算。"""
    settings = Settings()
    _configure_openai(settings, manager)
    page.update_settings(settings)
    settings.ai.enabled = True

    page.preview_page.aiToggled.emit(True)

    assert settings.ai.enabled is True


# ---------------------------------------------------------------------------
# API key 只走 keyring
# ---------------------------------------------------------------------------


def test_api_key_never_enters_options(
    page: SortFlowPage, manager: SettingsManager
) -> None:
    """需求 7.3：凭据不进 AIOptions，也就不会被序列化或打日志带出去。"""
    settings = Settings()
    _configure_openai(settings, manager)

    page.update_settings(settings)

    assert not hasattr(page.plan_service.ai_options, "api_key")
    assert "sk-test" not in repr(page.plan_service.ai_options)
