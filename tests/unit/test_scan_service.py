"""ScanService 与 WorkerService 的测试。

services 层不含业务判断，所以这里不测扫描结果的正确性（那是 test_scanner.py 的
职责），只测**线程与信号的翻译**是否可靠：

    作业结果最终以 finished 发出
    core 抛的异常变成 failed 而不是让线程静默死掉
    取消走 cancelledSignal
    并发启动被拒绝（ScanSession 的增量维护假设单线程访问）
    收尾后 busy 归位、线程被回收

用 pytest-qt 的 qtbot.waitSignal 驱动事件循环；conftest 里已把 Qt 平台设为
offscreen，不依赖桌面会话。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import ScanOptions
from app.core.progress import ProgressSnapshot
from app.core.safety import SafetyGuard
from app.core.scanner import ScanDelta, ScanResult
from app.services.base import WorkerService
from app.services.scan_service import ScanService
from tests.fixtures.trees import build_tree

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")

TREE = {
    "报告.pdf": "x",
    "发票.pdf": "y",
    "旧资料": {"合同.docx": "z", "更深": {"a.txt": "w"}},
    "微信文件": {"图片.png": "p"},
}


@pytest.fixture
def guard() -> SafetyGuard:
    """tmp_path 在 AppData 之下，必须关掉段黑名单才能作为根目录。"""
    return SafetyGuard(system_roots=(), denied_segments=())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return build_tree(tmp_path / "下载", TREE)


@pytest.fixture
def service(guard: SafetyGuard, qtbot: object) -> ScanService:  # noqa: ARG001
    svc = ScanService(guard=guard)
    yield svc
    svc.cancel()
    svc.wait(5000)


# ---------------------------------------------------------------------------
# 准入
# ---------------------------------------------------------------------------


def test_check_root_allows_normal_directory(service: ScanService, root: Path) -> None:
    assert service.check_root(root).allowed


def test_rejected_root_emits_signal_and_skips_scan(
    service: ScanService, tmp_path: Path, qtbot
) -> None:
    missing = tmp_path / "不存在"

    with qtbot.waitSignal(service.rootRejected, timeout=2000) as blocker:
        started = service.start_scan(missing)

    assert started is False
    assert blocker.args[0].allowed is False
    assert service.session is None


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def test_scan_emits_finished_with_result(
    service: ScanService, root: Path, qtbot
) -> None:
    with qtbot.waitSignal(service.finished, timeout=10000) as blocker:
        assert service.start_scan(root) is True

    result = blocker.args[0]
    assert isinstance(result, ScanResult)
    assert {e.name for e in result.entries} == {"报告.pdf", "发票.pdf"}


def test_subfolders_ready_fires_before_finished(
    service: ScanService, root: Path, qtbot
) -> None:
    """UI 拿到 finished 时子文件夹清单必须已经就位。"""
    order: list[str] = []
    service.subfoldersReady.connect(lambda _s: order.append("subfolders"))
    service.finished.connect(lambda _r: order.append("finished"))

    with qtbot.waitSignal(service.finished, timeout=10000):
        service.start_scan(root)

    assert order == ["subfolders", "finished"]


def test_subfolders_ready_carries_the_list(
    service: ScanService, root: Path, qtbot
) -> None:
    with qtbot.waitSignal(service.subfoldersReady, timeout=10000) as blocker:
        service.start_scan(root)

    names = {info.name for info in blocker.args[0]}
    assert names == {"旧资料", "微信文件"}


def test_progress_is_forwarded_as_snapshots(
    service: ScanService, root: Path, qtbot
) -> None:
    seen: list[object] = []
    service.progressChanged.connect(seen.append)

    with qtbot.waitSignal(service.finished, timeout=10000):
        service.start_scan(root, ScanOptions())

    assert seen
    assert all(isinstance(s, ProgressSnapshot) for s in seen)


def test_scan_options_are_passed_through(
    service: ScanService, tmp_path: Path, qtbot
) -> None:
    r = build_tree(tmp_path / "r", {".env": "SECRET", "a.txt": "x"})

    with qtbot.waitSignal(service.finished, timeout=10000) as blocker:
        service.start_scan(r, ScanOptions(include_hidden=True))

    assert {e.name for e in blocker.args[0].entries} == {".env", "a.txt"}


# ---------------------------------------------------------------------------
# 勾选
# ---------------------------------------------------------------------------


def test_set_selected_emits_delta(service: ScanService, root: Path, qtbot) -> None:
    with qtbot.waitSignal(service.finished, timeout=10000):
        service.start_scan(root)

    with qtbot.waitSignal(service.deltaReady, timeout=10000) as blocker:
        assert service.start_set_selected(root / "旧资料", True) is True

    delta = blocker.args[0]
    assert isinstance(delta, ScanDelta)
    assert {e.name for e in delta.added} == {"合同.docx"}


def test_deselect_reports_removed(service: ScanService, root: Path, qtbot) -> None:
    with qtbot.waitSignal(service.finished, timeout=10000):
        service.start_scan(root)
    with qtbot.waitSignal(service.finished, timeout=10000):
        service.start_set_selected(root / "旧资料", True)

    with qtbot.waitSignal(service.deltaReady, timeout=10000) as blocker:
        service.start_set_selected(root / "旧资料", False)

    assert {p.name for p in blocker.args[0].removed} == {"合同.docx"}


def test_set_selected_without_session_is_refused(service: ScanService, root: Path) -> None:
    assert service.start_set_selected(root / "旧资料", True) is False


def test_expand_is_synchronous_and_does_not_select(
    service: ScanService, root: Path, qtbot
) -> None:
    with qtbot.waitSignal(service.finished, timeout=10000):
        service.start_scan(root)

    children = service.expand(root / "旧资料")

    assert {c.name for c in children} == {"更深"}
    assert service.session is not None
    assert service.session.selection().selected == set()


def test_expand_without_session_returns_empty(service: ScanService, root: Path) -> None:
    assert service.expand(root / "旧资料") == []


# ---------------------------------------------------------------------------
# 并发与状态
# ---------------------------------------------------------------------------


def test_second_launch_while_busy_is_refused(
    service: ScanService, tmp_path: Path, qtbot
) -> None:
    """并发跑两个作业会让 ScanSession 的状态互相踩。"""
    big = build_tree(tmp_path / "big", {f"f{i:05d}.txt": "x" for i in range(3000)})

    assert service.start_scan(big) is True
    second = service.start_scan(big)

    qtbot.waitSignal(service.finished, timeout=30000).wait()
    assert second is False


def test_busy_changed_brackets_the_job(service: ScanService, root: Path, qtbot) -> None:
    states: list[bool] = []
    service.busyChanged.connect(states.append)

    with qtbot.waitSignal(service.finished, timeout=10000):
        service.start_scan(root)

    assert states[0] is True
    assert states[-1] is False
    assert service.busy is False


def test_service_is_reusable_after_completion(
    service: ScanService, root: Path, qtbot
) -> None:
    for _ in range(3):
        with qtbot.waitSignal(service.finished, timeout=10000) as blocker:
            assert service.start_scan(root) is True
        assert len(blocker.args[0].entries) == 2


# ---------------------------------------------------------------------------
# 取消与异常
# ---------------------------------------------------------------------------


def test_cancel_before_start_yields_cancelled_result(
    service: ScanService, root: Path, qtbot
) -> None:
    """扫描被取消时 core 返回 cancelled=True 的结果，仍走 finished。"""
    service.cancel_token.cancel()

    with qtbot.waitSignal(service.finished, timeout=10000) as blocker:
        service.start_scan(root)

    # start_scan 会重建 CancelToken，因此这次扫描正常完成
    assert blocker.args[0].cancelled is False


def test_job_exception_becomes_failed_signal(qtbot) -> None:
    """core 抛异常必须变成 failed，而不是让线程静默死掉。"""

    class Boom(WorkerService):
        def explode(self) -> bool:
            def job(_cancel: object, _on_progress: object) -> None:
                raise RuntimeError("炸了")

            return self._launch(job)

    svc = Boom()
    with qtbot.waitSignal(svc.failed, timeout=5000) as blocker:
        assert svc.explode() is True

    assert "RuntimeError" in blocker.args[0]
    assert "炸了" in blocker.args[0]
    assert svc.busy is False


def test_operation_cancelled_goes_to_cancelled_signal(qtbot) -> None:
    from app.core.progress import OperationCancelled

    class Quitter(WorkerService):
        def go(self) -> bool:
            def job(_cancel: object, _on_progress: object) -> None:
                raise OperationCancelled()

            return self._launch(job)

    svc = Quitter()
    with qtbot.waitSignal(svc.cancelledSignal, timeout=5000):
        svc.go()

    assert svc.busy is False


def test_transform_failure_becomes_failed_signal(qtbot) -> None:
    class BadTransform(WorkerService):
        def go(self) -> bool:
            return self._launch(lambda _c, _p: 42)

        def _transform(self, result: object) -> object:
            raise ValueError("解释结果失败")

    svc = BadTransform()
    with qtbot.waitSignal(svc.failed, timeout=5000) as blocker:
        svc.go()

    assert "解释结果失败" in blocker.args[0]


def test_wait_returns_true_when_idle(service: ScanService) -> None:
    assert service.wait(100) is True


def test_unreadable_subdirectory_is_isolated_not_failed(
    service: ScanService, tmp_path: Path, qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫描内部的错误被隔离成 ScanResult.errors，不该变成 failed 信号。

    刻意让**子目录**读取失败而不是根目录：根目录不可读会在准入阶段就被判
    NO_PERMISSION 拒掉（那是正确行为），压根不会进扫描。
    """
    from app.core import scanner as scanner_module

    r = build_tree(tmp_path / "r", {"a.txt": "x", "禁区": {"b.txt": "y"}})
    blocked = str((r / "禁区").resolve())
    real = scanner_module.os.scandir

    def deny(path: object, *a: object, **k: object) -> object:
        if str(path) == blocked:
            raise PermissionError("拒绝访问")
        return real(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(scanner_module.os, "scandir", deny)

    with qtbot.waitSignal(service.finished, timeout=10000) as blocker:
        assert service.start_scan(r) is True

    result = blocker.args[0]
    assert {e.name for e in result.entries} == {"a.txt"}
    assert any(err.stage == "listdir" for err in result.errors)


def test_unreadable_root_is_rejected_at_admission(
    service: ScanService, tmp_path: Path, qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """根目录不可读应在准入阶段拒绝，而不是扫出一个空结果。"""
    from app.core import safety as safety_module
    from app.core.models import RejectReason

    r = build_tree(tmp_path / "r", {"a.txt": "x"})
    monkeypatch.setattr(
        safety_module.SafetyGuard, "_is_readable", staticmethod(lambda _p: False)
    )

    with qtbot.waitSignal(service.rootRejected, timeout=2000) as blocker:
        assert service.start_scan(r) is False

    assert blocker.args[0].reason is RejectReason.NO_PERMISSION
