"""ExecuteService / UndoService：把 Executor 与 UndoManager 放进 QThread。

两者线程模型同构：单线程顺序执行。执行刻意不并发（journal 的 seq 必须全序），
撤销同理。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.core.executor import Executor, plan_for_resume
from app.core.journal import Journal, mirror_dir_for, new_run_id
from app.core.models import (
    ExecOptions,
    ExecutionReport,
    Manifest,
    OverrideSet,
    ScanSelection,
    SortPlan,
    SourceSnapshotEntry,
)
from app.core.undo import UndoManager, UndoReport
from app.services.base import WorkerService


class ExecuteService(WorkerService):
    """执行方案。"""

    #: 本次 run 的 id，在作业启动时立刻发出——UI 需要它才能提供「撤销本次整理」
    runStarted = Signal(str)

    def __init__(
        self,
        history_dir: Path,
        app_version: str = "0.1.0",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._history = Path(history_dir)
        self._app_version = app_version
        self._last_run_id: str | None = None
        self._last_report: ExecutionReport | None = None

    @property
    def last_run_id(self) -> str | None:
        return self._last_run_id

    @property
    def last_report(self) -> ExecutionReport | None:
        return self._last_report

    def start_execute(
        self,
        plan: SortPlan,
        options: ExecOptions,
        selection: ScanSelection | None = None,
        overrides: OverrideSet | None = None,
    ) -> bool:
        """启动执行。``options.dry_run`` 为真时不碰任何文件。"""
        run_id = new_run_id()
        history = self._history
        version = self._app_version
        overrides = overrides or OverrideSet()

        def job(cancel, on_progress) -> ExecutionReport:  # noqa: ANN001
            executor = Executor()
            if options.dry_run:
                # 模拟运行不写日志：它不产生任何可撤销的副作用，写一份 run 记录
                # 反而会让历史页出现「什么都没做」的条目
                return executor.run(
                    plan,
                    journal=None,
                    options=options,
                    selection=selection,
                    cancel=cancel,
                    on_progress=on_progress,
                )

            run_dir = history / run_id
            mirror = mirror_dir_for(plan.root, run_id)
            with Journal(run_dir, mirror) as journal:
                journal.write_manifest(
                    _build_manifest(
                        run_id=run_id,
                        plan=plan,
                        options=options,
                        overrides=overrides,
                        app_version=version,
                    )
                )
                return executor.run(
                    plan,
                    journal=journal,
                    options=options,
                    selection=selection,
                    cancel=cancel,
                    on_progress=on_progress,
                )

        started = self._launch(job)
        if started:
            self._last_run_id = None if options.dry_run else run_id
            if not options.dry_run:
                self.runStarted.emit(run_id)
        return started

    def start_resume(
        self,
        run_id: str,
        pending_sources: Sequence[str],
        selection: ScanSelection | None = None,
    ) -> bool:
        """续跑一次未收尾的 run。需求 13.8。

        接着往同一份 journal 里写，不新开 run：那次整理在用户眼里是**一次**操作，
        撤销时也该作为一次整体回滚。`Journal.open()` 会把 seq 接上。
        """
        run_dir = self._history / run_id
        manifest = Journal.read_manifest(run_dir)
        if manifest is None:
            return False

        plan = plan_for_resume(manifest, pending_sources)
        if not plan.all_items():
            return False

        options = ExecOptions(
            conflict_policy=manifest.conflict_policy,
            remove_empty_dirs=manifest.remove_empty_dirs,
        )

        def job(cancel, on_progress) -> ExecutionReport:  # noqa: ANN001
            mirror = mirror_dir_for(plan.root, run_id)
            with Journal(run_dir, mirror) as journal:
                return Executor().run(
                    plan,
                    journal=journal,
                    options=options,
                    selection=selection,
                    cancel=cancel,
                    on_progress=on_progress,
                )

        started = self._launch(job)
        if started:
            self._last_run_id = run_id
            self.runStarted.emit(run_id)
        return started

    def _transform(self, result: object) -> object:
        if isinstance(result, ExecutionReport):
            self._last_report = result
        return result


class UndoService(WorkerService):
    """撤销与重做。"""

    def __init__(self, history_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manager = UndoManager(Path(history_dir))
        self._last_run_id: str | None = None

    @property
    def manager(self) -> UndoManager:
        return self._manager

    @property
    def last_run_id(self) -> str | None:
        return self._last_run_id

    def start_undo(self, run_id: str) -> bool:
        manager = self._manager

        def job(_cancel, on_progress) -> UndoReport:  # noqa: ANN001
            return manager.undo(run_id, on_progress=on_progress)

        started = self._launch(job)
        if started:
            self._last_run_id = run_id
        return started

    def start_redo(self, run_id: str) -> bool:
        manager = self._manager

        def job(_cancel, on_progress) -> UndoReport:  # noqa: ANN001
            return manager.redo(run_id, on_progress=on_progress)

        started = self._launch(job)
        if started:
            self._last_run_id = run_id
        return started


def _build_manifest(
    *,
    run_id: str,
    plan: SortPlan,
    options: ExecOptions,
    overrides: OverrideSet,
    app_version: str,
) -> Manifest:
    """执行前的完整现场快照。需求 13.1、19.11。

    源树快照记录每个条目的 size 与 mtime——撤销前的比对（需求 14.5）就以它为基准。
    """
    from app.core.journal import utc_stamp

    snapshot = [
        SourceSnapshotEntry(
            path=str(item.entry.path),
            size=item.entry.size,
            mtime=item.entry.mtime,
        )
        for item in plan.all_items()
    ]
    return Manifest(
        run_id=run_id,
        created_at=utc_stamp(),
        root=str(plan.root),
        strategy=plan.strategy,
        scope=plan.scope,
        selected_subfolders=tuple(str(p) for p in plan.selected_subfolders),
        conflict_policy=options.conflict_policy,
        remove_empty_dirs=options.remove_empty_dirs,
        plan=plan,
        source_snapshot=snapshot,
        overrides=overrides,
        app_version=app_version,
    )
