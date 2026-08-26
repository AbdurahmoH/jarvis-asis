"""Quiet background coordinator for patterns, generated tools and registration."""
from __future__ import annotations

import importlib.util
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY, ToolRegistry
from core.safety import assess_risk
from core.security.atomic import atomic_json_write, load_json

from .generator import ToolGenerator
from .patterns import Pattern, PatternWatcher
from .sandbox import CodeEvaluator, SandboxReport, SandboxTester, SecurityDecision
from .backlog import ShadowBacklog


@dataclass(frozen=True)
class ToolPreparation:
    name: str
    status: str  # registered | proposed | rejected
    confidence: int
    report: SandboxReport
    path: Optional[Path] = None


class GeneratedShadowTool(Tool):
    """Adapter that presents a verified generated module as a normal Tool."""

    generated_by_shadow = True

    def __init__(self, name: str, description: str, path: Path,
                 *, evaluator: CodeEvaluator | None = None) -> None:
        self._name, self._description, self.path = name, description, path
        self._evaluator = evaluator or CodeEvaluator()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": True}

    def run(self, args: dict[str, Any], context: ToolContext) -> ActionResult:
        try:
            source = self.path.read_text(encoding="utf-8")
            report = SandboxTester(evaluator=self._evaluator).test_source(source, dict(args))
            if report.security_decision is not SecurityDecision.SAFE_TO_EVALUATE:
                return ActionResult(tool=self.name, args=args, ok=False,
                                    error=f"Shadow tool requires review: {report.security_decision.value}")
            check = self._evaluator.run_source(source, dict(args))
            if not check.passed:
                return ActionResult(tool=self.name, args=args, ok=False, error=check.detail)
            # The evaluator validates the structured contract.  A second tiny
            # JSON extraction keeps the public ActionResult useful without
            # importing generated code into the host interpreter.
            result = check.value or {"success": False, "result": None, "error": "empty evaluator result"}
            return ActionResult(tool=self.name, args=args, ok=result["success"],
                                output=result.get("result"), error=result.get("error"))
        except Exception as exc:  # generated code never escapes the executor
            return ActionResult(tool=self.name, args=args, ok=False, error=str(exc))


