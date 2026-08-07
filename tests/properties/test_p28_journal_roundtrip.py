# Feature: document-sorting-tool, Property 28: journal 记录序列化往返一致性
"""属性 28、29。

- 属性 28：对任意 JournalRecord 列表，写入 JSONL 后再解析产出等价的记录列表，
  seq 顺序与全部字段取值都被保留。
- 属性 29：对任意 SortPlan 与 Manifest（含扫描范围、勾选子文件夹清单、源树快照与
  override 集合），写入 manifest.json 后再读取产出等价的对象。

这两条是撤销可靠性的**读侧**前提。写得再全，读不回来就等于没写。

## 为什么生成器要刻意包含 U+2028

Windows 的文件名只禁 ``\\ / : * ? " < > |`` 和控制字符，U+2028（行分隔符）、U+2029、
U+0085 都是合法的文件名字符。而 ``json.dumps(ensure_ascii=False)`` 不转义它们，
``str.splitlines()`` 又会在它们处断行——两者相遇，那条记录就被切成两半、解析失败，
那个文件的撤销依据静默消失。这正是 JSONL 读取必须按 ``"\\n"`` 切而不能用
``splitlines()`` 的原因，生成器把这个字符放进字符池就是为了钉住它。

Validates: Requirements 13.1, 13.9, 13.10, 19.11
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.journal import JOURNAL_NAME, Journal
from app.core.models import (
    ActionKind,
    CategoryOverride,
    ItemOverride,
    JournalRecord,
    OverrideSet,
    RecordKind,
    from_jsonable,
    to_jsonable,
)
from tests.fixtures.execution import build_manifest, build_plan
from tests.fixtures.generators import conflict_policies, strategies_, tree_specs
from tests.fixtures.trees import build_tree

FS = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

#: 含 Windows 允许但 JSON 不转义的行分隔字符
TRICKY_CHARS = "\u0085\u2028\u2029"

paths_text = st.one_of(
    st.text(min_size=0, max_size=24),
    st.sampled_from(
        [
            r"C:\报告\发票 2024.pdf",
            "C:\\a\u2028b\\c.txt",
            "C:\\x\u0085y.pdf",
            "C:\\z\u2029w.pdf",
            "",
        ]
    ),
    st.text(alphabet="abc 中文" + TRICKY_CHARS, min_size=1, max_size=12),
)

json_scalars = st.one_of(
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=16),
    st.none(),
)


@st.composite
def journal_records(draw: st.DrawFn, seq: int) -> JournalRecord:
    return JournalRecord(
        seq=seq,
        ts=draw(st.text(max_size=32)),
        kind=draw(st.sampled_from(list(RecordKind))),
        op=draw(st.one_of(st.none(), st.sampled_from(list(ActionKind)))),
        src=draw(st.one_of(st.none(), paths_text)),
        dst=draw(st.one_of(st.none(), paths_text)),
        size=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=2**40))),
        mtime=draw(
            st.one_of(
                st.none(),
                st.floats(
                    min_value=-1e10,
                    max_value=1e10,
                    allow_nan=False,
                    allow_infinity=False,
                ),
            )
        ),
        sha256=draw(st.one_of(st.none(), st.text(alphabet="0123456789abcdef", max_size=64))),
        error=draw(st.one_of(st.none(), st.text(max_size=32))),
        extra=draw(
            st.dictionaries(
                st.text(min_size=1, max_size=8),
                st.one_of(json_scalars, st.lists(json_scalars, max_size=3)),
                max_size=3,
            )
        ),
    )


@st.composite
def record_lists(draw: st.DrawFn) -> list[JournalRecord]:
    count = draw(st.integers(min_value=0, max_value=8))
    return [draw(journal_records(seq=index + 1)) for index in range(count)]


# ---------------------------------------------------------------------------
# 属性 28
# ---------------------------------------------------------------------------


@FS
@given(records=record_lists())
def test_records_round_trip_through_jsonl(tmp_path_factory, records) -> None:
    """需求 13.9。"""
    run_dir = tmp_path_factory.mktemp("journal")
    with Journal(run_dir) as journal:
        for record in records:
            journal.append_record(record)

    restored = Journal.read_records(run_dir)

    assert restored == records
    assert [r.seq for r in restored] == [r.seq for r in records]


@FS
@given(records=record_lists())
def test_encoding_is_deterministic(tmp_path_factory, records) -> None:
    """同一份数据两次写出的字节必须相同，否则镜像一致性校验失去意义。"""
    first = tmp_path_factory.mktemp("j1")
    second = tmp_path_factory.mktemp("j2")
    for run_dir in (first, second):
        with Journal(run_dir) as journal:
            for record in records:
                journal.append_record(record)

    assert (first / JOURNAL_NAME).read_bytes() == (second / JOURNAL_NAME).read_bytes()


@FS
@given(record=journal_records(seq=7))
def test_single_record_never_spans_multiple_lines(tmp_path_factory, record) -> None:
    """一条记录必须占且仅占一行——JSONL 的全部前提。"""
    run_dir = tmp_path_factory.mktemp("journal")
    with Journal(run_dir) as journal:
        journal.append_record(record)

    raw = (run_dir / JOURNAL_NAME).read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    assert json.loads(raw.rstrip("\n"))


@FS
@given(records=record_lists())
def test_codec_round_trip_without_touching_disk(records) -> None:
    """编解码器本身的往返，与文件无关。"""
    for record in records:
        encoded = json.loads(json.dumps(to_jsonable(record), ensure_ascii=False))
        assert from_jsonable(encoded, JournalRecord) == record


# ---------------------------------------------------------------------------
# 属性 29
# ---------------------------------------------------------------------------


@st.composite
def override_sets(draw: st.DrawFn, known_paths: list[str]) -> OverrideSet:
    """含 item 与 category 两类覆盖的 override 集合。

    category 的 key 是**元组**，JSON 对象的 key 只能是字符串——这条往返正是
    ``to_jsonable`` 把非字符串 key 的字典编码成键值对列表的理由。
    """
    overrides = OverrideSet()
    if known_paths:
        for path in draw(
            st.lists(st.sampled_from(known_paths), unique=True, max_size=3)
        ):
            overrides.items[path] = ItemOverride(
                category_path=draw(
                    st.one_of(
                        st.none(),
                        st.lists(
                            st.sampled_from(["财务", "发票", "a/b", "文档"]),
                            min_size=1,
                            max_size=2,
                        ).map(tuple),
                    )
                ),
                included=draw(st.one_of(st.none(), st.booleans())),
            )
    for parts in draw(
        st.lists(
            st.lists(
                st.sampled_from(["文档", "PDF", "财务", "其他"]),
                min_size=1,
                max_size=2,
            ).map(tuple),
            unique=True,
            max_size=2,
        )
    ):
        overrides.categories[parts] = CategoryOverride(
            renamed_to=draw(
                st.one_of(st.none(), st.just(("改过的名字",)))
            ),
            color=draw(st.one_of(st.none(), st.just("#123456"))),
            deleted=draw(st.booleans()),
        )
    return overrides


@FS
@given(
    spec=tree_specs(),
    strategy=strategies_,
    policy=conflict_policies,
    cleanup=st.booleans(),
    data=st.data(),
)
def test_manifest_round_trips(
    tmp_path_factory, spec, strategy, policy, cleanup, data
) -> None:
    """需求 13.1、13.10、19.11。"""
    from app.core.models import ExecOptions

    root = build_tree(tmp_path_factory.mktemp("manifest"), spec).resolve()
    selectable = sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )
    selected = tuple(
        data.draw(st.lists(st.sampled_from(selectable), unique=True, max_size=2))
        if selectable
        else []
    )

    plan, _ = build_plan(
        root, strategy=strategy, conflict_policy=policy, selected=selected
    )
    overrides = data.draw(
        override_sets([str(i.entry.path) for i in plan.all_items()])
    )
    manifest = build_manifest(
        "run-x",
        plan,
        ExecOptions(conflict_policy=policy, remove_empty_dirs=cleanup),
        overrides,
    )

    run_dir = tmp_path_factory.mktemp("hist")
    with Journal(run_dir) as journal:
        journal.write_manifest(manifest)

    restored = Journal.read_manifest(run_dir)

    assert restored == manifest


@FS
@given(spec=tree_specs(), strategy=strategies_)
def test_manifest_preserves_scope_and_selection(
    tmp_path_factory, spec, strategy
) -> None:
    """撤销后重新生成方案要能复现当时的扫描范围，幂等性断言才有前提。"""
    root = build_tree(tmp_path_factory.mktemp("manifest"), spec).resolve()
    selectable = sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )
    selected = tuple(selectable[:1])

    plan, _ = build_plan(root, strategy=strategy, selected=selected)
    manifest = build_manifest("run-y", plan)

    run_dir = tmp_path_factory.mktemp("hist")
    with Journal(run_dir) as journal:
        journal.write_manifest(manifest)
    restored = Journal.read_manifest(run_dir)

    assert restored is not None
    assert restored.scope is plan.scope
    assert restored.selected_subfolders == tuple(str(p) for p in selected)
    assert restored.plan.selected_subfolders == plan.selected_subfolders


@FS
@given(spec=tree_specs())
def test_manifest_snapshot_covers_every_plan_item(tmp_path_factory, spec) -> None:
    """需求 13.1：源树快照是撤销前比对的基准，漏一条就有一个文件救不回来。"""
    root = build_tree(tmp_path_factory.mktemp("manifest"), spec).resolve()
    plan, _ = build_plan(root)
    manifest = build_manifest("run-z", plan)

    snapshot_paths = {entry.path for entry in manifest.source_snapshot}
    assert snapshot_paths == {str(i.entry.path) for i in plan.all_items()}
    for entry in manifest.source_snapshot:
        assert entry.size >= 0
        assert isinstance(entry.mtime, float)
