"""Regression: ДЫРА 5 — pydantic-ошибка с api_key не утекает в WS-ответ.

Реальный путь: settings:update → _update_cloud_settings бросает pydantic
ValidationError, у которого в тексте остаётся сырой input_value.
_on_message обязан отправить клиенту сообщение через _safe_error_text.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _pydantic_error_containing(secret: str):
    from pydantic import BaseModel, Field, ValidationError

    class _Model(BaseModel):
        name: str = Field(max_length=5)

    try:
        _Model(name=secret)  # слишком длинно → ValidationError с сырым input_value
    except ValidationError as exc:
        # Предусловие: сырая ошибка ДЕЙСТВИТЕЛЬНО содержит значение.
        assert secret in str(exc)
        return exc
    raise AssertionError("не удалось получить ValidationError")


def test_safe_error_text_strips_pydantic_input_value():
    from core.ws_server import _safe_error_text

    secret = "sk-super-secret-key-12345"
    exc = _pydantic_error_containing(secret)
    safe = _safe_error_text(exc)
    assert secret not in safe, f"ключ утёк через _safe_error_text: {safe!r}"


def test_settings_update_ws_error_does_not_leak_api_key():
    from tests.regressions.test_hole5_ws_error_no_key_leak import _make_ws_server

    server = _make_ws_server()
    secret = "sk-live-leaky-key-98765"

    def _boom(_patch):
        raise _pydantic_error_containing(secret)

    server._update_cloud_settings = _boom  # type: ignore[method-assign]

    sent: list[str] = []

    class _WS:
        async def send(self, raw: str) -> None:
            sent.append(raw)

    asyncio.run(server._on_message(
        _WS(), json.dumps({"type": "settings:update", "settings": {"api_key": secret}}),
    ))

    assert sent, "сервер не отправил ответ на settings:update"
    parsed = [json.loads(raw) for raw in sent]
    assert any(p.get("type") == "error" for p in parsed), parsed
    for raw in sent:
        assert secret not in raw, f"API-ключ утёк в WS-ответ: {raw!r}"
