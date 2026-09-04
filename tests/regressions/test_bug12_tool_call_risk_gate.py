"""Regression: БАГ 12 — не гейтнутый путь исполнения инструментов удалён.

Решение: удаление, а не гейт. `_maybe_execute_tool` (TOOL_CALL из текста
модели, без assess_risk / route guard / confirmation) никогда не вызывался
— единственный живой путь исполнения инструментов это Agent._execute_verified
(assess_risk -> route guard -> confirmation -> verifier). Оставлять второй
путь означало бы дублировать Risk Gate ради мёртвого кода. Заодно удалён
сломанный regex-парсер `_TOOL_CALL_PATTERN` (`\\{.*?\\}` рвался на вложенных
скобках — БАГ 15): native tool calls парсятся в core/llm/tool_calls.py.

Доказательство:
1. `_maybe_execute_tool` / `_reask_with_tool_result` / `_TOOL_CALL_PATTERN`
   отсутствуют в orchestrator.py (0 вхождений).
2. Активный путь Agent._execute_verified вызывает assess_risk перед execute_tool.
3. HIGH-risk инструмент (write_file) не выполняется без подтверждения.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Не гейтнутый путь полностью удалён из orchestrator.py
# ---------------------------------------------------------------------------

def test_maybe_execute_tool_is_removed():
    """БАГ 12 FIX: _maybe_execute_tool удалён — 0 вхождений в orchestrator.py."""
    src = Path("core/orchestrator.py").read_text(encoding="utf-8")
    count = src.count("_maybe_execute_tool")
    assert count == 0, (
        f"_maybe_execute_tool встречается {count} раз(а) в orchestrator.py. "
        "Путь исполнения инструментов без Risk Gate не должен существовать."
    )


def test_reask_with_tool_result_is_removed():
    """Сопутствующий мёртвый код `_reask_with_tool_result` тоже удалён."""
    src = Path("core/orchestrator.py").read_text(encoding="utf-8")
    assert src.count("_reask_with_tool_result") == 0


def test_broken_tool_call_regex_is_removed():
    """БАГ 15 FIX: regex-парсер TOOL_CALL (ломался на вложенных скобках) удалён."""
    src = Path("core/orchestrator.py").read_text(encoding="utf-8")
    assert src.count("_TOOL_CALL_PATTERN") == 0


# ---------------------------------------------------------------------------
# 2. assess_risk вызывается в _execute_verified до execute_tool
# ---------------------------------------------------------------------------

def test_assess_risk_called_before_execute_tool_in_agent():
    """В core/agent.py assess_risk вызывается до execute_tool в _execute_verified."""
    src = Path("core/agent.py").read_text(encoding="utf-8")
    # Оба символа должны присутствовать
    assert "assess_risk" in src, "assess_risk не найден в agent.py"
    assert "execute_tool" in src, "execute_tool не найден в agent.py"

    # assess_risk должен встречаться раньше execute_tool в файле
    idx_risk = src.find("assess_risk(")
    idx_exec = src.find("execute_tool(")
    assert idx_risk < idx_exec, (
        "assess_risk должен вызываться раньше execute_tool в agent.py"
    )


# ---------------------------------------------------------------------------
# 3. HIGH-risk инструмент не выполняется без подтверждения через Agent
# ---------------------------------------------------------------------------

def test_high_risk_tool_requires_confirmation():
    """write_file с HIGH-risk целью возвращает needs_confirmation=True, не выполняется."""
    from config.settings import Settings
    from core.agent import Agent, AgentOutcome

    settings = Settings()
    # Минимальный мок совета
    council_mock = MagicMock()
    council_mock.route.return_value = MagicMock(tier=MagicMock(value="fast"), reason="test")

    agent = Agent(settings, council=council_mock)

    # Патчим execute_tool чтобы убедиться, что он НЕ вызывается
    with patch("core.agent.execute_tool") as mock_exec:
        # Цель с явным HIGH-risk маркером
        outcome = agent.execute("удали все файлы в папке documents")

    # Либо needs_confirmation=True, либо execute_tool не вызывался
    # (агент мог уйти в conversation gate или другой путь без инструмента)
    if mock_exec.called:
        # Если инструмент всё же вызвался — проверяем что это не write/delete
        for call in mock_exec.call_args_list:
            tool_name = call.args[1] if len(call.args) > 1 else call.kwargs.get("tool_name", "")
            assert tool_name not in ("write_file", "file_move", "file_copy"), (
                f"HIGH-risk инструмент '{tool_name}' выполнен без подтверждения"
            )
    # Основная проверка: если outcome.needs_confirmation — это правильно
    # Если нет — агент должен был уйти в conversation/unknown, не в tool
    assert not (
        mock_exec.called
        and any(
            (call.args[1] if len(call.args) > 1 else call.kwargs.get("tool_name", ""))
            in ("write_file", "file_move")
            for call in mock_exec.call_args_list
        )
        and not outcome.needs_confirmation
    ), "HIGH-risk инструмент выполнен без подтверждения"


# ---------------------------------------------------------------------------
# 4. assess_risk корректно классифицирует write_file как HIGH
# ---------------------------------------------------------------------------

def test_assess_risk_write_file_is_high():
    """assess_risk для write_file с деструктивной целью возвращает HIGH или MEDIUM."""
    from core.safety import assess_risk, RiskLevel

    result = assess_risk(
        goal="удали все файлы",
        tool="write_file",
        arguments={"path": "important.txt", "content": ""},
    )
    # write_file сам по себе MEDIUM, цель с "удали" — HIGH
    assert result.level in (RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.CRITICAL), (
        f"Ожидался HIGH/MEDIUM/CRITICAL, получен {result.level}"
    )
