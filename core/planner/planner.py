"""Executive Planner — decomposes user goals into StructuredPlan.

Design decisions (documented per brief):
- Uses DeepSeek reasoning model for decomposition; falls back to deterministic
  patterns for known goal shapes (install, sort-desktop, prepare-lesson).
- Asks the user ONCE about execution mode (autonomous / together / escort)
  and stores the choice in plan.metadata["mode_confirmed"].
- Does NOT refactor agent.py / orchestrator.py — integrates at handle_input
  delegation level only.
- No new abstract base classes.  Concrete first.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from core.utils.logger import get_logger
from .models import (
    ExecutionMode,
    PlanEvent,
    PlanEventKind,
    PlanRequest,
    PlanStep,
    PlanStatus,
    StepStatus,
    StructuredPlan,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Deterministic patterns for common goal shapes
# ---------------------------------------------------------------------------

def _install_and_setup_pattern(goal: str, app_name: str) -> List[PlanStep]:
    """Deterministic decomposition for 'install X and set it up'."""
    return [
        PlanStep(
            intent=f"Проверить, установлен ли {app_name}",
            tool_name="system_status",
            args_template={"query": f"is {app_name} installed"},
            preconditions=[],
            verification=[f"process_running:{app_name.lower()}"],
            narration=f"Сначала проверю, есть ли уже {app_name}.",
        ),
        PlanStep(
            intent=f"Установить {app_name} через winget",
            tool_name="__skill_install__",
            args_template={"app": app_name, "source": "winget"},
            preconditions=["internet_available"],
            verification=[f"file_exists:C:/Program Files/{app_name}"],
            on_failure="ask",
            narration=f"Ставлю {app_name}. Займёт минуту.",
        ),
        PlanStep(
            intent=f"Запустить {app_name} и проверить",
            tool_name="open_app",
            args_template={"app_name": app_name},
            preconditions=[],
            verification=[f"process_running:{app_name.lower()}"],
            narration=f"Запускаю {app_name}, проверяю что открылся.",
        ),
    ]


def _sort_desktop_pattern() -> List[PlanStep]:
    return [
        PlanStep(
            intent="Получить список файлов на рабочем столе",
            tool_name="list_files",
            args_template={"path": "~/Desktop"},
            narration="Смотрю, что лежит на рабочем столе.",
        ),
        PlanStep(
            intent="Создать папки по типам файлов",
            tool_name="__skill_organize_desktop__",
            args_template={},
            narration="Раскладываю по папкам: документы, картинки, видео, прочее.",
        ),
        PlanStep(
            intent="Проверить результат",
            tool_name="list_files",
            args_template={"path": "~/Desktop"},
            verification=["desktop_organized"],
            narration="Готово. Рабочий стол разобран.",
        ),
    ]


def _prepare_lesson_pattern(time_str: str) -> List[PlanStep]:
    return [
        PlanStep(
            intent="Проверить интернет-соединение",
            tool_name="system_status",
            args_template={"query": "internet"},
            narration="Проверяю интернет.",
        ),
        PlanStep(
            intent="Проверить браузер и камеру",
            tool_name="system_status",
            args_template={"query": "browser camera microphone"},
            narration="Проверяю браузер, камеру и микрофон.",
        ),
        PlanStep(
            intent="Поставить напоминание на урок",
            tool_name="add_reminder",
            args_template={"text": "Онлайн-урок", "time": time_str},
            narration=f"Ставлю напоминание на {time_str}.",
        ),
        PlanStep(
            intent="Закрыть лишние приложения",
            tool_name="__skill_close_distractions__",
            args_template={},
            requires_user=True,
            narration="Закрою лишние вкладки и приложения — скажи, если что-то нужно оставить.",
        ),
    ]


# ---------------------------------------------------------------------------
# Known pattern registry
# ---------------------------------------------------------------------------

_INSTALL_RE = re.compile(
    r"(?i)(установи|поставь|install)\s+(.+?)(?:\s+и\s+|\s+and\s+|$)", re.UNICODE
)
_SORT_DESKTOP_RE = re.compile(
    r"(?i)(разбери|разберись|убери|почисти|наведи порядок).*(рабочий стол|desktop)", re.UNICODE
)
_PREPARE_LESSON_RE = re.compile(
    r"(?i)(подготовь|подготовиться|готов).*(урок|занятие|lesson|онлайн)", re.UNICODE
)
_TIME_RE = re.compile(r"(\d{1,2}[:\s]\d{2}|\d{1,2}\s*(?:час|утра|вечера|am|pm))", re.UNICODE | re.IGNORECASE)


def _try_deterministic(goal: str) -> Optional[List[PlanStep]]:
    """Return steps if goal matches a known pattern, else None."""
    m = _INSTALL_RE.search(goal)
    if m:
        app = m.group(2).strip().split()[0].capitalize()
        return _install_and_setup_pattern(goal, app)

    if _SORT_DESKTOP_RE.search(goal):
        return _sort_desktop_pattern()

    if _PREPARE_LESSON_RE.search(goal):
        t = _TIME_RE.search(goal)
        time_str = t.group(0) if t else "10:00"
        return _prepare_lesson_pattern(time_str)

    return None


# ---------------------------------------------------------------------------
# LLM-based decomposition
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = """Ты — планировщик задач Jarvis. Разложи цель пользователя на минимальный список шагов.
Каждый шаг — JSON-объект:
{
  "intent": "что делаем (по-русски)",
  "tool_name": "имя инструмента или null",
  "args_template": {},
  "preconditions": [],
  "verification": [],
  "on_failure": "ask",
  "requires_user": false,
  "estimated_seconds": null,
  "narration": "короткая фраза для голоса (по-русски)"
}

