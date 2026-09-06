"""Sprint 9 — capability acquisition, verification, learning and continuity."""
from __future__ import annotations

import pytest

from pathlib import Path

from config.settings import Settings
from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import ToolRegistry
from core.llm.factory import clear_backend_cache, get_llm_backend
from core.llm.tiers import Tier
from core.model_router import ModelRouter


@pytest.mark.skipif(not Settings().local_llm_enabled,
                    reason="local LLM disabled in cloud-only build")
def test_offline_coder_keeps_task_role_with_best_local_model(tmp_path):
    model = tmp_path / "qwen.gguf"
    model.write_bytes(b"fixture")
    settings = Settings(offline_mode=True)
    settings.local_model.gguf_path = str(model)
    settings.model_tiers.coder = "coder-role"
    settings.tier_providers.coder = "local"
    clear_backend_cache()
    try:
        decision = ModelRouter(settings).route("напиши Python код и тесты для функции")
        backend = get_llm_backend(settings, Tier.CODER)
        assert decision.tier is Tier.CODER
        assert backend.task_role == "coder"
        assert backend.gguf_path == model
    finally:
        clear_backend_cache()


def test_unknown_task_composes_existing_tools_before_generation(tmp_path):
    from core.capability_engine import CapabilityCatalog, CapabilityPlanner

    registry = ToolRegistry()
    for name in ("list_files", "file_move", "list_files_recursive"):
        registry.register(_FakeTool(name))
    generated = []
    planner = CapabilityPlanner(
        CapabilityCatalog(tmp_path / "capabilities"), registry,
        acquire=lambda goal: generated.append(goal),
    )

    plan = planner.plan("организуй файлы по расширению")

    assert plan.acquisition == "composed"
    assert [step.tool for step in plan.steps] == [
        "list_files", "file_move", "list_files_recursive",
    ]
    assert generated == []


def test_acquired_capability_is_persisted(tmp_path):
    from core.capability_engine import CapabilityCatalog, CapabilityDefinition, CapabilityKind

    catalog = CapabilityCatalog(tmp_path)
    capability = CapabilityDefinition(
        id="configure_streaming_application", description="Configure streaming software",
        tools=["app.discover", "file.write"], kind=CapabilityKind.LEARNED,
        success_criteria=["desired configuration matches"], confidence=0.91,
    )
    catalog.save(capability)

    restored = CapabilityCatalog(tmp_path).get(capability.id)
    assert restored is not None
    assert restored.kind is CapabilityKind.LEARNED
    assert restored.tools == capability.tools


def test_tool_success_does_not_complete_when_desired_state_is_missing():
    from core.capability_engine import DesiredStateVerifier

    result = DesiredStateVerifier().verify(
        {"installed": True, "encoder": "H264"},
        {"installed": True, "encoder": "default"},
    )
    assert result.verified is False
    assert result.missing == {"encoder": {"expected": "H264", "actual": "default"}}


def test_repair_targets_only_failed_desired_state_step(tmp_path):
    from core.capability_engine import (
        CapabilityCatalog, CapabilityEngine, ExecutionPlan, ExecutionStep,
    )

    calls = []
    plan = ExecutionPlan(
        goal="configure app", acquisition="composed",
        desired_state={"installed": True, "configured": True},
        steps=[
            ExecutionStep("install", "install", produces={"installed": True}),
            ExecutionStep("configure", "configure", depends_on=["install"],
                          produces={"configured": True}),
        ],
    )
    engine = CapabilityEngine(
        CapabilityCatalog(tmp_path), ToolRegistry(),
        executor=lambda tool, args: calls.append(tool) or ActionResult(tool, args, True, "ok"),
        observer=lambda: {"installed": True, "configured": False},
    )

    report = engine.execute(plan, max_repairs=1)
    assert report.completed is False
    assert calls == ["install", "configure", "configure"]
    assert report.repairs == ["configure"]


