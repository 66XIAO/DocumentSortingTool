"""分类管线、冲突判定与 Planner 的测试。

属性 2、7、8、9、10、16、17 的 Hypothesis 版本在任务 21。本文件覆盖具体行为，
重点在几个「写错不会报错、只会静默产出错方案」的地方：五态判定顺序、幂等 skip、
同批目标撞车、小类目合并与 override 的先后。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.classifiers.base import (
    OTHER,
    UNCLASSIFIED,
    ClassifierPipeline,
    ClassifyContext,
    Suggestion,
    build_pipeline,
)
from app.core.classifiers.by_date import DateClassifier, date_parts
from app.core.classifiers.by_extension import ExtensionClassifier
from app.core.classifiers.by_filename import FilenameClassifier
from app.core.conflicts import TargetAllocator, is_locked
from app.core.models import (
    ActionKind,
    ClassifyOptions,
    ConflictKind,
    ConflictPolicy,
    DateGranularity,
    FileEntry,
    ScanSelection,
    Strategy,
)
from app.core.planner import EmptyDirPredictor, Planner
from app.core.rules import Rule, RuleEngine
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from tests.fixtures.trees import build_tree

# 2024-03-15 10:00:00 本地时间附近的时间戳
MTIME = 1_710_468_000.0


@pytest.fixture
def guard() -> SafetyGuard:
    return SafetyGuard(system_roots=(), denied_segments=())


@pytest.fixture
def engine() -> RuleEngine:
    return RuleEngine()


def entry(name: str, root: Path = Path("D:/下载"), size: int = 100, mtime: float = MTIME) -> FileEntry:
    from app.core.scanner import _split_ext

    return FileEntry(
        path=root / name,
        name=name,
        ext=_split_ext(name),
        size=size,
        mtime=mtime,
        is_hidden=False,
        depth=1,
    )


# ---------------------------------------------------------------------------
# 分类器
# ---------------------------------------------------------------------------


def test_filename_beats_extension(engine: RuleEngine) -> None:
    """需求 3.9：发票.pdf 应进「财务/发票」而不是「文档/PDF」。"""
    pipeline = ClassifierPipeline(
        [FilenameClassifier(engine), ExtensionClassifier(engine)]
    )

    suggestion = pipeline.classify(entry("2024发票.pdf"), ClassifyContext())

    assert suggestion.category == ("财务", "发票")
    assert suggestion.source == "filename"


def test_extension_used_when_filename_has_no_hit(engine: RuleEngine) -> None:
    pipeline = ClassifierPipeline(
        [FilenameClassifier(engine), ExtensionClassifier(engine)]
    )

    suggestion = pipeline.classify(entry("随便起的名字.pdf"), ClassifyContext())

    assert suggestion.category == ("文档", "PDF")
    assert suggestion.source == "extension"


def test_unclassified_when_nothing_matches(engine: RuleEngine) -> None:
    """需求 3.8。"""
    pipeline = ClassifierPipeline(
        [FilenameClassifier(engine), ExtensionClassifier(engine)]
    )

    suggestion = pipeline.classify(entry("neko.zzz"), ClassifyContext())

    assert suggestion.category == UNCLASSIFIED
    assert suggestion.confidence == 0.0
    assert suggestion.reason


def test_every_suggestion_carries_a_reason(engine: RuleEngine) -> None:
    """需求 3.10：reason 决定用户敢不敢点执行，不能为空。"""
    pipeline = ClassifierPipeline(
        [FilenameClassifier(engine), ExtensionClassifier(engine)]
    )
    for name in ("发票.pdf", "随便.pdf", "未知.zzz", "Screenshot_1.png"):
        suggestion = pipeline.classify(entry(name), ClassifyContext())
        assert suggestion.reason.strip()
        assert 0.0 <= suggestion.confidence <= 1.0


def test_threshold_rejects_low_confidence(engine: RuleEngine) -> None:
    """需求 3.7：低于阈值的建议不被采纳，且理由说明为什么。"""
    pipeline = ClassifierPipeline([ExtensionClassifier(engine)])
    ctx = ClassifyContext(options=ClassifyOptions(min_confidence=0.95))

    suggestion = pipeline.classify(entry("a.pdf"), ctx)

    assert suggestion.category == UNCLASSIFIED
    assert "低于阈值" in suggestion.reason


def test_register_orders_by_priority(engine: RuleEngine) -> None:
    """需求 3.11。"""
    pipeline = ClassifierPipeline()
    pipeline.register(ExtensionClassifier(engine))
    pipeline.register(FilenameClassifier(engine))

    assert pipeline.names() == ("filename", "extension")


def test_pipeline_is_deterministic(engine: RuleEngine) -> None:
    """需求 3.13。"""
    pipeline = ClassifierPipeline(
        [FilenameClassifier(engine), ExtensionClassifier(engine)]
    )
    e = entry("发票.pdf")

    first = [pipeline.classify(e, ClassifyContext()) for _ in range(5)]

    assert len(set(first)) == 1


def test_build_pipeline_skips_missing_classifiers(engine: RuleEngine) -> None:
    """SMART 策略下 content 与 llm 可能缺席，管线应自然退化。需求 6.4、6.6。"""
    available = {
        "filename": FilenameClassifier(engine),
        "extension": ExtensionClassifier(engine),
    }

    pipeline = build_pipeline(Strategy.SMART, available)

    assert pipeline.names() == ("filename", "extension")


@pytest.mark.parametrize(
    ("granularity", "expected"),
    [
        (DateGranularity.YEAR, ("2024",)),
        (DateGranularity.YEAR_MONTH, ("2024", "2024-03")),
    ],
)
def test_date_parts(granularity: DateGranularity, expected: tuple[str, ...]) -> None:
    assert date_parts(MTIME, granularity) == expected


def test_date_classifier_does_not_read_the_clock() -> None:
    """确定性的前提：只依赖 mtime。"""
    classifier = DateClassifier()
    ctx = ClassifyContext()

    a = classifier.classify(entry("a.txt"), ctx)
    b = classifier.classify(entry("a.txt"), ctx)

    assert a == b
    assert a is not None
    assert a.confidence == 1.0


def test_date_classifier_tolerates_absurd_mtime() -> None:
    classifier = DateClassifier()

    suggestion = classifier.classify(entry("a.txt", mtime=-1e18), ClassifyContext())

    assert suggestion is not None


# ---------------------------------------------------------------------------
# 冲突判定
# ---------------------------------------------------------------------------


def test_none_conflict_for_free_target(tmp_path: Path, guard: SafetyGuard) -> None:
    src = tmp_path / "a.pdf"
    src.write_text("x", encoding="utf-8")
    allocator = TargetAllocator(root=tmp_path, guard=guard)

    target, conflict, renamed = allocator.allocate(
        src, tmp_path / "文档", "a.pdf", ConflictPolicy.AUTO_RENAME
    )

    assert conflict is ConflictKind.NONE
    assert target == tmp_path / "文档" / "a.pdf"
    assert renamed is None


def test_path_escape_beats_everything(tmp_path: Path, guard: SafetyGuard) -> None:
    """逃出根目录是「路径根本不可用」，不该被改名策略掩盖。"""
    src = tmp_path / "a.pdf"
    src.write_text("x", encoding="utf-8")
    allocator = TargetAllocator(root=tmp_path / "root", guard=guard)

    _, conflict, _ = allocator.allocate(
        src, tmp_path / "外面", "a.pdf", ConflictPolicy.AUTO_RENAME
    )

    assert conflict is ConflictKind.PATH_ESCAPE


def test_path_too_long_is_detected(tmp_path: Path, guard: SafetyGuard) -> None:
    src = tmp_path / "a.pdf"
    src.write_text("x", encoding="utf-8")
    allocator = TargetAllocator(root=tmp_path, guard=guard)
    deep = tmp_path / ("长" * 200)

    _, conflict, _ = allocator.allocate(
        src, deep, "a.pdf", ConflictPolicy.AUTO_RENAME
    )

    assert conflict is ConflictKind.PATH_TOO_LONG


def test_exists_triggers_auto_rename(tmp_path: Path, guard: SafetyGuard) -> None:
    src = tmp_path / "a.pdf"
    src.write_text("x", encoding="utf-8")
    category = tmp_path / "文档"
    category.mkdir()
    (category / "a.pdf").write_text("占位", encoding="utf-8")
    allocator = TargetAllocator(root=tmp_path, guard=guard)

    target, conflict, renamed = allocator.allocate(
        src, category, "a.pdf", ConflictPolicy.AUTO_RENAME
    )

    assert conflict is ConflictKind.EXISTS
    assert target.name == "a (2).pdf"
    assert renamed == "a.pdf"


def test_exists_with_skip_policy_keeps_original_name(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    src = tmp_path / "a.pdf"
    src.write_text("x", encoding="utf-8")
    category = tmp_path / "文档"
    category.mkdir()
    (category / "a.pdf").write_text("占位", encoding="utf-8")
    allocator = TargetAllocator(root=tmp_path, guard=guard)

    target, conflict, renamed = allocator.allocate(
        src, category, "a.pdf", ConflictPolicy.SKIP
    )

    assert conflict is ConflictKind.EXISTS
    assert target.name == "a.pdf"
    assert renamed is None


def test_same_batch_collision_is_avoided(tmp_path: Path, guard: SafetyGuard) -> None:
    """同批两个源文件算出同一目标时，只查磁盘会撞车。"""
    src_a = tmp_path / "x" / "a.pdf"
    src_b = tmp_path / "y" / "a.pdf"
    for src in (src_a, src_b):
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("x", encoding="utf-8")
    allocator = TargetAllocator(root=tmp_path, guard=guard)
    category = tmp_path / "文档"

    first, _, _ = allocator.allocate(src_a, category, "a.pdf", ConflictPolicy.AUTO_RENAME)
    second, conflict, renamed = allocator.allocate(
        src_b, category, "a.pdf", ConflictPolicy.AUTO_RENAME
    )

    assert first != second
    assert second.name == "a (2).pdf"
    assert conflict is ConflictKind.EXISTS
    assert renamed == "a.pdf"


def test_is_locked_on_normal_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("x", encoding="utf-8")

    assert is_locked(path) is False


def test_is_locked_on_missing_file(tmp_path: Path) -> None:
    assert is_locked(tmp_path / "不存在") is True


def test_conflict_kind_domain_is_closed(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 9.3：取值必须落在封闭集合内。"""
    src = tmp_path / "a.pdf"
    src.write_text("x", encoding="utf-8")
    allocator = TargetAllocator(root=tmp_path, guard=guard)

    _, conflict, _ = allocator.allocate(
        src, tmp_path / "文档", "a.pdf", ConflictPolicy.AUTO_RENAME
    )

    assert conflict in set(ConflictKind)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _scan(root: Path, guard: SafetyGuard) -> list[FileEntry]:
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    return session.entries()


