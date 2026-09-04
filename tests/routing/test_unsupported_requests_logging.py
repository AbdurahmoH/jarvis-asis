"""Тесты логирования неподдерживаемых запросов (unsupported_requests.jsonl).

Проверяет:
1. Запись в data/logs/unsupported_requests.jsonl при попадании запроса в _handle_unsupported.
2. Санитизацию и маскирование секретов (redact_secrets).
3. Соответствие схеме JSONL: ts (ISO-8601), text, top_candidates (до 3), risk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.settings import Settings
from core.agent import Agent, AgentConfig
from core.routing.semantic_router import RoutingDecision


def test_unsupported_requests_logged_to_jsonl_with_redaction(tmp_path: Path):
    settings = Settings()
    settings.paths.data_dir = str(tmp_path / "data")
    settings.offline_mode = True
    settings.deepseek_brain_mode = False

    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    decision = RoutingDecision(
        kind="action",
        tool=None,
        confidence=0.55,
        tier="semantic",
        risk="low",
        needs_confirmation=False,
        candidates=[("open_app", 0.3), ("play_music", 0.2), ("volume", 0.1), ("weather", 0.05)],
        trace={},
    )

    sensitive_goal = "построй космический корабль с ключом sk-1234567890abcdef1234567890abcdef"
    outcome = agent._handle_unsupported(decision, sensitive_goal, mission=None, trace=[])

    assert outcome.mode == "unsupported"
    assert "не умею" in outcome.text

    log_file = Path(settings.data_dir) / "logs" / "unsupported_requests.jsonl"
    assert log_file.exists(), "Файл unsupported_requests.jsonl не был создан"

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1

    last_entry = json.loads(lines[-1])

    # Проверка схемы
    assert "ts" in last_entry
    assert "T" in last_entry["ts"]  # ISO-8601
    assert "text" in last_entry
    assert "top_candidates" in last_entry
    assert "risk" in last_entry

    # Проверка санитизации секретов
    assert "sk-1234567890abcdef" not in last_entry["text"]
    assert "<secret>" in last_entry["text"]

    # Проверка ограничения top-3
    assert len(last_entry["top_candidates"]) == 3
    assert last_entry["top_candidates"] == ["open_app", "play_music", "volume"]
    assert last_entry["risk"] == "low"
