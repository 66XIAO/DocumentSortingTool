"""Provider 协议与统一的五类失败分类。

需求 7.1 要求提供 ``OpenAICompatProvider`` 与 ``OllamaProvider`` 两个实现。
两者共享同一个 ``Provider`` 协议，但内部走不同的 API 路径：
    OpenAICompatProvider  POST /v1/chat/completions（Bearer <REDACTED>）
    OllamaProvider         POST /api/chat（本地 HTTP，无需鉴权）

五类失败（需求 6.5）：超时、非 2xx、schema 不符、额度耗尽、网络错误。
统一成 ``ProviderError`` 异常，调用方按类型决定是回落还是中止整批。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import httpx

#: 五类失败分类。需求 6.5。
FailureKind = Literal["timeout", "non_2xx", "schema", "quota_exhausted", "network"]


class ProviderError(Exception):
    """Provider 调用失败。

    统一的异常类型让上层只需捕获一种，按 ``kind`` 决定处理方式。
    """

    def __init__(self, kind: FailureKind, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"[{self.kind}] {self.args[0]}: {self.detail}"
        return f"[{self.kind}] {self.args[0]}"


@dataclass(frozen=True)
class ChatMessage:
    """单条对话消息。"""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ChatRequest:
    """一次对话请求。"""

    messages: tuple[ChatMessage, ...]
    temperature: float = 0.0
    response_format: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChatResponse:
    """一次对话响应。"""

    content: str  # assistant 回复的原始文本


@runtime_checkable
class Provider(Protocol):
    """LLM 服务适配层协议。需求 7.1。

    协议而非基类：两个实现没有共享代码，强制继承只会引入无用的耦合。
    """

    name: str

    def chat(self, request: ChatRequest, timeout_seconds: float) -> ChatResponse: ...

    def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
        """发起一次最小请求，返回成功提示或抛出 ProviderError。需求 7.4。"""


def _classify_http_error(status: int) -> FailureKind:
    """按 HTTP 状态码归类失败。需求 6.5。"""
    if status == 429:
        return "quota_exhausted"
    if status in (401, 403):
        return "quota_exhausted"
    if status >= 500:
        return "non_2xx"
    return "non_2xx"


def _extract_error_detail(response: httpx.Response) -> str:
    """从错误响应中提取可读信息。"""
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error", {})
            if isinstance(error, dict):
                return str(error.get("message", response.text[:200]))
        return str(data)[:200]
    except Exception:
        return response.text[:200]


class OpenAICompatProvider:
    """兼容 /v1/chat/completions 的 Provider。需求 7.1。

    覆盖所有兼容该 API 的服务：OpenAI、DeepSeek、Moonshot、本地 vLLM 等。
    """

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def chat(self, request: ChatRequest, timeout_seconds: float) -> ChatResponse:
        url = f"{self._base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
            "temperature": request.temperature,
        }
        if request.response_format:
            payload["response_format"] = request.response_format

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "LLM 请求超时", str(exc)) from exc
        except httpx.NetworkError as exc:
            raise ProviderError("network", "网络连接失败", str(exc)) from exc

        if response.status_code != 200:
            detail = _extract_error_detail(response)
            raise ProviderError(
                _classify_http_error(response.status_code),
                f"HTTP {response.status_code}",
                detail,
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return ChatResponse(content=content)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "schema", "响应格式不符合 OpenAI 约定", str(exc)
            ) from exc

    def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
        """发一条最小请求确认服务可达。需求 7.4。"""
        request = ChatRequest(
            messages=(ChatMessage("user", "ping"),),
            temperature=0.0,
        )
        response = self.chat(request, timeout_seconds=timeout_seconds)
        return f"连接成功：{response.content[:50]}"


class OllamaProvider:
    """本地 Ollama Provider。需求 7.1。

    Ollama 跑在本地，零外发，隐私最优。API 路径和参数与 OpenAI 不兼容，
    但消息结构相似。
    """

    name = "ollama"

    def __init__(self, host: str, model: str) -> None:
        self._host = host.rstrip("/")
        self._model = model

    def chat(self, request: ChatRequest, timeout_seconds: float) -> ChatResponse:
        url = f"{self._host}/api/chat"
        payload = {
            "model": self._model,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
            "stream": False,
            "options": {
                "temperature": request.temperature,
            },
        }
        if request.response_format:
            payload["format"] = request.response_format.get("json_schema")

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "Ollama 请求超时", str(exc)) from exc
        except httpx.NetworkError as exc:
            raise ProviderError("network", "无法连接 Ollama 服务", str(exc)) from exc

        if response.status_code != 200:
            detail = _extract_error_detail(response)
            raise ProviderError(
                _classify_http_error(response.status_code),
                f"HTTP {response.status_code}",
                detail,
            )

        try:
            data = response.json()
            content = data["message"]["content"]
            return ChatResponse(content=content)
        except (KeyError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "schema", "响应格式不符合 Ollama 约定", str(exc)
            ) from exc

    def test_connectivity(self, timeout_seconds: float = 10.0) -> str:
        """确认 Ollama 服务可达。需求 7.4。"""
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.get(f"{self._host}/api/tags")
            if response.status_code == 200:
                data = response.json()
                models = [m.get("name", "?") for m in data.get("models", [])]
                return f"连接成功，可用模型：{', '.join(models[:5])}"
            return f"HTTP {response.status_code}"
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "连接 Ollama 超时", str(exc)) from exc
        except httpx.NetworkError as exc:
            raise ProviderError("network", "无法连接 Ollama", str(exc)) from exc


def make_provider(
    kind: str,
    *,
    base_url: str = "",
    api_key: str = "",
    host: str = "",
    model: str = "",
) -> Provider:
    """按类型构造 Provider。"""
    if kind == "openai_compat":
        return OpenAICompatProvider(base_url=base_url, api_key=api_key, model=model)
    if kind == "ollama":
        return OllamaProvider(host=host, model=model)
    raise ValueError(f"未知 Provider 类型: {kind}")
