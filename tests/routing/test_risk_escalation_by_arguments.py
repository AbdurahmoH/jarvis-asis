"""Тест эскалации риска по аргументам инструмента (R3).

Требование R3:
- safety.py — адаптер над assess_risk из core/routing.
- Regex-таблицы риска по ТЕКСТУ удалены.
- Эскалация риска по АРГУМЕНТАМ инструмента (shell-метасимволы ;|& ` $(,
  деструктивные команды в командных строках, системные пути, маски, "все/всё")
  есть и покрыта тестом для двух путей: repair-патч и LLM-план.
"""

from __future__ import annotations

import pytest
from core.safety import assess_risk, RiskLevel
from config.settings import Settings
from core.agent import Agent, AgentConfig


@pytest.mark.parametrize(
    "tool,args,expected_reason_substr",
    [
        ("read_file", {"path": "file.txt; rm -rf /"}, "shell-метасимвол"),
        ("read_file", {"path": "file.txt | dir"}, "shell-метасимвол"),
        ("read_file", {"path": "file.txt && echo 1"}, "shell-метасимвол"),
        ("read_file", {"path": "file.txt`whoami`"}, "shell-метасимвол"),
        ("read_file", {"path": "file.txt$(id)"}, "shell-метасимвол"),
        ("read_file", {"path": "data.txt", "cmd": "del /f /q C:\\*"}, "деструктивн"),
        ("read_file", {"path": "C:\\Windows\\System32\\drivers\\etc\\hosts"}, "системн"),
        ("read_file", {"path": "C:\\Users\\Admin\\Desktop\\*.*"}, "маск"),
        ("file_move", {"source": "test", "target": "все"}, "массов"),
    ],
)
def test_argument_risk_escalation_direct(tool, args, expected_reason_substr):
    """Прямая эскалация риска в assess_risk по аргументам инструмента при нейтральной цели."""
    goal = "посмотри файл"
    assessment = assess_risk(goal, tool=tool, arguments=args)
    assert assessment.level in (RiskLevel.HIGH, RiskLevel.CRITICAL), (
        f"Ожидался HIGH или CRITICAL для tool={tool}, args={args}, получено: {assessment.level}"
    )
    assert assessment.needs_confirmation is True
    assert any(expected_reason_substr in r.lower() for r in assessment.reasons), (
        f"В reasons {assessment.reasons} не найдена подстрока {expected_reason_substr}"
    )


def test_argument_risk_escalation_in_llm_plan(monkeypatch, tmp_path):
    """Путь 1: LLM-план содержит опасные аргументы -> запрос подтверждения."""
    settings = Settings()
    settings.offline_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    # Моделируем решение планировщика с shell-инъекцией в аргументе
    from core.structured import ToolCallDecision
    unsafe_decision = ToolCallDecision(
        tool="read_file",
        arguments={"path": "report.txt; rm -rf /"},
        answer="",
    )

    monkeypatch.setattr(agent, "_decide_with_model", lambda *args, **kwargs: (unsafe_decision, ""))

    outcome = agent.execute("напиши эссе о влиянии технологий на развитие общества")
    assert outcome.needs_confirmation is True, "LLM-план с опасными аргументами обязан требовать подтверждения"
    assert outcome.risk.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert any("shell-метасимвол" in r.lower() for r in outcome.risk.reasons)


def test_argument_risk_escalation_in_repair_patch(tmp_path):
    """Путь 2: repair-патч предлагает опасные аргументы -> блокируется риск-гейтом repair."""
    settings = Settings()
    settings.offline_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    # Проверяем внутренний _repair_risk_gate логики восстановления
    goal = "почини чтение файла"
    unsafe_patched_args = {"path": "config.json; del /f C:\\*"}

    gate_risk = assess_risk(goal, "read_file", unsafe_patched_args)
    assert gate_risk.needs_confirmation is True, "Патч аргументов с деструктивными командами обязан требовать подтверждения"
    assert gate_risk.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