class ShadowEngine:
    """Local-only Shadow Engine. It does no capture and emits no noisy events."""

    def __init__(self, *, data_dir: Path | str, registry: Optional[ToolRegistry] = None,
                 settings: Any = None, enabled: bool = False,
                 generator: Optional[ToolGenerator] = None,
                 tester: Optional[SandboxTester] = None,
                 brain_fabric: Any = None) -> None:
        self.root = Path(data_dir) / "shadow"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tools_dir = Path(data_dir) / "tools" / "shadow"
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry if registry is not None else DEFAULT_REGISTRY
        self.enabled = enabled
        self.watcher = PatternWatcher(self.root)
        self.backlog = ShadowBacklog(self.root)
        from core.living.resources import CapabilityQualityLoop
        self.quality = CapabilityQualityLoop(self.root / "quality", self.backlog)
        self.generator = generator or ToolGenerator(self.root, settings=settings)
        self.tester = tester or SandboxTester()
        self._settings = settings
        self._brain_fabric = brain_fabric
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._load_registered_tools()

    def select_background_brain(self, role: Any = None):
        """Request a cheap/local background route under BrainPolicy."""
        if self._brain_fabric is None:
            return None
        from core.brain import BrainRequest, BrainRole, PrivacyClass
        selected_role = role or BrainRole.CODER
        return self._brain_fabric.select_route(BrainRequest(
            user_request="Shadow background capability preparation",
            role=selected_role, privacy=PrivacyClass.LOCAL_ONLY,
            background=True,
        ))

    def attach_brain_fabric(self, brain_fabric: Any) -> None:
        self._brain_fabric = brain_fabric

    def observe_command(self, text: str, *, outcome: str) -> None:
        if self.enabled:
            self.watcher.record_command(text, outcome=outcome)

    def observe_manual_workaround(self, text: str) -> None:
        """Accepts a workaround only when another authorized component links it."""
        if self.enabled:
            self.watcher.record_manual_workaround(text)

    def observe_screen(self, *, active_window: str, permission: bool, ocr_summary: str = "") -> bool:
        if not self.enabled:
            return False
        return self.watcher.record_screen_context(active_window=active_window, permission=permission,
                                                  ocr_summary=ocr_summary)

    def enqueue_ranked_learning(self, item_id: str, *, reason: str,
                                user_pain: float, frequency: float,
                                time_saved: float, reuse_probability: float,
                                risk: float, learning_cost: float):
        """Place evidence-backed learning into the shared Shadow backlog."""
        return self.backlog.add_ranked(
            item_id, reason=reason, user_pain=user_pain, frequency=frequency,
            time_saved=time_saved, reuse_probability=reuse_probability,
            risk=risk, learning_cost=learning_cost,
        )

    def record_capability_quality(self, capability_id: str, *, verified: bool,
                                  duration: float, expected_duration: float,
                                  repairs: int = 0, fallbacks: int = 0):
        return self.quality.record(
            capability_id, verified=verified, duration=duration,
            expected_duration=expected_duration, repairs=repairs,
            fallbacks=fallbacks,
        )

    def start(self, interval_sec: int = 300) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, args=(max(30, interval_sec),),
                                        daemon=False, name="ShadowEngine")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return bool(self._thread is None or not self._thread.is_alive())

    @property
    def stopped(self) -> bool:
        return not bool(self._thread and self._thread.is_alive())

    def tick(self) -> list[ToolPreparation]:
        if not self.enabled or not bool(getattr(getattr(self._settings, "shadow", None), "auto_generate", True)):
            return []
        prepared: list[ToolPreparation] = []
        for pattern in self.watcher.analyze():
            if pattern.frequency <= 2 or pattern.confidence <= 0.8 or self.registry.get(pattern.suggested_tool):
                continue
            try:
                prepared.append(self._generate_and_prepare(
                    name=pattern.suggested_tool, description=pattern.description,
                    confidence=pattern.confidence, params={}, test_params={},
                ))
            except Exception:
                # Shadow mode stays silent. The unfulfilled pattern remains for
                # a later pass when the local coder model is available.
                continue
        return prepared

    def prepare_on_demand(self, goal: str) -> Optional[ToolPreparation]:
        """Try a new low-risk request immediately, using only the local coder.

        The caller still applies the normal JARVIS risk gate before execution.
        If Qwen or validation is unavailable this returns ``None`` quietly,
        leaving Active mode responsive.
        """
        if not self.enabled or not (goal or "").strip():
            return None
        name = self._safe_name("shadow_" + "_".join(re.findall(r"[a-zA-Z0-9]+", goal)[:4]))
        if self.registry.get(name) is not None:
            return None
        try:
            return self._generate_and_prepare(
                name=name, description=goal, confidence=0.81,
                params={"request": "string"}, test_params={"request": "test"},
            )
        except Exception:
            return None

    def _generate_and_prepare(self, *, name: str, description: str, confidence: float,
                              params: dict[str, Any], test_params: dict[str, Any]) -> ToolPreparation:
        """Regenerates at most three times after a functional-only failure."""
        latest: Optional[ToolPreparation] = None
        for _ in range(3):
            source = self.generator.generate(
                name=name, description=description, confidence=confidence, params=params,
                expected_output="user-friendly result", examples=self._existing_tool_examples(),
            )
            latest = self.prepare_tool(name=name, description=description, source=source,
                                       test_params=test_params)
            if latest.status != "rejected" or not latest.report.safety.passed or latest.report.functional.passed:
                return latest
        assert latest is not None
        return latest

    def prepare_tool(self, *, name: str, description: str, source: str,
                     test_params: dict[str, Any]) -> ToolPreparation:
        safe_name = self._safe_name(name)
        if "def run(params: dict)" not in source:
            source = ToolGenerator.wrap(name=safe_name, description=description,
                                        confidence=1.0, body=source)
        report = self.tester.test_source(source, test_params)
        if report.security_decision is SecurityDecision.BLOCKED:
            self._save_rejected(safe_name, source, report)
            return ToolPreparation(safe_name, "rejected", report.quality_score, report)
        if report.registration_allowed or (report.security_decision is SecurityDecision.SAFE_TO_EVALUATE and report.quality_score >= 90):
            path = self._register(safe_name, description, source)
            return ToolPreparation(safe_name, "registered", report.quality_score, report, path)
        if report.quality_score >= 70:
            self._save_candidate(safe_name, source, report)
            return ToolPreparation(safe_name, "proposed", report.quality_score, report)
        self._save_rejected(safe_name, source, report)
        return ToolPreparation(safe_name, "rejected", report.quality_score, report)

    def suggestion_for(self, goal: str, *, now: Optional[datetime] = None) -> Optional[str]:
        """One quiet, cooldown-protected suggestion for a relevant new tool."""
        goal_l = (goal or "").lower()
        for tool in self.registry.list_tools():
            if not getattr(tool, "generated_by_shadow", False):
                continue
            tokens = set(re.findall(r"[\w]+", (tool.name + " " + tool.description).lower()))
            if tokens and any(token in goal_l for token in tokens if len(token) > 3):
                if self._cooldown_allows(tool.name, now=now):
                    return f"Кстати, я подготовил «{tool.name}». Хочешь попробуешь?"
        return None

    def _loop(self, interval_sec: int) -> None:
        while not self._stop.wait(interval_sec):
            self.tick()

    def _register(self, name: str, description: str, source: str) -> Path:
        path = self.tools_dir / f"{name}.py"
        from core.security.atomic import atomic_write_text
        atomic_write_text(path, source)
        if self.registry.get(name) is None:
            tool = GeneratedShadowTool(name, description, path)
            self.registry.register(tool)
            self._register_capability(tool)
        self._write_metadata(name, description, path)
        return path

    def _load_registered_tools(self) -> None:
        for path in self.tools_dir.glob("*.py"):
            name = self._safe_name(path.stem)
            if self.registry.get(name) is None:
                tool = GeneratedShadowTool(name, f"Shadow-generated tool: {name}", path)
                self.registry.register(tool)
                self._register_capability(tool)

    @staticmethod
    def _register_capability(tool: Tool) -> None:
        try:
            from core.capabilities import CAPABILITIES, Capability
            CAPABILITIES.register(Capability.from_tool(tool, tags=tool.name.split("_")))
        except Exception:
            pass

    def _existing_tool_examples(self) -> str:
        examples = []
        for tool in self.registry.list_tools()[:5]:
            examples.append(f"{tool.name}: {tool.description}")
        return "\n".join(examples)

    def _save_candidate(self, name: str, source: str, report: SandboxReport) -> None:
        path = self.root / "candidates"
        path.mkdir(exist_ok=True)
        from core.security.atomic import atomic_write_text
        atomic_write_text(path / f"{name}.py", source)
        self._write_json(path / f"{name}.json", {"confidence": report.confidence, "status": "proposed"})

    def _save_rejected(self, name: str, source: str, report: SandboxReport) -> None:
        path = self.root / "rejected"
        path.mkdir(exist_ok=True)
        from core.security.atomic import atomic_write_text
        atomic_write_text(path / f"{name}.py", source)
        self._write_json(path / f"{name}.json", {"confidence": report.confidence, "status": "rejected"})
        self.backlog.add(name, priority=0.8, reason="generated capability failed validation")

    def _write_metadata(self, name: str, description: str, path: Path) -> None:
        manifest = self.root / "tools.json"
        try:
            data = load_json(manifest, default={})
        except (OSError, ValueError):
            data = {"tools": []}
        tools = [item for item in data.get("tools", []) if item.get("name") != name]
        tools.append({"name": name, "description": description, "path": str(path),
                      "generated_by_shadow": True})
        self._write_json(manifest, {"tools": tools})

    def _cooldown_allows(self, name: str, *, now: Optional[datetime]) -> bool:
        path = self.root / "notification_cooldowns.json"
        current = now or datetime.now(timezone.utc)
        try:
            data = load_json(path, default={})
            then = datetime.fromisoformat(data.get(name, ""))
            if current - then < timedelta(days=7):
                return False
        except (OSError, ValueError, TypeError):
            data = {}
        data[name] = current.isoformat()
        self._write_json(path, data)
        return True

    @staticmethod
    def _safe_name(name: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_").lower()
        return clean[:64] or "shadow_tool"

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        atomic_json_write(path, payload)


def active_mode_message(goal: str) -> str:
    """Fallback wording for a novel request; never frames learning as inability."""
    risk = assess_risk(goal)
    if risk.needs_confirmation:
        return "Сейчас разберусь. Перед опасной операцией потребуется ваше подтверждение."
    return "Сейчас разберусь. Подготовлю подход и проверю результат."
