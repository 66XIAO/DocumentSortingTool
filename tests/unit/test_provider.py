"""Provider 协议与实现单元测试。"""

from __future__ import annotations

from typing import get_args

import pytest

from app.core.llm.provider import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FailureKind,
    OpenAICompatProvider,
    OllamaProvider,
    Provider,
    ProviderError,
    _classify_http_error,
    make_provider,
)


class TestFailureKind:
    def test_all_kinds_valid(self) -> None:
        """五类失败都应有对应的字符串值。"""
        kinds = get_args(FailureKind)
        assert set(kinds) == {"timeout", "non_2xx", "schema", "quota_exhausted", "network"}


class TestProviderError:
    def test_attributes(self) -> None:
        err = ProviderError("timeout", "msg", "detail")
        assert err.kind == "timeout"
        assert err.detail == "detail"
        assert "msg" in str(err)

    def test_str_without_detail(self) -> None:
        err = ProviderError("network", "网络错误")
        assert "[网络" not in str(err) or "[network]" in str(err)


class TestClassifyHttpError:
    def test_429_is_quota(self) -> None:
        assert _classify_http_error(429) == "quota_exhausted"

    def test_401_is_quota(self) -> None:
        assert _classify_http_error(401) == "quota_exhausted"

    def test_500_is_non_2xx(self) -> None:
        assert _classify_http_error(500) == "non_2xx"

    def test_400_is_non_2xx(self) -> None:
        assert _classify_http_error(400) == "non_2xx"


class TestMakeProvider:
    def test_openai_compat(self) -> None:
        p = make_provider("openai_compat", base_url="https://api.example.com", api_key="sk-xxx", model="gpt-4")
        assert isinstance(p, OpenAICompatProvider)
        assert p.name == "openai_compat"

    def test_ollama(self) -> None:
        p = make_provider("ollama", host="http://localhost:11434", model="llama3")
        assert isinstance(p, OllamaProvider)
        assert p.name == "ollama"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="未知"):
            make_provider("unknown")


class TestProtocolConformance:
    def test_openai_is_provider(self) -> None:
        p = OpenAICompatProvider(base_url="https://api.example.com", api_key="k", model="m")
        assert isinstance(p, Provider)

    def test_ollama_is_provider(self) -> None:
        p = OllamaProvider(host="http://localhost:11434", model="m")
        assert isinstance(p, Provider)


class TestChatRequest:
    def test_frozen(self) -> None:
        msg = ChatMessage(role="user", content="hi")
        req = ChatRequest(messages=(msg,), temperature=0.0)
        assert req.temperature == 0.0
        with pytest.raises(AttributeError):
            req.temperature = 0.5  # type: ignore[misc]


class TestChatResponse:
    def test_content(self) -> None:
        resp = ChatResponse(content="hello")
        assert resp.content == "hello"
