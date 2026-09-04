from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.agent import Agent, AgentConfig
from core.actions.base import ActionResult
from core.actions.system import system_status
from core.executive import (
    DomainObservation,
    FactType,
    LocalWorldObserver,
    UnifiedWorldState,
    WorldQueryRouter,
)
from core.verifier import verify_action_result, verify_system_metrics


def test_real_drive_observation_enumerates_logical_volumes_without_assuming_c():
    observation = LocalWorldObserver().observe("storage")

    assert observation.ok, observation.error
    assert observation.source == "psutil.disk_partitions"
    assert observation.data["volumes"]
    assert all(item["mountpoint"] for item in observation.data["volumes"])
    assert all("total_bytes" in item and "free_bytes" in item for item in observation.data["volumes"])


def test_storage_totals_are_internally_consistent():
    observation = LocalWorldObserver().observe("storage")

    assert observation.ok, observation.error
    for volume in observation.data["volumes"]:
        tolerance = max(4096, int(volume["total_bytes"] * 0.01))
        assert abs(volume["total_bytes"] - volume["used_bytes"] - volume["free_bytes"]) <= tolerance
        assert 0.0 <= volume["used_percent"] <= 100.0


def test_current_memory_state_comes_from_real_os_observation():
    observation = LocalWorldObserver().observe("machine")

    assert observation.ok, observation.error
    memory = observation.data["memory"]
    assert memory["total_bytes"] > 0
    assert memory["available_bytes"] >= 0
    assert 0.0 <= memory["used_percent"] <= 100.0
    assert "cpu" in observation.data


def test_system_status_keeps_structured_fresh_os_evidence():
    status = system_status()
    result = ActionResult("system_status", {}, not status["errors"], status)

    assert status["fact_type"] == "observed"
    assert status["freshness"] == "fresh"
    assert status["observed_at"]
    assert status["source"]
    assert verify_system_metrics(result).verified is True


def test_process_observation_is_real_bounded_and_llm_independent():
    observation = LocalWorldObserver().observe("processes", limit=4096)

    assert observation.ok, observation.error
    assert observation.data["total_count"] >= len(observation.data["processes"])
    assert any(item["pid"] == os.getpid() for item in observation.data["processes"])
    assert all({"pid", "name", "status"} <= set(item) for item in observation.data["processes"])


def test_desktop_observation_is_evidence_backed_or_honestly_unavailable():
    observation = LocalWorldObserver().observe("desktop")

    if observation.ok:
        assert observation.source == "native_windows"
        assert "active_window" in observation.data
        assert "windows" in observation.data
        assert all(
            {"handle", "title", "process_id", "process_name"} <= set(item)
            for item in observation.data["windows"]
        )
    else:
        assert observation.error
        assert observation.data == {}


def test_stale_observation_is_not_reused_as_current(tmp_path):
    now = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
    calls: list[str] = []

    class Observer:
        def observe(self, domain: str, **kwargs):
            calls.append(domain)
            return DomainObservation(
                domain=domain,
                data={"sample": len(calls)},
                source="fixture_os",
                ttl_seconds=5,
                observed_at=now + timedelta(seconds=10 * (len(calls) - 1)),
            )

    clock = [now]
    world = UnifiedWorldState(tmp_path, observer=Observer(), clock=lambda: clock[0])
    first = world.observe_domain("machine")
    clock[0] += timedelta(seconds=4)
    cached = world.observe_domain("machine")
    clock[0] += timedelta(seconds=2)
    refreshed = world.observe_domain("machine")

    assert first is cached
    assert refreshed.value == {"sample": 2}
    assert calls == ["machine", "machine"]


def test_provenance_distinguishes_observed_memory_inference_and_user_report(tmp_path):
    world = UnifiedWorldState(tmp_path)

    facts = [
        world.observe("machine.cpu", 10, source="psutil", fact_type=FactType.OBSERVED),
        world.observe("preference.browser", "Chrome", source="memory", fact_type=FactType.REMEMBERED),
        world.observe("diagnosis", "disk pressure", source="reasoner", fact_type=FactType.INFERRED),
        world.observe("user.symptom", "slow", source="user", fact_type=FactType.USER_REPORTED),
    ]

    assert [fact.fact_type for fact in facts] == [
        FactType.OBSERVED.value,
        FactType.REMEMBERED.value,
        FactType.INFERRED.value,
        FactType.USER_REPORTED.value,
    ]
    assert world.get("preference.browser").fact_type != FactType.OBSERVED.value


def test_natural_storage_query_uses_local_perception_without_action_planning(settings, monkeypatch):
    """Детерминированный ответ на запрос состояния дисков через system_status без LLM (§3, Вариант А)."""
    settings.deepseek_brain_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    monkeypatch.setattr(
        agent,
        "_discover_capabilities",
        lambda **kwargs: pytest.fail("current system observation reached capability planning"),
    )
    monkeypatch.setattr(
        agent,
        "_stream_consume",
        lambda *args, **kwargs: pytest.fail("simple OS observation reached an LLM"),
    )

    outcome = agent.execute("сколько свободного места на дисках?")

    assert outcome.mode == "fast_path"
    assert outcome.verified is True
    assert outcome.tool_used == "system_status"
    assert "свободно" in outcome.text.lower()
    assert "диск" in outcome.text.lower()


