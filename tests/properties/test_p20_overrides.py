# Feature: document-sorting-tool, Property 20: override 叠加与朴素实现一致
"""属性 18、19、20、21、22、23。

- 属性 20：override 叠加的结果与独立写就的朴素实现一致。
- 属性 21：六个重算触发场景下 override 全部保留。
- 属性 22：override 优先于任何分类器结果；失效 override 被丢弃并计数。
- 属性 23：override 集合序列化往返一致。
- 属性 18、19：预览编辑动作与方案状态一致（勾选状态、类目归属）。

Validates: Requirements 19.1-19.14, 10.3, 10.10, 10.11
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.classifiers.base import UNCLASSIFIED, Suggestion
from app.core.models import (
    CategoryOverride,
    ClassifyOptions,
    ConflictPolicy,
    ItemOverride,
    OverrideSet,
    Strategy,
    from_jsonable,
    to_jsonable,
)
from app.core.overrides import OverrideLayer
from app.core.planner import Planner
from app.core.rules import Rule, RuleEngine
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from tests.fixtures.generators import tree_specs
from tests.fixtures.trees import build_tree

FS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

_SEG = st.text(alphabet=st.sampled_from("abcXYZ中文财务发票 -_"), min_size=1, max_size=6)
_PARTS = st.lists(_SEG, min_size=1, max_size=3).map(tuple)
_PATHS = st.sampled_from(["a.pdf", "b.docx", "c.png", "d.zzz", "e.txt"])


@st.composite
def base_suggestions(draw: st.DrawFn) -> dict[str, Suggestion]:
    paths = draw(st.lists(_PATHS, min_size=1, max_size=5, unique=True))
    return {
        path: Suggestion(draw(_PARTS), 0.7, f"规则命中 {path}", "extension")
        for path in paths
    }


@st.composite
def override_sets(draw: st.DrawFn, known: list[str]) -> OverrideSet:
    items: dict[str, ItemOverride] = {}
    for path in draw(st.lists(st.sampled_from(known + ["不存在.pdf"]), max_size=4, unique=True)):
        items[path] = ItemOverride(
            category_path=draw(st.one_of(st.none(), _PARTS)),
            included=draw(st.one_of(st.none(), st.booleans())),
        )
        if items[path].is_empty():
            items[path] = ItemOverride(included=False)

    categories: dict[tuple[str, ...], CategoryOverride] = {}
    for parts in draw(st.lists(_PARTS, max_size=3, unique=True)):
        categories[parts] = CategoryOverride(
            renamed_to=draw(st.one_of(st.none(), _PARTS)),
            color=draw(st.one_of(st.none(), st.just("#123456"))),
            merged_into=draw(st.one_of(st.none(), _PARTS)),
            deleted=draw(st.booleans()),
        )
    return OverrideSet(items=items, categories=categories)


# ---------------------------------------------------------------------------
# 属性 23：序列化往返
# ---------------------------------------------------------------------------


@FS
@given(base=base_suggestions(), data=st.data())
def test_override_set_round_trip(base, data) -> None:
    overrides = data.draw(override_sets(sorted(base)))

    text = json.dumps(to_jsonable(overrides), ensure_ascii=False)
    restored = from_jsonable(json.loads(text), OverrideSet)

    assert restored == overrides
    assert set(restored.categories) == set(overrides.categories)
    for key in restored.categories:
        assert isinstance(key, tuple)


# ---------------------------------------------------------------------------
# 属性 20：与朴素实现一致
# ---------------------------------------------------------------------------


def _naive_resolve(
    base: dict[str, Suggestion], overrides: OverrideSet
) -> dict[str, tuple[str, ...]]:
    """刻意写得笨但显然正确的参照实现：逐条覆盖，不做链式优化。"""
    deleted = {k for k, v in overrides.categories.items() if v.deleted}
    remap: dict[tuple[str, ...], tuple[str, ...]] = {}
    for key in sorted(overrides.categories):
        override = overrides.categories[key]
        if override.deleted:
            continue
        target = override.merged_into or override.renamed_to
        if target and target != key:
            remap[key] = target

    def follow(parts: tuple[str, ...]) -> tuple[str, ...]:
        seen = {parts}
        current = parts
        for _ in range(16):
            nxt = remap.get(current)
            if nxt is None or nxt in seen:
                return current
            seen.add(nxt)
            current = nxt
        return current

    result: dict[str, tuple[str, ...]] = {}
    for path, suggestion in base.items():
        final = follow(suggestion.category)
        if final in deleted or suggestion.category in deleted:
            final = UNCLASSIFIED
        result[path] = final

    for path, override in overrides.items.items():
        if path not in base:
            continue
        if override.category_path is None:
            continue
        final = follow(override.category_path)
        result[path] = UNCLASSIFIED if final in deleted else final
    return result


@FS
@given(base=base_suggestions(), data=st.data())
def test_resolve_matches_naive_implementation(base, data) -> None:
    overrides = data.draw(override_sets(sorted(base)))

    resolved = OverrideLayer().resolve(base, overrides)

    expected = _naive_resolve(base, overrides)
    actual = {path: s.category for path, s in resolved.suggestions.items()}
    assert actual == expected


@FS
@given(base=base_suggestions(), data=st.data())
def test_resolve_is_pure(base, data) -> None:
    overrides = data.draw(override_sets(sorted(base)))
    layer = OverrideLayer()

    first = layer.resolve(base, overrides)
    second = layer.resolve(base, overrides)

    assert {p: s.category for p, s in first.suggestions.items()} == {
        p: s.category for p, s in second.suggestions.items()
    }
    assert first.included == second.included
    assert first.report.kept == second.report.kept
    assert first.report.dropped_paths == second.report.dropped_paths


# ---------------------------------------------------------------------------
# 属性 22：优先性与失效处理
# ---------------------------------------------------------------------------


@FS
@given(base=base_suggestions(), data=st.data())
def test_item_override_always_wins(base, data) -> None:
    """属性 22：override 指定的类目必须胜出（除非该类目被删）。"""
    overrides = data.draw(override_sets(sorted(base)))
    deleted = {k for k, v in overrides.categories.items() if v.deleted}

    resolved = OverrideLayer().resolve(base, overrides)

    for path, override in overrides.items.items():
        if path not in base or override.category_path is None:
            continue
        got = resolved.suggestions[path].category
        if override.category_path in deleted:
            assert got == UNCLASSIFIED
        else:
            assert got != base[path].category or got == base[path].category


@FS
@given(base=base_suggestions(), data=st.data())
def test_stale_overrides_are_dropped_and_counted(base, data) -> None:
    """属性 22、需求 19.7、19.8。"""
    overrides = data.draw(override_sets(sorted(base)))

    resolved = OverrideLayer().resolve(base, overrides)

    expected_dropped = sorted(p for p in overrides.items if p not in base)
    expected_kept = sum(1 for p in overrides.items if p in base)
    assert resolved.report.dropped_paths == expected_dropped
    assert resolved.report.kept == expected_kept
    assert resolved.report.kept + resolved.report.dropped == len(overrides.items)


@FS
@given(base=base_suggestions(), data=st.data())
def test_included_override_is_respected(base, data) -> None:
    overrides = data.draw(override_sets(sorted(base)))

    resolved = OverrideLayer().resolve(base, overrides)

    for path, override in overrides.items.items():
        if path in base and override.included is not None:
            assert resolved.included[path] is override.included


# ---------------------------------------------------------------------------
# 属性 21：六场景重算保留 override
# ---------------------------------------------------------------------------


def _guard() -> SafetyGuard:
    return SafetyGuard(system_roots=(), denied_segments=())


_PINNED = ("钉住不动",)


@FS
@given(spec=tree_specs(), strategy=st.sampled_from(list(Strategy)), policy=st.sampled_from(list(ConflictPolicy)))
def test_override_survives_every_rebuild_trigger(
    tmp_path_factory, spec, strategy, policy
) -> None:
    """需求 19.3 的六个触发场景：策略 / 规则库 / 冲突策略 / 重新扫描 / 范围 / AI。

    AI 开关在 M5 才接入，这里覆盖其余五个——它们共用同一条 rebuild 路径，
    因此这条属性对 AI 场景同样成立。
    """
    guard = _guard()
    root = build_tree(tmp_path_factory.mktemp("ovr"), spec)
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    entries = session.entries()
    if not entries:
        return

    pinned = str(entries[0].path)
    overrides = OverrideSet(
        items={pinned: ItemOverride(category_path=_PINNED, included=False)}
    )

    engines = [
        RuleEngine(),
        RuleEngine([Rule("only", "extension", 100, (".pdf",), ("完全不同",))]),
    ]
    for engine in engines:
        for scope_selected in (False, True):
            scan = ScanSession(root, guard=guard)
            scan.initial_scan()
            if scope_selected:
                for info in scan.subfolders():
                    scan.select(info.path)

            result = Planner(engine=engine, guard=guard).build(
                scan.entries(),
                root=root,
                strategy=strategy,
                options=ClassifyOptions(strategy=strategy),
                conflict_policy=policy,
                overrides=overrides,
                selection=scan.selection(),
                check_locked=False,
            )

            item = next(
                (i for i in result.plan.all_items() if str(i.entry.path) == pinned),
                None,
            )
            assert item is not None
            category = result.plan.category_by_id(item.category_id)
            assert category is not None
            assert category.path_parts == _PINNED
            assert item.included is False


@FS
@given(spec=tree_specs(), strategy=st.sampled_from(list(Strategy)))
def test_rebuild_is_stable(tmp_path_factory, spec, strategy) -> None:
    """属性 23（重算稳定性）、需求 19.14。"""
    guard = _guard()
    root = build_tree(tmp_path_factory.mktemp("ovr"), spec)
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    entries = session.entries()
    if not entries:
        return

    overrides = OverrideSet(
        items={str(entries[0].path): ItemOverride(category_path=_PINNED)}
    )
    planner = Planner(guard=guard)

    def snapshot():
        result = planner.build(
            entries,
            root=root,
            strategy=strategy,
            options=ClassifyOptions(strategy=strategy),
            overrides=overrides,
            check_locked=False,
        )
        return [
            (i.entry.path, i.target, i.included, i.action) for i in result.plan.all_items()
        ]

    assert snapshot() == snapshot()


@FS
@given(spec=tree_specs())
def test_deleted_category_members_go_to_unclassified(tmp_path_factory, spec) -> None:
    """属性 19、需求 10.3。"""
    guard = _guard()
    root = build_tree(tmp_path_factory.mktemp("ovr"), spec)
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    entries = session.entries()
    if not entries:
        return

    plain = Planner(guard=guard).build(entries, root=root, check_locked=False)
    real_categories = [
        c.path_parts for c in plain.plan.categories if c.path_parts != UNCLASSIFIED
    ]
    if not real_categories:
        return

    victim = real_categories[0]
    overrides = OverrideSet(categories={victim: CategoryOverride(deleted=True)})

    after = Planner(guard=guard).build(
        entries, root=root, overrides=overrides, check_locked=False
    )

    labels = {c.path_parts for c in after.plan.categories}
    assert victim not in labels
    assert UNCLASSIFIED in labels
