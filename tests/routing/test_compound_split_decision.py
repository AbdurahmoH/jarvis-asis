"""Разбиение составных команд по решению роутера — R1/аудит 1b.

Сплит по союзу «и»/«потом» сам по себе запрещён: разбиение допустимо только
когда каждая часть маршрутизируется роутером как самостоятельное действие
(kind action/fresh_data). Миссии и болтовня не разбиваются; неподдерживаемая
часть составной команды получает честный отказ (R8) и прекращает батч.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.settings import Settings
from core.agent import Agent, AgentConfig
from core.routing.semantic_router import split_compound_by_decision


def test_essay_with_conjunction_is_not_split():
    """«напиши эссе о технологиях и обществе» — одна миссия, а не две команды."""
    assert split_compound_by_decision("напиши эссе о технологиях и обществе") == []


def test_chat_with_conjunction_is_not_split():
    assert split_compound_by_decision("поставь музыку, настроения нет") == []


def test_two_actions_are_split():
    parts = split_compound_by_decision("открой блокнот и сделай потише")
    assert parts == ["открой блокнот", "сделай потише"]


def test_action_with_unsupported_part_is_split():
    """Обе части kind=action (одна без инструмента) — батч допустим."""
    parts = split_compound_by_decision("удали файл report_old.txt и создай файл report_new.txt")
    assert parts == ["удали файл report_old.txt", "создай файл report_new.txt"]


@pytest.fixture
def offline_agent(settings, tmp_path):
    settings.offline_mode = True
    settings.deepseek_brain_mode = False
    settings.paths.documents_dir = str(tmp_path / "docs")
    settings.paths.data_dir = str(tmp_path / "data")
    Path(settings.paths.documents_dir).mkdir(parents=True, exist_ok=True)
    return Agent(settings, config=AgentConfig(enable_skill_forge=False))


def test_compound_unsupported_part_plus_write_confirmation(offline_agent, settings, tmp_path):
    """«удали файл danger.txt и создай новый»: часть без тула -> честный отказ (R8,
    записан в unsupported_requests.jsonl), вторая часть -> write_file с подтверждением.
    Пользователь видит и отказ, и запрос подтверждения."""
    from core.structured import ToolCallDecision

    planner_plan = ToolCallDecision(
        tool="write_file",
        arguments={"path": "danger.txt", "content": "x"},
        answer="",
    )
    offline_agent._decide_with_model = lambda *args, **kwargs: (planner_plan, "")

    outcome = offline_agent.execute("удали файл danger.txt и создай новый")

    # Отказ по неподдерживаемой части виден в ответе вместе с подтверждением.
    assert outcome.needs_confirmation is True
    assert "не поддерживается" in outcome.text or "не умею" in outcome.text
    assert "подтверждение" in outcome.text.lower()

    # Честный отказ зафиксирован в jsonl-бэклоге.
    log_file = tmp_path / "data" / "logs" / "unsupported_requests.jsonl"
    assert log_file.exists(), "unsupported_requests.jsonl не создан"
    entry = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "удали файл danger.txt" in entry["text"]
    assert "ts" in entry and "top_candidates" in entry and "risk" in entry
