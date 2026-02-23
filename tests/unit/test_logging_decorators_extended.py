"""Testes para os decoradores log_ai_interaction e log_http_request."""

from __future__ import annotations

import pytest

from src.infrastructure.logging.decorators import (
    log_ai_interaction,
    log_execution,
    log_http_request,
)


# ── log_ai_interaction ──────────────────────────────────────────────


class _FakeAgent:
    agent_id = "agent-1"
    model = "llama3"
    tool_id = "tool-x"


class TestLogAiInteraction:
    def test_success(self):
        @log_ai_interaction()
        def ai_call(obj, data):
            return "response"

        result = ai_call(_FakeAgent(), {"prompt": "hi"})
        assert result == "response"

    def test_success_with_kwargs(self):
        @log_ai_interaction()
        def ai_call(model_id="m1", factory_ia_model="ollama"):
            return "ok"

        result = ai_call(model_id="gpt-4", factory_ia_model="openai")
        assert result == "ok"

    def test_error_raises(self):
        @log_ai_interaction()
        def ai_fail():
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            ai_fail()

    def test_no_self_object(self):
        @log_ai_interaction()
        def standalone():
            return 42

        assert standalone() == 42


# ── log_http_request ────────────────────────────────────────────────


class _FakeHttpTool:
    route = "http://example.com/api/{id}"
    http_method = "GET"
    id = "tool-1"


class TestLogHttpRequest:
    def test_success(self):
        @log_http_request()
        def http_call(obj):
            return '{"status": 200}'

        result = http_call(_FakeHttpTool())
        assert "200" in result

    def test_success_with_status_categories(self):
        @log_http_request()
        def call_200():
            return "Response 200 OK"

        @log_http_request()
        def call_400():
            return "Error 400 bad request"

        @log_http_request()
        def call_500():
            return "Error 500 internal"

        assert "200" in call_200()
        assert "400" in call_400()
        assert "500" in call_500()

    def test_error_raises(self):
        @log_http_request()
        def http_fail(obj):
            raise ConnectionError("timeout")

        with pytest.raises(ConnectionError, match="timeout"):
            http_fail(_FakeHttpTool())

    def test_no_self_object(self):
        @log_http_request()
        def plain_call():
            return "plain"

        assert plain_call() == "plain"


# ── log_execution extras ────────────────────────────────────────────


class TestLogExecutionExtras:
    def test_mask_sensitive_kwargs(self):
        @log_execution(include_args=True, mask_sensitive=True)
        def fn(password="FAKE_PASS_FOR_TEST", data="safe"):  # noqa: S107
            return True

        assert fn(password="s3cret_test", data="value") is True

    def test_no_masking(self):
        @log_execution(include_args=True, mask_sensitive=False)
        def fn(password="FAKE_PASS_FOR_TEST"):  # noqa: S107
            return True

        assert fn(password="plain_test") is True

    def test_with_dict_arg(self):
        @log_execution(include_args=True, mask_sensitive=True)
        def fn(config):
            return config

        result = fn({"api_key": "FAKE_KEY_FOR_TESTING", "name": "test"})  # noqa: S106
        assert result == {"api_key": "FAKE_KEY_FOR_TESTING", "name": "test"}

    def test_with_self_like_object(self):
        """Função com primeiro arg que tem __dict__ (simula self)."""
        @log_execution(include_args=True)
        def method(self_like, x):
            return x * 2

        obj = type("Obj", (), {"__dict__": {}})()
        assert method(obj, 5) == 10
