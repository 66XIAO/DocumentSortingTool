"""LLM 批级缓存单元测试。"""

from __future__ import annotations

import pytest

from app.core.llm.cache import LLMCache
from app.core.llm.taxonomy import FileSummary, LLMAssignment, LLMBatchResult


def _summaries(n: int = 3) -> list[FileSummary]:
    return [
        FileSummary(name=f"file{i}.txt", ext=".txt", size=100 + i, mtime="2024-01-01", rel_path=f"file{i}.txt")
        for i in range(n)
    ]


def _result(names: list[str]) -> LLMBatchResult:
    return LLMBatchResult(
        assignments=[
            LLMAssignment(name=n, category=("文档",), confidence=0.8, reason="test")
            for n in names
        ]
    )


class TestLLMCache:
    def test_miss_then_hit(self, tmp_path) -> None:
        cache = LLMCache(tmp_path / "cache.db")
        summaries = _summaries()

        assert cache.get(summaries) is None
        assert cache.stats.misses == 1

        result = _result(["file0.txt", "file1.txt", "file2.txt"])
        cache.put(summaries, result)

        cached = cache.get(summaries)
        assert cached is not None
        assert len(cached.assignments) == 3
        assert cache.stats.hits == 1

    def test_order_independent_key(self, tmp_path) -> None:
        cache = LLMCache(tmp_path / "cache.db")
        s1 = _summaries()
        s2 = list(reversed(_summaries()))

        cache.put(s1, _result(["file0.txt", "file1.txt", "file2.txt"]))

        # 不同顺序应命中同一缓存
        assert cache.get(s2) is not None

    def test_roundtrip_consistency(self, tmp_path) -> None:
        """需求 8.9：缓存往返一致性。"""
        cache = LLMCache(tmp_path / "cache.db")
        summaries = _summaries()

        original = LLMBatchResult(
            assignments=[
                LLMAssignment(name="a.txt", category=("财务", "发票"), confidence=0.9, reason="匹配"),
                LLMAssignment(name="b.txt", category=None, confidence=0.0, reason="unknown"),
            ]
        )
        cache.put(summaries, original)

        loaded = cache.get(summaries)
        assert loaded is not None
        assert loaded.assignments[0].category == ("财务", "发票")
        assert loaded.assignments[1].category is None
        assert loaded.assignments[0].confidence == 0.9

    def test_clear(self, tmp_path) -> None:
        cache = LLMCache(tmp_path / "cache.db")
        summaries = _summaries()

        cache.put(summaries, _result(["file0.txt"]))
        assert cache.get(summaries) is not None

        cache.clear()
        assert cache.get(summaries) is None

    def test_make_key_stable(self) -> None:
        s = _summaries()
        k1 = LLMCache.make_key(s)
        k2 = LLMCache.make_key(s)
        assert k1 == k2
        assert len(k1) == 64  # SHA-256 hex

    def test_different_summaries_different_key(self) -> None:
        s1 = _summaries(3)
        s2 = _summaries(4)
        assert LLMCache.make_key(s1) != LLMCache.make_key(s2)

    def test_overwrite(self, tmp_path) -> None:
        cache = LLMCache(tmp_path / "cache.db")
        summaries = _summaries()

        cache.put(summaries, _result(["file0.txt"]))
        new_result = _result(["file0.txt", "file1.txt"])
        cache.put(summaries, new_result)

        loaded = cache.get(summaries)
        assert loaded is not None
        assert len(loaded.assignments) == 2
        assert cache.stats.inserts == 2
