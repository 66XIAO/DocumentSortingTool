"""Executor：按 SortPlan 动文件。需求 11、12、20。

## 单线程顺序，刻意不并发

journal 的 `seq` 必须是全序，撤销依赖「按 done 记录逆序还原」这一语义。并发执行会
让逆序退化成偏序，撤销正确性就无法再用一条属性描述。同卷移动本身是元数据操作，
串行的代价可以接受。

## 主循环

    journal.append(intent) + fsync          # 需求 13.3
    ensure_category_dir()                    # 新建则 append(created_dir)，需求 12.5
    覆盖策略且目标存在: to_trash(目标) + append(trashed)   # 需求 12.4
    同卷: atomic_move() / 跨卷: copy_verify_trash()        # 需求 12.1、12.2
    journal.append(done | failed) + fsync    # 需求 13.4、12.6

## 空目录清理的候选集合是推导出来的，不是判断出来的

候选 = 本次 run `done` 移动记录的源父目录 ∩ 已勾选子文件夹闭包 − 根目录。需求 20 的
六条约束因此成为**结构性结论**而非六个分支：

- `remove_empty_dirs` 为 false → 整个阶段不执行 → 全部目录保留（20.17）
- `scope == top_level_only` → 闭包为空 → 候选为空 → 子文件夹全保留（20.10）
- 未勾选的子文件夹永不进闭包 → 范围外不变性成立（20.13、2.20）
- 执行前就已为空的目录不会出现在 done 记录的源父目录里 → 保留（20.9）
- 两层勾选缺任一层 → 候选为空（20.5）
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core import fsops
from app.core.cleanup import collect_candidates, predict_removed
from app.core.journal import Journal
from app.core.models import (
    ActionKind,
    ConflictKind,
    ConflictPolicy,
    ExecOptions,
    ExecutionReport,
    JournalRecord,
    Manifest,
    Outcome,
    PlanItem,
    RecordKind,
    ResultRow,
    ScanSelection,
    SortPlan,
)
from app.core.progress import (
    DEFAULT_INTERVAL_MS,
    CancelToken,
    NullCancelToken,
    ProgressSink,
    ProgressSnapshot,
    ProgressThrottle,
)

logger = logging.getLogger(__name__)

PHASE_EXECUTE = "execute"
PHASE_CLEANUP = "cleanup"


@dataclass
class _Outcome:
    row: ResultRow
    #: 被移出的**源文件路径**（不是父目录）。空目录清理既要知道「哪些目录有文件
    #: 被移出」，也要知道「移出的是哪几个条目」——后者是模拟运行预测待删目录的
    #: 唯一依据（需求 20.20），只记父目录就预测不出来。
    moved_source: Path | None = None


class Executor:
    """执行方案。"""

    def __init__(
        self,
        trash: fsops.TrashFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        progress_interval_ms: int = DEFAULT_INTERVAL_MS,
    ) -> None:
        self._trash = trash or fsops.to_trash
        self._clock = clock
        self._interval = progress_interval_ms

    # -- 对外 -------------------------------------------------------------

    def run(
        self,
        plan: SortPlan,
        journal: Journal | None,
        options: ExecOptions | None = None,
        selection: ScanSelection | None = None,
        cancel: CancelToken | None = None,
        on_progress: ProgressSink | None = None,
    ) -> ExecutionReport:
        options = options or ExecOptions()
        if cancel is None:
            cancel = NullCancelToken()
        started = self._clock()

        throttle = (
            ProgressThrottle(on_progress, self._interval, self._clock)
            if on_progress is not None
            else None
        )

        report = ExecutionReport(dry_run=options.dry_run)
        pending = [item for item in plan.all_items()]
        total = len(pending)
        created_dirs: list[Path] = []
        moved_files: list[Path] = []

        for index, item in enumerate(pending, start=1):
            if cancel.cancelled:
                # 需求 11.7：停止在当前单文件操作完成后生效，剩下的都算跳过
                report.stopped = True
                for remaining in pending[index - 1 :]:
                    report.skipped.append(
                        self._row(remaining, Outcome.SKIPPED, "已停止执行")
                    )
                break

            if throttle is not None:
                throttle.update(
                    ProgressSnapshot(
                        PHASE_EXECUTE, index, total, current=item.entry.name
                    )
                )

            outcome = self._handle(item, journal, options, created_dirs)
            if outcome.row.outcome is Outcome.SUCCEEDED:
                report.succeeded.append(outcome.row)
                if outcome.moved_source is not None:
                    moved_files.append(outcome.moved_source)
            elif outcome.row.outcome is Outcome.SKIPPED:
                report.skipped.append(outcome.row)
            else:
                report.failed.append(outcome.row)

        report.removed_dirs, report.predicted_removed_dirs = self._cleanup(
            plan=plan,
            options=options,
            selection=selection,
            moved_files=moved_files,
            journal=journal,
        )

        if throttle is not None:
            throttle.finish(
                ProgressSnapshot(PHASE_CLEANUP, total, total)
            )
        report.elapsed_ms = int((self._clock() - started) * 1000)
        return report

    # -- 单条 -------------------------------------------------------------

    def _handle(
        self,
        item: PlanItem,
        journal: Journal | None,
        options: ExecOptions,
        created_dirs: list[Path],
    ) -> _Outcome:
        entry = item.entry

        if not item.included:
            return self._skip(item, journal, "未勾选", options)
        if item.action is ActionKind.SKIP:
            return self._skip(item, journal, self._skip_reason(item), options)
        if item.conflict in (
            ConflictKind.PATH_ESCAPE,
            ConflictKind.PATH_TOO_LONG,
            ConflictKind.LOCKED,
        ):
            return self._skip(item, journal, self._skip_reason(item), options)

        if options.dry_run:
            # 需求 11.8：产出结构相同的报告但不碰文件。moved_source 仍要记——
            # 它是模拟运行预测待删空目录的输入（需求 20.20）。
            return _Outcome(
                self._row(item, Outcome.SUCCEEDED, item.reason),
                moved_source=entry.path,
            )

        # stat 失败也必须先写 intent 再写 failed：属性 27 要求「每条 done/failed
        # 都存在 seq 更小的同操作 intent」。若这里直接写 failed，就出现了一条没有
        # 前驱 intent 的 failed，未收尾检测与撤销的配对逻辑都会失去依据。
        size: int | None = None
        mtime: float | None = None
        stat_error: str | None = None
        try:
            stat = entry.path.stat()
        except OSError as exc:
            stat_error = str(exc)
        else:
            size, mtime = stat.st_size, stat.st_mtime

        self._record(
            journal, RecordKind.INTENT, item, size=size, mtime=mtime
        )

        if stat_error is not None:
            self._record(journal, RecordKind.FAILED, item, error=stat_error)
            return _Outcome(
                self._row(item, Outcome.FAILED, f"读取源文件失败: {stat_error}")
            )
        assert size is not None and mtime is not None

        try:
            self._ensure_dirs(item, journal, created_dirs)
        except OSError as exc:
            self._record(journal, RecordKind.FAILED, item, error=str(exc))
            return _Outcome(self._row(item, Outcome.FAILED, f"创建目标目录失败: {exc}"))

        if (
            options.conflict_policy is ConflictPolicy.OVERWRITE
            and item.target.exists()
        ):
            # 需求 12.4：被覆盖的文件先进回收站，且记录下来供撤销说明
            try:
                self._trash(item.target)
                if journal is not None:
                    journal.append(
                        RecordKind.TRASHED, src=item.target, dst=None
                    )
            except OSError as exc:
                self._record(journal, RecordKind.FAILED, item, error=str(exc))
                return _Outcome(
                    self._row(item, Outcome.FAILED, f"移入回收站失败: {exc}")
                )

        return self._move(item, journal, options, size, mtime)

    def _move(
        self,
        item: PlanItem,
        journal: Journal | None,
        options: ExecOptions,
        size: int,
        mtime: float,
    ) -> _Outcome:
        source = item.entry.path
        target = item.target
        # 必须在动手**之前**判定：移动之后源路径已不存在，same_volume 会退化成
        # 「拿最近的已存在祖先去比」，得到的答案与实际走过的分支无关。
        cross_volume = not fsops.same_volume(source, target)

        if not cross_volume:
            try:
                fsops.atomic_move(source, target)
            except OSError as exc:
                self._record(journal, RecordKind.FAILED, item, error=str(exc))
                return _Outcome(self._row(item, Outcome.FAILED, f"移动失败: {exc}"))
            digest = None
        else:
            outcome = fsops.copy_verify_trash(
                source, target, verify_hash=options.verify_hash, trash=self._trash
            )
            if not outcome.ok:
                self._record(
                    journal, RecordKind.FAILED, item, error=outcome.error
                )
                return _Outcome(
                    self._row(item, Outcome.FAILED, outcome.error or "跨卷移动失败")
                )
            digest = outcome.sha256

        self._record(
            journal,
            RecordKind.DONE,
            item,
            size=size,
            mtime=mtime,
            sha256=digest,
            cross_volume=cross_volume,
        )
        return _Outcome(
            self._row(item, Outcome.SUCCEEDED, item.reason), moved_source=source
        )

    def _ensure_dirs(
        self, item: PlanItem, journal: Journal | None, created: list[Path]
    ) -> None:
        """建目标目录，并把**每一级**新建的目录都记进 created_dir。

        多级类目（文档/PDF/2024）一次 mkdir 可能创建三层，只记最深一层会在撤销时
        留下空壳目录。
        """
        chain = fsops.created_dir_chain(item.target.parent, stop_at=item.entry.path.anchor)
        item.target.parent.mkdir(parents=True, exist_ok=True)
        for directory in chain:
            created.append(directory)
            if journal is not None:
                journal.append(RecordKind.CREATED_DIR, dst=directory)

    # -- 空目录清理 -------------------------------------------------------

    def _cleanup(
        self,
        *,
        plan: SortPlan,
        options: ExecOptions,
        selection: ScanSelection | None,
        moved_files: Sequence[Path],
        journal: Journal | None,
    ) -> tuple[list[Path], list[Path]]:
        if not options.remove_empty_dirs or selection is None:
            return [], []

        candidates = collect_cleanup_candidates(
            [Path(p).parent for p in moved_files], selection, plan.root
        )
        if options.dry_run:
            # 需求 20.20：只列清单不动目录。文件还没搬走，所以不能问「现在空不空」，
            # 要问「减去本次将移出的条目后空不空」——同一个纯函数，属性 38 因此成立。
            return [], predict_removed_dirs(candidates, moved_files)

        removed: list[Path] = []
        for directory in candidates:
            if not fsops.is_effectively_empty(directory):
                continue
            try:
                directory.rmdir()
            except OSError as exc:
                # 需求：清理失败不升级——目录还在是安全结果，没有数据风险
                logger.warning("删除空目录失败 %s: %s", directory, exc)
                continue
            removed.append(directory)
            if journal is not None:
                journal.append(RecordKind.REMOVED_DIR, dst=directory)
        return removed, list(removed)

    # -- 辅助 -------------------------------------------------------------

    @staticmethod
    def _skip_reason(item: PlanItem) -> str:
        labels = {
            ConflictKind.PATH_ESCAPE: "目标越出根目录",
            ConflictKind.PATH_TOO_LONG: "目标路径过长",
            ConflictKind.LOCKED: "源文件被占用或只读",
            ConflictKind.EXISTS: "目标已存在，按策略跳过",
        }
        if item.conflict in labels:
            return labels[item.conflict]
        if item.entry.path.parent == item.target.parent:
            return "已在目标类目中"
        return "按方案跳过"

    def _skip(
        self,
        item: PlanItem,
        journal: Journal | None,
        reason: str,
        options: ExecOptions,
    ) -> _Outcome:
        if not options.dry_run:
            self._record(journal, RecordKind.SKIPPED, item, error=reason)
        return _Outcome(self._row(item, Outcome.SKIPPED, reason))

    @staticmethod
    def _record(
        journal: Journal | None,
        kind: RecordKind,
        item: PlanItem,
        **fields: object,
    ) -> JournalRecord | None:
        if journal is None:
            return None
        return journal.append(
            kind,
            op=item.action,
            src=item.entry.path,
            dst=item.target,
            **fields,  # type: ignore[arg-type]
        )

    @staticmethod
    def _row(item: PlanItem, outcome: Outcome, reason: str) -> ResultRow:
        return ResultRow(
            src=str(item.entry.path),
            dst=str(item.target),
            action=item.action,
            outcome=outcome,
            reason=reason,
        )


def plan_for_resume(
    manifest: Manifest, pending_sources: Sequence[str]
) -> SortPlan:
    """从 manifest 的原方案里挑出仍停在源位置的条目，构成续跑方案。需求 13.8。

    「恢复执行」不重新规划——重新规划会得到一份**新**方案，而用户当初确认的是旧那份。
    manifest 保存了执行前的完整现场，正是为了让续跑用回同一份决定（需求 13.1）。

    只收「源文件仍然存在」的条目：源已不在说明那次移动其实做成了，只是日志没写完，
    此时再搬一次会把目标位置的文件覆盖掉。这类条目交给需人工确认（需求 14.11）。
    """
    wanted = {str(p) for p in pending_sources}
    items = [
        item
        for item in manifest.plan.items
        if str(item.entry.path) in wanted and item.entry.path.exists()
    ]
    unclassified = [
        item
        for item in manifest.plan.unclassified
        if str(item.entry.path) in wanted and item.entry.path.exists()
    ]
    return SortPlan(
        root=manifest.plan.root,
        strategy=manifest.plan.strategy,
        categories=manifest.plan.categories,
        items=items,
        unclassified=unclassified,
        scope=manifest.plan.scope,
        selected_subfolders=manifest.plan.selected_subfolders,
    )


#: 空目录清理的判定全部搬到 ``app.core.cleanup``，让方案预览、模拟运行与真实执行
#: 共用同一段代码（属性 38）。这里保留两个别名，因为它们是既有调用方与测试的入口名。
collect_cleanup_candidates = collect_candidates
predict_removed_dirs = predict_removed
