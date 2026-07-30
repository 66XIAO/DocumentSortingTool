"""WorkerService：QThread 生命周期与信号翻译。

本层是唯一同时看见 Qt 与 core 的地方，职责只有两件：

1. 把 core 的阻塞调用放进 ``QThread``
2. 把 core 的 ``on_progress(snapshot)`` 普通回调翻译成 Qt 信号

**不含任何业务判断。** core 里没有 ``Signal``，这里没有业务逻辑——这条分界让 core
的 42 条正确性属性可以不拉起 ``QApplication`` 就跑完。

## 为什么 job 是一个普通 callable

``WorkerService`` 不知道自己在跑扫描还是执行，只知道要跑一个
``(CancelToken, ProgressSink) -> Any`` 的函数。这样线程与取消的处理只需写一遍，
五个子类各自只负责组装 job 与解释结果。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from app.core.progress import CancelToken, OperationCancelled, ProgressSnapshot

logger = logging.getLogger(__name__)

#: core 侧的作业签名：拿到取消令牌与进度回调，返回任意结果
Job = Callable[[CancelToken, Callable[[ProgressSnapshot], None]], Any]


class _Worker(QObject):
    """在工作线程里跑一个 job。

    刻意不持有 service 的引用：跨线程访问 service 的状态是竞态的温床，所有结果
    都通过信号回主线程。
    """

    progress = Signal(object)  # ProgressSnapshot
    done = Signal(object)
    cancelled = Signal()
    error = Signal(str)

    def __init__(self, job: Job, cancel: CancelToken) -> None:
        super().__init__()
        self._job = job
        self._cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            result = self._job(self._cancel, self.progress.emit)
        except OperationCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 工作线程里的异常必须转成信号
            logger.exception("后台作业失败")
            detail = f"{type(exc).__name__}: {exc}"
            self.error.emit(detail)
        else:
            self.done.emit(result)


class WorkerService(QObject):
    """后台作业的通用外壳。"""

    progressChanged = Signal(object)  # ProgressSnapshot
    finished = Signal(object)
    failed = Signal(str)
    cancelledSignal = Signal()
    busyChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._cancel = CancelToken()

    # -- 状态 -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel

    # -- 控制 -------------------------------------------------------------

    def cancel(self) -> None:
        """请求取消。协作式：真正的及时性取决于 core 侧检查点的密度。"""
        self._cancel.cancel()

    def wait(self, timeout_ms: int = 5000) -> bool:
        """等待作业收尾。供关闭窗口与测试使用。"""
        thread = self._thread
        if thread is None:
            return True
        return thread.wait(timeout_ms)

    # -- 内部：启动 -------------------------------------------------------

    def _launch(self, job: Job) -> bool:
        """启动一个 job。已有作业在跑时拒绝并返回 False。

        拒绝而不是排队：两次扫描并发跑会让 ``ScanSession`` 的状态互相踩，而
        ScanSession 的增量维护假设了单线程访问。
        """
        if self.busy:
            logger.warning("已有后台作业在运行，忽略本次请求")
            return False

        self._cancel = CancelToken()
        thread = QThread()
        worker = _Worker(job, self._cancel)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self.progressChanged)
        worker.done.connect(self._on_done)
        worker.cancelled.connect(self._on_cancelled)
        worker.error.connect(self._on_error)

        self._thread = thread
        self._worker = worker
        thread.start()
        self.busyChanged.emit(True)
        return True

    # -- 内部：收尾 -------------------------------------------------------

    @Slot(object)
    def _on_done(self, result: Any) -> None:
        self._teardown()
        try:
            payload = self._transform(result)
        except Exception as exc:  # noqa: BLE001 - 结果解释失败也要报出来
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            return
        self.finished.emit(payload)

    @Slot()
    def _on_cancelled(self) -> None:
        self._teardown()
        self.cancelledSignal.emit()

    @Slot(str)
    def _on_error(self, detail: str) -> None:
        self._teardown()
        self.failed.emit(detail)

    def _teardown(self) -> None:
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()
        self.busyChanged.emit(False)

    # -- 子类钩子 ---------------------------------------------------------

    def _transform(self, result: Any) -> Any:
        """在 ``finished`` 发出前加工结果。子类可覆写以额外发别的信号。"""
        return result
