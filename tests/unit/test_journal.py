"""Journal 的例子级测试。任务 31。

覆盖三条撤销可靠性的前提：写了就落盘（fsync）、镜像坏了主副本照旧、坏行不毁整份
日志。这些都是「用户几天后才发现整理错了」时唯一的退路。

Validates: Requirements 13.1, 13.3, 13.4, 13.5, 13.6, 13.7, 13.9, 13.10
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import journal as journal_mod
from app.core.journal import (
    JOURNAL_NAME,
    MANIFEST_NAME,
    MIRROR_DIR_NAME,
    Journal,
    has_unfinished_intents,
    mirror_dir_for,
    new_run_id,
    unfinished_sources,
)
from app.core.models import (
    ActionKind,
    RecordKind,
)
from tests.fixtures.execution import build_manifest, build_plan
from tests.fixtures.trees import build_tree


def test_append_assigns_monotonic_seq(tmp_path: Path) -> None:
    with Journal(tmp_path / "run") as journal:
        first = journal.append(RecordKind.INTENT, src=tmp_path / "a.txt")
        second = journal.append(RecordKind.DONE, src=tmp_path / "a.txt")

    assert (first.seq, second.seq) == (1, 2)
    records = Journal.read_records(tmp_path / "run")
    assert [r.seq for r in records] == [1, 2]
    assert [r.kind for r in records] == [RecordKind.INTENT, RecordKind.DONE]


def test_every_append_is_fsynced(tmp_path: Path, monkeypatch) -> None:
    """需求 13.3、13.4。flush 只交给操作系统，掉电仍会丢。"""
    calls: list[int] = []
    real_fsync = journal_mod.os.fsync

    def counting_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(journal_mod.os, "fsync", counting_fsync)

    with Journal(tmp_path / "run") as journal:
        for index in range(4):
            journal.append(RecordKind.INTENT, src=tmp_path / f"{index}.txt")

    assert len(calls) == 4


def test_record_round_trips_through_jsonl(tmp_path: Path) -> None:
    """需求 13.9。撤销读不回来就等于没有撤销。"""
    with Journal(tmp_path / "run") as journal:
        written = journal.append(
            RecordKind.DONE,
            op=ActionKind.MOVE,
            src=tmp_path / "源 文件.pdf",
            dst=tmp_path / "文档" / "源 文件.pdf",
            size=1234,
            mtime=1700000000.5,
            sha256="deadbeef",
            cross_volume=True,
        )

    (restored,) = Journal.read_records(tmp_path / "run")
    assert restored == written
    assert restored.op is ActionKind.MOVE
    assert restored.extra == {"cross_volume": True}


def test_manifest_round_trips(tmp_path: Path) -> None:
    """需求 13.1、13.10。"""
    root = build_tree(tmp_path / "root", {"报告.pdf": "x", "图.png": "y"})
    plan, _ = build_plan(root)
    manifest = build_manifest("run-1", plan)

    with Journal(tmp_path / "hist" / "run-1") as journal:
        journal.write_manifest(manifest)

    restored = Journal.read_manifest(tmp_path / "hist" / "run-1")
    assert restored is not None
    assert restored.run_id == manifest.run_id
    assert restored.root == manifest.root
    assert restored.strategy is manifest.strategy
    assert restored.scope is manifest.scope
    assert len(restored.plan.all_items()) == len(plan.all_items())
    assert restored.source_snapshot == manifest.source_snapshot


def test_manifest_write_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    root = build_tree(tmp_path / "root", {"a.txt": "x"})
    plan, _ = build_plan(root)
    run_dir = tmp_path / "hist" / "run-1"

    with Journal(run_dir) as journal:
        journal.write_manifest(build_manifest("run-1", plan))

    assert (run_dir / MANIFEST_NAME).exists()
    assert not list(run_dir.glob("*.tmp"))


def test_mirror_matches_main_copy(tmp_path: Path) -> None:
    """需求 13.5。镜像让文件夹搬到别的机器后仍有记录。"""
    root = tmp_path / "root"
    root.mkdir()
    mirror = mirror_dir_for(root, "run-1")

    with Journal(tmp_path / "hist" / "run-1", mirror) as journal:
        journal.append(RecordKind.INTENT, src=root / "a.txt")
        journal.append(RecordKind.DONE, src=root / "a.txt", dst=root / "文档/a.txt")

    main_lines = (tmp_path / "hist" / "run-1" / JOURNAL_NAME).read_text("utf-8")
    mirror_lines = (mirror / JOURNAL_NAME).read_text("utf-8")
    assert main_lines == mirror_lines
    assert mirror.parent.name == MIRROR_DIR_NAME


def test_mirror_failure_does_not_break_main(tmp_path: Path, caplog) -> None:
    """需求 13.6。根目录可能只读，但那不该让整次整理失败。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我是文件，不是目录", encoding="utf-8")

    with caplog.at_level("WARNING"):
        with Journal(tmp_path / "hist" / "run-1", blocked / "run-1") as journal:
            assert journal.mirror_healthy is False
            journal.append(RecordKind.DONE, src=tmp_path / "a.txt")

    records = Journal.read_records(tmp_path / "hist" / "run-1")
    assert len(records) == 1
    assert any("镜像" in message for message in caplog.messages)


def test_corrupt_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    good = json.dumps({"seq": 2, "ts": "t", "kind": "done", "src": "a"})
    (run_dir / JOURNAL_NAME).write_text(
        f"{{ 这不是 json\n{good}\n\n", encoding="utf-8"
    )

    records = Journal.read_records(run_dir)
    assert [r.seq for r in records] == [2]


def test_read_records_on_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert Journal.read_records(tmp_path / "nope") == []
    assert Journal.read_manifest(tmp_path / "nope") is None


def test_reopening_continues_seq_from_existing_records(tmp_path: Path) -> None:
    """崩溃后重启接着写，seq 不能倒退——撤销依赖 seq 全序。"""
    run_dir = tmp_path / "run"
    with Journal(run_dir) as journal:
        journal.append(RecordKind.INTENT, src=tmp_path / "a.txt")

    with Journal(run_dir) as journal:
        second = journal.append(RecordKind.DONE, src=tmp_path / "a.txt")

    assert second.seq == 2
    assert [r.seq for r in Journal.read_records(run_dir)] == [1, 2]


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ([RecordKind.INTENT, RecordKind.DONE], False),
        ([RecordKind.INTENT, RecordKind.FAILED], False),
        ([RecordKind.INTENT, RecordKind.SKIPPED], False),
        ([RecordKind.INTENT], True),
        ([], False),
    ],
)
def test_unfinished_detection(tmp_path: Path, kinds, expected) -> None:
    """需求 13.7。存在 intent 但缺少对应 done/failed 即未收尾。"""
    with Journal(tmp_path / "run") as journal:
        for kind in kinds:
            journal.append(kind, src=tmp_path / "a.txt", dst=tmp_path / "b.txt")

    records = Journal.read_records(tmp_path / "run")
    assert has_unfinished_intents(records) is expected
    assert bool(unfinished_sources(records)) is expected


def test_unfinished_sources_lists_only_pending(tmp_path: Path) -> None:
    with Journal(tmp_path / "run") as journal:
        journal.append(RecordKind.INTENT, src=tmp_path / "a.txt")
        journal.append(RecordKind.DONE, src=tmp_path / "a.txt")
        journal.append(RecordKind.INTENT, src=tmp_path / "b.txt")

    records = Journal.read_records(tmp_path / "run")
    assert unfinished_sources(records) == [str(tmp_path / "b.txt")]


def test_new_run_id_is_unique_within_the_same_second() -> None:
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
