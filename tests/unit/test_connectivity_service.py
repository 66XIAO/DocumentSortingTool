"""ConnectivityService：测试连通性必须离开 UI 线程。需求 7.4。

原实现在按钮回调里直接同步调用，10 秒硬超时整整冻在 UI 线程上——填错 base_url 或
网络不可达时窗口假死，用户看不到进展也没法取消。

这里测的是服务契约：
    结果一律经 finished 带回 ConnectivityResult，连不上不算作业失败
    五类 ProviderError 都转成可读文案，不把异常类名甩给用户
    超时上限是需求 7.4 规定的 10 秒
    并发点击被拒绝，按钮在测试期间置灰
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.llm import runner as runner_module
from app.core.llm.provider import ProviderError
from app.core.models import AIOptions, ProviderKind
from app.services.connectivity_service import (
    TIMEOUT_SECONDS,
    ConnectivityResult,
    ConnectivityService,
)

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")


@pytest.fixture
def service(qtbot) -> Iterator[ConnectivityService]:  # noqa: ARG001
    svc = ConnectivityService()
    yield svc
    svc.cancel()
    svc.wait(15000)


def _options(**kwargs: object) -> AIOptions:
    base: dict[str, object] = {
        "provider": ProviderKind.OLLAMA,
        "host": "http://localhost:11434",
        "model": "test-model",
    }
    base.update(kwargs)
    return AIOptions(**base)  # type: ignore[arg-type]


def _install(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr(runner_module, "make_provider", lambda *a, **k: provider)


class _Ok:
    def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
        return f"连接成功，超时 {timeout_seconds}"


class _Fails:
    def __init__(self, kind: str, detail: str = "") -> None:
        self._kind = kind
        self._detail = detail

    def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
        raise ProviderError(self._kind, "失败", self._detail)  # type: ignore[arg-type]


class _Explodes:
    def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
        raise RuntimeError("意料之外")


def _run(service: ConnectivityService, qtbot) -> ConnectivityResult:
    with qtbot.waitSignal(service.finished, timeout=15000) as blocker:
        assert service.start_test(_options(), "sk-x") is True
    service.wait(15000)
    return blocker.args[0]


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_success_is_reported_via_finished(
    qtbot, monkeypatch: pytest.MonkeyPatch, service: ConnectivityService
) -> None:
    _install(monkeypatch, _Ok())

    result = _run(service, qtbot)

    assert isinstance(result, ConnectivityResult)
    assert result.ok is True
    assert "连接成功" in result.message


def test_timeout_is_ten_seconds(
    qtbot, monkeypatch: pytest.MonkeyPatch, service: ConnectivityService
) -> None:
    """需求 7.4：必须在 10 秒内返回结果或失败原因。"""
    _install(monkeypatch, _Ok())

    result = _run(service, qtbot)

    assert TIMEOUT_SECONDS == 10.0
    assert str(TIMEOUT_SECONDS) in result.message


# ---------------------------------------------------------------------------
# 失败路径：走 finished 而不是 failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("timeout", "请求超时"),
        ("non_2xx", "服务返回错误状态"),
        ("schema", "返回内容不符合约定格式"),
        ("quota_exhausted", "额度耗尽或凭据无效"),
        ("network", "网络连接失败"),
    ],
)
def test_five_failure_kinds_become_readable_messages(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: ConnectivityService,
    kind: str,
    expected: str,
) -> None:
    _install(monkeypatch, _Fails(kind))

    result = _run(service, qtbot)

    assert result.ok is False
    assert expected in result.message
    # 不该把异常类名甩给用户
    assert "ProviderError" not in result.message


def test_failure_does_not_emit_failed_signal(
    qtbot, monkeypatch: pytest.MonkeyPatch, service: ConnectivityService
) -> None:
    """连不上服务是这个作业的正常结果之一，不是作业出错。"""
    _install(monkeypatch, _Fails("network"))
    seen: list[str] = []
    service.failed.connect(seen.append)

    _run(service, qtbot)

    assert seen == []


def test_unexpected_exception_is_still_a_result(
    qtbot, monkeypatch: pytest.MonkeyPatch, service: ConnectivityService
) -> None:
    _install(monkeypatch, _Explodes())

    result = _run(service, qtbot)

    assert result.ok is False
    assert "意料之外" in result.message


def test_provider_detail_is_included(
    qtbot, monkeypatch: pytest.MonkeyPatch, service: ConnectivityService
) -> None:
    _install(monkeypatch, _Fails("non_2xx", "HTTP 404 not found"))

    result = _run(service, qtbot)

    assert "HTTP 404 not found" in result.message


def test_unknown_provider_kind_is_a_config_error(
    qtbot, service: ConnectivityService
) -> None:
    """未知 Provider 类型是配置问题，也要变成可读结果而不是崩掉作业。"""
    bogus = AIOptions(provider="不存在的类型", model="m")  # type: ignore[arg-type]

    with qtbot.waitSignal(service.finished, timeout=15000) as blocker:
        assert service.start_test(bogus, "") is True
    service.wait(15000)

    result = blocker.args[0]
    assert result.ok is False
    assert "不存在的类型" in result.message


# ---------------------------------------------------------------------------
# 并发与界面状态
# ---------------------------------------------------------------------------


def test_busy_signal_brackets_the_job(
    qtbot, monkeypatch: pytest.MonkeyPatch, service: ConnectivityService
) -> None:
    _install(monkeypatch, _Ok())
    states: list[bool] = []
    service.busyChanged.connect(states.append)

    _run(service, qtbot)

    assert states[0] is True
    assert states[-1] is False


def test_settings_page_disables_button_while_testing(
    qtbot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按钮在测试期间必须置灰，否则用户会连点出一串请求。"""
    from app.config.settings import Settings, SettingsManager
    from app.ui.pages import settings_page as module
    from tests.fixtures.doubles import MemoryKeyring

    _install(monkeypatch, _Ok())
    monkeypatch.setattr(module, "toast_info", lambda *_args: None)
    monkeypatch.setattr(module, "toast_error", lambda *_args: None)

    settings = Settings()
    settings.ai.provider = ProviderKind.OLLAMA
    page = module.SettingsPage(
        settings,
        SettingsManager(base_dir=tmp_path, keyring_backend=MemoryKeyring()),
    )
    qtbot.addWidget(page)
    page._model.setText("m")  # noqa: SLF001

    page._on_connectivity_busy(True)  # noqa: SLF001
    assert page._test_conn_btn.isEnabled() is False  # noqa: SLF001

    page._on_connectivity_busy(False)  # noqa: SLF001
    assert page._test_conn_btn.isEnabled() is True  # noqa: SLF001

    page.connectivity_service.cancel()
    page.connectivity_service.wait(15000)


