from __future__ import annotations

import threading
from unittest.mock import patch

from core.agent import Agent, AgentConfig
from core.capabilities import CAPABILITIES
from core.actions.media import play_music
from core.safety import assess_risk
from core.actions.base import ActionResult
from core.verifier import verify_action_result


def test_bare_music_command_uses_media_fast_path(settings, fake_backend):
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.media.duckduckgo_search", return_value=[{
             "title": "Track", "url": "https://www.youtube.com/watch?v=fixture", "snippet": "",
         }]), patch("core.actions.media._open_target", return_value=True), \
         patch("core.actions.media._request_playback", return_value=True), \
         patch("core.verifier._active_audio_sessions", return_value=["fixture-player"]):
        outcome = agent.execute("поставь музыку")

    assert outcome.mode == "fast_path"
    assert outcome.tool_used == "play_music"
    assert outcome.verified is True
    assert "video_opened" in outcome.text
    assert not fake_backend.calls


def test_bare_music_opens_default_player_without_network():
    with patch("core.actions.media._open_target", return_value=True) as opener:
        result = play_music()

    assert result.ok is True
    assert "музыкальный плеер" in result.output.lower()
    opener.assert_called_once()


def test_mood_context_does_not_become_an_implicit_network_query():
    with patch("core.actions.media._open_target", return_value=True) as opener:
        result = play_music(mood="нет")

    assert result.ok is True
    assert "музыкальный плеер" in result.output.lower()
    opener.assert_called_once()


def test_network_music_search_is_not_mistaken_for_verified_playback(settings):
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    capability = CAPABILITIES.get("play_music")
    with patch("core.actions.media._open_target", return_value=True) as opener, \
         patch("core.actions.media.webbrowser.open") as browser:
        outcome = agent._execute_verified(
            goal="поставь музыку",
            tool="play_music",
            args={"query": "случайный запрос", "source": "youtube", "allow_network": True},
            mission=None,
            cancel=threading.Event(),
            trace=[],
            risk=assess_risk("поставь музыку"),
            caps=[capability] if capability is not None else [],
        )

    assert outcome.verified is False
    assert outcome.tool_used == "play_music"
    assert "Ошибка действия play_music" in outcome.text
    assert opener.called
    browser.assert_not_called()


def test_auto_network_source_never_falls_through_to_youtube():
    with patch("core.actions.media._open_target") as opener:
        result = play_music(query="случайный запрос", allow_network=True, source="auto")

    assert result.ok is False
    assert "конкретный источник" in result.error
    opener.assert_not_called()


def test_search_page_is_not_verified_as_playback():
    result = ActionResult(
        tool="play_music",
        args={"query": "трек", "source": "youtube", "allow_network": True},
        ok=True,
        output="Открыл поиск музыки: трек",
    )

    verification = verify_action_result(result)

    assert verification.verified is False
    assert verification.method == "media_playback"
