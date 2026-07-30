"""领域模型与统一编解码器的例子级测试。

正式的属性测试分散在各自任务里（属性 9 规则往返、23 override 往返、28 journal
往返、29 manifest 往返、30 配置往返）。本文件只钉住编解码器本身的关键行为与
边界，尤其是几个「错了会静默失效」的点：

    StrEnum 必须被解码回枚举而不是留作字符串
    Path 必须被解码回 Path 而不是留作字符串
    tuple 必须被解码回 tuple 而不是留作 list
    以元组为 key 的字典不能被拼接成字符串 key

最后一条是重点：类目名本身可能含 "/"，一旦拼接就无法区分 ("a/b",) 与
("a", "b")，override 会挂到错误的类目上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.models import (
    CSV_FIELDNAMES,
    ActionKind,
    Category,
    CategoryOverride,
    ConflictKind,
    ConflictPolicy,
    ExecutionReport,
    FileEntry,
    ItemOverride,
    JournalRecord,
    Manifest,
    Outcome,
    OverrideSet,
    PlanItem,
    RecordKind,
    ResultRow,
    RunMeta,
    RunStatus,
    ScanScope,
    ScanSelection,
    SortPlan,
    SourceSnapshotEntry,
    Strategy,
    from_jsonable,
    to_jsonable,
)

ROOT = Path(r"D:\下载")


def _entry(name: str = "报告.pdf", size: int = 1234) -> FileEntry:
    return FileEntry(
        path=ROOT / name,
        name=name,
        ext=".pdf",
        size=size,
        mtime=1_700_000_000.5,
        is_hidden=False,
        depth=1,
    )


def _plan_item(entry: FileEntry, category: Category) -> PlanItem:
    return PlanItem(
        entry=entry,
        category_id=category.id,
        target=ROOT / "文档" / "PDF" / entry.name,
        action=ActionKind.MOVE,
        conflict=ConflictKind.NONE,
        confidence=0.95,
        reason="扩展名 .pdf → 文档/PDF",
    )


def _roundtrip(obj: object, cls: type) -> object:
    """经过真实 JSON 文本，而不是只在内存里转一圈。

    只做 to_jsonable -> from_jsonable 会漏掉「编码结果不是合法 JSON」这类问题，
    而 journal 与 manifest 都是要落盘的。
    """
    encoded = to_jsonable(obj)
    text = json.dumps(encoded, ensure_ascii=False)
    return from_jsonable(json.loads(text), cls)


# ---------------------------------------------------------------------------
# 基础类型转换
# ---------------------------------------------------------------------------


def test_strenum_encodes_to_plain_string() -> None:
    assert to_jsonable(ActionKind.MOVE) == "move"
    assert json.dumps(to_jsonable(ActionKind.MOVE)) == '"move"'


def test_strenum_decodes_back_to_enum_not_str() -> None:
    decoded = from_jsonable("move", ActionKind)
    assert decoded is ActionKind.MOVE
    assert isinstance(decoded, ActionKind)


def test_path_roundtrip_keeps_path_type() -> None:
    decoded = from_jsonable(to_jsonable(ROOT), Path)
    assert decoded == ROOT
    assert isinstance(decoded, Path)


def test_unencodable_type_raises() -> None:
    class Weird:
        pass

    with pytest.raises(TypeError):
        to_jsonable(Weird())


# ---------------------------------------------------------------------------
# FileEntry / PlanItem / SortPlan
# ---------------------------------------------------------------------------


def test_file_entry_roundtrip() -> None:
    entry = _entry()
    entry.text_head = "发票代码 011002000111"
    entry.error = None

    restored = _roundtrip(entry, FileEntry)
    assert restored == entry
    assert isinstance(restored.path, Path)


def test_sort_plan_roundtrip_preserves_tuples_and_enums() -> None:
    category = Category.create(("财务", "发票"), "#2563EB", "rule:fin_invoice")
    entry = _entry("发票_2024.pdf")
    plan = SortPlan(
        root=ROOT,
        strategy=Strategy.SMART,
        categories=[category],
        items=[_plan_item(entry, category)],
        unclassified=[],
        scope=ScanScope.SELECTED_SUBFOLDERS,
        selected_subfolders=(ROOT / "旧资料", ROOT / "微信文件"),
    )

    restored = _roundtrip(plan, SortPlan)

    assert restored == plan
    assert restored.strategy is Strategy.SMART
    assert restored.scope is ScanScope.SELECTED_SUBFOLDERS
    assert isinstance(restored.selected_subfolders, tuple)
    assert isinstance(restored.categories[0].path_parts, tuple)
    assert isinstance(restored.items[0].target, Path)


def test_plan_stats_counts() -> None:
    cat = Category.create(("文档", "PDF"), "#2563EB", "extension")
    keep = _plan_item(_entry("a.pdf"), cat)
    excluded = _plan_item(_entry("b.pdf"), cat)
    excluded.included = False
    conflicted = _plan_item(_entry("c.pdf"), cat)
    conflicted.conflict = ConflictKind.EXISTS
    skipped = _plan_item(_entry("d.pdf"), cat)
    skipped.action = ActionKind.SKIP
    unclassified = _plan_item(_entry("e.bin"), cat)

    plan = SortPlan(
        root=ROOT,
        strategy=Strategy.BY_TYPE,
        categories=[cat],
        items=[keep, excluded, conflicted, skipped],
        unclassified=[unclassified],
    )
    stats = plan.stats()

    assert stats.total_files == 5
    assert stats.category_count == 1
    # keep / conflicted / unclassified 计入待移动；excluded 被排除，skipped 是 skip
    assert stats.pending_move == 3
    assert stats.conflict_count == 1
    assert stats.unclassified_count == 1


# ---------------------------------------------------------------------------
# Category id 的稳定性与无歧义
# ---------------------------------------------------------------------------


def test_category_id_is_stable() -> None:
    assert Category.make_id(("财务", "发票")) == Category.make_id(("财务", "发票"))


def test_category_id_distinguishes_segment_boundary() -> None:
    """("财务/发票",) 与 ("财务", "发票") 必须是不同的类目。

    用 "/" 连接段来生成 id 就会让这两者撞在一起——而类目名确实可能含 "/"。
    """
    assert Category.make_id(("财务/发票",)) != Category.make_id(("财务", "发票"))


# ---------------------------------------------------------------------------
# OverrideSet：非字符串 key 的字典
# ---------------------------------------------------------------------------


def test_override_set_roundtrip_with_tuple_keys() -> None:
    overrides = OverrideSet(
        items={
            str(ROOT / "发票_2024.pdf"): ItemOverride(
                category_path=("财务", "发票"), included=None
            ),
            str(ROOT / "废弃.tmp"): ItemOverride(included=False),
        },
        categories={
            ("财务", "发票"): CategoryOverride(
                renamed_to=("财务", "增值税发票"), color="#16A34A"
            ),
            ("截图",): CategoryOverride(deleted=True),
        },
    )

    restored = _roundtrip(overrides, OverrideSet)

    assert restored == overrides
    assert set(restored.categories) == {("财务", "发票"), ("截图",)}
    for key in restored.categories:
        assert isinstance(key, tuple)
    assert restored.categories[("财务", "发票")].renamed_to == ("财务", "增值税发票")


def test_override_tuple_keys_are_not_joined_into_strings() -> None:
    """编码后类目 key 必须仍是段列表，不能被拼成字符串。"""
    overrides = OverrideSet(
        categories={("财务/发票",): CategoryOverride(color="#DC2626")}
    )
    encoded = to_jsonable(overrides)

    assert isinstance(encoded["categories"], list)
    key, _value = encoded["categories"][0]
    assert key == ["财务/发票"]


def test_override_set_distinguishes_ambiguous_category_paths() -> None:
    overrides = OverrideSet(
        categories={
            ("财务/发票",): CategoryOverride(color="#DC2626"),
            ("财务", "发票"): CategoryOverride(color="#16A34A"),
        }
    )
    restored = _roundtrip(overrides, OverrideSet)

    assert len(restored.categories) == 2
    assert restored.categories[("财务/发票",)].color == "#DC2626"
    assert restored.categories[("财务", "发票")].color == "#16A34A"


def test_set_item_drops_empty_override() -> None:
    overrides = OverrideSet()
    overrides.set_item(ROOT / "a.pdf", ItemOverride())
    assert overrides.is_empty()

    overrides.set_item(ROOT / "a.pdf", ItemOverride(included=False))
    assert len(overrides) == 1
    assert overrides.item_for(ROOT / "a.pdf") == ItemOverride(included=False)


# ---------------------------------------------------------------------------
# JournalRecord / Manifest
# ---------------------------------------------------------------------------


def test_journal_record_roundtrip_with_optional_fields_absent() -> None:
    record = JournalRecord(
        seq=7,
        ts="2026-07-27T10:00:00+08:00",
        kind=RecordKind.DONE,
        op=ActionKind.MOVE,
        src=str(ROOT / "a.pdf"),
        dst=str(ROOT / "文档" / "PDF" / "a.pdf"),
        size=1234,
        mtime=1_700_000_000.5,
        sha256="ab" * 32,
    )
    restored = _roundtrip(record, JournalRecord)

    assert restored == record
    assert restored.kind is RecordKind.DONE
    assert restored.op is ActionKind.MOVE
    assert restored.error is None
    assert restored.extra == {}


def test_journal_record_extra_passes_through_untyped() -> None:
    record = JournalRecord(
        seq=1,
        ts="2026-07-27T10:00:00+08:00",
        kind=RecordKind.REMOVED_DIR,
        extra={"dir": str(ROOT / "旧资料"), "depth": 2, "nested": [1, {"k": "v"}]},
    )
    restored = _roundtrip(record, JournalRecord)
    assert restored.extra == record.extra


def test_created_dir_and_removed_dir_are_distinct_kinds() -> None:
    """两者撤销语义相反，绝不能混为一谈（需求 20.19）。"""
    assert RecordKind.CREATED_DIR != RecordKind.REMOVED_DIR
    assert from_jsonable("created_dir", RecordKind) is RecordKind.CREATED_DIR
    assert from_jsonable("removed_dir", RecordKind) is RecordKind.REMOVED_DIR


def test_manifest_roundtrip() -> None:
    category = Category.create(("文档", "PDF"), "#2563EB", "extension")
    entry = _entry()
    manifest = Manifest(
        run_id="20260727-100000-abcd",
        created_at="2026-07-27T10:00:00+08:00",
        root=str(ROOT),
        strategy=Strategy.BY_TYPE,
        scope=ScanScope.SELECTED_SUBFOLDERS,
        selected_subfolders=(str(ROOT / "旧资料"),),
        conflict_policy=ConflictPolicy.AUTO_RENAME,
        remove_empty_dirs=True,
        plan=SortPlan(
            root=ROOT,
            strategy=Strategy.BY_TYPE,
            categories=[category],
            items=[_plan_item(entry, category)],
            unclassified=[],
            scope=ScanScope.SELECTED_SUBFOLDERS,
            selected_subfolders=(ROOT / "旧资料",),
        ),
        source_snapshot=[
            SourceSnapshotEntry(path=str(entry.path), size=entry.size, mtime=entry.mtime)
        ],
        overrides=OverrideSet(
            items={str(entry.path): ItemOverride(included=False)},
            categories={("文档", "PDF"): CategoryOverride(color="#F59E0B")},
        ),
        app_version="0.1.0",
    )

    restored = _roundtrip(manifest, Manifest)

    assert restored == manifest
    assert restored.remove_empty_dirs is True
    assert restored.scope is ScanScope.SELECTED_SUBFOLDERS
    assert isinstance(restored.selected_subfolders, tuple)
    assert restored.overrides.categories[("文档", "PDF")].color == "#F59E0B"


def test_run_meta_undoable_states() -> None:
    def meta(status: RunStatus) -> RunMeta:
        return RunMeta(
            run_id="r",
            started_at="2026-07-27T10:00:00+08:00",
            root=str(ROOT),
            strategy=Strategy.BY_TYPE,
            file_count=3,
            status=status,
        )

    assert meta(RunStatus.COMPLETED).is_undoable()
    assert meta(RunStatus.FAILED).is_undoable()
    assert meta(RunStatus.UNFINISHED).is_undoable()
    assert not meta(RunStatus.UNDONE).is_undoable()
    assert not meta(RunStatus.RUNNING).is_undoable()


# ---------------------------------------------------------------------------
# ScanSelection
# ---------------------------------------------------------------------------


def test_scan_selection_scope_follows_emptiness() -> None:
    selection = ScanSelection(root=ROOT)
    assert selection.scope() is ScanScope.TOP_LEVEL_ONLY

    selection.selected.add(ROOT / "旧资料")
    assert selection.scope() is ScanScope.SELECTED_SUBFOLDERS

    selection.selected.clear()
    assert selection.scope() is ScanScope.TOP_LEVEL_ONLY


def test_scan_selection_closure_excludes_root() -> None:
    """根目录在任何配置下都不是空目录清理的候选（需求 20.14）。"""
    selection = ScanSelection(root=ROOT, selected={ROOT / "旧资料"})
    closure = selection.selected_closure()

    assert closure == frozenset({ROOT / "旧资料"})
    assert ROOT not in closure


def test_scan_selection_roundtrip() -> None:
    selection = ScanSelection(root=ROOT, selected={ROOT / "b", ROOT / "a"})
    restored = _roundtrip(selection, ScanSelection)

    assert restored.root == ROOT
    assert restored.selected == selection.selected
    assert isinstance(restored.selected, set)


def test_set_encoding_is_order_stable() -> None:
    """集合无序，但落盘内容必须稳定，否则同一份数据两次写出的文件不同。"""
    a = ScanSelection(root=ROOT, selected={ROOT / "a", ROOT / "b", ROOT / "c"})
    b = ScanSelection(root=ROOT, selected={ROOT / "c", ROOT / "a", ROOT / "b"})

    assert to_jsonable(a) == to_jsonable(b)


# ---------------------------------------------------------------------------
# ExecutionReport 与 CSV 导出
# ---------------------------------------------------------------------------


def _row(name: str, outcome: Outcome) -> ResultRow:
    return ResultRow(
        src=str(ROOT / name),
        dst=str(ROOT / "文档" / name),
        action=ActionKind.MOVE,
        outcome=outcome,
        reason="扩展名 .pdf → 文档/PDF",
    )


def test_csv_row_count_equals_sum_of_three_groups() -> None:
    """需求 16.5。这里是恒等式而非巧合：to_csv_rows 就是三组的顺序拼接。"""
    report = ExecutionReport(
        succeeded=[_row("a.pdf", Outcome.SUCCEEDED), _row("b.pdf", Outcome.SUCCEEDED)],
        skipped=[_row("c.pdf", Outcome.SKIPPED)],
        failed=[_row("d.pdf", Outcome.FAILED)],
    )
    rows = report.to_csv_rows()

    assert len(rows) == report.total() == 4
    assert len(rows) == sum(report.counts().values())


def test_csv_rows_use_declared_fieldnames() -> None:
    report = ExecutionReport(succeeded=[_row("a.pdf", Outcome.SUCCEEDED)])
    (row,) = report.to_csv_rows()

    assert tuple(row) == CSV_FIELDNAMES
    assert row["动作"] == "move"
    assert row["结果"] == "succeeded"


def test_empty_report_exports_no_rows() -> None:
    report = ExecutionReport()
    assert report.to_csv_rows() == []
    assert report.total() == 0
