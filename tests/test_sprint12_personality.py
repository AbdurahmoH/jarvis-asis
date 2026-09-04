from __future__ import annotations

import json

from core.personality import (
    CommunicationAdapter,
    HumorPolicy,
    IdentityProfile,
    PersonalityEngine,
    PersonalityProfile,
    UserProfile,
)


def test_identity_and_personality_are_structured_and_loadable(tmp_path):
    identity_path = tmp_path / "identity.json"
    personality_path = tmp_path / "personality.json"
    identity_path.write_text(json.dumps({
        "name": "ATLAS",
        "role": "digital intelligence",
        "mission": "understand and advance user goals safely",
        "values": ["accuracy", "privacy", "initiative", "reliability"],
    }), encoding="utf-8")
    personality_path.write_text(json.dumps({
        "name": "ATLAS",
        "tone": "professional_friendly",
        "humor": 0.35,
        "verbosity": "adaptive",
        "initiative": "assistant",
        "respect_level": "high",
    }), encoding="utf-8")

    engine = PersonalityEngine(identity_path=identity_path, personality_path=personality_path)

    assert engine.identity == IdentityProfile()
    assert engine.profile == PersonalityProfile()
    assert engine.identity.values == ("accuracy", "privacy", "initiative", "reliability")


def test_communication_adapter_shortens_for_busy_user():
    style = CommunicationAdapter().adapt(
        user_context={"busy": True, "typing_active": True},
        urgency="high",
        task_type="work",
        user_preference=UserProfile(communication_style="adaptive"),
    )

    assert style.verbosity == "short"
    assert style.max_sentences == 2
    assert style.structured is False


def test_communication_adapter_is_detailed_for_learning_and_structured_for_report():
    adapter = CommunicationAdapter()

    learning = adapter.adapt({}, "normal", "learning", UserProfile())
    report = adapter.adapt({}, "normal", "report", UserProfile())

    assert learning.verbosity == "detailed"
    assert learning.explanation_depth == "step_by_step"
    assert report.structured is True
    assert report.verbosity in {"balanced", "detailed"}


def test_explicit_user_style_overrides_adaptive_context():
    style = CommunicationAdapter().adapt(
        user_context={}, urgency="normal", task_type="conversation",
        user_preference=UserProfile(communication_style="short"),
    )

    assert style.verbosity == "short"
    assert style.max_sentences == 3


def test_action_and_technical_preferences_change_explanation_strategy():
    adapter = CommunicationAdapter()

    action = adapter.adapt(
        {}, "normal", "work",
        UserProfile(prefers_action_over_explanation=True, technical_level="advanced"),
    )
    learning = adapter.adapt(
        {}, "normal", "learning", UserProfile(technical_level="advanced"),
    )

    assert action.explanation_depth == "action_first"
    assert learning.explanation_depth == "technical"


def test_humor_policy_is_zero_for_danger_and_errors():
    policy = HumorPolicy(base_level=0.35)

    assert policy.calibrate(task_type="conversation") > 0
    assert policy.calibrate(task_type="work") <= 0.1
    assert policy.calibrate(task_type="work", is_error=True) == 0
    assert policy.calibrate(task_type="conversation", risk="high") == 0


def test_natural_completion_requires_verified_success():
    engine = PersonalityEngine()

    verified = engine.naturalize("Task completed successfully.", verified=True, task_type="work")
    unverified = engine.naturalize("Task completed successfully.", verified=False, task_type="work")

    assert verified == "Готово, сэр. Проверил результат — всё применилось."
    assert "Готово" not in unverified


def test_natural_completion_empty_address_omits_address(tmp_path):
    p_path = tmp_path / "personality.json"
    p_path.write_text(json.dumps({"name": "ATLAS", "address": ""}), encoding="utf-8")
    engine = PersonalityEngine(personality_path=p_path)
    verified = engine.naturalize("Task completed successfully.", verified=True, task_type="work")
    assert verified == "Готово. Проверил результат — всё применилось."


def test_prompt_fragment_is_compact_and_contains_structured_policy():
    engine = PersonalityEngine()
    style = engine.style_for(task_type="report", urgency="normal")

    fragment = engine.prompt_fragment(style, memories=["Пользователь предпочитает короткие ответы"])

    assert "Identity: ATLAS" in fragment
    assert "Стиль ответа:" in fragment
    assert "Релевантные предпочтения:" in fragment
    assert len(fragment) < 1200


def test_short_style_bounds_natural_conversation_without_cutting_reports():
    engine = PersonalityEngine()
    short = engine.style_for(
        task_type="conversation",
        user_preference=UserProfile(communication_style="short"),
    )
    report = engine.style_for(task_type="report")
    text = "Первый вывод. Второй вывод. Третий вывод. Четвёртый вывод."

    compact = engine.adapt_response(text, short)

    assert compact == "Первый вывод. Второй вывод. Третий вывод."
    assert engine.adapt_response(text, report) == text
