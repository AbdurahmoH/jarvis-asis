"""E2E-тест clarify-цикла в Orchestrator.

Проверяет:
1. Вопрос -> clarify-ответ пользователю с assistant_output (НЕ confirmation_required).
2. Ответ пользователя («да» / «первое» / «закрой») -> резолвинг кандидата и исполнение.
3. Истечение 60-секундного TTL -> сессия сбрасывается, следующая реплика маршрутизируется с нуля.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from core.actions.base import ActionResult
from core.agent import AgentOutcome
from core.orchestrator import Orchestrator
from core.routing.semantic_router import RoutingDecision
from core.voice import AssistantOutput


def test_clarify_cycle_e2e_resolution():
    settings = Settings()
    settings.offline_mode = True
    settings.deepseek_brain_mode = False

    orch = Orchestrator(settings)

    tts_outputs: list[AssistantOutput] = []
    plain_outputs: list[str] = []
    orch._queue_assistant_output = lambda output: tts_outputs.append(output)
    orch._output_callback = lambda text: plain_outputs.append(text)
    orch._remember_exchange_background = lambda *a, **kw: None

    # 1. Запрос, вызывающий clarify
    clarify_decision = RoutingDecision(
        kind="clarify",
        tool=None,
        confidence=0.51,
        tier="clarify",
        risk="low",
        needs_confirmation=False,
        candidates=[("close_app", 0.45), ("open_app", 0.42)],
        clarify_question="Уточните: вы хотите закрыть или открыть приложение?",
        trace={"raw_text": "окно браузера"},
    )

    with patch("core.orchestrator.semantic_route", return_value=clarify_decision):
        state1 = orch.handle_input("окно браузера", channel="test")

    # Проверяем, что ответ - clarification, уходит в assistant_output (не confirmation)
    assert state1.get("mode") == "clarification"
    assert "закрыть или открыть" in state1.get("response", "")
    assert orch._pending_clarification() is not None
    assert orch._pending_clarification()["question"] == clarify_decision.clarify_question

    # 2. Пользователь отвечает "закрой"
    with patch(
        "core.actions.app_control.close_app",
        return_value=ActionResult(tool="close_app", args={"name": "браузер"}, ok=True, output="Закрыл браузер."),
    ), patch("core.verifier._process_matches", return_value=[]):
        state2 = orch.handle_input("закрой", channel="test")

    # Сессия очищена
    assert orch._pending_clarification() is None
    # Запрос исполнен выбранным инструментом close_app
    assert (
        state2.get("tool_used") == "close_app"
        or state2.get("tool") == "close_app"
        or "закр" in state2.get("response", "").lower()
    )


def test_clarify_cycle_ttl_expiry():
    settings = Settings()
    settings.offline_mode = True
    settings.deepseek_brain_mode = False

    orch = Orchestrator(settings)
    orch._remember_exchange_background = lambda *a, **kw: None

    clarify_decision = RoutingDecision(
        kind="clarify",
        tool=None,
        confidence=0.51,
        tier="clarify",
        risk="low",
        needs_confirmation=False,
        candidates=[("close_app", 0.45), ("open_app", 0.42)],
        clarify_question="Уточните: вы хотите закрыть или открыть приложение?",
        trace={"raw_text": "окно браузера"},
    )

    with patch("core.orchestrator.semantic_route", return_value=clarify_decision):
        orch.handle_input("окно браузера", channel="test")

    pending = orch._pending_clarification()
    assert pending is not None

    # Имитируем прошествие 61 секунды (TTL = 60s)
    orch._clarification["ts"] = time.monotonic() - 61.0

    # Проверяем истечение TTL
    expired_pending = orch._pending_clarification()
    assert expired_pending is None
    assert orch._clarification is None

    # Следующая реплика маршрутизируется с нуля без перехвата clarify
    with patch(
        "core.orchestrator.semantic_route",
        return_value=RoutingDecision(
            kind="chat", tool=None, confidence=0.9, tier="semantic",
            risk="low", needs_confirmation=False, candidates=[("chat", 0.9)],
            trace={},
        ),
    ), patch.object(orch._agent, "_answer_conversation", return_value=AgentOutcome(text="Привет!", mode="chat")):
        state = orch.handle_input("привет", channel="test")
        assert state.get("mode") in ("chat", "conversation", "fast")