def test_successful_unknown_task_creates_retrievable_generalized_episode(tmp_path):
    from core.capability_engine import CapabilityCatalog, CapabilityEpisode

    catalog = CapabilityCatalog(tmp_path)
    episode = CapabilityEpisode(
        goal="configure OBS from reference", capability="configure_obs",
        task_class="configure_streaming_software",
        successful_strategy=["inspect", "write config", "verify"],
        verification=["encoder=H264"], generalized_procedure=[
            "streaming applications expose encoder bitrate resolution fps audio"
        ], confidence=0.93,
    )
    catalog.record_episode(episode)

    matches = CapabilityCatalog(tmp_path).retrieve_episodes(
        "configure Streamlabs encoder bitrate and fps", limit=3,
    )
    assert matches
    assert matches[0].task_class == "configure_streaming_software"


def test_high_confidence_high_risk_still_requires_confirmation():
    from core.capability_engine import RiskConfidencePolicy

    decision = RiskConfidencePolicy().decide(confidence=0.99, risk="high")
    assert decision.action == "confirm"
    assert decision.auto_execute is False


def test_failed_file_change_rolls_back(tmp_path):
    from core.capability_engine import Transaction

    target = tmp_path / "settings.ini"
    target.write_text("before", encoding="utf-8")
    tx = Transaction(tmp_path / "checkpoints")
    tx.checkpoint_file(target)
    target.write_text("after", encoding="utf-8")

    tx.rollback()
    assert target.read_text(encoding="utf-8") == "before"


def test_persistent_mission_can_cancel_explain_and_restore(tmp_path):
    from core.capability_engine import MissionState, MissionStateStore

    store = MissionStateStore(tmp_path)
    mission = MissionState(
        mission_id="M-1", goal="configure app", state="executing", current_step=1,
        desired_state={"configured": True}, completed_steps=["discover"],
        pending_steps=["configure", "verify"], rollback=[{"kind": "file"}],
    )
    store.save(mission)
    restored = MissionStateStore(tmp_path).load("M-1")
    assert restored is not None
    assert restored.explain_current_step() == "configure"
    restored.cancel()
    store.save(restored)
    assert MissionStateStore(tmp_path).load("M-1").state == "cancelled"


def test_reference_interpreter_extracts_desired_state_not_click_sequence():
    from core.capability_engine import ReferenceInterpreter

    result = ReferenceInterpreter().interpret({
        "application": "fixture-app",
        "settings": {"encoder": "H264", "fps": 60},
        "clicks": [[10, 20], [30, 40]],
    })
    assert result.desired_state == {"encoder": "H264", "fps": 60}
    assert "clicks" not in result.desired_state


def test_windows_execution_ladder_prefers_uia_over_vision():
    from core.platform.windows import ProviderChain, ProviderResult, WindowsAutomationProvider

    class Provider(WindowsAutomationProvider):
        def __init__(self, name, level, supported):
            self.name, self.ladder_level, self.supported = name, level, supported
        def supports(self, operation): return self.supported
        def invoke(self, operation, **kwargs): return ProviderResult(True, self.name)

    chain = ProviderChain([
        Provider("vision", 6, True), Provider("uia", 4, True), Provider("raw", 7, True),
    ])
    assert chain.invoke("ui.inspect").value == "uia"


def test_browser_provider_uses_dom_engine():
    from core.platform.browser import BrowserAutomationProvider

    class FakeEngine:
        def open(self, url): return {"ok": True, "url": url}
        def read(self, index=None): return {"ok": True, "text": "DOM text"}
        def list_elements(self): return [{"index": 0, "text": "button"}]

    browser = BrowserAutomationProvider(FakeEngine())
    assert browser.navigate("https://fixture.invalid")["ok"] is True
    assert browser.read_page()["text"] == "DOM text"
    assert browser.inspect()[0]["index"] == 0


def test_shadow_backlog_pauses_during_high_load(tmp_path):
    from core.shadow import ShadowBacklog

    backlog = ShadowBacklog(tmp_path)
    backlog.add("improve_pdf", priority=0.9, reason="frequent repairs")
    assert backlog.next(cpu_percent=95, gpu_percent=20, gaming=True) is None
    assert backlog.next(cpu_percent=10, gpu_percent=10, gaming=False).id == "improve_pdf"