Доступные инструменты: {tools}

Верни ТОЛЬКО JSON-массив шагов, без пояснений.
"""


def _parse_llm_steps(raw: str) -> List[PlanStep]:
    """Parse LLM JSON output into PlanStep list."""
    # Strip markdown fences
    text = re.sub(r"```(?:json)?", "", raw).strip()
    # Find first [ ... ]
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
        return [PlanStep.from_dict(item) for item in items if isinstance(item, dict)]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("Planner: failed to parse LLM steps: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class Planner:
    """Decomposes goals into StructuredPlan objects.

    Args:
        llm_fn: async-or-sync callable(prompt, system) -> str.
                If None, only deterministic patterns are used.
        capabilities: list of available tool names (for LLM context).
    """

    def __init__(
        self,
        llm_fn: Optional[Callable[[str, str], str]] = None,
        capabilities: Optional[List[str]] = None,
        world_state: Optional[Any] = None,
    ) -> None:
        self._llm = llm_fn
        self._capabilities = capabilities or []
        self._world = world_state

    def set_world_state(self, world_state: Any) -> None:
        self._world = world_state

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, request: PlanRequest) -> StructuredPlan:
        """Decompose a goal into a StructuredPlan.

        Tries deterministic patterns first; falls back to LLM decomposition.
        """
        plan = StructuredPlan(
            goal=request.goal,
            profile_id=request.profile_id,
            mode=request.mode,
            status=PlanStatus.DRAFT,
            metadata={
                "world_snapshot": request.world_snapshot,
                "grants": request.grants,
            },
        )

        # 1. Deterministic patterns
        steps = _try_deterministic(request.goal)

        # 2. LLM decomposition
        if steps is None and self._llm is not None:
            steps = self._llm_decompose(request.goal, request.capabilities or self._capabilities)

        # 3. Fallback: single-step plan
        if not steps:
            steps = [PlanStep(
                intent=request.goal,
                tool_name=None,
                narration=f"Выполняю: {request.goal}",
            )]

        plan.steps = steps
        plan.status = PlanStatus.ACTIVE
        plan.touch()
        log.info("Planner: created plan %s with %d steps for goal: %s",
                 plan.plan_id, len(steps), request.goal[:80])
        return plan

    def ask_mode_question(self, goal: str) -> str:
        """Return the one-time mode question for this goal."""
        short = goal[:60].rstrip()
        return f"«{short}» — сделать самому или вместе настроим?"

    def replan(self, plan: StructuredPlan, reason: str) -> StructuredPlan:
        """Create a new plan for the same goal after a failure."""
        log.info("Planner: replanning %s — reason: %s", plan.plan_id, reason)
        request = PlanRequest(
            goal=plan.goal,
            profile_id=plan.profile_id,
            mode=plan.mode,
            world_snapshot=plan.metadata.get("world_snapshot", {}),
            grants=plan.metadata.get("grants", []),
        )
        new_plan = self.plan(request)
        new_plan.metadata["replanned_from"] = plan.plan_id
        new_plan.metadata["replan_reason"] = reason
        return new_plan

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _llm_decompose(self, goal: str, capabilities: List[str]) -> List[PlanStep]:
        """Call LLM to decompose goal into steps."""
        if self._llm is None:
            return []
        tools_str = ", ".join(capabilities[:40]) if capabilities else "open_app, web_search, write_file"
        system = _DECOMPOSE_SYSTEM.format(tools=tools_str)
        try:
            raw = self._llm(goal, system)
            steps = _parse_llm_steps(raw)
            if steps:
                log.info("Planner: LLM produced %d steps", len(steps))
            return steps
        except Exception as exc:
            log.warning("Planner: LLM decomposition failed: %s", exc)
            return []
