"""OverrideLayer 与 override 叠加进 Planner 后的行为。

属性 18-23 的 Hypothesis 版本在后续的属性测试轮次。本文件覆盖四步算法的每一步，
以及最要紧的一条：**重算不丢手工调整**（需求 19.3、19.13、19.14）——那是用户不必
反复重做同样修改的唯一保证。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.classifiers.base import UNCLASSIFIED, Suggestion
from app.core.models import (
    CategoryOverride,
    ClassifyOptions,
    ItemOverride,
    OverrideSet,
    Strategy,
)
from app.core.overrides import ApplyReport, OverrideLayer
from app.core.planner import Planner
from app.core.rules import Rule, RuleEngine
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from tests.fixtures.trees import build_tree


@pytest.fixture
def guard() -> SafetyGuard:
    return SafetyGuard(system_roots=(), denied_segments=())


def suggestion(parts: tuple[str, ...], confidence: float = 0.7) -> Suggestion:
    return Suggestion(parts, confidence, f"规则命中 {'/'.join(parts)}", "extension")


# ---------------------------------------------------------------------------
# 1) 类目级重映射
# ---------------------------------------------------------------------------


def test_rename_remaps_members() -> None:
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        categories={("文档", "PDF"): CategoryOverride(renamed_to=("我的PDF",))}
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == ("我的PDF",)
    assert resolved.report.category_hits == 1


def test_merge_remaps_members() -> None:
    base = {
        "a.pdf": suggestion(("文档", "PDF")),
        "b.docx": suggestion(("文档", "Word")),
    }
    overrides = OverrideSet(
        categories={("文档", "Word"): CategoryOverride(merged_into=("文档", "PDF"))}
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["b.docx"].category == ("文档", "PDF")
    assert resolved.suggestions["a.pdf"].category == ("文档", "PDF")


def test_merge_wins_over_rename() -> None:
    """合并是比改名更强的意图。"""
    base = {"a.pdf": suggestion(("A",))}
    overrides = OverrideSet(
        categories={
            ("A",): CategoryOverride(renamed_to=("B",), merged_into=("C",))
        }
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == ("C",)


def test_remap_chain_is_followed() -> None:
    """A 改名成 B、B 又并进 C，A 的成员应当落到 C。"""
    base = {"a.pdf": suggestion(("A",))}
    overrides = OverrideSet(
        categories={
            ("A",): CategoryOverride(renamed_to=("B",)),
            ("B",): CategoryOverride(merged_into=("C",)),
        }
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == ("C",)


def test_cyclic_remap_terminates() -> None:
    """A→B→A 不能把调用方挂死。"""
    base = {"a.pdf": suggestion(("A",))}
    overrides = OverrideSet(
        categories={
            ("A",): CategoryOverride(renamed_to=("B",)),
            ("B",): CategoryOverride(renamed_to=("A",)),
        }
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category in {("A",), ("B",)}


def test_deleted_category_sends_members_to_unclassified() -> None:
    """需求 10.3。"""
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        categories={("文档", "PDF"): CategoryOverride(deleted=True)}
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == UNCLASSIFIED
    assert "已被删除" in resolved.suggestions["a.pdf"].reason


def test_color_override_is_collected() -> None:
    base = {"a.pdf": suggestion(("图片",))}
    overrides = OverrideSet(
        categories={("图片",): CategoryOverride(color="#123456")}
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.colors[("图片",)] == "#123456"


# ---------------------------------------------------------------------------
# 2) 类目重建
# ---------------------------------------------------------------------------


def test_category_referenced_only_by_override_is_rebuilt() -> None:
    """需求 19.6：规则变了导致类目消失，也不该把成员冲到未分类。"""
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        items={"a.pdf": ItemOverride(category_path=("我手建的",))}
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == ("我手建的",)
    assert ("我手建的",) in resolved.report.rebuilt_categories


# ---------------------------------------------------------------------------
# 3) 条目级覆盖
# ---------------------------------------------------------------------------


def test_item_override_beats_classifier() -> None:
    """需求 19.5、19.13。"""
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        items={"a.pdf": ItemOverride(category_path=("财务", "发票"))}
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == ("财务", "发票")
    assert resolved.suggestions["a.pdf"].source == "user"
    assert resolved.report.kept == 1


def test_item_override_records_original_reason() -> None:
    """用户看得到「原来是怎么判的」，才好判断自己改得对不对。"""
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        items={"a.pdf": ItemOverride(category_path=("财务",))}
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert "原：" in resolved.suggestions["a.pdf"].reason


def test_item_override_follows_category_remap() -> None:
    """条目 override 记的可能是改名前的类目名。"""
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        items={"a.pdf": ItemOverride(category_path=("旧名",))},
        categories={("旧名",): CategoryOverride(renamed_to=("新名",))},
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == ("新名",)


def test_item_override_into_deleted_category_falls_to_unclassified() -> None:
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        items={"a.pdf": ItemOverride(category_path=("没了",))},
        categories={("没了",): CategoryOverride(deleted=True)},
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.suggestions["a.pdf"].category == UNCLASSIFIED


def test_included_override_is_reported() -> None:
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(items={"a.pdf": ItemOverride(included=False)})

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.included["a.pdf"] is False
    assert resolved.report.kept == 1


# ---------------------------------------------------------------------------
# 4) 失效清理
# ---------------------------------------------------------------------------


def test_override_for_missing_file_is_dropped() -> None:
    """需求 19.7。"""
    base = {"a.pdf": suggestion(("文档", "PDF"))}
    overrides = OverrideSet(
        items={
            "a.pdf": ItemOverride(included=False),
            "已删除.pdf": ItemOverride(category_path=("财务",)),
        }
    )

    resolved = OverrideLayer().resolve(base, overrides)

    assert resolved.report.dropped_paths == ["已删除.pdf"]
    assert resolved.report.kept == 1


def test_report_summary_mentions_both_numbers() -> None:
    report = ApplyReport(kept=3, dropped_paths=["x", "y"])

    text = report.summary()

    assert "3" in text
    assert "2" in text


def test_empty_overrides_change_nothing() -> None:
    base = {"a.pdf": suggestion(("文档", "PDF"))}

    resolved = OverrideLayer().resolve(base, OverrideSet())

    assert resolved.suggestions == base
    assert resolved.report.kept == 0
    assert resolved.report.dropped_paths == []


def test_resolve_is_pure() -> None:
    """同样的输入必然产出同样的输出（需求 19.14 的基础）。"""
    base = {"a.pdf": suggestion(("A",)), "b.pdf": suggestion(("B",))}
    overrides = OverrideSet(
        items={"a.pdf": ItemOverride(category_path=("C",))},
        categories={("B",): CategoryOverride(merged_into=("C",))},
    )
    layer = OverrideLayer()

    first = layer.resolve(base, overrides)
    second = layer.resolve(base, overrides)

    assert first.suggestions == second.suggestions
    assert first.included == second.included
    assert first.report.kept == second.report.kept


# ---------------------------------------------------------------------------
# 接进 Planner 后的端到端行为
# ---------------------------------------------------------------------------


def _entries(root: Path, guard: SafetyGuard) -> list:
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    return session.entries()


def test_planner_honours_item_override(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(tmp_path / "下载", {"随便.pdf": "x"})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        items={str(root / "随便.pdf"): ItemOverride(category_path=("我的类目",))}
    )

    result = Planner(guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )

    (item,) = result.plan.all_items()
    assert item.target == root / "我的类目" / "随便.pdf"
    assert result.plan.categories[0].path_parts == ("我的类目",)


def test_planner_honours_included_override(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        items={str(root / "a.pdf"): ItemOverride(included=False)}
    )

    result = Planner(guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )

    (item,) = result.plan.all_items()
    assert item.included is False
    assert result.plan.stats().pending_move == 0


def test_planner_honours_color_override(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        categories={("文档", "PDF"): CategoryOverride(color="#ABCDEF")}
    )

    result = Planner(guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )

    assert result.plan.categories[0].color == "#ABCDEF"


def test_override_survives_rule_change(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 19.3 的核心场景：换了规则库，手工调整还在。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x", "b.pdf": "y"})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        items={str(root / "a.pdf"): ItemOverride(category_path=("钉住不动",))}
    )

    with_default = Planner(guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )
    other_engine = RuleEngine(
        [Rule("x", "extension", 100, (".pdf",), ("完全不同的类目",))]
    )
    with_other = Planner(engine=other_engine, guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )

    def category_of(result: object, name: str) -> tuple[str, ...]:
        plan = result.plan  # type: ignore[attr-defined]
        item = next(i for i in plan.all_items() if i.entry.name == name)
        category = plan.category_by_id(item.category_id)
        return category.path_parts if category else ()

    assert category_of(with_default, "a.pdf") == ("钉住不动",)
    assert category_of(with_other, "a.pdf") == ("钉住不动",)
    assert category_of(with_default, "b.pdf") == ("文档", "PDF")
    assert category_of(with_other, "b.pdf") == ("完全不同的类目",)


def test_override_survives_strategy_change(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        items={str(root / "a.pdf"): ItemOverride(category_path=("钉住",))}
    )
    planner = Planner(guard=guard)

    for strategy in (Strategy.BY_TYPE, Strategy.BY_DATE, Strategy.TYPE_AND_DATE):
        result = planner.build(
            entries,
            root=root,
            strategy=strategy,
            options=ClassifyOptions(strategy=strategy),
            overrides=overrides,
            check_locked=False,
        )
        (item,) = result.plan.all_items()
        assert item.target.parent == root / "钉住", strategy


def test_rebuild_is_stable(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 19.14：配置与范围不变时连续两次重算结果相同。"""
    root = build_tree(tmp_path / "下载", {f"f{i}.pdf": "x" for i in range(10)})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        items={str(root / "f3.pdf"): ItemOverride(category_path=("特别",))}
    )
    planner = Planner(guard=guard)

    first = planner.build(entries, root=root, overrides=overrides, check_locked=False)
    second = planner.build(entries, root=root, overrides=overrides, check_locked=False)

    assert [(i.entry.path, i.target, i.included) for i in first.plan.all_items()] == [
        (i.entry.path, i.target, i.included) for i in second.plan.all_items()
    ]


def test_deleted_category_members_land_in_unclassified(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        categories={("文档", "PDF"): CategoryOverride(deleted=True)}
    )

    result = Planner(guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )

    assert len(result.plan.unclassified) == 1
    assert result.plan.items == []


def test_stale_override_is_reported_by_planner(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _entries(root, guard)
    overrides = OverrideSet(
        items={str(root / "早就没了.pdf"): ItemOverride(category_path=("X",))}
    )

    result = Planner(guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )

    assert result.overrides.dropped_paths == [str(root / "早就没了.pdf")]
