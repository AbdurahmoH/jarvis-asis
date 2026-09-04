"""End-to-end operator orchestration contracts inside Capability Engine."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.actions.base import ActionResult
from core.actions.registry import DEFAULT_REGISTRY
from core.capability_engine import CapabilityCatalog, CapabilityEngine
from core.operator.knowledge import AppKnowledge, AppKnowledgeStore
from core.operator.mission import MissionControl, OperatorMission
from core.operator.software import SoftwareCandidate


class _Resolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, _name, **_kwargs):
        self.calls += 1
        return SoftwareCandidate(
            name="Fixture App", package_id="Fixture.App", official_source="winget://Fixture.App",
            source_kind="package_manager", package_manager="winget", installer_type="exe",
            trusted=True, version="1.0", expected_executable="fixture.exe",
        )


class _Installer:
    def __init__(self) -> None:
        self.calls = 0

    def install(self, candidate, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            verified=True, executable="C:/Fixture/fixture.exe", version=candidate.version,
            window={"title": "Fixture App", "process_id": 42}, failed_checks=[],
        )


class _Explorer:
    def __init__(self) -> None:
        self.calls = 0

    def explore(self, application, **_kwargs):
        self.calls += 1
        return AppKnowledge(
            application=application,
            windows=[{"name": application, "control_type": "Window"}],
            controls=[{"name": "Theme", "control_type": "ComboBox"}],
            discovery_steps=4,
        )


class _Adapter:
    foreground_required = False
    window_title = "Fixture App"

    def __init__(self, *, repairable: bool = True) -> None:
        self.state = {"theme": "Light", "wrap": False}
        self.calls: list[tuple[str, object]] = []
        self.repairable = repairable
        self.wrap_attempts = 0
        self.rolled_back = False
        self.semantic_selectors = {
            "theme": {"name": "Theme", "control_type": "ComboBox"},
            "wrap": {"name": "Word wrap", "control_type": "CheckBox"},
        }

    def observe(self):
        return dict(self.state)

    def checkpoint(self, _paths):
        return dict(self.state)

    def apply_setting(self, path, value):
        self.calls.append((path, value))
        if path == "wrap":
            self.wrap_attempts += 1
            if self.repairable and self.wrap_attempts >= 2:
                self.state[path] = value
        elif self.repairable:
            self.state[path] = value
        # Deliberately ok even when the state did not change.
        return ActionResult("operator.apply_setting", {"path": path}, True, "accepted")

    def rollback(self, checkpoint):
        self.state = dict(checkpoint)
        self.rolled_back = True
        return {"restored": True}


def _mission(tmp_path: Path, resolver: _Resolver, installer: _Installer,
             explorer: _Explorer) -> OperatorMission:
    catalog = CapabilityCatalog(tmp_path / "capabilities")
    engine = CapabilityEngine(catalog, DEFAULT_REGISTRY)
    return OperatorMission(
        capability_engine=engine,
        knowledge_store=AppKnowledgeStore(tmp_path / "app_knowledge"),
        resolver=resolver,
        installer=installer,
        explorer=explorer,
    )


def test_operator_mission_observes_repairs_only_mismatch_then_learns(tmp_path: Path) -> None:
    resolver, installer, explorer = _Resolver(), _Installer(), _Explorer()
    mission = _mission(tmp_path, resolver, installer, explorer)
    adapter = _Adapter(repairable=True)

    report = mission.run(
        request="Install Fixture App and configure it",
        application="Fixture App",
        reference={"application": "Fixture App", "settings": {"theme": "Dark", "wrap": True}},
        adapter=adapter,
        package_id="Fixture.App",
    )

    assert report.completed is True
    assert "Готово" in report.user_message and ("проверен" in report.user_message.casefold() or "проверяйте" in report.user_message.casefold())
    assert report.repairs == ["wrap"]
    assert adapter.calls == [("theme", "Dark"), ("wrap", True), ("wrap", True)]
    assert report.observed == {"theme": "Dark", "wrap": True}
    assert report.episode_id
    assert report.knowledge_path and Path(report.knowledge_path).is_file()
    learned = mission.knowledge_store.load("Fixture App")
    assert learned is not None
    assert learned.best_execution_method == "uia"
    assert resolver.calls == installer.calls == explorer.calls == 1


def test_action_result_ok_never_completes_mission_without_observed_state(tmp_path: Path) -> None:
    mission = _mission(tmp_path, _Resolver(), _Installer(), _Explorer())
    adapter = _Adapter(repairable=False)

    report = mission.run(
        request="Install and configure Fixture App",
        application="Fixture App",
        reference={"settings": {"theme": "Dark", "wrap": True}},
        adapter=adapter,
    )

    assert report.completed is False
    assert report.state == "verification_failed"
    assert "Готово" not in report.user_message
    assert adapter.rolled_back is True
    assert report.episode_id == ""


def test_second_run_reuses_app_knowledge_and_capability_episode(tmp_path: Path) -> None:
    resolver, installer, explorer = _Resolver(), _Installer(), _Explorer()
    mission = _mission(tmp_path, resolver, installer, explorer)
    first = mission.run(
        request="Install Fixture App and configure it",
        application="Fixture App",
        reference={"settings": {"theme": "Dark", "wrap": True}},
        adapter=_Adapter(),
    )
    second_adapter = _Adapter()
    second = mission.run(
        request="Install Fixture App and configure it",
        application="Fixture App",
        reference={"settings": {"theme": "Dark", "wrap": True}},
        adapter=second_adapter,
    )

    assert first.completed and second.completed
    assert second.reused_knowledge is True
    assert second.reused_episode is True
    assert second.metrics["discovery_steps"] < first.metrics["discovery_steps"]
    assert second.metrics["research_calls"] < first.metrics["research_calls"]
    assert resolver.calls == installer.calls == explorer.calls == 1


def test_mission_rejects_secret_settings_before_persistence(tmp_path: Path) -> None:
    mission = _mission(tmp_path, _Resolver(), _Installer(), _Explorer())

    report = mission.run(
        request="Configure Fixture App",
        application="Fixture App",
        reference={"settings": {"password": "do-not-store", "theme": "Dark"}},
        adapter=_Adapter(),
    )

    assert report.completed is False
    assert report.state == "user_required"
    assert "do-not-store" not in str(report)
    assert not list((tmp_path / "app_knowledge").glob("*.json"))


def test_mission_control_handles_pause_resume_cancel_and_protected_paths() -> None:
    control = MissionControl()

    assert control.command("пауза")["state"] == "paused"
    assert control.command("продолжай")["state"] == "running"
    control.command("не меняй это", path="theme")
    assert control.is_protected("theme") is True
    assert control.command("что ты делаешь?")["state"] == "running"
    assert control.command("отмени")["state"] == "cancelled"
