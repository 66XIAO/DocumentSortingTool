"""属性 13-15：LLM 批处理与 taxonomy 收敛、缓存一致性、Provider 调用计数。

需求 7.5-7.11、8.5、8.6、8.9。
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.llm.taxonomy import (
    MAX_CATEGORIES,
    MAX_DEPTH,
    Taxonomy,
    enforce_constraints,
    merge_similar_categories,
)
from app.core.llm.cache import LLMCache
from app.core.llm.taxonomy import FileSummary, LLMAssignment, LLMBatchResult


# ---------------------------------------------------------------------------
# 属性 13：taxonomy 收敛
# ---------------------------------------------------------------------------

@st.composite
def category_paths(draw: st.DrawFn) -> list[tuple[str, ...]]:
    """生成类目路径列表。"""
    names = draw(st.lists(st.text(min_size=1, max_size=10, alphabet="abcdefghij"), min_size=0, max_size=20))
    return [(n,) for n in names]


@given(cats=category_paths())
@settings(max_examples=100)
def test_enforce_constraints_always_within_bounds(cats: list[tuple[str, ...]]) -> None:
    """属性 13：收敛后的类目数不超过 12、层级不超过 2。"""
    result = enforce_constraints(cats)
    assert len(result) <= MAX_CATEGORIES
    for cat in result:
        assert 1 <= len(cat) <= MAX_DEPTH


@given(cats=category_paths())
@settings(max_examples=100)
def test_merge_similar_idempotent(cats: list[tuple[str, ...]]) -> None:
    """属性 13：归并函数幂等。"""
    once = merge_similar_categories(cats)
    twice = merge_similar_categories(once)
    assert once == twice


# ---------------------------------------------------------------------------
# 属性 15：缓存一致性
# ---------------------------------------------------------------------------

@given(n=st.integers(min_value=1, max_value=10))
@settings(max_examples=30, deadline=None)
def test_cache_roundtrip(n: int) -> None:
    """属性 15：缓存往返一致性。"""
    import tempfile
    from pathlib import Path

    summaries = [
        FileSummary(name=f"f{i}.txt", ext=".txt", size=i * 100, mtime="0", rel_path=f"f{i}.txt")
        for i in range(n)
    ]
    result = LLMBatchResult(
        assignments=[
            LLMAssignment(name=f"f{i}.txt", category=("文档",), confidence=0.8, reason="test")
            for i in range(n)
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        cache = LLMCache(Path(tmp) / "c.db")
        try:
            cache.put(summaries, result)
            loaded = cache.get(summaries)
            assert loaded is not None
            assert len(loaded.assignments) == len(result.assignments)
            for orig, cached in zip(result.assignments, loaded.assignments):
                assert orig.name == cached.name
                assert orig.category == cached.category
                assert orig.confidence == cached.confidence
        finally:
            # Windows 上需要先关闭文件句柄才能删除临时目录
            import gc
            del cache
            gc.collect()
