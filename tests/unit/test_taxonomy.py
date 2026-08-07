"""Taxonomy 两阶段调用单元测试。"""

from __future__ import annotations

import json

import pytest

from app.core.llm.taxonomy import (
    MAX_CATEGORIES,
    MAX_DEPTH,
    OTHER_CATEGORY,
    FileSummary,
    Taxonomy,
    TaxonomyBuilder,
    _parse_assignments_json,
    _parse_taxonomy_json,
    _similar,
    enforce_constraints,
    merge_similar_categories,
)
from tests.fixtures.doubles import FakeProvider


def _summaries(n: int) -> list[FileSummary]:
    return [
        FileSummary(name=f"file{i}.txt", ext=".txt", size=100, mtime="2024-01-01", rel_path=f"file{i}.txt")
        for i in range(n)
    ]


class TestMergeSimilarCategories:
    def test_idempotent(self) -> None:
        cats = [("财务", "发票"), ("合同协议",), ("票据",)]
        once = merge_similar_categories(cats)
        twice = merge_similar_categories(once)
        assert once == twice

    def test_synonym_merge(self) -> None:
        cats = [("财务", "发票"), ("财务", "票据")]
        result = merge_similar_categories(cats)
        assert len(result) == 1

    def test_no_merge_different(self) -> None:
        # 用 3 字以上的名字避免编辑距离误判
        cats = [("财务类",), ("合同协议",), ("图片集",)]
        result = merge_similar_categories(cats)
        assert len(result) == 3

    def test_empty(self) -> None:
        assert merge_similar_categories([]) == []


class TestSimilar:
    def test_exact_match(self) -> None:
        assert _similar("发票", "发票") is True

    def test_synonym(self) -> None:
        assert _similar("发票", "票据") is True

    def test_different(self) -> None:
        assert _similar("发票", "合同") is False

    def test_edit_distance_near(self) -> None:
        assert _similar("发票", "发票单") is True


class TestEnforceConstraints:
    def test_truncates_depth(self) -> None:
        cats = [("a", "b", "c"), ("x", "y", "z")]
        result = enforce_constraints(cats, max_count=12, max_depth=2)
        assert all(len(c) <= 2 for c in result)

    def test_limits_count(self) -> None:
        cats = [(f"cat{i}",) for i in range(20)]
        result = enforce_constraints(cats, max_count=12, max_depth=2)
        assert len(result) <= 12

    def test_passes_small_set(self) -> None:
        cats = [("财务类",), ("合同协议",), ("图片集",)]
        result = enforce_constraints(cats, max_count=12, max_depth=2)
        assert len(result) == 3


class TestParseTaxonomyJson:
    def test_simple_array(self) -> None:
        text = '[["财务", "发票"], ["合同协议"]]'
        tax = _parse_taxonomy_json(text)
        assert ("财务", "发票") in tax.categories
        assert ("合同协议",) in tax.categories

    def test_with_code_fence(self) -> None:
        text = '```json\n[["财务", "发票"]]\n```'
        tax = _parse_taxonomy_json(text)
        assert tax.categories == [("财务", "发票")]

    def test_invalid_returns_empty(self) -> None:
        tax = _parse_taxonomy_json("not json")
        assert tax.categories == []


class TestParseAssignmentsJson:
    def test_valid(self) -> None:
        text = '[{"name": "a.txt", "category": ["财务", "发票"], "reason": "ok"}]'
        data = _parse_assignments_json(text)
        assert len(data) == 1
        assert data[0]["name"] == "a.txt"

    def test_null_category(self) -> None:
        text = '[{"name": "a.txt", "category": null}]'
        data = _parse_assignments_json(text)
        assert data[0]["category"] is None


class TestTaxonomyBuilder:
    def test_build_taxonomy_basic(self) -> None:
        response = json.dumps([["财务", "发票"], ["合同协议"], ["图片"]])
        provider = FakeProvider(scripted_responses=[response])
        builder = TaxonomyBuilder(provider=provider, sample_size=5, rng=__import__('random').Random(42))
        summaries = _summaries(3)
        tax = builder.build_taxonomy(summaries)
        assert len(tax.categories) > 0
        assert all(len(c) <= MAX_DEPTH for c in tax.categories)

    def test_classify_batch(self) -> None:
        tax_text = json.dumps([["财务", "发票"], ["合同协议"]])
        cls_text = json.dumps([
            {"name": "file0.txt", "category": ["财务", "发票"], "reason": "匹配"},
            {"name": "file1.txt", "category": None, "reason": "不确定"},
        ])
        provider = FakeProvider(scripted_responses=[tax_text, cls_text])
        builder = TaxonomyBuilder(provider=provider, sample_size=10, rng=__import__('random').Random(42))

        summaries = _summaries(2)
        tax = builder.build_taxonomy(summaries)
        result = builder.classify_batch(summaries, tax)

        assert len(result.assignments) == 2
        # 第一个分配到类目
        assert result.assignments[0].category == ("财务", "发票")
        # 第二个 unknown
        assert result.assignments[1].category is None

    def test_estimate_request_count(self) -> None:
        provider = FakeProvider()
        builder = TaxonomyBuilder(provider=provider, batch_size=100)
        # 250 文件 = 1 + ceil(250/100) = 4
        assert builder.estimate_request_count(250) == 4

    def test_estimate_zero_files(self) -> None:
        provider = FakeProvider()
        builder = TaxonomyBuilder(provider=provider)
        assert builder.estimate_request_count(0) == 0

    def test_failure_returns_empty_taxonomy(self) -> None:
        provider = FakeProvider(fail_with="network")
        builder = TaxonomyBuilder(provider=provider, sample_size=5, rng=__import__('random').Random(42))
        tax = builder.build_taxonomy(_summaries(3))
        assert tax.categories == []


class TestTaxonomyModel:
    def test_has_category(self) -> None:
        tax = Taxonomy(categories=[("财务", "发票"), ("合同",)])
        assert tax.has_category(("财务", "发票")) is True
        assert tax.has_category(("图片",)) is False

    def test_len(self) -> None:
        tax = Taxonomy(categories=[("a",), ("b",)])
        assert len(tax) == 2
