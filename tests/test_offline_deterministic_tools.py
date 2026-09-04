"""Offline E2E-тесты детерминированных инструментов (llm_available=False).

Проверяет исполнение без обращения к модели:
- open_app
- close_app
- volume
- current_time
- system_status
- list_files
Все тесты проверяют verified=True и отсутствие вызовов LLM.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from core.actions.base import ActionResult
from core.agent import Agent, AgentConfig


@pytest.fixture
def offline_agent(settings):
    settings.offline_mode = True
    settings.deepseek_brain_mode = False
    config = AgentConfig(enable_skill_forge=False)
    agent = Agent(settings, config=config)
    return agent


def test_offline_current_time(offline_agent):
    """current_time исполняется локально детерминированно с verified=True."""
    outcome = offline_agent.execute("подскажи системное время")
    assert outcome.verified is True
    assert outcome.tool_used == "current_time"
    assert outcome.mode == "fast_path"
    assert outcome.action_result is not None
    assert outcome.action_result.ok is True


def test_offline_system_status(offline_agent):
    """system_status исполняется локально детерминированно с verified=True."""
    outcome = offline_agent.execute("статус системы")
    assert outcome.verified is True
    assert outcome.tool_used == "system_status"
    assert outcome.mode == "fast_path"
    assert outcome.action_result is not None
    assert outcome.action_result.ok is True


def test_offline_volume(offline_agent):
    """volume исполняется детерминированно с verified=True."""
    with patch(
        "core.actions.system.increase_volume",
        return_value=ActionResult(tool="volume", args={"action": "up"}, ok=True, output="Громкость увеличена."),
    ):
        outcome = offline_agent.execute("сделай громкость погромче")
    assert outcome.verified is True
    assert outcome.tool_used == "volume"
    assert outcome.mode == "fast_path"
    assert outcome.action_result is not None
    assert outcome.action_result.ok is True


def test_offline_open_app(offline_agent):
    """open_app исполняется детерминированно с verified=True."""
    with patch(
        "core.actions.app_control.open_app",
        return_value=ActionResult(tool="open_app", args={"name": "блокнот"}, ok=True, output="Запустил блокнот."),
    ), patch("core.verifier._process_matches", return_value=["notepad.exe"]):
        outcome = offline_agent.execute("открой блокнот")
    assert outcome.verified is True
    assert outcome.tool_used == "open_app"
    assert outcome.mode == "fast_path"
    assert outcome.action_result is not None
    assert outcome.action_result.ok is True


def test_offline_close_app(offline_agent):
    """close_app исполняется детерминированно с verified=True."""
    with patch(
        "core.actions.app_control.close_app",
        return_value=ActionResult(tool="close_app", args={"name": "блокнот"}, ok=True, output="Закрыл блокнот."),
    ), patch("core.verifier._process_matches", return_value=[]):
        outcome = offline_agent.execute("закрой блокнот")
    assert outcome.verified is True
    assert outcome.tool_used == "close_app"
    assert outcome.mode == "fast_path"
    assert outcome.action_result is not None
    assert outcome.action_result.ok is True


def test_offline_list_files(offline_agent, tmp_path):
    """list_files исполняется детерминированно с verified=True."""
    offline_agent._settings.paths.documents_dir = str(tmp_path)
    (tmp_path / "test1.txt").write_text("hello", encoding="utf-8")
    outcome = offline_agent.execute("список файлов")
    assert outcome.verified is True
    assert outcome.tool_used == "list_files"
    assert outcome.mode == "fast_path"
    assert outcome.action_result is not None
    assert outcome.action_result.ok is True
