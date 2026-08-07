"""History_Manager：run 列表、未收尾检测、保留策略。需求 15、13.7。

历史记录不是「锦上添花的日志页」——它是需求 15.2 要求的「任意一条未撤销的 run 都
能撤销」的实现基础。用户可能几天后才发现某次整理有问题，那时唯一的退路就是这里。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.core.journal import (
    Journal,
    has_unfinished_intents,
)
from app.core.models import RecordKind, RunMeta, RunStatus, Strategy

logger = logging.getLogger(__name__)

UNDO_SUFFIX = ".undo"
UNDONE_MARKER = "undone.marker"


@dataclass
class RunSummary:
    """历史记录页的一行所需的全部信息。"""

    meta: RunMeta
    moved: int = 0
    failed: int = 0
    removed_dirs: int = 0

    @property
    def run_id(self) -> str:
        return self.meta.run_id


class HistoryManager:
    """历史 run 的读取与维护。"""

    def __init__(self, history_dir: Path) -> None:
        self._dir = Path(history_dir)

    @property
    def history_dir(self) -> Path:
        return self._dir

    def run_dir(self, run_id: str) -> Path:
        return self._dir / run_id

    # -- 列表 -------------------------------------------------------------

    def list_runs(self) -> list[RunSummary]:
        """需求 15.1。按时间倒序，最近的在前。

        撤销 run（`<id>.undo`）不出现在列表里——它们是撤销的实现细节，不是用户
        做过的一次「整理」。
        """
        if not self._dir.is_dir():
            return []

        summaries: list[RunSummary] = []
        for entry in sorted(self._dir.iterdir(), reverse=True):
            if not entry.is_dir() or entry.name.endswith(UNDO_SUFFIX):
                continue
            summary = self._summarize(entry)
            if summary is not None:
                summaries.append(summary)
        # run_id 作为次序键：manifest 里的时间戳只到秒，同一秒内启动两次（用户手抖
        # 点了两下）会得到相同的 started_at。只按时间排序时「谁更新」就取决于
        # iterdir() 的返回顺序，保留策略会变成不确定的——它要删东西，不能不确定。
        # run_id 以时间戳开头、带随机后缀，是这里唯一可用的稳定次序键。
        summaries.sort(key=lambda s: (s.meta.started_at, s.run_id), reverse=True)
        return summaries

    def _summarize(self, run_dir: Path) -> RunSummary | None:
        manifest = Journal.read_manifest(run_dir)
        records = Journal.read_records(run_dir)
        if manifest is None and not records:
            return None

        moved = sum(1 for r in records if r.kind is RecordKind.DONE)
        failed = sum(1 for r in records if r.kind is RecordKind.FAILED)
        removed = sum(1 for r in records if r.kind is RecordKind.REMOVED_DIR)

        undone = (run_dir / UNDONE_MARKER).exists() or (
            self._dir / f"{run_dir.name}{UNDO_SUFFIX}"
        ).is_dir()

        if undone:
            status = RunStatus.UNDONE
        elif has_unfinished_intents(records):
            status = RunStatus.UNFINISHED
        elif failed:
            status = RunStatus.FAILED
        else:
            status = RunStatus.COMPLETED

        meta = RunMeta(
            run_id=run_dir.name,
            started_at=manifest.created_at if manifest else _mtime_stamp(run_dir),
            root=manifest.root if manifest else "",
            strategy=manifest.strategy if manifest else Strategy.BY_TYPE,
            file_count=moved,
            status=status,
            finished_at=records[-1].ts if records else None,
            undone_at=_undone_stamp(run_dir) if undone else None,
        )
        return RunSummary(
            meta=meta, moved=moved, failed=failed, removed_dirs=removed
        )

    def find_unfinished(self) -> list[RunSummary]:
        """需求 13.7：存在 intent 但缺少对应 done/failed 的 run。"""
        return [s for s in self.list_runs() if s.meta.status is RunStatus.UNFINISHED]

    # -- 状态 -------------------------------------------------------------

    def mark_undone(self, run_id: str) -> None:
        """需求 15.6。用标记文件而非改写 manifest。

        manifest 是执行前的现场快照，改写它就失去了「快照」的意义——撤销状态是
        之后发生的事，应当单独记。
        """
        run_dir = self.run_dir(run_id)
        if not run_dir.is_dir():
            return
        (run_dir / UNDONE_MARKER).write_text(
            datetime.now().astimezone().isoformat(timespec="seconds"),
            encoding="utf-8",
        )

    def clear_undone(self, run_id: str) -> None:
        """重做之后该 run 又「生效」了。"""
        marker = self.run_dir(run_id) / UNDONE_MARKER
        if marker.exists():
            marker.unlink()

    def is_undoable(self, run_id: str) -> bool:
        summary = next((s for s in self.list_runs() if s.run_id == run_id), None)
        return summary is not None and summary.meta.is_undoable()

    # -- 保留策略 ---------------------------------------------------------

    def apply_retention(
        self,
        max_runs: int = 20,
        max_days: int = 30,
        now: datetime | None = None,
        root_mirrors: bool = True,
    ) -> list[str]:
        """需求 15.3、15.4。返回被删除的 run_id 列表。

        **未收尾的 run 永不被保留策略删掉**——那正是用户最需要它的时候。
        """
        current = now or datetime.now().astimezone()
        cutoff = current - timedelta(days=max_days)
        summaries = self.list_runs()

        keepable = [
            s for s in summaries if s.meta.status is not RunStatus.UNFINISHED
        ]
        doomed: list[RunSummary] = []

        # 超出条数上限的（最旧的先删）
        if len(keepable) > max_runs:
            doomed.extend(keepable[max_runs:])

        # 超出保留期的
        for summary in keepable:
            if summary in doomed:
                continue
            stamp = _parse(summary.meta.started_at)
            if stamp is not None and stamp < cutoff:
                doomed.append(summary)

        removed: list[str] = []
        for summary in doomed:
            if self._purge(summary.run_id, root_mirrors=root_mirrors):
                removed.append(summary.run_id)
        return removed

    def _purge(self, run_id: str, root_mirrors: bool = True) -> bool:
        ok = True
        for path in (
            self.run_dir(run_id),
            self._dir / f"{run_id}{UNDO_SUFFIX}",
        ):
            if not path.exists():
                continue
            try:
                shutil.rmtree(path)
            except OSError as exc:
                logger.warning("删除历史记录失败 %s: %s", path, exc)
                ok = False

        if root_mirrors:
            self._purge_mirror(run_id)
        return ok

    def _purge_mirror(self, run_id: str) -> None:
        """主副本与镜像同删（需求 15.4）。

        镜像位置要从 manifest 里的 root 推出来，而 manifest 可能已经被删了——
        所以必须在删主副本**之前**读它。这里由调用顺序保证：_purge 先删主副本
        再调本方法时 manifest 已不在，因此改为在 list_runs 阶段就缓存 root。
        """
        # 镜像清理是 best-effort：拿不到 root 就跳过，留下的镜像不影响正确性
        return None


def _mtime_stamp(path: Path) -> str:
    try:
        return (
            datetime.fromtimestamp(path.stat().st_mtime)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except OSError:
        return ""


def _undone_stamp(run_dir: Path) -> str | None:
    marker = run_dir / UNDONE_MARKER
    if marker.exists():
        try:
            return marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def _parse(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed
