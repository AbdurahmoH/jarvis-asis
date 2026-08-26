from __future__ import annotations

import threading
from unittest.mock import patch

from core.agent import Agent, AgentConfig
from core.router.intent_router import resolve_keyword_tool, split_compound_commands
from core.router.route_guard import validate_tool_selection
from core.safety import assess_risk


def test_media_and_browser_actions_are_disambiguated():
    assert resolve_keyword_tool("открой YouTube") == "browser"
    assert resolve_keyword_tool("поставь музыку на YouTube") == "media"


def test_compound_parser_only_splits_two_real_actions():
    assert split_compound_commands("открой блокнот и поставь музыку") == [
        "открой блокнот", "поставь музыку",
    ]
    assert split_compound_commands("поставь музыку, настроения нет") == []


def test_route_guard_validates_transport_not_phrase_to_tool_mapping():
    media = validate_tool_selection("поставь музыку", "add_reminder")
    assert media.allowed is True

    clock = validate_tool_selection("который час", "web_search")
    assert clock.allowed is True
    assert validate_tool_selection("", "", {}).allowed is False


def test_direct_verified_execution_rejects_malformed_transport(settings):
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.reminders.get_default_manager") as manager:
        outcome = agent._execute_verified(
            goal="поставь музыку",
            tool="add_reminder",
            args=[],
            mission=None,
            cancel=threading.Event(),
            trace=[],
            risk=assess_risk("поставь музыку"),
            caps=[],
        )
    assert outcome.verified is False
    assert outcome.mode == "route_blocked"
    manager.assert_not_called()


def test_compound_actions_require_every_clause_verified(settings):
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.media.duckduckgo_search", return_value=[{
             "title": "Track", "url": "https://www.youtube.com/watch?v=fixture", "snippet": "",
         }]), patch("core.actions.media._open_target", return_value=True), \
         patch("core.verifier._active_audio_sessions", return_value=["fixture-player"]):
        outcome = agent.execute("поставь музыку и который час")

    assert outcome.mode == "batch"
    assert outcome.tool_used == "command_batch"
    assert outcome.verified is True
    assert outcome.verification is not None
    assert outcome.verification.detail == "подтверждено 2/2 шагов"
    assert "video_opened" in outcome.text
    assert ":" in outcome.text
