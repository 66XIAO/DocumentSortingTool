"""LLMRunner 与 AI 主链接线的单元测试。

覆盖需求 3.6（智能策略求值顺序）、6.3（Provider 可用判定）、6.4（关闭时跳过
LLM）、6.5 与 6.6（失败回落且方案仍覆盖全部文件）、7.8（结构化输出）、
7.9（类目不在 taxonomy 时交给后续分类器）、8.5（批级缓存）、8.6（预估请求次数）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.classifiers.base import SOURCE_LLM
from app.core.classifiers.by_content import ContentClassifier
from app.core.classifiers.by_extension import ExtensionClassifier
from app.core.classifiers.by_filename import FilenameClassifier
from app.core.classifiers.by_llm import PRIORITY_LLM, LLMClassifier
from app.core.llm.cache import LLMCache
from app.core.llm.runner import (
    FALLBACK_MESSAGE,
    LLMRunner,
    cache_scope,
    estimate_requests,
    make_provider_for,
    provider_configured,
)
from app.core.llm.taxonomy import FileSummary, Taxonomy
from app.core.models import (
    AIOptions,
    ClassifyOptions,
    FileEntry,
    PrivacyLevel,
    ProviderKind,
    Strategy,
)
from app.core.planner import Planner
from app.core.rules import RuleEngine
from tests.fixtures.doubles import FakeProvider


def _entry(root: Path, rel: str, text: str = "") -> FileEntry:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "x", encoding="utf-8")
    stat = path.stat()
    return FileEntry(
        path=path,
        name=path.name,
        ext=path.suffix.lower(),
        size=stat.st_size,
        mtime=stat.st_mtime,
        is_hidden=False,
        depth=1,
    )


def _ai(**kwargs: object) -> AIOptions:
    base: dict[str, object] = {
        "enabled": True,
        "provider": ProviderKind.OPENAI_COMPAT,
        "base_url": "https://api.example.com",
        "model": "gpt-test",
        "batch_size": 100,
    }
    base.update(kwargs)
    return AIOptions(**base)  # type: ignore[arg-type]


def _scripted(taxonomy: list[list[str]], assignments: list[dict]) -> FakeProvider:
    return FakeProvider(
        scripted_responses=[json.dumps(taxonomy), json.dumps(assignments)]
    )


# ---------------------------------------------------------------------------
# 管线顺序（需求 3.6）
# ---------------------------------------------------------------------------


def test_llm_priority_sits_between_content_and_extension() -> None:
    """原值 50 低于 extension，会让 LLM 结果永远被扩展名规则抢先。"""
    engine = RuleEngine()

    assert ExtensionClassifier(engine).priority < PRIORITY_LLM
    assert PRIORITY_LLM < ContentClassifier(engine).priority
    assert ContentClassifier(engine).priority < FilenameClassifier(engine).priority


def test_smart_pipeline_order_with_ai_on() -> None:
    """需求 3.6：filename → content → llm → extension。"""
    pipeline = Planner()._pipeline_for(Strategy.SMART, ai_enabled=True)

    assert pipeline.names() == ("filename", "content", "llm", "extension")


def test_smart_pipeline_skips_llm_when_ai_off() -> None:
    """需求 6.4：ai.enabled 为 false 时管线跳过 LLM_Classifier。"""
    pipeline = Planner()._pipeline_for(Strategy.SMART, ai_enabled=False)

    assert "llm" not in pipeline.names()
    assert pipeline.names() == ("filename", "content", "extension")


# ---------------------------------------------------------------------------
# Provider 可用判定（需求 6.3）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "api_key", "expected"),
    [
        (_ai(), "sk-x", True),
        (_ai(), "", False),  # 缺 api_key
        (_ai(base_url=""), "sk-x", False),  # 缺 base_url
        (_ai(model=""), "sk-x", False),  # 缺 model
        (_ai(provider=ProviderKind.OLLAMA, host="http://localhost:11434"), "", True),
        (_ai(provider=ProviderKind.OLLAMA, host=""), "", False),
    ],
)
def test_provider_configured(options: AIOptions, api_key: str, expected: bool) -> None:
    assert provider_configured(options, api_key) is expected


def test_make_provider_for_selects_implementation() -> None:
    assert make_provider_for(_ai(), "sk-x").name == "openai_compat"
    ollama = _ai(provider=ProviderKind.OLLAMA, host="http://localhost:11434")
    assert make_provider_for(ollama).name == "ollama"


# ---------------------------------------------------------------------------
# 预估请求次数（需求 8.6）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "batch", "expected"),
    [(0, 100, 0), (1, 100, 2), (100, 100, 2), (101, 100, 3), (1240, 100, 14)],
)
def test_estimate_requests(total: int, batch: int, expected: int) -> None:
    assert estimate_requests(total, batch) == expected


# ---------------------------------------------------------------------------
# 两阶段调用与回填
# ---------------------------------------------------------------------------


def test_run_produces_suggestions(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "发票单据.pdf"), _entry(tmp_path, "note.txt")]
    provider = _scripted(
        [["财务", "发票"], ["文档"]],
        [
            {"name": "发票单据.pdf", "rel_path": "发票单据.pdf",
             "category": ["财务", "发票"], "reason": "标题含发票"},
            {"name": "note.txt", "rel_path": "note.txt",
             "category": ["文档"], "reason": "普通文本"},
        ],
    )
    runner = LLMRunner(provider=provider, options=_ai())

    result = runner.run(entries, tmp_path)

    assert result.ok
    assert result.requests == 2  # 1 次 taxonomy + 1 批分类
    assert set(result.suggestions) == {str(e.path) for e in entries}
    first = result.suggestions[str(entries[0].path)]
    assert first.category == ("财务", "发票")
    assert first.source == SOURCE_LLM
    assert "标题含发票" in first.reason


def test_run_uses_structured_output(tmp_path: Path) -> None:
    """需求 7.8：temperature 0 且要求 JSON schema 结构化输出。"""
    provider = _scripted([["文档"]], [])
    LLMRunner(provider=provider, options=_ai()).run([_entry(tmp_path, "a.txt")], tmp_path)

    assert provider.requests, "应至少发出一次请求"
    for request in provider.requests:
        assert request.temperature == 0.0
        assert request.response_format is not None
        assert request.response_format["type"] == "json_schema"
        assert "schema" in request.response_format["json_schema"]


def test_category_outside_taxonomy_is_dropped(tmp_path: Path) -> None:
    """需求 7.9：不在 taxonomy 中的类目不产出建议，交由后续 Classifier。"""
    entry = _entry(tmp_path, "a.txt")
    provider = _scripted(
        [["文档"]],
        [{"name": "a.txt", "rel_path": "a.txt", "category": ["臆造类目"], "reason": ""}],
    )

    result = LLMRunner(provider=provider, options=_ai()).run([entry], tmp_path)

    assert result.suggestions == {}


def test_duplicate_names_are_disambiguated_by_rel_path(tmp_path: Path) -> None:
    """同名文件必须按相对路径回填，否则类目会挂到错误的文件上。"""
    a = _entry(tmp_path, "甲/报告.docx")
    b = _entry(tmp_path, "乙/报告.docx")
    provider = _scripted(
        [["财务"], ["文档"]],
        [
            {"name": "报告.docx", "rel_path": str(Path("甲/报告.docx")),
             "category": ["财务"], "reason": "甲"},
            {"name": "报告.docx", "rel_path": str(Path("乙/报告.docx")),
             "category": ["文档"], "reason": "乙"},
        ],
    )

    result = LLMRunner(provider=provider, options=_ai()).run([a, b], tmp_path)

    assert result.suggestions[str(a.path)].category == ("财务",)
    assert result.suggestions[str(b.path)].category == ("文档",)


def test_duplicate_names_without_rel_path_are_skipped(tmp_path: Path) -> None:
    """模型没回显路径且名字重复时宁可放弃，也不能猜错文件。"""
    a = _entry(tmp_path, "甲/报告.docx")
    b = _entry(tmp_path, "乙/报告.docx")
    provider = _scripted(
        [["财务"]],
        [{"name": "报告.docx", "category": ["财务"], "reason": "拿不准是哪一份"}],
    )

    result = LLMRunner(provider=provider, options=_ai()).run([a, b], tmp_path)

    assert result.suggestions == {}


# ---------------------------------------------------------------------------
# 失败回落（需求 6.5、6.6）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["timeout", "non_2xx", "schema", "quota_exhausted", "network"]
)
def test_all_five_failure_kinds_fall_back(tmp_path: Path, kind: str) -> None:
    """需求 6.5：五类失败都回落到规则分类并给出提示，不向上抛异常。"""
    provider = FakeProvider(fail_with=kind)  # type: ignore[arg-type]

    result = LLMRunner(provider=provider, options=_ai()).run(
        [_entry(tmp_path, "a.txt")], tmp_path
    )

    assert result.suggestions == {}
    assert FALLBACK_MESSAGE in result.failure


def test_plan_still_covers_all_entries_after_failure(tmp_path: Path) -> None:
    """需求 6.6：LLM 失败时方案仍覆盖全部 FileEntry。"""
    entries = [_entry(tmp_path, f"f{i}.txt") for i in range(5)]
    provider = FakeProvider(fail_with="network")
    ai_result = LLMRunner(provider=provider, options=_ai()).run(entries, tmp_path)

    result = Planner().build(
        entries,
        root=tmp_path,
        strategy=Strategy.SMART,
        options=ClassifyOptions(strategy=Strategy.SMART),
        llm_suggestions=ai_result.suggestions,
        ai_enabled=True,
        check_locked=False,
    )

    covered = {str(i.entry.path) for i in result.plan.all_items()}
    assert covered == {str(e.path) for e in entries}


def test_empty_taxonomy_falls_back(tmp_path: Path) -> None:
    """模型没给出可用类目表时也要回落，而不是产出空方案。"""
    provider = FakeProvider(scripted_responses=["not json at all"])

    result = LLMRunner(provider=provider, options=_ai()).run(
        [_entry(tmp_path, "a.txt")], tmp_path
    )

    assert result.suggestions == {}
    assert FALLBACK_MESSAGE in result.failure


# ---------------------------------------------------------------------------
# 缓存（需求 8.5、8.9）
# ---------------------------------------------------------------------------


def test_cache_hit_skips_provider_call(tmp_path: Path) -> None:
    """需求 8.5：命中缓存的批次不再发请求。"""
    entries = [_entry(tmp_path, "a.txt")]
    cache = LLMCache(tmp_path / "cache.db")
    assignments = [
        {"name": "a.txt", "rel_path": "a.txt", "category": ["文档"], "reason": "r"}
    ]

    first = LLMRunner(
        provider=_scripted([["文档"]], assignments), options=_ai(), cache=cache
    ).run(entries, tmp_path)
    assert first.cache_hits == 0

    provider2 = _scripted([["文档"]], assignments)
    second = LLMRunner(provider=provider2, options=_ai(), cache=cache).run(
        entries, tmp_path
    )

    assert second.cache_hits == 1
    # 只应发出 taxonomy 那一次，分类那一批走了缓存
    assert provider2.call_count == 1
    assert second.suggestions == first.suggestions


def test_cache_scope_changes_with_model_and_privacy() -> None:
    """换模型或换隐私档必须换 key，否则会复用不该复用的结果。"""
    taxonomy = Taxonomy(categories=[("文档",)])
    base = cache_scope(_ai(), taxonomy)

    assert base != cache_scope(_ai(model="other-model"), taxonomy)
    assert base != cache_scope(
        _ai(privacy_level=PrivacyLevel.METADATA_PLUS_HEAD500), taxonomy
    )
    assert base != cache_scope(_ai(), Taxonomy(categories=[("图片",)]))
    assert base == cache_scope(_ai(), taxonomy)


def test_cache_key_distinguishes_same_name_in_different_dirs() -> None:
    """只用 name 的旧实现会让不同目录下的同名文件串味。"""
    a = FileSummary(name="报告.docx", ext=".docx", size=10, mtime="1", rel_path="甲/报告.docx")
    b = FileSummary(name="报告.docx", ext=".docx", size=10, mtime="1", rel_path="乙/报告.docx")

    assert LLMCache.make_key([a]) != LLMCache.make_key([b])


def test_cache_key_distinguishes_modified_files() -> None:
    a = FileSummary(name="a.txt", ext=".txt", size=10, mtime="1", rel_path="a.txt")
    b = FileSummary(name="a.txt", ext=".txt", size=10, mtime="2", rel_path="a.txt")

    assert LLMCache.make_key([a]) != LLMCache.make_key([b])


def test_cache_key_distinguishes_text_head() -> None:
    """隐私档升级后请求内容变了，不能命中只发过元数据的那条缓存。"""
    a = FileSummary(name="a.txt", ext=".txt", size=10, mtime="1", rel_path="a.txt")
    b = FileSummary(
        name="a.txt", ext=".txt", size=10, mtime="1", rel_path="a.txt",
        text_head="正文开头",
    )

    assert LLMCache.make_key([a]) != LLMCache.make_key([b])


# ---------------------------------------------------------------------------
# 隐私（需求 8.1、8.2、8.4）
# ---------------------------------------------------------------------------


def test_metadata_only_never_sends_text(tmp_path: Path) -> None:
    """需求 8.1：默认隐私档不外发正文，也不外发绝对路径。"""
    entry = _entry(tmp_path, "子目录/机密.txt", text="这是不该外发的正文内容")
    entry.text_head = "这是不该外发的正文内容"
    provider = _scripted([["文档"]], [])

    LLMRunner(provider=provider, options=_ai()).run([entry], tmp_path)

    sent = "\n".join(m.content for r in provider.requests for m in r.messages)
    assert "这是不该外发的正文内容" not in sent
    assert str(tmp_path) not in sent


def test_head500_sends_truncated_text(tmp_path: Path) -> None:
    """需求 8.2：升档后发送正文前 500 字。"""
    entry = _entry(tmp_path, "a.txt")
    entry.text_head = "甲" * 800
    provider = _scripted([["文档"]], [])
    options = _ai(privacy_level=PrivacyLevel.METADATA_PLUS_HEAD500)

    LLMRunner(provider=provider, options=options).run([entry], tmp_path)

    classify_prompt = provider.requests[-1].messages[-1].content
    assert "甲" * 500 in classify_prompt
    assert "甲" * 501 not in classify_prompt


def test_cancel_before_taxonomy_makes_no_call(tmp_path: Path) -> None:
    """取消后不该再发起任何请求。"""

    class _Cancelled:
        cancelled = True

    provider = _scripted([["文档"]], [])
    result = LLMRunner(provider=provider, options=_ai()).run(
        [_entry(tmp_path, "a.txt")], tmp_path, cancel=_Cancelled()  # type: ignore[arg-type]
    )

    assert result.cancelled
    assert provider.call_count == 0


def test_llm_classifier_reads_snapshot_from_context() -> None:
    """LLMClassifier 只查表，不发请求——这是管线确定性的前提（需求 3.13）。"""
    classifier = LLMClassifier()

    assert classifier.name == "llm"
    assert not hasattr(classifier, "chat")
