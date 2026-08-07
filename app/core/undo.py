"""Undo_Manager：撤销与重做。需求 14。

这是整个工具最不能出错的一块——用户敢点「开始整理」的全部理由就在这里。

## 三阶段顺序不可调换

    阶段 A  按 removed_dir 记录重建目录（深度升序，先父后子）      需求 20.18
    阶段 B  按 done 记录逆序还原文件                               需求 14.2
    阶段 C  按 created_dir 记录逆序删除目录，且仅当当前为空        需求 14.7

A 必须在 B 前：文件的原路径可能就在被清理掉的空目录里，目录不存在 `os.replace`
会失败。C 必须在 B 后：文件还没移走时类目目录不空，删不掉。

## 还原前必须校验

比对当前文件的 size 与 mtime 和 journal 记录值（需求 14.5）。不一致意味着文件在
整理之后被人改过——此时**跳过并列入需人工确认**（需求 14.6），绝不覆盖。宁可留一个
待处理项，也不能悄悄用旧位置的判断把用户后来的修改冲掉。

## 重做就是「撤销的撤销」

撤销自身也写一份 journal，kind 与正向执行相同但 src/dst 互换（需求 14.9）。因此
`redo` 复用同一套代码，撤销/重做幂等（需求 14.13）不依赖额外逻辑。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core import fsops
from app.core.journal import Journal, unfinished_sources
from app.core.models import (
    ActionKind,
    JournalRecord,
    RecordKind,
)
from app.core.progress import (
    DEFAULT_INTERVAL_MS,
    ProgressSink,
    ProgressSnapshot,
    ProgressThrottle,
)

logger = logging.getLogger(__name__)

PHASE_UNDO = "undo"

#: mtime 比对容差（秒）。不同文件系统的时间精度不同（FAT 是 2 秒），
#: 完全相等的要求会把正常文件误判成「被改过」。
MTIME_TOLERANCE = 2.0


@dataclass
class AttentionItem:
    """需人工确认项。需求 14.6、14.11。

    并排给出源与目标的现状，让用户自己判断该保哪一份——工具不替他猜。
    """

    src: str
    dst: str
    reason: str
    recorded_size: int | None = None
    recorded_mtime: float | None = None
    current_size: int | None = None
    current_mtime: float | None = None


@dataclass
class UndoReport:
    restored: int = 0
    recreated_dirs: int = 0
    removed_dirs: int = 0
    needs_attention: list[AttentionItem] = field(default_factory=list)
    failed: list[AttentionItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.needs_attention and not self.failed

    def summary(self) -> str:
        text = f"已还原 {self.restored} 个文件"
        if self.recreated_dirs:
            text += f"，重建 {self.recreated_dirs} 个目录"
        if self.removed_dirs:
            text += f"，清理 {self.removed_dirs} 个空目录"
        if self.needs_attention:
            text += f"，{len(self.needs_attention)} 项需人工确认"
        if self.failed:
            text += f"，{len(self.failed)} 项失败"
        return text


@dataclass
class ResumeReport:
    """崩溃恢复的探测结果。需求 13.8、14.11。"""

    at_source: list[str] = field(default_factory=list)
    at_target: list[str] = field(default_factory=list)
    both: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> list[str]:
        """两处都有或两处都没有的，必须人工判断。"""
        return [*self.both, *self.missing]


class UndoManager:
    """按 journal 还原一次 run。"""

    def __init__(
        self,
        history_dir: Path,
        trash: fsops.TrashFn | None = None,
        progress_interval_ms: int = DEFAULT_INTERVAL_MS,
    ) -> None:
        self._history = Path(history_dir)
        self._trash = trash or fsops.to_trash
        self._interval = progress_interval_ms

    # -- 对外 -------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self._history / run_id

    def undo(
        self,
        run_id: str,
        on_progress: ProgressSink | None = None,
        write_journal: bool = True,
    ) -> UndoReport:
        """撤销一次 run。需求 14.2-14.9。"""
        source_dir = self.run_dir(run_id)
        records = Journal.read_records(source_dir)
        if not records:
            return UndoReport()

        report = UndoReport()
        throttle = (
            ProgressThrottle(on_progress, self._interval)
            if on_progress is not None
            else None
        )

        undo_journal: Journal | None = None
        if write_journal:
            undo_journal = Journal(self._history / f"{run_id}.undo").open()

        try:
            self._recreate_removed_dirs(records, report, undo_journal)
            self._restore_files(records, report, undo_journal, throttle)
            self._drop_created_dirs(records, report, undo_journal)
        finally:
            if undo_journal is not None:
                undo_journal.close()

        self._flag_unsettled_intents(records, report)

        if throttle is not None:
            throttle.finish(
                ProgressSnapshot(PHASE_UNDO, report.restored, report.restored)
            )
        return report

    def _flag_unsettled_intents(
        self, records: list[JournalRecord], report: UndoReport
    ) -> None:
        """停在 intent 状态的条目一律列入需人工确认。需求 14.11。

        这些条目**没有 done 记录**，所以撤销无从下手：我们不知道那次移动到底做成了
        没有。放在这里而不是放在 UI 里，是为了让「全部撤销之后 intent 条目必被列出」
        对任何调用方都成立——崩溃恢复对话框、历史记录页、Ctrl+Z 走的是同一段代码。
        """
        pending = unfinished_sources(records)
        if not pending:
            return

        by_src = {r.src: r for r in records if r.kind is RecordKind.INTENT}
        for src in pending:
            record = by_src.get(src)
            dst = record.dst if record and record.dst else ""
            source_exists = Path(src).exists()
            target_exists = bool(dst) and Path(dst).exists()
            if source_exists and not target_exists:
                reason = "上次整理中断在动手之前，文件仍在原处，无需还原"
            elif target_exists and not source_exists:
                reason = "上次整理已移走该文件但没记完日志，请确认要不要移回原处"
            elif source_exists and target_exists:
                reason = "源与目标都存在同名文件，请自行判断保留哪一份"
            else:
                reason = "源与目标都找不到该文件，请到回收站确认"
            report.needs_attention.append(
                AttentionItem(
                    src=src,
                    dst=dst,
                    reason=reason,
                    recorded_size=record.size if record else None,
                    recorded_mtime=record.mtime if record else None,
                    current_size=_size_of(Path(src)) if source_exists else None,
                )
            )

    def redo(
        self, run_id: str, on_progress: ProgressSink | None = None
    ) -> UndoReport:
        """重做 = 对撤销 run 再撤销一次。需求 14.10、14.13。"""
        return self.undo(f"{run_id}.undo", on_progress, write_journal=False)

    def resume(self, run_id: str) -> ResumeReport:
        """探测停留在 intent 状态的条目实际在哪。需求 13.8、14.11。"""
        records = Journal.read_records(self.run_dir(run_id))
        pending = set(unfinished_sources(records))
        by_src = {r.src: r for r in records if r.kind is RecordKind.INTENT}

        report = ResumeReport()
        for src in sorted(pending):
            record = by_src.get(src)
            if record is None or record.dst is None:
                report.missing.append(src)
                continue
            source_exists = Path(src).exists()
            target_exists = Path(record.dst).exists()
            if source_exists and target_exists:
                report.both.append(src)
            elif source_exists:
                report.at_source.append(src)
            elif target_exists:
                report.at_target.append(src)
            else:
                report.missing.append(src)
        return report

    # -- 阶段 A -----------------------------------------------------------

    def _recreate_removed_dirs(
        self,
        records: list[JournalRecord],
        report: UndoReport,
        journal: Journal | None,
    ) -> None:
        """需求 20.18：重建被清理掉的空目录。

        深度升序（先父后子）：子目录的父目录必须先存在。
        """
        targets = [
            Path(r.dst)
            for r in records
            if r.kind is RecordKind.REMOVED_DIR and r.dst
        ]
        for directory in sorted(targets, key=lambda p: len(p.parts)):
            if directory.exists():
                continue
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                report.failed.append(
                    AttentionItem(
                        src="", dst=str(directory), reason=f"重建目录失败: {exc}"
                    )
                )
                continue
            report.recreated_dirs += 1
            if journal is not None:
                journal.append(RecordKind.CREATED_DIR, dst=directory)

    # -- 阶段 B -----------------------------------------------------------

    def _restore_files(
        self,
        records: list[JournalRecord],
        report: UndoReport,
        journal: Journal | None,
        throttle: ProgressThrottle | None,
    ) -> None:
        done = [r for r in records if r.kind is RecordKind.DONE and r.src and r.dst]
        total = len(done)

        for index, record in enumerate(reversed(done), start=1):
            if throttle is not None:
                throttle.update(
                    ProgressSnapshot(
                        PHASE_UNDO, index, total, current=Path(record.dst or "").name
                    )
                )
            self._restore_one(record, report, journal)

    def _restore_one(
        self,
        record: JournalRecord,
        report: UndoReport,
        journal: Journal | None,
    ) -> None:
        assert record.src and record.dst
        source = Path(record.src)
        target = Path(record.dst)

        if not target.exists():
            report.needs_attention.append(
                AttentionItem(
                    src=record.src,
                    dst=record.dst,
                    reason="目标位置的文件已不存在，无法还原",
                    recorded_size=record.size,
                    recorded_mtime=record.mtime,
                )
            )
            return

        verdict = self._verify(record, target)
        if verdict is not None:
            report.needs_attention.append(verdict)
            return

        if source.exists():
            report.needs_attention.append(
                AttentionItem(
                    src=record.src,
                    dst=record.dst,
                    reason="原路径已被别的文件占用，不覆盖",
                    recorded_size=record.size,
                    recorded_mtime=record.mtime,
                    current_size=_size_of(source),
                )
            )
            return

        try:
            source.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            report.failed.append(
                AttentionItem(record.src, record.dst, f"重建原目录失败: {exc}")
            )
            return

        if fsops.same_volume(target, source):
            try:
                fsops.atomic_move(target, source)
            except OSError as exc:
                report.failed.append(
                    AttentionItem(record.src, record.dst, f"还原失败: {exc}")
                )
                return
        else:
            # 需求 14.4：跨卷时复制回原路径、校验一致后删除目标副本
            outcome = fsops.copy_verify_trash(
                target, source, verify_hash=True, trash=self._trash
            )
            if not outcome.ok:
                report.failed.append(
                    AttentionItem(
                        record.src, record.dst, outcome.error or "跨卷还原失败"
                    )
                )
                return

        report.restored += 1
        if journal is not None:
            # kind 相同但 src/dst 互换——这让 redo 复用同一套代码
            journal.append(
                RecordKind.DONE,
                op=ActionKind.MOVE,
                src=target,
                dst=source,
                size=record.size,
                mtime=record.mtime,
            )

    @staticmethod
    def _verify(record: JournalRecord, target: Path) -> AttentionItem | None:
        """需求 14.5、14.6：size 与 mtime 必须与记录一致。"""
        try:
            stat = target.stat()
        except OSError as exc:
            return AttentionItem(
                record.src or "", record.dst or "", f"无法读取目标文件: {exc}"
            )

        size_ok = record.size is None or stat.st_size == record.size
        mtime_ok = (
            record.mtime is None
            or abs(stat.st_mtime - record.mtime) <= MTIME_TOLERANCE
        )
        if size_ok and mtime_ok:
            return None

        return AttentionItem(
            src=record.src or "",
            dst=record.dst or "",
            reason="文件在整理之后被修改过，未做还原以免覆盖你的改动",
            recorded_size=record.size,
            recorded_mtime=record.mtime,
            current_size=stat.st_size,
            current_mtime=stat.st_mtime,
        )

    # -- 阶段 C -----------------------------------------------------------

    def _drop_created_dirs(
        self,
        records: list[JournalRecord],
        report: UndoReport,
        journal: Journal | None,
    ) -> None:
        """需求 14.7：只删自己创建过、且当前为空的目录。"""
        targets = [
            Path(r.dst)
            for r in records
            if r.kind is RecordKind.CREATED_DIR and r.dst
        ]
        # 深度降序：先删子目录，父目录才有机会随之变空
        for directory in sorted(targets, key=lambda p: len(p.parts), reverse=True):
            if not directory.exists():
                continue
            if not fsops.is_effectively_empty(directory):
                continue
            try:
                directory.rmdir()
            except OSError as exc:
                logger.warning("删除目录失败 %s: %s", directory, exc)
                continue
            report.removed_dirs += 1
            if journal is not None:
                journal.append(RecordKind.REMOVED_DIR, dst=directory)


def _size_of(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None
