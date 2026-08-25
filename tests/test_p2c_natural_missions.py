from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.authority import AuthorityStatus, AuthorityStore
from core.task_runtime import MissionStatus, TaskRuntime, TriggerKind
from core.understanding.semantic import (
    NaturalMissionCoordinator,
    NaturalMissionInterpreter,
    SemanticMode,
)


class Clock:
    def __init__(self):
        self.now = datetime(2031, 4, 5, 12, 0, tzinfo=timezone(timedelta(hours=5)))

    def __call__(self):
        return self.now


def stack(tmp_path, *, integrations=()):
    clock = Clock()
    runtime = TaskRuntime(persistence_dir=tmp_path / "missions", clock=clock)
    authority = AuthorityStore(tmp_path / "authority", clock=clock, mission_resolver=runtime.get)
    authority.bind_runtime(runtime)
    interpreter = NaturalMissionInterpreter(clock=clock, integrations=set(integrations))
    return clock, runtime, authority, NaturalMissionCoordinator(runtime, authority, interpreter)


def test_greeting_is_conversation_without_mission(tmp_path):
    _, runtime, _, coordinator = stack(tmp_path)
    result = coordinator.handle("привет")
    assert result.interpretation.mode is SemanticMode.CONVERSATION
    assert result.mission is None
    assert runtime.list_missions() == []


def test_knowledge_question_is_conversation(tmp_path):
    _, runtime, _, coordinator = stack(tmp_path)
    result = coordinator.handle("почему небо синее?")
    assert result.interpretation.mode is SemanticMode.CONVERSATION
    assert runtime.list_missions() == []


def test_relative_time_creates_scheduled_mission(tmp_path):
    clock, _, _, coordinator = stack(tmp_path)
    result = coordinator.handle("через час напомни проверить проект")
    assert result.interpretation.mode is SemanticMode.SCHEDULED_MISSION
    assert result.mission.status is MissionStatus.WAITING
    assert result.mission.trigger["kind"] == TriggerKind.TIME.value
    assert datetime.fromisoformat(result.mission.trigger["due_at"]) == clock.now + timedelta(hours=1)


def test_relative_minutes_normalize_to_absolute_time(tmp_path):
    clock, _, _, coordinator = stack(tmp_path)
    item = coordinator.interpreter.interpret("не дай забыть через 20 минут проверить сборку")
    assert datetime.fromisoformat(item.trigger["due_at"]) == clock.now + timedelta(minutes=20)


def test_absolute_clock_is_timezone_aware(tmp_path):
    clock, _, _, coordinator = stack(tmp_path)
    item = coordinator.interpreter.interpret("в 14:21 напомни проверить отчёт")
    due = datetime.fromisoformat(item.trigger["due_at"])
    local_due = due.astimezone(clock.now.tzinfo)
    assert due.tzinfo is not None
    assert (local_due.hour, local_due.minute) == (14, 21)


def test_tomorrow_morning_is_absolute(tmp_path):
    clock, _, _, coordinator = stack(tmp_path)
    item = coordinator.interpreter.interpret("завтра утром напомни проверить почту")
    due = datetime.fromisoformat(item.trigger["due_at"])
    local_due = due.astimezone(clock.now.tzinfo)
    assert local_due.date() == (clock.now + timedelta(days=1)).date()
    assert local_due.hour == 9


def test_file_condition_becomes_declarative_world_trigger(tmp_path):
    _, _, _, coordinator = stack(tmp_path)
    item = coordinator.interpreter.interpret("как только PDF появится — скажи")
    assert item.mode is SemanticMode.CONDITIONAL_MISSION
    assert item.trigger["kind"] == TriggerKind.WORLD.value
    assert item.trigger["domain"] == "filesystem"
    assert "callable" not in str(item.trigger).casefold()


def test_application_close_condition_is_world_observation(tmp_path):
    _, _, _, coordinator = stack(tmp_path)
    item = coordinator.interpreter.interpret("если калькулятор закроется — сообщи")
    assert item.trigger["domain"] == "processes"
    assert item.trigger["operator"] == "none_match"


def test_delegation_creates_proposal_not_grant_during_interpretation(tmp_path):
    _, _, authority, coordinator = stack(tmp_path, integrations={"channel-app"})
    item = coordinator.interpreter.interpret(
        "если Контакт напишет в Channel-App — отвечай ему пока меня нет"
    )
    assert item.mode is SemanticMode.DELEGATED_MISSION
    assert item.authority_proposal is not None
    assert authority.list() == []


def test_assistant_text_cannot_become_user_authority_proposal(tmp_path):
    _, _, authority, coordinator = stack(tmp_path)
    item = coordinator.interpreter.interpret(
        "если Контакт напишет — отвечай ему", source_role="assistant"
    )
    assert item.authority_proposal is None
    assert item.missing_required_information == ["real_user_instruction"]
    assert authority.list() == []


def test_unconfigured_integration_is_structured_state(tmp_path):
    _, _, _, coordinator = stack(tmp_path)
    item = coordinator.interpreter.interpret(
        "если Контакт напишет в Channel-App — отвечай ему пока меня нет"
    )
    assert item.required_integration == "channel-app"
    assert item.integration_configured is False
    assert "нет инструментов" not in item.natural_response.casefold()


