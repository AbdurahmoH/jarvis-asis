"""Regression: БАГ 16 (функциональный уровень) — search_opened не запускает
repair loop и не перезапускает музыку (второго Popen нет).

execute_tool замокан: он возвращает честный ActionResult play_music со
stage=search_opened (поиск открыт, воспроизведение не подтверждено) и
ФИКСИРУЕТ число вызовов. Если repair loop запустится — тест упадёт.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest


def _search_opened_result():
    from core.actions.base import ActionResult

    return ActionResult(
        tool="play_music",
        args={"query": "включи рок музыку", "source": "youtube", "allow_network": True},
        ok=True,
        output={"stage": "search_opened", "query": "включи рок музыку",
                "url": "https://www.youtube.com/results?search_query=rock"},
    )


def test_search_opened_does_not_trigger_repair_or_second_launch():
    from config.settings import Settings
    from core.agent import Agent
    from core.safety import assess_risk

    agent = Agent(Settings(), council=MagicMock())
    result = _search_opened_result()

    with patch("core.agent.execute_tool", return_value=result) as exec_mock, \
            patch.object(agent._repair, "run", wraps=agent._repair.run) as repair_mock:
        outcome = agent._execute_verified(
            goal="включи рок музыку",
            tool="play_music",
            args={"query": "включи рок музыку", "source": "youtube", "allow_network": True},
            mission=None,
            cancel=threading.Event(),
            trace=[],
            risk=assess_risk("включи рок музыку", tool="play_music", arguments={}),
            caps=[],
            fast_path=True,
        )

    # Ровно один запуск инструмента.
    assert exec_mock.call_count == 1, (
        f"инструмент вызван {exec_mock.call_count} раз(а) — повторный запуск музыки"
    )
    # Repair loop не должен запускаться: действие уже выполнено, повторять нельзя.
    assert repair_mock.call_count == 0, "repair loop запустился для non_repeatable результата"
    # Честный ответ пользователю: действие сделано, но не подтверждено.
    assert outcome.verified is False
    assert outcome.text, "пользователь должен получить объяснение вместо тишины"


def test_repeatable_failure_still_repairs():
    """Контроль: обычная (repeatable) ошибка по-прежнему уходит в repair."""
    from config.settings import Settings
    from core.agent import Agent
    from core.actions.base import ActionResult
    from core.safety import assess_risk

    agent = Agent(Settings(), council=MagicMock())
    failing = ActionResult(
        tool="play_music",
        args={"query": "x", "source": "youtube", "allow_network": True},
        ok=False,
        error="медиасервис не открылся",
    )

    with patch("core.agent.execute_tool", return_value=failing), \
            patch.object(agent._repair, "run",
                         return_value=MagicMock(trace=["repair attempt"])) as repair_mock:
        agent._execute_verified(
            goal="включи музыку",
            tool="play_music",
            args={"query": "x", "source": "youtube", "allow_network": True},
            mission=None,
            cancel=threading.Event(),
            trace=[],
            risk=assess_risk("включи музыку", tool="play_music", arguments={}),
            caps=[],
            fast_path=True,
        )
    assert repair_mock.call_count >= 1, (
        "repeatable-ошибка должна по-прежнему ремонтироваться — "
        "фикс БАГ 16 не должен отключить repair целиком"
    )