def test_missing_model_is_rejected_before_launching(
    qtbot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没填模型就不该发请求——省一次注定失败的网络等待。"""
    from app.config.settings import Settings, SettingsManager
    from app.ui.pages import settings_page as module
    from tests.fixtures.doubles import MemoryKeyring

    errors: list[str] = []
    monkeypatch.setattr(
        module, "toast_error", lambda _p, _t, body: errors.append(body)
    )
    monkeypatch.setattr(module, "toast_info", lambda *_args: None)

    settings = Settings()
    settings.ai.provider = ProviderKind.OLLAMA
    page = module.SettingsPage(
        settings,
        SettingsManager(base_dir=tmp_path, keyring_backend=MemoryKeyring()),
    )
    qtbot.addWidget(page)
    page._model.setText("")  # noqa: SLF001

    page._test_connectivity()  # noqa: SLF001

    assert errors and "模型" in errors[0]
    assert page.connectivity_service.busy is False


def test_current_ai_options_reads_widgets_not_saved_settings(
    qtbot, tmp_path
) -> None:
    """测的是界面上现在填着的参数，不是已保存的旧配置。"""
    from app.config.settings import Settings, SettingsManager
    from app.ui.pages import settings_page as module
    from tests.fixtures.doubles import MemoryKeyring

    settings = Settings()
    settings.ai.model = "已保存的旧模型"
    page = module.SettingsPage(
        settings,
        SettingsManager(base_dir=tmp_path, keyring_backend=MemoryKeyring()),
    )
    qtbot.addWidget(page)
    page._model.setText("刚填的新模型")  # noqa: SLF001

    assert page.current_ai_options().model == "刚填的新模型"
    assert settings.ai.model == "已保存的旧模型"


def test_api_key_never_enters_options(qtbot, tmp_path) -> None:
    """需求 7.3：凭据不进 AIOptions。"""
    from app.config.settings import Settings, SettingsManager
    from app.ui.pages import settings_page as module
    from tests.fixtures.doubles import MemoryKeyring

    page = module.SettingsPage(
        Settings(),
        SettingsManager(base_dir=tmp_path, keyring_backend=MemoryKeyring()),
    )
    qtbot.addWidget(page)
    page._api_key.setText("sk-secret")  # noqa: SLF001

    options = page.current_ai_options()

    assert "sk-secret" not in repr(options)
    assert page.current_api_key() == "sk-secret"