def test_safe_unknown_file_organization_executes_verifies_and_learns(tmp_path):
    from core.actions import DEFAULT_REGISTRY
    from core.capability_engine import CapabilityCatalog, CapabilityEngine, CapabilityPlanner

    for name, content in (("a.txt", "a"), ("b.md", "b"), ("c.txt", "c")):
        (tmp_path / name).write_text(content, encoding="utf-8")
    settings = Settings()
    settings.paths.documents_dir = str(tmp_path)
    catalog = CapabilityCatalog(tmp_path / ".capabilities")
    planner = CapabilityPlanner(catalog, DEFAULT_REGISTRY)
    plan = planner.plan("организуй файлы по расширению")
    engine = CapabilityEngine(catalog, DEFAULT_REGISTRY,
                              context=ToolContext(settings=settings))

    first = engine.execute(plan)
    second_plan = CapabilityPlanner(catalog, DEFAULT_REGISTRY).plan(
        "снова организуй файлы по расширению"
    )

    assert first.completed is True
    assert (tmp_path / "txt" / "a.txt").is_file()
    assert (tmp_path / "md" / "b.md").is_file()
    assert first.episode is not None
    assert second_plan.acquisition == "learned"


def test_agent_unknown_path_uses_capability_composition(tmp_path):
    from core.agent import Agent, AgentConfig

    for name in ("one.txt", "two.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    settings = Settings()
    settings.paths.data_dir = str(tmp_path / "data")
    settings.paths.documents_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    outcome = agent._handle_unknown("организуй файлы по расширению", [], None, [], "no direct tool")

    assert outcome.verified is True
    assert outcome.mode == "capability"
    assert "Готово" in outcome.text
    assert (tmp_path / "txt" / "one.txt").is_file()


def test_task_runtime_persists_and_restores_mission(tmp_path):
    from core.task_runtime import TaskRuntime

    runtime = TaskRuntime(persistence_dir=tmp_path)
    mission = runtime.submit("fixture mission", lambda current, cancel: "done")
    finished = runtime.wait(mission.task_id, timeout=5)
    assert finished.status.value == "completed"
    runtime.shutdown()

    restored_runtime = TaskRuntime(persistence_dir=tmp_path)
    restored = restored_runtime.get(mission.task_id)
    assert restored is not None
    assert restored.result == "done"
    assert restored.status.value == "completed"


def test_task_runtime_pause_skip_and_explain(tmp_path):
    from core.task_runtime import Mission, MissionStatus, TaskRuntime

    runtime = TaskRuntime(persistence_dir=tmp_path)
    mission = Mission("M-control", "configure", status=MissionStatus.PAUSED,
                      plan=[{"description": "install", "status": "completed"},
                            {"description": "configure", "status": "pending"}])
    runtime.restore_mission(mission)
    assert runtime.explain_current_step("M-control") == "configure"
    assert runtime.skip_step("M-control") is True
    assert runtime.explain_current_step("M-control") == "completed"


def test_unknown_research_prioritizes_official_sources_and_structures_plan():
    from core.capability_engine import CapabilityResearch

    plan = CapabilityResearch().structure("install fixture app", [
        {"url": "https://third.invalid/setup.exe", "kind": "third_party", "verified": False},
        {"url": "https://official.invalid/docs", "kind": "official", "verified": True},
        {"url": "https://github.invalid/org/repo", "kind": "repository", "verified": True},
    ])
    assert plan.sources[0]["kind"] == "official"
    assert all(source["verified"] for source in plan.sources)
    assert plan.steps
    assert plan.verification
    assert plan.risks


class _FakeTool(Tool):
    def __init__(self, name: str): self._name = name
    @property
    def name(self): return self._name
    @property
    def description(self): return self._name
    @property
    def input_schema(self): return {"type": "object"}
    def run(self, args, context): return ActionResult(self.name, args, True, "ok")
