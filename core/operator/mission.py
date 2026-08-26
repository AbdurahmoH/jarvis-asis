"""Capability Engine operator mission: execute → observe → verify → repair → learn."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.actions.base import ActionResult
from core.operator.reference import DesiredStateDiff, ReferenceInterpreter
from core.operator.session import ForegroundClass, ForegroundSession
from core.security.redaction import contains_secret


class MissionCancelled(RuntimeError):
    pass


class MissionControl:
    """Thread-safe user interruption state retained for the active mission."""

    def __init__(self) -> None:
        self.state = "running"
        self.protected_paths: set[str] = set()
        self.skipped_paths: set[str] = set()
        self.current_action = ""
        self._condition = threading.Condition()

    def command(self, command: str, *, path: str = "") -> dict[str, Any]:
        low = " ".join(command.casefold().split())
        with self._condition:
            if low in {"стоп", "отмени", "cancel", "stop"}:
                self.state = "cancelled"
            elif low in {"пауза", "pause"}:
                self.state = "paused"
            elif low in {"продолжай", "resume", "continue"}:
                self.state = "running"
                self._condition.notify_all()
            elif low in {"не меняй это", "protect"} and path:
                self.protected_paths.add(path)
            elif low in {"пропусти", "skip"} and path:
                self.skipped_paths.add(path)
            return {"state": self.state, "current_action": self.current_action,
                    "protected_paths": sorted(self.protected_paths)}

    def checkpoint(self, action: str) -> None:
        with self._condition:
            self.current_action = action
            while self.state == "paused":
                self._condition.wait(timeout=0.25)
            if self.state == "cancelled":
                raise MissionCancelled("mission cancelled")

    def is_protected(self, path: str) -> bool:
        return path in self.protected_paths or path in self.skipped_paths


@dataclass
class OperatorMissionReport:
    completed: bool
    state: str
    user_message: str
    desired_state: dict[str, Any]
    current_before: dict[str, Any]
    observed: dict[str, Any]
    changes: dict[str, dict[str, Any]]
    repairs: list[str] = field(default_factory=list)
    action_trace: list[dict[str, Any]] = field(default_factory=list)
    installer: dict[str, Any] | None = None
    knowledge_path: str = ""
    episode_id: str = ""
    reused_knowledge: bool = False
    reused_episode: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)


class OperatorMission:
    """One verified path through the existing Capability Engine."""

    def __init__(self, *, capability_engine: Any, knowledge_store: Any,
                 resolver: Any, installer: Any, explorer: Any,
                 reference_interpreter: ReferenceInterpreter | None = None,
                 windows: Any = None) -> None:
        self.capability_engine = capability_engine
        self.knowledge_store = knowledge_store
        self.resolver = resolver
        self.installer = installer
        self.explorer = explorer
        self.reference_interpreter = reference_interpreter or ReferenceInterpreter()
        self.windows = windows

    def run(self, *, request: str, application: str, reference: Any, adapter: Any,
            package_id: str | None = None, max_repairs: int = 2,
            control: MissionControl | None = None) -> OperatorMissionReport:
        started = time.perf_counter()
        control = control or MissionControl()
        trace: list[dict[str, Any]] = []
        results: list[ActionResult] = []
        interpreted = self.reference_interpreter.interpret(reference)
        desired = dict(interpreted.desired_state)
        safe_desired, has_secret = self._without_secrets(desired)
        if has_secret:
            return OperatorMissionReport(
                False, "user_required",
                "Сэр, здесь нужен ваш пароль. Введите его — дальше я сам.",
                safe_desired, {}, {}, {},
                action_trace=[{"phase": "reference", "status": "user_required"}],
                metrics={"duration": time.perf_counter() - started},
            )

        reused_episodes = self.capability_engine.catalog.retrieve_episodes(request, limit=1)
        existing = self.knowledge_store.load(application)
        reused_knowledge = existing is not None
        research_calls = 0
        discovery_steps = 0
        installer_payload: dict[str, Any] | None = None
        candidate = None
        try:
            if existing is None:
                control.checkpoint("research trusted software source")
                research_calls += 1
                candidate = self.resolver.resolve(application, package_id=package_id)
                trace.append({"phase": "research", "status": "resolved" if candidate else "missing",
                              "source": getattr(candidate, "official_source", "")})
                if candidate is None or not getattr(candidate, "trusted", False):
                    return self._failure(
                        "research_required", safe_desired, trace, started,
                        message="Сэр, проверенный источник пока не найден.",
                    )
                control.checkpoint("install and independently verify application")
                evidence = self.installer.install(candidate, launch=True)
                installer_payload = self._public_installer_evidence(evidence)
                trace.append({"phase": "install", "status": "verified" if evidence.verified else "failed",
                              "failed_checks": list(getattr(evidence, "failed_checks", []))})
                if not evidence.verified:
                    return self._failure(
                        "installation_verification_failed", safe_desired, trace, started,
                        installer=installer_payload,
                        message="Сэр, установка не прошла независимую проверку.",
                    )
                control.checkpoint("inspect application accessibility tree")
                existing = self.explorer.explore(application)
                existing.executable = str(getattr(evidence, "executable", ""))
                existing.software = candidate.to_dict() if hasattr(candidate, "to_dict") else {}
                discovery_steps = int(existing.discovery_steps)
                trace.append({"phase": "explore", "status": "mapped",
                              "controls": len(existing.controls)})
            else:
                existing = self.knowledge_store.mark_reused(application) or existing
                trace.append({"phase": "explore", "status": "reused",
                              "selectors": len(existing.successful_selectors)})

            control.checkpoint("observe current application state")
            current = dict(adapter.observe())
            delta = DesiredStateDiff.between(current, safe_desired)
            trace.append({"phase": "diff", "changes": sorted(delta.changes),
                          "matches": delta.matches})
            checkpoint = adapter.checkpoint(list(delta.changes))

            self._apply_changes(adapter, delta.changes, control, trace, results, repair=False)
            observed = dict(adapter.observe())
            remaining = DesiredStateDiff.between(observed, safe_desired)
            repairs: list[str] = []
            attempts = 0
            while remaining.changes and attempts < max(0, int(max_repairs)):
                control.checkpoint("targeted repair")
                paths = list(remaining.changes)
                repairs.extend(path for path in paths if path not in repairs)
                self._apply_changes(adapter, remaining.changes, control, trace, results, repair=True)
                observed = dict(adapter.observe())
                remaining = DesiredStateDiff.between(observed, safe_desired)
                attempts += 1

            if remaining.changes:
                rollback = adapter.rollback(checkpoint)
                trace.append({"phase": "rollback", "status": "restored" if rollback.get("restored") else "failed"})
                return OperatorMissionReport(
                    False, "verification_failed", "Сэр, результат не совпал с заданным состоянием.",
                    safe_desired, current, observed, remaining.changes, repairs, trace,
                    installer_payload, reused_knowledge=reused_knowledge,
                    reused_episode=bool(reused_episodes),
                    metrics={"research_calls": research_calls, "discovery_steps": discovery_steps,
                             "duration": time.perf_counter() - started},
                )

            existing.successful_selectors.update(dict(getattr(adapter, "semantic_selectors", {}) or {}))
            method = str(getattr(adapter, "execution_method", "") or "")
            if method:
                existing.best_execution_method = method
            for path, selector in dict(getattr(adapter, "semantic_selectors", {}) or {}).items():
                location = str(selector.get("path", "")) if isinstance(selector, dict) else ""
                if location:
                    existing.settings_locations[path] = [location]
            existing.verification_rules = [
                {"path": path, "equals": value} for path, value in self._flatten(safe_desired).items()
            ]
            knowledge_path = self.knowledge_store.save(existing)
            duration = time.perf_counter() - started
            episode = self.capability_engine.record_verified_operator_episode(
                goal=request,
                application=application,
                desired_state=safe_desired,
                observed=observed,
                results=results,
                repairs=repairs,
                duration=duration,
            )
            trace.append({"phase": "verification", "status": "verified",
                          "observed": observed})
            trace.append({"phase": "learning", "status": "saved",
                          "knowledge": str(knowledge_path), "episode": episode.episode_id})
            return OperatorMissionReport(
                True, "completed", "Готово. Результат проверен.", safe_desired,
                current, observed, {}, repairs, trace, installer_payload,
                str(knowledge_path), episode.episode_id, reused_knowledge,
                bool(reused_episodes),
                {"research_calls": research_calls, "discovery_steps": discovery_steps,
                 "repairs": len(repairs), "duration": duration},
            )
        except MissionCancelled:
            return self._failure("cancelled", safe_desired, trace, started,
                                 message="Понял. Действие отменено.")

    def _apply_changes(self, adapter: Any, changes: dict[str, dict[str, Any]],
                       control: MissionControl, trace: list[dict[str, Any]],
                       results: list[ActionResult], *, repair: bool) -> None:
        foreground = bool(getattr(adapter, "foreground_required", False))
        session = ForegroundSession(
            self.windows,
            classification=ForegroundClass.FOREGROUND_REQUIRED,
            target_title=str(getattr(adapter, "window_title", "")),
        ) if foreground and self.windows is not None else None

        def execute() -> None:
            for path, change in changes.items():
                if control.is_protected(path):
                    trace.append({"phase": "repair" if repair else "execution",
                                  "path": path, "status": "protected"})
                    continue
                control.checkpoint(f"{'repair' if repair else 'apply'} {path}")
                result = adapter.apply_setting(path, change["desired"])
                if not isinstance(result, ActionResult):
                    result = ActionResult("operator.apply_setting", {"path": path},
                                          bool(getattr(result, "ok", False)), output=result)
                results.append(result)
                trace.append({"phase": "repair" if repair else "execution", "path": path,
                              "status": "accepted" if result.ok else "failed"})

        if session is None:
            execute()
        else:
            with session:
                execute()

    @staticmethod
    def _without_secrets(value: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        result: dict[str, Any] = {}
        found = False
        for key, item in value.items():
            if contains_secret(item, field_name=key):
                found = True
                continue
            if isinstance(item, dict):
                nested, nested_found = OperatorMission._without_secrets(item)
                result[key] = nested
                found = found or nested_found
            else:
                result[key] = item
        return result, found

    @staticmethod
    def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                result.update(OperatorMission._flatten(item, path))
            else:
                result[path] = item
        return result

    @staticmethod
    def _public_installer_evidence(evidence: Any) -> dict[str, Any]:
        return {
            "installer_exit": getattr(evidence, "installer_exit", None),
            "installed": bool(getattr(evidence, "installed", False)),
            "verified": bool(getattr(evidence, "verified", False)),
            "executable": str(getattr(evidence, "executable", "")),
            "version": str(getattr(evidence, "version", "")),
            "signature_status": str(getattr(evidence, "signature_status", "")),
            "launched": bool(getattr(evidence, "launched", False)),
            "window": getattr(evidence, "window", None),
            "checks": dict(getattr(evidence, "checks", {}) or {}),
            "failed_checks": list(getattr(evidence, "failed_checks", [])),
        }

    @staticmethod
    def _failure(state: str, desired: dict[str, Any], trace: list[dict[str, Any]],
                 started: float, *, message: str, installer: dict[str, Any] | None = None) -> OperatorMissionReport:
        return OperatorMissionReport(
            False, state, message, desired, {}, {}, {}, action_trace=trace,
            installer=installer, metrics={"duration": time.perf_counter() - started},
        )