def test_observation_failure_is_structured_and_never_invented(tmp_path):
    class Observer:
        def observe(self, domain: str, **kwargs):
            raise PermissionError("fixture access denied")

    world = UnifiedWorldState(tmp_path, observer=Observer())
    result = world.query("сколько свободного места на дисках?", force=True)

    assert result.ok is False
    assert result.observations[0].error == "PermissionError: fixture access denied"
    assert result.observations[0].value is None
    assert result.observations[0].fact_type == FactType.OBSERVED.value


def test_world_initialization_is_lazy_and_does_not_scan_or_call_models(tmp_path):
    class Observer:
        def __init__(self):
            self.calls = 0

        def observe(self, domain: str, **kwargs):
            self.calls += 1
            raise AssertionError("initialization performed an observation")

    observer = Observer()
    started = time.perf_counter()
    world = UnifiedWorldState(tmp_path, observer=observer)
    elapsed = time.perf_counter() - started

    assert observer.calls == 0
    assert world.current() == {}
    assert elapsed < 0.2


def test_recent_pdf_search_is_bounded_and_scoped(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    old_pdf = downloads / "old.pdf"
    new_pdf = downloads / "new.pdf"
    other = downloads / "note.txt"
    old_pdf.write_bytes(b"old")
    new_pdf.write_bytes(b"new")
    other.write_text("text", encoding="utf-8")
    os.utime(old_pdf, (1_700_000_000, 1_700_000_000))
    os.utime(new_pdf, (1_800_000_000, 1_800_000_000))

    observer = LocalWorldObserver(roots={"downloads": downloads})
    observation = observer.observe(
        "filesystem",
        roots=("downloads",),
        extension=".pdf",
        sort="modified_desc",
        limit=10,
        max_files=25,
    )

    assert observation.ok, observation.error
    assert [Path(item["path"]).name for item in observation.data["files"]] == [
        "new.pdf",
        "old.pdf",
    ]
    assert observation.data["scanned_files"] <= 25


def test_world_query_router_separates_current_state_from_history():
    router = WorldQueryRouter()

    assert router.route("запущен ли браузер сейчас?").domains == ("browser",)
    assert router.route("я запускал майнкрафт вчера?").current is False
    assert router.route("что сейчас активно на экране?").domains == ("screen",)


def test_human_memory_question_does_not_route_to_machine_observation():
    query = WorldQueryRouter().route("почему память человека ограничена?")

    assert query.domains == ()


def test_ordinary_prose_does_not_trigger_storage_from_preposition_tom():
    query = WorldQueryRouter().route(
        "Напиши эссе о том, как паровые машины изменили промышленность"
    )

    assert query.domains == ()


@pytest.mark.parametrize(
    ("query", "domain"),
    [
        ("какие у меня диски?", "storage"),
        ("сколько свободного места?", "storage"),
        ("сколько оперативной памяти сейчас занято?", "machine"),
        ("какие программы сейчас открыты?", "desktop"),
        ("что у меня сейчас открыто?", "desktop"),
        ("запущен ли браузер?", "browser"),
        ("найди тот PDF, который я вчера скачивал", "filesystem"),
    ],
)
def test_world_query_router_covers_general_current_state_concepts(query, domain):
    assert domain in WorldQueryRouter().route(query).domains


def test_volatile_observation_is_not_promoted_to_durable_memory(tmp_path):
    class Observer:
        def observe(self, domain: str, **kwargs):
            return DomainObservation(domain=domain, data={"current": True}, source="fixture_os")

    world = UnifiedWorldState(tmp_path, observer=Observer())
    world.observe_domain("machine")

    assert world.current()
    assert UnifiedWorldState(tmp_path, observer=Observer()).current() == {}


def test_model_context_is_domain_scoped_and_process_output_is_reduced(tmp_path):
    class Observer:
        def observe(self, domain: str, **kwargs):
            return DomainObservation(
                domain=domain,
                source="fixture_os",
                data={
                    "total_count": 30,
                    "processes": [
                        {
                            "pid": index,
                            "name": f"process-{index}",
                            "status": "running",
                            "cpu_percent": 0.0,
                            "memory_percent": 0.1,
                            "executable": f"C:/private/{index}.exe",
                        }
                        for index in range(30)
                    ],
                },
            )

    world = UnifiedWorldState(tmp_path, observer=Observer())
    world.observe_domain("processes")
    context = world.context_for("какие процессы сейчас запущены?")
    processes = next(iter(context.values()))["value"]["processes"]

    assert len(processes) == 10
    assert all("executable" not in item for item in processes)


def test_process_absence_is_not_verified_when_os_observation_fails(monkeypatch):
    monkeypatch.setattr("core.verifier._process_matches", lambda names: None)
    result = ActionResult("close_app", {"name": "fixture"}, True, "closed")

    verification = verify_action_result(result)

    assert verification.verified is False
    assert "недоступно" in verification.detail
