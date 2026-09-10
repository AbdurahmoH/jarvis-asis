"""Plan Executor — drives a StructuredPlan step by step.

Yields PlanEvent objects so callers (Orchestrator, WS server) can stream
progress to the UI and TTS queue.

Key design choices:
- Cooperative: checks plan.status after each step so pause/cancel works.
- Mode-aware: AUTONOMOUS = silent; TOGETHER = narrate + ask; ESCORT = guide.
- Repair loop: on step failure, tries repair_fn once, then asks user.
- World re-check: before each step, verifies preconditions via WorldCheck.
- Durable: plan state is mutated in-place; caller persists via TaskRuntime.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Generator, Optional

from core.utils.logger import get_logger
from .models import (
    ExecutionMode,
    PlanEvent,
    PlanEventKind,
    PlanStatus,
    PlanStep,
    StepStatus,
    StructuredPlan,
)
from .world_check import WorldCheck

log = get_logger(__name__)

# Callable types
ToolRunner   = Callable[[str, dict], str]          # (tool_name, args) -> result
RepairFn     = Callable[[str, str], Optional[str]] # (step_intent, error) -> fix or None
ConfirmFn    = Callable[[str, list], str]           # (question, options) -> chosen option
NarrateFn    = Callable[[str], None]               # (text) -> speak it


class PlanExecutor:
    """Executes a StructuredPlan, yielding PlanEvent objects.

    Args:
        tool_runner:  Calls an existing tool by name.
        repair_fn:    Attempts to repair a failed step (optional).
        confirm_fn:   Asks the user a question and returns their answer.
        narrate_fn:   Speaks a line via TTS (optional).
        world_check:  WorldCheck instance for precondition evaluation.
        cancel_event: threading.Event; set to pause/cancel execution.
    """

    def __init__(
        self,
        tool_runner:  Optional[ToolRunner]  = None,
        repair_fn:    Optional[RepairFn]    = None,
        confirm_fn:   Optional[ConfirmFn]   = None,
        narrate_fn:   Optional[NarrateFn]   = None,
        world_check:  Optional[WorldCheck]  = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self._run_tool   = tool_runner
        self._repair     = repair_fn
        self._confirm    = confirm_fn
        self._narrate    = narrate_fn
        self._wcheck     = world_check or WorldCheck()
        self._cancel     = cancel_event or threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        plan: StructuredPlan,
        mode: Optional[ExecutionMode] = None,
    ) -> Generator[PlanEvent, None, None]:
        """Drive the plan to completion, yielding events.

        Caller should iterate this generator in a background thread and
        forward events to the WS bus.
        """
        effective_mode = mode or plan.mode
        plan.status = PlanStatus.ACTIVE
        plan.touch()

        yield PlanEvent(
            plan_id=plan.plan_id,
            kind=PlanEventKind.PLAN_CREATED,
            payload={"goal": plan.goal, "mode": effective_mode.value,
                     "steps": len(plan._flat_steps())},
        )

        flat = plan._flat_steps()
        total = len(flat)

        for idx, step in enumerate(flat):
            if self._cancel.is_set():
                plan.status = PlanStatus.PAUSED
                plan.touch()
                yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.PLAN_PAUSED,
                                step_id=step.step_id, payload={"reason": "cancelled"})
                return

            if plan.status in (PlanStatus.PAUSED, PlanStatus.CANCELLED):
                return

            if step.status in (StepStatus.DONE, StepStatus.SKIPPED):
                continue  # resume after restart

            plan.current_step_index = idx

            # --- Precondition check ---
            ok, failed = self._wcheck.check_preconditions(
                step.preconditions, plan.metadata.get("world_snapshot", {})
            )
            if not ok:
                log.warning("Executor: preconditions failed for step %s: %s",
                            step.step_id, failed)
                if effective_mode != ExecutionMode.AUTONOMOUS:
                    yield from self._handle_precondition_failure(plan, step, failed)
                    if step.status == StepStatus.SKIPPED:
                        continue
                    if step.status == StepStatus.FAILED:
                        break

            # --- Narrate (TOGETHER / ESCORT) ---
            if effective_mode != ExecutionMode.AUTONOMOUS and step.narration:
                self._speak(step.narration)
                yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.VOICE_STATUS,
                                step_id=step.step_id, payload={"text": step.narration})

            # --- Wait for user (ESCORT or requires_user) ---
            if step.requires_user or effective_mode == ExecutionMode.ESCORT:
                yield from self._wait_for_user(plan, step, effective_mode)
                if step.status == StepStatus.SKIPPED:
                    continue

            # --- Execute step ---
            step.status = StepStatus.RUNNING
            plan.touch()
            yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_STARTED,
                            step_id=step.step_id,
                            payload={"intent": step.intent, "tool": step.tool_name,
                                     "progress": idx / max(total, 1)})

            result, error = self._run_step(step, plan)

            if error:
                yield from self._handle_step_failure(plan, step, error, effective_mode)
                if step.status == StepStatus.FAILED:
                    plan.status = PlanStatus.FAILED
                    plan.touch()
                    yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.PLAN_FAILED,
                                    step_id=step.step_id, payload={"error": error})
                    return
            else:
                step.status = StepStatus.DONE
                step.result = result
                plan.touch()
                yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_DONE,
                                step_id=step.step_id,
                                payload={"result": result,
                                         "progress": (idx + 1) / max(total, 1)})

        # --- Plan complete ---
        plan.status = PlanStatus.COMPLETED
        plan.touch()
        summary = self._completion_summary(plan)
        if effective_mode != ExecutionMode.AUTONOMOUS:
            self._speak(summary)
        yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.PLAN_COMPLETED,
                        payload={"summary": summary, "progress": 1.0})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_step(self, step: PlanStep, plan: StructuredPlan) -> tuple[Optional[str], Optional[str]]:
        """Execute a single step.  Returns (result, error)."""
        if step.tool_name is None:
            # No tool — treat as a user-action placeholder
            return "acknowledged", None

        if self._run_tool is None:
            return None, f"no tool runner configured for '{step.tool_name}'"

        # Skill stubs (prefixed __skill_*__) are handled by SkillDiscovery
        # at the orchestrator level; here we just call through.
        try:
            result = self._run_tool(step.tool_name, step.args_template)
            return str(result) if result is not None else "ok", None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def _handle_step_failure(
        self,
        plan: StructuredPlan,
        step: PlanStep,
        error: str,
        mode: ExecutionMode,
    ) -> Generator[PlanEvent, None, None]:
        """Attempt repair, then ask user."""
        step.error = error
        log.warning("Executor: step %s failed: %s", step.step_id, error)

        # 1. Try repair
        if self._repair is not None:
            fix = self._repair(step.intent, error)
            if fix:
                step.args_template["_repair_hint"] = fix
                result, err2 = self._run_step(step, plan)
                if not err2:
                    step.status = StepStatus.DONE
                    step.result = result
                    plan.touch()
                    yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_DONE,
                                    step_id=step.step_id,
                                    payload={"result": result, "repaired": True})
                    return

        # 2. Ask user
        if mode != ExecutionMode.AUTONOMOUS and self._confirm is not None:
            question = (
                f"Шаг «{step.intent}» не удался: {error[:120]}. "
                "Что делаем?"
            )
            choice = self._confirm(question, ["Повторить", "Пропустить", "Отменить план"])
            if choice and "повтор" in choice.lower():
                result, err3 = self._run_step(step, plan)
                if not err3:
                    step.status = StepStatus.DONE
                    step.result = result
                    plan.touch()
                    yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_DONE,
                                    step_id=step.step_id, payload={"result": result})
                    return
                step.error = err3
            elif choice and "пропуст" in choice.lower():
                step.status = StepStatus.SKIPPED
                plan.touch()
                yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_SKIPPED,
                                step_id=step.step_id, payload={"reason": "user skipped"})
                return
            # else: abort — fall through to FAILED

        step.status = StepStatus.FAILED
        plan.touch()
        yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_FAILED,
                        step_id=step.step_id, payload={"error": error})

    def _handle_precondition_failure(
        self,
        plan: StructuredPlan,
        step: PlanStep,
        failed: list,
    ) -> Generator[PlanEvent, None, None]:
        if self._confirm is None:
            step.status = StepStatus.SKIPPED
            yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_SKIPPED,
                            step_id=step.step_id,
                            payload={"reason": f"preconditions not met: {failed}"})
            return
        question = (
            f"Условие для шага «{step.intent}» не выполнено: {failed}. "
            "Продолжить всё равно?"
        )
        choice = self._confirm(question, ["Продолжить", "Пропустить", "Отменить"])
        if choice and "продолж" in choice.lower():
            return  # proceed
        elif choice and "пропуст" in choice.lower():
            step.status = StepStatus.SKIPPED
            yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_SKIPPED,
                            step_id=step.step_id, payload={"reason": "user skipped"})
        else:
            step.status = StepStatus.FAILED
            yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.STEP_FAILED,
                            step_id=step.step_id,
                            payload={"error": f"preconditions not met: {failed}"})

    def _wait_for_user(
        self,
        plan: StructuredPlan,
        step: PlanStep,
        mode: ExecutionMode,
    ) -> Generator[PlanEvent, None, None]:
        """Pause and wait for user to complete an action."""
        plan.status = PlanStatus.PAUSED
        plan.touch()
        prompt = step.narration or f"Выполни шаг: {step.intent}"
        yield PlanEvent(plan_id=plan.plan_id, kind=PlanEventKind.WAITING_USER,
                        step_id=step.step_id, payload={"prompt": prompt})

        if self._confirm is not None:
            self._confirm(prompt + " — скажи «готово» когда сделаешь.", ["Готово", "Пропустить"])

        plan.status = PlanStatus.ACTIVE
        plan.touch()

    def _speak(self, text: str) -> None:
        if self._narrate is not None:
            try:
                self._narrate(text)
            except Exception as exc:
                log.debug("Executor: narrate failed: %s", exc)

    @staticmethod
    def _completion_summary(plan: StructuredPlan) -> str:
        flat = plan._flat_steps()
        done = sum(1 for s in flat if s.status == StepStatus.DONE)
        skipped = sum(1 for s in flat if s.status == StepStatus.SKIPPED)
        parts = [f"Готово."]
        if done:
            parts.append(f"Выполнено шагов: {done}.")
        if skipped:
            parts.append(f"Пропущено: {skipped}.")
        return " ".join(parts)
