# Feature: document-sorting-tool, Property 2: 目标路径永不逃逸根目录且形态统一
"""属性 2、7、12、16、17。

- 属性 2：每个 target 要么满足 resolve 后 is_relative_to(root) 且形如
  `root/清洗后类目/文件名`、类目顶层段父目录恰为 root、各段不含非法字符，要么被
  标记 path_escape。
- 属性 7：管线按 priority 求值、采纳第一个达阈值的建议、全落空则 `_未分类`；
  每条结果带 reason 与 [0,1] 的 confidence。
- 属性 12：items 与 unclassified 合起来覆盖全部 FileEntry 且不重复。
- 属性 16：conflict 取值落在封闭集合内且判定确定。
- 属性 17：相同配置重复求值结果相同；执行后重新规划全为 skip（幂等）。

Validates: Requirements 1.4, 1.5, 3.7, 3.8, 3.10, 3.13, 9.1-9.13, 10.11
"""

from __future__ import annotations

import re
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.classifiers.base import UNCLASSIFIED
from app.core.fsops import MAX_PATH_LEN
from app.core.models import ActionKind, ConflictKind, ConflictPolicy
from app.core.planner import Planner
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from tests.fixtures.generators import (
    classify_options,
    conflict_policies,
    tree_specs,
)
from tests.fixtures.trees import build_tree

FS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')


def _guard() -> SafetyGuard:
    return SafetyGuard(system_roots=(), denied_segments=())


@FS
@given(spec=tree_specs(), options=classify_options(), policy=conflict_policies)
def test_targets_never_escape_root(tmp_path_factory, spec, options, policy) -> None:
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()

    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=options.strategy,
        options=options,
        conflict_policy=policy,
        check_locked=False,
    )

    for item in result.plan.all_items():
        if item.conflict is ConflictKind.PATH_ESCAPE:
            continue
        resolved = item.target.resolve()
        assert resolved.is_relative_to(root.resolve()), item.target


@FS
@given(spec=tree_specs(), options=classify_options())
def test_target_shape_is_uniform(tmp_path_factory, spec, options) -> None:
    """形如 root/清洗后类目/文件名，且类目顶层段的父目录恰为 root（需求 9.11）。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()

    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=options.strategy,
        options=options,
        check_locked=False,
    )

    for item in result.plan.all_items():
        if item.conflict in (ConflictKind.PATH_ESCAPE, ConflictKind.PATH_TOO_LONG):
            continue
        relative = item.target.resolve().relative_to(root.resolve())
        assert len(relative.parts) >= 2, item.target
        assert (root / relative.parts[0]).parent == root
        for segment in relative.parts[:-1]:
            assert not ILLEGAL_RE.search(segment), segment


@FS
@given(spec=tree_specs(), options=classify_options())
def test_category_segments_are_sanitised(tmp_path_factory, spec, options) -> None:
    """需求 9.8：逐段清洗非法字符。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()

    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=options.strategy,
        options=options,
        check_locked=False,
    )

    for category in result.plan.categories:
        for segment in category.path_parts:
            assert segment
            assert not ILLEGAL_RE.search(segment), segment
            assert segment == segment.rstrip(". ") or segment == "_"


@FS
@given(spec=tree_specs(), options=classify_options(), policy=conflict_policies)
def test_plan_covers_every_entry_exactly_once(
    tmp_path_factory, spec, options, policy
) -> None:
    """属性 12。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    entries = session.entries()

    result = Planner(guard=guard).build(
        entries,
        root=root,
        strategy=options.strategy,
        options=options,
        conflict_policy=policy,
        check_locked=False,
    )

    covered = [item.entry.path for item in result.plan.all_items()]
    assert sorted(covered) == sorted(e.path for e in entries)
    assert len(covered) == len(set(covered))


@FS
@given(spec=tree_specs(), options=classify_options())
def test_every_item_carries_reason_and_bounded_confidence(
    tmp_path_factory, spec, options
) -> None:
    """属性 7、需求 3.10。reason 决定用户敢不敢点执行，不能为空。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()

    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=options.strategy,
        options=options,
        check_locked=False,
    )

    for item in result.plan.all_items():
        assert item.reason.strip(), item.entry.name
        assert 0.0 <= item.confidence <= 1.0, item.confidence