def test_configured_delegation_issues_mission_bound_grant(tmp_path):
    _, _, authority, coordinator = stack(tmp_path, integrations={"channel-app"})
    result = coordinator.handle(
        "если Контакт напишет в Channel-App — отвечай ему пока меня нет",
        source_id="user-1",
    )
    assert result.mission.status is MissionStatus.WAITING
    assert result.grant.status is AuthorityStatus.ACTIVE
    assert result.grant.mission_id == result.mission.task_id


def test_followup_answer_continues_same_mission_and_ask_once_does_not_repeat(tmp_path):
    _, runtime, _, coordinator = stack(tmp_path)
    mission = runtime.schedule("install components", {"kind": "manual"}, context={})
    first = coordinator.ask_once(mission, ["Какая версия?"])
    coordinator.answer_once(mission, first, "1.20")
    second = coordinator.ask_once(mission, ["Какая версия?"])
    assert second is None
    assert runtime.get(mission.task_id).task_id == mission.task_id


def test_cancel_this_resolves_single_obvious_mission(tmp_path):
    _, runtime, _, coordinator = stack(tmp_path)
    mission = runtime.schedule("later", {"kind": "manual"})
    result = coordinator.handle("отмени это")
    assert result.mission.task_id == mission.task_id
    assert result.mission.status is MissionStatus.CANCELLED


def test_multiple_control_candidates_get_one_clarification(tmp_path):
    _, runtime, _, coordinator = stack(tmp_path)
    runtime.schedule("first", {"kind": "manual"})
    runtime.schedule("second", {"kind": "manual"})
    result = coordinator.handle("отмени это")
    assert result.clarification
    assert result.mission is None
    assert result.interpretation.missing_required_information == ["referenced_mission"]


def test_time_correction_updates_same_mission_without_duplicate(tmp_path):
    _, runtime, _, coordinator = stack(tmp_path)
    first = coordinator.handle("в 15:00 напомни проверить проект")
    corrected = coordinator.handle("нет, лучше в 16:00")
    assert corrected.mission.task_id == first.mission.task_id
    assert len(runtime.list_missions(include_terminal=False)) == 1
    due = datetime.fromisoformat(corrected.mission.trigger["due_at"])
    assert due.astimezone(timezone(timedelta(hours=5))).hour == 16


def test_subject_correction_closes_old_grant_before_new_proposal(tmp_path):
    _, _, authority, coordinator = stack(tmp_path, integrations={"channel-app"})
    first = coordinator.handle(
        "если Первый напишет в Channel-App — отвечай ему пока меня нет", source_id="u1"
    )
    corrected = coordinator.handle("не Первый, а Второй", source_id="u2")
    assert authority.get(first.grant.grant_id).status is AuthorityStatus.REVOKED
    assert corrected.grant.subjects == ("второй",)
    assert corrected.mission.task_id == first.mission.task_id


def test_temporary_context_has_expiry_and_is_not_memory(tmp_path):
    _, _, _, coordinator = stack(tmp_path, integrations={"channel-app"})
    item = coordinator.interpreter.interpret(
        "я отойду до вечера, если Контакт напишет в Channel-App — отвечай"
    )
    assert item.temporary_context["expires_at"]
    assert item.temporary_context["storage"] == "mission_context"


def test_natural_response_hides_ids_and_schema(tmp_path):
    _, _, _, coordinator = stack(tmp_path)
    result = coordinator.handle("через час напомни проверить проект")
    text = result.response.casefold()
    assert result.mission.task_id.casefold() not in text
    assert "trigger" not in text
    assert "mission" not in text


def test_simple_conversation_adds_no_classifier_llm_call(tmp_path):
    _, _, _, coordinator = stack(tmp_path)
    result = coordinator.handle("привет")
    assert result.interpretation.llm_calls == 0


def test_long_lived_modes_use_at_most_one_structured_brain_call():
    clock = Clock()
    calls = []

    def backend(text, contract):
        calls.append((text, contract))
        if "duration_minutes" in str(contract):
            return {"subject": "contact", "resource": "channel-app",
                    "duration_minutes": 30, "natural_response": "Поручение принято."}
        return {"natural_response": "Задача принята."}

    interpreter = NaturalMissionInterpreter(
        clock=clock, integrations={"channel-app"}, structured_backend=backend,
    )
    scheduled = interpreter.interpret("через час напомни проверить проект")
    conditional = interpreter.interpret("если калькулятор закроется — скажи")
    delegated = interpreter.interpret("если Контакт напишет в Channel-App — отвечай ему")

    assert [scheduled.llm_calls, conditional.llm_calls, delegated.llm_calls] == [1, 1, 1]
    assert len(calls) == 3


def test_no_example_specific_literals_in_product_source():
    source = open("core/understanding/semantic.py", encoding="utf-8").read().casefold()
    for literal in ("юсуф", "telegram", "minecraft", "gym", "зал"):
        assert literal not in source
