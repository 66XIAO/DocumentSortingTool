"""PlanService 的 AI 主链接线测试。

这里测的是**接线**，不是分类正确性（那在 test_llm_runner.py）：

    智能策略下正文被提取并写回 FileEntry（需求 5.1-5.4）
    ai.enabled 为 false 时一次 Provider 调用都不发（需求 6.4）
    未配置 Provider 时即使开着开关也不调用（需求 6.3）
    LLM 结果进入方案且带 AI 角标（需求 8.7）
    失败经 aiFallback 提示且方案照常产出（需求 6.5、6.6）
    预估请求次数经 aiEstimate 发出（需求 8.6）

目录树刻意让「文件名不含关键词、正文含关键词」，否则 filename 分类器（priority
200）会抢在 content（150）之前命中，就测不到正文那一环了。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.llm import runner as runner_module
from app.core.models import (
    AIOptions,
    ClassifyOptions,
    PrivacyLevel,
    ProviderKind,
    Strategy,
)
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from app.services.plan_service import PlanService
from tests.fixtures.doubles import FakeProvider
from tests.fixtures.trees import build_tree

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")

#: 正文含「发票」但文件名不含，用来验证 content 那一环真的接上了
CONTENT_FILE = "aaa0001.txt"
#: 文件名与正文都不命中规则，留给 LLM 分配
PLAIN_FILE = "bbb0002.txt"

TREE = {
    CONTENT_FILE: "本次报销的发票明细如下",
    PLAIN_FILE: "今天写了点东西",
}


@pytest.fixture
def guard() -> SafetyGuard:
    """tmp_path 位于 AppData 之下，必须关掉段黑名单才能作为根目录。"""
    return SafetyGuard(system_roots=(), denied_segments=())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return build_tree(tmp_path / "下载", TREE)


@pytest.fixture
def entries(root: Path, guard: SafetyGuard) -> list:
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    return session.entries()


@pytest.fixture
def service(guard: SafetyGuard, qtbot: object) -> Iterator[PlanService]:  # noqa: ARG001
    """必须显式收尾：QThread 还在跑时让 service 被回收会直接把解释器带崩。"""
    svc = PlanService(guard=guard)
    yield svc
    svc.cancel()
    svc.wait(15000)


def _ai(**kwargs: object) -> AIOptions:
    base: dict[str, object] = {
        "enabled": True,
        "provider": ProviderKind.OPENAI_COMPAT,
        "base_url": "https://api.example.com",
        "model": "gpt-test",
    }
    base.update(kwargs)
    return AIOptions(**base)  # type: ignore[arg-type]


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    """把 Provider 构造换成替身，绝不发出真实网络请求。"""
    monkeypatch.setattr(runner_module, "make_provider", lambda *a, **k: provider)


def _rebuild(service: PlanService, root: Path, entries: list, qtbot) -> object:
    with qtbot.waitSignal(service.finished, timeout=20000) as blocker:
        assert service.rebuild(
            entries,
            root=root,
            strategy=Strategy.SMART,
            options=ClassifyOptions(strategy=Strategy.SMART),
        )
    return blocker.args[0]


# ---------------------------------------------------------------------------
# 正文提取（需求 5.1-5.4）
# ---------------------------------------------------------------------------


def test_smart_strategy_extracts_content(
    qtbot, service: PlanService, root: Path, entries: list
) -> None:
    """智能策略下正文被提取并写回 FileEntry，无需重新扫描。"""
    assert all(e.text_head is None for e in entries)

    _rebuild(service, root, entries, qtbot)

    extracted = {e.name: e.text_head for e in entries}
    assert extracted[CONTENT_FILE] is not None
    assert "发票" in extracted[CONTENT_FILE]


def test_extracted_content_drives_classification(
    qtbot, service: PlanService, root: Path, entries: list
) -> None:
    """需求 5.5：文件名不命中时，类目来自正文关键词。"""
    result = _rebuild(service, root, entries, qtbot)

    reasons = {i.entry.name: i.reason for i in result.plan.all_items()}
    assert "正文" in reasons[CONTENT_FILE]


def test_extraction_is_not_repeated(
    qtbot, service: PlanService, root: Path, entries: list
) -> None:
    """已提取过的条目不再重复读盘。"""
    _rebuild(service, root, entries, qtbot)
    target = next(e for e in entries if e.name == CONTENT_FILE)
    target.text_head = "手工改过的正文"

    _rebuild(service, root, entries, qtbot)

    assert target.text_head == "手工改过的正文"


# ---------------------------------------------------------------------------
# AI 开关（需求 6.3、6.4）
# ---------------------------------------------------------------------------


def test_ai_disabled_makes_zero_provider_calls(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: PlanService,
    root: Path,
    entries: list,
) -> None:
    """需求 6.4：ai.enabled 为 false 时 Provider 调用次数为 0。"""
    provider = FakeProvider(scripted_responses=[json.dumps([["文档"]])])
    _install_provider(monkeypatch, provider)
    service.configure_ai(_ai(enabled=False), "sk-x")

    _rebuild(service, root, entries, qtbot)

    assert provider.call_count == 0


def test_unconfigured_provider_makes_zero_calls(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: PlanService,
    root: Path,
    entries: list,
) -> None:
    """需求 6.3：没有可用 Provider 时即使开着开关也不该调用。"""
    provider = FakeProvider(scripted_responses=[json.dumps([["文档"]])])
    _install_provider(monkeypatch, provider)
    service.configure_ai(_ai(model=""), "sk-x")

    assert service.ai_available() is False
    _rebuild(service, root, entries, qtbot)
    assert provider.call_count == 0


def test_ai_available_reflects_configuration(service: PlanService) -> None:
    service.configure_ai(_ai(), "sk-x")
    assert service.ai_available() is True

    service.configure_ai(_ai(), "")
    assert service.ai_available() is False


# ---------------------------------------------------------------------------
# LLM 结果进入方案（需求 8.7）
# ---------------------------------------------------------------------------


def test_llm_result_reaches_plan_with_badge(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: PlanService,
    root: Path,
    entries: list,
) -> None:
    """需求 8.7：由 LLM 分配的条目带 by_llm 角标；content 命中的不该被 AI 抢走。"""
    provider = FakeProvider(
        scripted_responses=[
            json.dumps([["日记"]]),
            json.dumps(
                [
                    {
                        "name": PLAIN_FILE,
                        "rel_path": PLAIN_FILE,
                        "category": ["日记"],
                        "reason": "像是随笔",
                    }
                ]
            ),
        ]
    )
    _install_provider(monkeypatch, provider)
    service.configure_ai(_ai(), "sk-x")

    result = _rebuild(service, root, entries, qtbot)

    by_name = {i.entry.name: i for i in result.plan.all_items()}
    assert by_name[PLAIN_FILE].by_llm is True
    assert by_name[PLAIN_FILE].target.parent.name == "日记"
    # content(150) 高于 llm(120)，正文命中的那份仍由规则胜出
    assert by_name[CONTENT_FILE].by_llm is False


def test_ai_estimate_is_emitted(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: PlanService,
    root: Path,
    entries: list,
) -> None:
    """需求 8.6：方案生成前发出文件数与预估请求次数。"""
    _install_provider(monkeypatch, FakeProvider(fail_with="network"))
    service.configure_ai(_ai(), "sk-x")

    with qtbot.waitSignal(service.aiEstimate, timeout=5000) as blocker:
        service.rebuild(
            entries,
            root=root,
            strategy=Strategy.SMART,
            options=ClassifyOptions(strategy=Strategy.SMART),
        )
    service.wait(20000)

    files, requests = blocker.args
    assert files == len(entries)
    assert requests == 2  # 2 个文件、1 批 → taxonomy 1 次 + 分类 1 次


# ---------------------------------------------------------------------------
# 失败回落（需求 6.5、6.6）
# ---------------------------------------------------------------------------


def test_failure_emits_fallback_and_still_produces_plan(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: PlanService,
    root: Path,
    entries: list,
) -> None:
    """需求 6.5、6.6：失败经 aiFallback 提示，方案照常覆盖全部文件。"""
    _install_provider(monkeypatch, FakeProvider(fail_with="quota_exhausted"))
    service.configure_ai(_ai(), "sk-x")
    messages: list[str] = []
    service.aiFallback.connect(messages.append)

    result = _rebuild(service, root, entries, qtbot)

    assert messages and "已使用规则分类" in messages[0]
    covered = {i.entry.name for i in result.plan.all_items()}
    assert covered == {e.name for e in entries}


def test_failure_does_not_emit_failed(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: PlanService,
    root: Path,
    entries: list,
) -> None:
    """LLM 失败不是方案生成失败：failed 信号不该被触发。"""
    _install_provider(monkeypatch, FakeProvider(fail_with="timeout"))
    service.configure_ai(_ai(), "sk-x")
    seen: list[str] = []
    service.failed.connect(seen.append)

    _rebuild(service, root, entries, qtbot)

    assert seen == []


# ---------------------------------------------------------------------------
# 隐私（需求 8.1）
# ---------------------------------------------------------------------------


def test_default_privacy_does_not_send_content(
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
    service: PlanService,
    root: Path,
    entries: list,
) -> None:
    """默认隐私档下，即使正文已提取也不外发，绝对路径也不外发。"""
    provider = FakeProvider(scripted_responses=[json.dumps([["文档"]]), "[]"])
    _install_provider(monkeypatch, provider)
    service.configure_ai(_ai(privacy_level=PrivacyLevel.METADATA_ONLY), "sk-x")

    _rebuild(service, root, entries, qtbot)

    sent = "\n".join(m.content for r in provider.requests for m in r.messages)
    assert "本次报销的发票明细如下" not in sent
    assert str(root) not in sent