@FS
@given(spec=tree_specs(), options=classify_options())
def test_unclassified_only_holds_the_fallback_category(
    tmp_path_factory, spec, options
) -> None:
    """需求 3.8：兜底类目与其他类目不混。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()

    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=options.strategy,
        options=options,
        check_locked=False,
    )
    plan = result.plan

    for item in plan.unclassified:
        category = plan.category_by_id(item.category_id)
        assert category is not None and category.path_parts == UNCLASSIFIED
    for item in plan.items:
        category = plan.category_by_id(item.category_id)
        assert category is not None and category.path_parts != UNCLASSIFIED


@FS
@given(spec=tree_specs(), options=classify_options(), policy=conflict_policies)
def test_conflict_domain_is_closed(tmp_path_factory, spec, options, policy) -> None:
    """属性 16。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()

    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=options.strategy,
        options=options,
        conflict_policy=policy,
        check_locked=False,
    )

    for item in result.plan.all_items():
        assert item.conflict in set(ConflictKind)
        if item.conflict is ConflictKind.PATH_TOO_LONG:
            assert len(str(item.target)) > MAX_PATH_LEN
        if item.renamed_from:
            assert item.conflict is ConflictKind.EXISTS


@FS
@given(spec=tree_specs(), options=classify_options(), policy=conflict_policies)
def test_included_targets_are_unique(tmp_path_factory, spec, options, policy) -> None:
    """同批两个文件不能算出同一个目标——否则执行时互相覆盖。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()

    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=options.strategy,
        options=options,
        conflict_policy=policy,
        check_locked=False,
    )

    live = [
        item
        for item in result.plan.all_items()
        if item.included
        and item.action is not ActionKind.SKIP
        and item.conflict
        not in (ConflictKind.PATH_ESCAPE, ConflictKind.PATH_TOO_LONG)
    ]
    if policy is ConflictPolicy.OVERWRITE:
        return  # 覆盖策略下多个源指向同一目标是用户显式选择的语义
    targets = [item.target for item in live]
    assert len(targets) == len(set(targets))


@FS
@given(spec=tree_specs(), options=classify_options(), policy=conflict_policies)
def test_plan_is_deterministic(tmp_path_factory, spec, options, policy) -> None:
    """属性 17、需求 3.13。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    entries = session.entries()
    planner = Planner(guard=guard)

    def snapshot():
        result = planner.build(
            entries,
            root=root,
            strategy=options.strategy,
            options=options,
            conflict_policy=policy,
            check_locked=False,
        )
        return [
            (i.entry.path, i.target, i.action, i.conflict, i.included)
            for i in result.plan.all_items()
        ]

    assert snapshot() == snapshot()


@FS
@given(spec=tree_specs())
def test_replanning_after_execution_is_all_skip(tmp_path_factory, spec) -> None:
    """属性 17 的幂等部分：执行完再规划应全为 skip。"""
    root = build_tree(tmp_path_factory.mktemp("plan"), spec)
    guard = _guard()
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    planner = Planner(guard=guard)
    first = planner.build(session.entries(), root=root, check_locked=False)

    moved: list[Path] = []
    for item in first.plan.all_items():
        if item.action is ActionKind.SKIP or not item.included:
            continue
        if item.conflict in (
            ConflictKind.PATH_ESCAPE,
            ConflictKind.PATH_TOO_LONG,
            ConflictKind.LOCKED,
        ):
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.entry.path.replace(item.target)
        moved.append(item.target)

    if not moved:
        return

    second_session = ScanSession(root, guard=guard)
    second_session.initial_scan()
    for folder in {m.parent for m in moved}:
        second_session.select(folder)
    second = planner.build(second_session.entries(), root=root, check_locked=False)

    assert all(i.action is ActionKind.SKIP for i in second.plan.all_items())
