"""Regression: ДЫРА 2 — таймер подтверждения.

1. Ответ до таймаута отменяет таймер; авто-reject не срабатывает.
2. Таймаут — авто-reject ровно один раз; словари чистятся.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest


def _make_agent():
    from config.settings import Settings
    from core.agent import Agent
    from core.safety import assess_risk

    settings = Settings()
    council = MagicMock()
    agent = Agent(settings, council=council)
    agent._config.confirmation_timeout_sec = 0.2

    def _register(cid: str) -> None:
        entry = {
            "goal": "удали файл test.txt",
            "tool": "write_file",
            "args": {"path": "test.txt", "content": "x"},
            "risk": assess_risk("удали файл test.txt", tool="write_file", arguments={}),
            "caps": [],
            "mission": None,
            "cancel": threading.Event(),
            "trace": [],
        }
        with agent._lock:
            agent._pending_confirmations[cid] = entry

    return agent, _register


def test_answer_before_timeout_cancels_timer():
    agent, register = _make_agent()
    register("conf-ok")
    agent._start_confirmation_watchdog("conf-ok")

    # Человек успел ответить до таймаута.
    outcome = agent.answer_confirmation("conf-ok", approved=False)
    assert outcome is not None and outcome.mode == "confirmation_rejected"
    assert outcome.text == "Понял. Действие отменено."

    time.sleep(0.45)  # таймаут (0.2s) давно истёк
    assert "conf-ok" not in agent._pending_confirmations
    assert "conf-ok" not in agent._confirmation_timers, (
        "Таймер не убран из словаря после ответа"
    )


def test_timeout_auto_reject_exactly_once():
    agent, register = _make_agent()
    register("conf-timeout")

    calls: list[tuple[str, bool]] = []
    real_answer = agent.answer_confirmation

    def _counting(cid: str, approved: bool):
        calls.append((cid, approved))
        return real_answer(cid, approved)

    agent.answer_confirmation = _counting  # type: ignore[method-assign]
    agent._start_confirmation_watchdog("conf-timeout")

    # Ждём дольше таймаута: таймер должен один раз auto-reject.
    deadline = time.time() + 2.0
    while time.time() < deadline and not calls:
        time.sleep(0.02)

    time.sleep(0.45)
    assert calls == [("conf-timeout", False)], (
        f"auto-reject сработал {len(calls)} раз(а): {calls}"
    )
    assert "conf-timeout" not in agent._pending_confirmations
    assert "conf-timeout" not in agent._confirmation_timers
