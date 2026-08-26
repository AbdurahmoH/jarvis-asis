from __future__ import annotations

from unittest.mock import patch

import pytest

from core.actions.base import ActionResult
from core.actions.registry import ToolRegistry
from core.agent import Agent, AgentConfig
from core.capabilities import CAPABILITIES
from core.cognitive import (
    CognitiveOrchestrator,
    ContinuityResolver,
    CurrentMindState,
    GoalStack,
)
from core.safety import assess_risk
from core.verifier import verify_action_result


@pytest.mark.parametrize("goal", ["привет", "почему моча такая жёлтая?"])
def test_production_brain_routes_conversation_without_action_discovery(
    settings, monkeypatch, goal,
):
    settings.deepseek_brain_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    calls: list[str] = []

    monkeypatch.setattr(agent._model_router, "route", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_retrieve_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(agent, "_backend_for_routing", lambda routing: (object(), None))

    def generate(backend, messages, system, **kwargs):
        calls.append(system or "")
        if "Capability surface:" in (system or ""):
            return (
                '{"decision":"answer","intent_clear":true,'
                '"capability_ids":[],"required_capability_ids":[],"clarification":""}'
            )
        return "Естественный ответ без инструментов."

    monkeypatch.setattr(agent, "_stream_consume", generate)

    outcome = agent.execute(goal)

    assert outcome.mode == "conversation"
    assert outcome.verified is True
    assert len(calls) == 1
    assert "Capability surface:" not in calls[0]


def test_production_provider_does_not_disable_safe_runtime_reflexes(
    settings, monkeypatch,
):
    settings.deepseek_brain_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    monkeypatch.setattr(
        agent,
        "_discover_capabilities",
        lambda **kwargs: pytest.fail("safe media reflex reached model discovery"),
    )
    monkeypatch.setattr("core.actions.media._open_target", lambda *args, **kwargs: True)
    monkeypatch.setattr("core.actions.media.duckduckgo_search", lambda *args, **kwargs: [
        {"title": "Track", "url": "https://www.youtube.com/watch?v=fixture", "snippet": ""},
    ])
    monkeypatch.setattr("core.actions.media._request_playback", lambda: True)
    monkeypatch.setattr("core.verifier._active_audio_sessions", lambda: ["fixture-player"])

    outcome = agent.execute("поставь музыку")

    assert outcome.mode == "fast_path"
    assert outcome.tool_used == "play_music"
    assert outcome.verified is True


def test_safe_visible_browser_navigation_is_low_risk():
    risk = assess_risk(
        "открой сайт",
        "browser_bridge",
        {"action": "open", "url": "https://example.com"},
    )

    assert risk.level.value == "low"
    assert risk.needs_confirmation is False


def test_internal_headless_browser_is_not_model_discoverable():
    surface = CAPABILITIES.surface_summary()
    selected = CAPABILITIES.discover(
        "открой сайт в видимом браузере",
        ["browser_automation", "browser_bridge"],
        top_k=8,
    )

    assert "browser_automation:" not in surface
    assert [cap.name for cap in selected][0] == "browser_bridge"
    assert all(cap.name != "browser_automation" for cap in selected)


def test_internal_browser_cannot_become_required_user_goal_evidence(settings):
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    required = agent._required_capability_contract(
        ["browser_automation", "browser_bridge"],
    )

    assert required == ("browser_bridge",)


def test_headless_browser_success_cannot_verify_user_visible_goal():
    result = ActionResult(
        tool="browser_automation",
        args={"action": "open", "url": "https://example.com"},
        ok=True,
        output={"ok": True, "url": "https://example.com", "title": "Example"},
    )

    verification = verify_action_result(result)

    assert verification.verified is False
    assert verification.method == "internal_browser_observation"


def test_visible_browser_requires_explicit_user_visible_evidence():
    unscoped = ActionResult(
        tool="browser_bridge",
        args={"action": "open", "url": "https://example.com"},
        ok=True,
        output={"url": "https://example.com", "dom_hash": "abc"},
    )
    visible = ActionResult(
        tool="browser_bridge",
        args={"action": "open", "url": "https://example.com"},
        ok=True,
        output={
            "url": "https://example.com",
            "dom_hash": "abc",
            "evidence_scope": "user_visible",
        },
    )

    assert verify_action_result(unscoped).verified is False
    assert verify_action_result(visible).verified is True


def test_ok_without_independent_verifier_is_not_completion():
    result = ActionResult(
        tool="fixture_without_verifier",
        args={},
        ok=True,
        output="provider accepted request",
    )

    verification = verify_action_result(result)

    assert verification.verified is False
    assert verification.strict is False
    assert verification.method == "trust_ok"


@pytest.mark.parametrize(
    "correction",
    [
        "ты не открыл",
        "ничего не произошло",
        "не запустилось",
        "файл не создался",
        "сообщение не отправилось",
    ],
)
def test_user_correction_reopens_previous_completion(tmp_path, correction):
    state = CurrentMindState(
        current_goal="открой сайт",
        mission_state="completed",
        last_verified_result="сайт открыт",
        confidence=1.0,
    )

    resolution = ContinuityResolver(GoalStack(tmp_path)).resolve(correction, state)

    assert resolution.action == "retry"
    assert resolution.goal == "открой сайт"
    assert resolution.confidence < 1.0
    assert "user correction" in resolution.evidence


def test_cognitive_correction_clears_stale_verified_result(tmp_path):
    cognitive = CognitiveOrchestrator(tmp_path, registry=ToolRegistry())
    cognitive.state.current_goal = "открой сайт"
    cognitive.state.mission_state = "completed"
    cognitive.state.last_verified_result = "сайт открыт"
    cognitive.state.confidence = 1.0
    cognitive.store.save(cognitive.state)

    turn = cognitive.begin_interaction("ничего не произошло", implicit_address=True)

    assert turn.action == "retry"
    assert turn.goal == "открой сайт"
    assert cognitive.state.last_verified_result == ""
    assert cognitive.state.pending_verification == ["user-reported outcome mismatch"]
    assert cognitive.state.mission_state == "repairing"


def test_high_risk_operation_still_requires_confirmation():
    risk = assess_risk(
        "удали важный файл",
        "delete_file",
        {"path": "C:/fixture/important.txt"},
    )

    assert risk.needs_confirmation is True
    assert risk.level.value in {"high", "critical"}


def test_music_surface_or_search_is_not_playback_completion(monkeypatch):
    monkeypatch.setattr(
        "core.verifier._active_audio_sessions",
        lambda: [],
        raising=False,
    )
    opened_player = ActionResult(
        tool="play_music",
        args={"source": "auto"},
        ok=True,
        output="Открыл локальный музыкальный плеер.",
    )
    opened_search = ActionResult(
        tool="play_music",
        args={"query": "трек", "source": "youtube", "allow_network": True},
        ok=True,
        output="Открыл поиск музыки: трек",
    )

    assert verify_action_result(opened_player).verified is False
    assert verify_action_result(opened_search).verified is False
