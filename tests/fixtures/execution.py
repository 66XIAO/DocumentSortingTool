"""M4（执行与撤销）测试的公共装配。

M4 的每个测试都要走同一段路：建树 → 扫描 → 生成方案 → 开 journal → 执行。把这段
装配抽出来的理由不只是省字数——**执行与撤销必须在完全相同的装配上比较**，否则
「撤销后与执行前一致」这类断言可能是装配差异造成的假绿。

三条约定：

**守卫必须放开。** pytest 的 ``tmp_path`` 位于 ``AppData\\Local\\Temp`` 之下，默认
``SafetyGuard`` 会把它判成系统路径而拒绝准入。测试用 ``open_guard()``。

**回收站必须替换。** 默认 ``to_trash`` 会把测试文件塞进用户真实的回收站。全部执行
路径统一注入 ``TrashRecorder``，既隔离副作用，也让属性 27 的「trashed 记录集合等于
实际进回收站的路径集合」可断言。

**run_id 可指定。** 崩溃恢复类测试需要先造一份被截断的 journal 再让 HistoryManager
去发现它，run_id 不可控就写不出这种场景。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.executor import Executor
from app.core.journal import (
    Journal,
    mirror_dir_for,
    new_run_id,
    utc_stamp,
)
from app.core.models import (
    ClassifyOptions,
    ConflictPolicy,
    ExecOptions,
    ExecutionReport,
    JournalRecord,
    Manifest,
    OverrideSet,
    RecordKind,
    ScanSelection,
    SortPlan,
    SourceSnapshotEntry,
    Strategy,
)
from app.core.planner import Planner
from app.core.progress import DEFAULT_INTERVAL_MS, CancelToken, ProgressSink
from app.core.safety import SafetyGuard
from app.core.scanner import ScanSession
from tests.fixtures.doubles import TrashRecorder

APP_VERSION = "test-0.0.0"


def open_guard() -> SafetyGuard:
    """放开系统路径判定的守卫。

    ``tmp_path`` 在 ``AppData\\Local\\Temp`` 之下，默认守卫会拒绝它——那是生产环境
    该有的行为，但会让每个用真实文件的测试都无法开始。
    """
    return SafetyGuard(system_roots=(), denied_segments=())


def scan_root(root: Path, selected: tuple[Path, ...] = ()) -> ScanSession:
    """扫描根目录并勾选给定子文件夹。"""
    session = ScanSession(root, guard=open_guard())
    session.initial_scan()
    for folder in selected:
        session.select(folder)
    return session


def build_plan(
    root: Path,
    *,
    strategy: Strategy = Strategy.BY_TYPE,
    options: ClassifyOptions | None = None,
    conflict_policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME,
    selected: tuple[Path, ...] = (),
    overrides: OverrideSet | None = None,
) -> tuple[SortPlan, ScanSelection]:
    """扫描 + 规划。返回方案与当时的勾选状态。

    勾选状态要一起返回：空目录清理的候选集合是 ``moved_sources ∩ 勾选闭包``，
    执行时拿不到同一个 ScanSelection 就无法复现范围。
    """
    session = scan_root(root, selected)
    guard = open_guard()
    result = Planner(guard=guard).build(
        session.entries(),
        root=root,
        strategy=strategy,
        options=options or ClassifyOptions(strategy=strategy),
        conflict_policy=conflict_policy,
        overrides=overrides,
        selection=session.selection(),
        check_locked=False,
    )
    return result.plan, session.selection()


def build_manifest(
    run_id: str,
    plan: SortPlan,
    options: ExecOptions | None = None,
    overrides: OverrideSet | None = None,
) -> Manifest:
    """执行前的现场快照。与 ExecuteService._build_manifest 同构。"""
    options = options or ExecOptions()
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
        source_snapshot=[
            SourceSnapshotEntry(
                path=str(item.entry.path),
                size=item.entry.size,
                mtime=item.entry.mtime,
            )
            for item in plan.all_items()
        ],
        overrides=overrides or OverrideSet(),
        app_version=APP_VERSION,
    )


@dataclass
class RunResult:
    """一次执行的全部产物，供断言取用。"""

    run_id: str
    root: Path
    history: Path
    plan: SortPlan
    selection: ScanSelection
    report: ExecutionReport
    trash: TrashRecorder
    options: ExecOptions
    before: set[Path] = field(default_factory=set)

    @property
    def run_dir(self) -> Path:
        return self.history / self.run_id

    @property
    def mirror_dir(self) -> Path:
        return mirror_dir_for(self.root, self.run_id)

    def records(self) -> list[JournalRecord]:
        return Journal.read_records(self.run_dir)

    def mirror_records(self) -> list[JournalRecord]:
        return Journal.read_records(self.mirror_dir)

    def of_kind(self, kind: RecordKind) -> list[JournalRecord]:
        return [r for r in self.records() if r.kind is kind]

    def manifest(self) -> Manifest | None:
        return Journal.read_manifest(self.run_dir)


def execute_plan(
    plan: SortPlan,
    history: Path,
    *,
    options: ExecOptions | None = None,
    selection: ScanSelection | None = None,
    trash: TrashRecorder | None = None,
    cancel: CancelToken | None = None,
    on_progress: ProgressSink | None = None,
    run_id: str | None = None,
    write_journal: bool = True,
    mirror: bool = True,
    progress_interval_ms: int = DEFAULT_INTERVAL_MS,
) -> RunResult:
    """执行方案，写完整 journal（含根目录镜像）。

    ``progress_interval_ms`` 传 0 可关掉节流：想在进度回调里触发取消的测试必须
    这么做，否则 5 个小文件的中间进度会被 200ms 窗口合并掉，取消永远触发不了。
    """
    options = options or ExecOptions()
    trash = trash or TrashRecorder()
    run_id = run_id or new_run_id()
    root = Path(plan.root)
    before = tree_paths(root)

    executor = Executor(trash=trash, progress_interval_ms=progress_interval_ms)

    if not write_journal:
        report = executor.run(
            plan,
            journal=None,
            options=options,
            selection=selection,
            cancel=cancel,
            on_progress=on_progress,
        )
    else:
        mirror_dir = mirror_dir_for(root, run_id) if mirror else None
        with Journal(history / run_id, mirror_dir) as journal:
            journal.write_manifest(build_manifest(run_id, plan, options))
            report = executor.run(
                plan,
                journal=journal,
                options=options,
                selection=selection,
                cancel=cancel,
                on_progress=on_progress,
            )

    return RunResult(
        run_id=run_id,
        root=root,
        history=history,
        plan=plan,
        selection=selection or ScanSelection(root=root),
        report=report,
        trash=trash,
        options=options,
        before=before,
    )


def sort_and_execute(
    root: Path,
    history: Path,
    *,
    strategy: Strategy = Strategy.BY_TYPE,
    conflict_policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME,
    options: ExecOptions | None = None,
    selected: tuple[Path, ...] = (),
    trash: TrashRecorder | None = None,
    run_id: str | None = None,
) -> RunResult:
    """建方案并执行，一步到位。绝大多数测试只需要这个入口。"""
    exec_options = options or ExecOptions(conflict_policy=conflict_policy)
    plan, selection = build_plan(
        root,
        strategy=strategy,
        conflict_policy=exec_options.conflict_policy,
        selected=selected,
    )
    return execute_plan(
        plan,
        history,
        options=exec_options,
        selection=selection,
        trash=trash,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# 文件树快照
# ---------------------------------------------------------------------------


def tree_paths(root: Path, skip_metadata: bool = True) -> set[Path]:
    """root 之下的全部路径（含目录），用于「前后完全一致」这类断言。

    ``.docsort`` 默认排除：镜像日志是本次 run 自己写出来的，把它算进快照会让
    「执行前后路径集合一致」永远为假，而那与用户文件的安全性无关。
    """
    collected: set[Path] = set()
    if not root.exists():
        return collected
    for path in root.rglob("*"):
        if skip_metadata and ".docsort" in path.parts:
            continue
        collected.add(path)
    return collected


def file_fingerprints(root: Path) -> dict[Path, tuple[int, bytes]]:
    """相对路径 -> (大小, 内容)。撤销往返比较内容而不只比较路径。"""
    prints: dict[Path, tuple[int, bytes]] = {}
    for path in sorted(tree_paths(root)):
        if not path.is_file():
            continue
        data = path.read_bytes()
        prints[path.relative_to(root)] = (len(data), data)
    return prints


def dirs_under(root: Path) -> set[Path]:
    return {p for p in tree_paths(root) if p.is_dir()}


def make_history_run(
    history: Path,
    run_id: str,
    created_at: str,
    *,
    done: int = 1,
    failed: int = 0,
    removed_dirs: int = 0,
    unfinished: bool = False,
) -> Path:
    """造一条合成的历史 run，不走真实执行。

    保留策略与列表排序测的是历史管理本身，跑一遍真实执行只会让 30 条 run 的测试慢
    上百倍，而且引入与被测逻辑无关的失败面。
    """
    from app.core.models import RecordKind, SortPlan, Strategy

    root = history.parent / "root"
    root.mkdir(parents=True, exist_ok=True)
    plan = SortPlan(
        root=root,
        strategy=Strategy.BY_TYPE,
        categories=[],
        items=[],
        unclassified=[],
    )
    manifest = build_manifest(run_id, plan)
    manifest.created_at = created_at

    run_dir = history / run_id
    with Journal(run_dir) as journal:
        journal.write_manifest(manifest)
        for index in range(done):
            src = root / f"{run_id}-{index}.txt"
            dst = root / "c" / src.name
            journal.append(RecordKind.INTENT, src=src, dst=dst)
            journal.append(RecordKind.DONE, src=src, dst=dst)
        for index in range(failed):
            src = root / f"{run_id}-f{index}.txt"
            journal.append(RecordKind.INTENT, src=src, dst=root / "c" / src.name)
            journal.append(RecordKind.FAILED, src=src, error="注入的失败")
        for index in range(removed_dirs):
            journal.append(RecordKind.REMOVED_DIR, dst=root / f"空{index}")
        if unfinished:
            src = root / f"{run_id}-pending.txt"
            journal.append(RecordKind.INTENT, src=src, dst=root / "c" / src.name)
    return run_dir


def touch(path: Path, content: str | bytes = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path