def test_plan_covers_every_entry(tmp_path: Path, guard: SafetyGuard) -> None:
    """属性 12：items 与 unclassified 合起来覆盖全部 FileEntry 且不重复。"""
    root = build_tree(
        tmp_path / "下载",
        {"发票.pdf": "a", "报告.docx": "b", "未知.zzz": "c", "图.png": "d"},
    )
    entries = _scan(root, guard)

    result = Planner(guard=guard).build(entries, root=root, check_locked=False)
    covered = [i.entry.path for i in result.plan.all_items()]

    assert sorted(covered) == sorted(e.path for e in entries)
    assert len(covered) == len(set(covered))


def test_targets_land_directly_under_root(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 9.11：类目目录是根目录的直接子文件夹。

    这条让已归类文件在 top_level_only 范围下天然落在扫描范围之外，重复整理不会
    再搬一次。
    """
    root = build_tree(tmp_path / "下载", {"发票.pdf": "a", "报告.docx": "b"})
    entries = _scan(root, guard)

    result = Planner(guard=guard).build(entries, root=root, check_locked=False)

    for item in result.plan.all_items():
        assert item.target.is_relative_to(root)
        top = item.target.relative_to(root).parts[0]
        assert (root / top).parent == root


def test_category_names_are_sanitised(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 9.8：非法字符逐段替换。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _scan(root, guard)
    engine = RuleEngine(
        [Rule("bad", "extension", 100, (".pdf",), ('非:法*名"字',))]
    )

    result = Planner(engine=engine, guard=guard).build(
        entries, root=root, check_locked=False
    )

    (category,) = result.plan.categories
    assert category.path_parts == ("非_法_名_字",)
    assert not any(ch in category.path_parts[0] for ch in r'\/:*?"<>|')


def test_idempotent_skip_when_already_in_place(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    """需求 9.12：文件已在目标类目目录里就跳过。"""
    root = build_tree(tmp_path / "下载", {"文档": {"PDF": {"a.pdf": "x"}}})
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    session.select(root / "文档" / "PDF")

    result = Planner(guard=guard).build(
        session.entries(), root=root, check_locked=False
    )

    (item,) = result.plan.all_items()
    assert item.action is ActionKind.SKIP


def test_replanning_after_execution_is_all_skip(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    """属性 17 幂等性的例子级版本。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _scan(root, guard)
    planner = Planner(guard=guard)
    first = planner.build(entries, root=root, check_locked=False)

    # 模拟执行：把文件搬到目标位置
    (item,) = first.plan.all_items()
    item.target.parent.mkdir(parents=True, exist_ok=True)
    item.entry.path.rename(item.target)

    session = ScanSession(root, guard=guard)
    session.initial_scan()
    session.select(item.target.parent)
    second = planner.build(session.entries(), root=root, check_locked=False)

    assert all(i.action is ActionKind.SKIP for i in second.plan.all_items())


def test_strategy_by_date(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _scan(root, guard)

    result = Planner(guard=guard).build(
        entries,
        root=root,
        strategy=Strategy.BY_DATE,
        options=ClassifyOptions(
            strategy=Strategy.BY_DATE, date_granularity=DateGranularity.YEAR
        ),
        check_locked=False,
    )

    (category,) = result.plan.categories
    assert len(category.path_parts) == 1
    assert category.path_parts[0].isdigit()


def test_strategy_type_and_date_is_two_levels(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    """需求 3.5：第一级类型、第二级时间。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _scan(root, guard)

    result = Planner(guard=guard).build(
        entries,
        root=root,
        strategy=Strategy.TYPE_AND_DATE,
        options=ClassifyOptions(strategy=Strategy.TYPE_AND_DATE),
        check_locked=False,
    )

    (category,) = result.plan.categories
    assert category.path_parts[:2] == ("文档", "PDF")
    assert len(category.path_parts) == 3


def test_small_categories_merge_into_other(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 3.12。"""
    root = build_tree(
        tmp_path / "下载",
        {"a.pdf": "1", "b.pdf": "2", "c.pdf": "3", "只有一个.png": "4"},
    )
    entries = _scan(root, guard)

    result = Planner(guard=guard).build(
        entries,
        root=root,
        options=ClassifyOptions(merge_small_categories=True, small_category_threshold=3),
        check_locked=False,
    )
    labels = {"/".join(c.path_parts) for c in result.plan.categories}

    assert "文档/PDF" in labels
    assert "/".join(OTHER) in labels
    assert "图片" not in labels


def test_unclassified_never_merges_into_other(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    """未分类并进「其他」只会让用户更难找到没被识别的文件。"""
    root = build_tree(tmp_path / "下载", {"孤零零.zzz": "x"})
    entries = _scan(root, guard)

    result = Planner(guard=guard).build(
        entries,
        root=root,
        options=ClassifyOptions(merge_small_categories=True, small_category_threshold=5),
        check_locked=False,
    )

    assert len(result.plan.unclassified) == 1
    labels = {"/".join(c.path_parts) for c in result.plan.categories}
    assert "/".join(UNCLASSIFIED) in labels


def test_plan_is_deterministic(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(
        tmp_path / "下载", {f"f{i}.pdf": "x" for i in range(20)}
    )
    entries = _scan(root, guard)
    planner = Planner(guard=guard)

    a = planner.build(entries, root=root, check_locked=False).plan
    b = planner.build(entries, root=root, check_locked=False).plan

    assert [(i.entry.path, i.target, i.action) for i in a.all_items()] == [
        (i.entry.path, i.target, i.action) for i in b.all_items()
    ]


def test_stats_reflect_plan(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(
        tmp_path / "下载", {"发票.pdf": "a", "报告.docx": "b", "未知.zzz": "c"}
    )
    entries = _scan(root, guard)

    stats = Planner(guard=guard).build(entries, root=root, check_locked=False).plan.stats()

    assert stats.total_files == 3
    assert stats.unclassified_count == 1
    assert stats.pending_move == 3


def test_empty_entry_list_yields_empty_plan(tmp_path: Path, guard: SafetyGuard) -> None:
    root = build_tree(tmp_path / "下载", {})

    result = Planner(guard=guard).build([], root=root, check_locked=False)

    assert result.plan.all_items() == []
    assert result.plan.categories == []
    assert result.space.ok


def test_selection_is_recorded_in_plan(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 13.1 要求 manifest 记住扫描范围，Plan 得先带上它。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x", "子": {"b.pdf": "y"}})
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    session.select(root / "子")

    result = Planner(guard=guard).build(
        session.entries(), root=root, selection=session.selection(), check_locked=False
    )

    assert result.plan.selected_subfolders == (root / "子",)
    assert result.plan.scope.value == "selected_subfolders"


# ---------------------------------------------------------------------------
# EmptyDirPredictor
# ---------------------------------------------------------------------------


def test_predictor_returns_empty_without_selection(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    """需求 20.5、20.10：两层勾选缺一层，候选集合就是空的。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    entries = _scan(root, guard)
    plan = Planner(guard=guard).build(entries, root=root, check_locked=False).plan

    predicted = EmptyDirPredictor().predict(plan, ScanSelection(root=root))

    assert predicted == []


def test_predictor_finds_folder_emptied_by_this_run(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    root = build_tree(tmp_path / "下载", {"子": {"a.pdf": "x"}})
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    session.select(root / "子")
    plan = Planner(guard=guard).build(
        session.entries(), root=root, selection=session.selection(), check_locked=False
    ).plan

    predicted = EmptyDirPredictor().predict(plan, session.selection())

    assert predicted == [root / "子"]


def test_predictor_skips_folder_that_keeps_a_file(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    root = build_tree(tmp_path / "下载", {"子": {"a.pdf": "x", "留下": {"b.txt": "y"}}})
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    session.select(root / "子")
    plan = Planner(guard=guard).build(
        session.entries(), root=root, selection=session.selection(), check_locked=False
    ).plan

    predicted = EmptyDirPredictor().predict(plan, session.selection())

    assert predicted == []


def test_predictor_never_returns_root(tmp_path: Path, guard: SafetyGuard) -> None:
    """需求 20.14。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x"})
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    selection = session.selection()
    selection.selected.add(root)
    plan = Planner(guard=guard).build(
        session.entries(), root=root, selection=selection, check_locked=False
    ).plan

    assert root not in EmptyDirPredictor().predict(plan, selection)


def test_predictor_skips_folder_already_empty_before_run(
    tmp_path: Path, guard: SafetyGuard
) -> None:
    """需求 20.9：执行前就已为空的目录不在删除范围。"""
    root = build_tree(tmp_path / "下载", {"a.pdf": "x", "本来就空": {}})
    session = ScanSession(root, guard=guard)
    session.initial_scan()
    session.select(root / "本来就空")
    plan = Planner(guard=guard).build(
        session.entries(), root=root, selection=session.selection(), check_locked=False
    ).plan

    assert EmptyDirPredictor().predict(plan, session.selection()) == []
