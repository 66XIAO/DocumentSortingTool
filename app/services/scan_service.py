"""ScanService：把 ScanSession 的阻塞调用放进 QThread。

``ScanSession`` 的增量维护假设单线程访问（``_listing_cache`` 与 ``_selection``
不是线程安全的）。基类的 ``_launch`` 在已有作业运行时直接拒绝，正是为了维持这个
假设——所以本类不需要额外加锁。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.core.models import ScanOptions, ScanScope
from app.core.safety import AdmissionResult, SafetyGuard
from app.core.scanner import ScanDelta, ScanResult, ScanSession
from app.services.base import WorkerService


class ScanService(WorkerService):
    """扫描与增量补扫。"""

    #: 扫描完成后单独发一次子文件夹清单，供选择器直接填充（需求 2.4、2.6）
    subfoldersReady = Signal(list)
    #: 勾选变更的增量结果（需求 2.10、2.11）
    deltaReady = Signal(object)
    #: 根目录准入被拒（需求 1.1-1.3）
    rootRejected = Signal(object)

    def __init__(
        self,
        guard: SafetyGuard | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._guard = SafetyGuard() if guard is None else guard
        self._session: ScanSession | None = None

    # -- 只读访问 ---------------------------------------------------------

    @property
    def session(self) -> ScanSession | None:
        return self._session

    @property
    def guard(self) -> SafetyGuard:
        return self._guard

    def entries(self) -> list:
        return self._session.entries() if self._session else []

    def scope(self) -> ScanScope:
        return self._session.scope() if self._session else ScanScope.TOP_LEVEL_ONLY

    # -- 准入 -------------------------------------------------------------

    def check_root(self, root: Path) -> AdmissionResult:
        """同步判定，不开线程：纯路径运算，微秒级。"""
        result = self._guard.check_root(Path(root))
        if not result.allowed:
            self.rootRejected.emit(result)
        return result

    # -- 扫描 -------------------------------------------------------------

    def start_scan(self, root: Path, options: ScanOptions | None = None) -> bool:
        """准入通过后开始扫描。返回是否真的启动了作业。

        准入失败时不启动，并已通过 ``rootRejected`` 告知调用方——把这道拦截放在
        启动之前，用户就不会看到一个「扫描中」然后才被拒。
        """
        admission = self.check_root(Path(root))
        if not admission.allowed:
            return False

        resolved = admission.resolved or Path(root)
        session = ScanSession(resolved, options=options, guard=self._guard)
        self._session = session

        return self._launch(
            lambda cancel, on_progress: session.initial_scan(
                cancel=cancel, on_progress=on_progress
            )
        )

    # -- 勾选 -------------------------------------------------------------

    def start_set_selected(self, folder: Path, selected: bool) -> bool:
        """勾选或取消勾选一个子文件夹，并补扫新增范围。"""
        session = self._session
        if session is None:
            return False

        target = Path(folder)
        return self._launch(
            lambda cancel, _on_progress: session.set_selected(
                target, selected, cancel=cancel
            )
        )

    def expand(self, folder: Path) -> list:
        """展开一层子文件夹。

        同步执行：展开只读单个目录，不值得开线程，而且 UI 的树形控件在
        ``expanded`` 信号里就要拿到子节点。
        """
        if self._session is None:
            return []
        return self._session.expand(Path(folder))

    # -- 结果解释 ---------------------------------------------------------

    def _transform(self, result: object) -> object:
        """按结果类型补发对应的专用信号。

        基类只知道「作业完成了」，由这里区分是初次扫描还是勾选增量——放在
        ``_transform`` 里能保证专用信号先于 ``finished`` 发出，UI 拿到 finished
        时子文件夹清单已经就位。
        """
        if isinstance(result, ScanResult):
            self.subfoldersReady.emit(list(result.subfolders))
        elif isinstance(result, ScanDelta):
            self.deltaReady.emit(result)
        return result
