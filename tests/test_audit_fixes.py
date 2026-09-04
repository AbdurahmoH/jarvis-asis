from __future__ import annotations

from unittest.mock import patch

from config.settings import Settings
from core.actions import DEFAULT_REGISTRY, ToolContext, execute_tool
from core.actions.time import CurrentTimeTool
from core.router.intent_router import resolve_keyword_tool
from core.verifier import verify_action_result


def test_time_intent_and_tool_are_deterministic():
    assert resolve_keyword_tool("Который час") == "system"
    result = CurrentTimeTool().run({}, ToolContext(settings=Settings()))
    assert result.ok
    assert verify_action_result(result).verified


def test_media_request_never_routes_to_reminder(monkeypatch):
    assert resolve_keyword_tool("Поставь музыку, настроения нет") == "media"
    assert DEFAULT_REGISTRY.get("play_music") is not None
    with patch("core.actions.media._open_target", return_value=True), patch(
        "core.actions.media.duckduckgo_search",
        return_value=[{"url": "https://www.youtube.com/watch?v=ambient"}],
    ):
        result = execute_tool(DEFAULT_REGISTRY, "play_music", {
            "query": "ambient", "source": "youtube", "allow_network": True,
        }, ToolContext(settings=Settings()))
    assert result.tool == "play_music"
    assert result.tool != "add_reminder"
    assert result.ok


def test_open_app_pid_is_a_strict_observation():
    from core.actions.base import ActionResult
    from core.verifier import verify_action_result

    result = ActionResult(
        tool="open_app", args={"name": "fixture"}, ok=True,
        output=f"Запустил fixture (pid={__import__('os').getpid()}).",
    )
    verification = verify_action_result(result)
    assert verification.verified is True
    assert verification.method == "process_running"
