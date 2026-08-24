"""ConnectivityService：把「测试连通性」放进工作线程。需求 7.4。

## 为什么必须开线程

原实现在设置页的按钮回调里直接调 ``provider.test_connectivity()``。它确实有 10 秒
硬超时，但那 10 秒是**冻在 UI 线程上的**：填错了 base_url、或者服务所在网络不可达
时，整个窗口会假死到超时为止——用户既看不到「正在测试」，也没法取消，只会以为程序
崩了。网络调用是这个应用里唯一时长完全不受自己控制的操作，它必须离开 UI 线程。

## 为什么失败不走 failed 信号

``WorkerService`` 的 ``failed`` 是给「作业本身出错了」用的，它把异常类名拼进消息
（``ProviderError: [timeout] ...``），那串东西不该给用户看。连不上服务是这个作业的
**正常结果之一**，所以统一用 ``finished`` 带回 ``ConnectivityResult``，由 UI 决定
显示成功还是失败提示。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject

from app.core.llm.provider import ProviderError
from app.core.llm.runner import make_provider_for
from app.core.models import AIOptions
from app.services.base import WorkerService

logger = logging.getLogger(__name__)

#: 需求 7.4 规定的上限：必须在 10 秒内给出结果或失败原因。
TIMEOUT_SECONDS = 10.0

#: 五类失败的可读说明，与 llm.runner 保持同一套措辞。
_FAILURE_LABELS = {
    "timeout": "请求超时",
    "non_2xx": "服务返回错误状态",
    "schema": "返回内容不符合约定格式",
    "quota_exhausted": "额度耗尽或凭据无效",
    "network": "网络连接失败",
}


@dataclass(frozen=True)
class ConnectivityResult:
    """一次连通性测试的结果。"""

    ok: bool
    message: str


class ConnectivityService(WorkerService):
    """在工作线程里发一次最小请求。"""

    def start_test(self, options: AIOptions, api_key: str = "") -> bool:
        """启动测试。已有测试在跑时返回 False（基类拒绝并发作业）。

        ``options`` 与 ``api_key`` 由调用方从界面控件现取——用户可能还没保存，
        要测的是**当前填在框里**的那套参数。
        """

        def job(_cancel: object, _on_progress: object) -> ConnectivityResult:
            try:
                provider = make_provider_for(options, api_key)
            except ValueError as exc:
                # 未知 Provider 类型：配置问题，不是网络问题
                return ConnectivityResult(False, str(exc))

            try:
                message = provider.test_connectivity(
                    timeout_seconds=TIMEOUT_SECONDS
                )
            except ProviderError as exc:
                label = _FAILURE_LABELS.get(exc.kind, exc.kind)
                detail = f"：{exc.detail}" if exc.detail else ""
                return ConnectivityResult(False, f"{label}{detail}")
            except Exception as exc:  # noqa: BLE001 - 任何意外都要变成可读结果
                logger.warning("连通性测试异常", exc_info=True)
                return ConnectivityResult(False, f"{type(exc).__name__}: {exc}")

            return ConnectivityResult(True, message)

        return self._launch(job)


__all__ = [
    "TIMEOUT_SECONDS",
    "ConnectivityResult",
    "ConnectivityService",
]
