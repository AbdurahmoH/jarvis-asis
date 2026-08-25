"""Task / Mission Runtime — сердце асинхронной жизни J.A.R.V.I.S.

Один из центральных модулей J.A.R.V.I.S. 3.0 (см. ТЗ §3, §4, §30, §33, §35, §36).

Главная идея ТЗ:
    USER REQUEST  ->  БЫСТРОЕ ПОДТВЕРЖДЕНИЕ (ACK)
                 ->  ДОЛГАЯ ЗАДАЧА ПРОДОЛЖАЕТСЯ АСИНХРОННО
                 ->  progress / activity updates
                 ->  result
                 ->  verification

Задача (``Mission``) живёт ОТДЕЛЬНО от HTTP/UI request lifecycle. Пользователь
не обязан ждать, пока один synchronous function call закончится.

КРИТИЧЕСКОЕ ПРАВИЛО (§33 — НЕТ ИСКУССТВЕННОГО "3-SECOND THINKING LIMIT"):
    В этом рантайме НЕТ и НЕ БУДЕТ политики
        if reasoning_time > N:  fail_task()
    Единственные ограничения по времени — РЕАЛЬНЫЕ:
        * network timeout        (инструмент не отвечает в сеть)
        * tool / process timeout (внешняя программа зависла)
        * явная cancellation     (пользователь отменил)
        * опциональный mission watchdog (реальный потолок на ВСЮ задачу,
          задаваемый явно, по умолчанию — НЕТ, то есть безлимит).
    "Долго выполняется" != "невозможно выполнить".

Lifecycle (§4):
    queued -> acknowledging -> analyzing -> planning -> executing
           -> verifying -> repairing -> completed
    а также: paused / cancelled / failed

События (§35, §36) — типизированные, чтобы будущий UI мог подписаться:
    task_started, plan_ready, step_started, step_completed, tool_called,
    tool_result, stream_chunk, stream_end, confirmation_required, error,
    task_completed, task_progress.
"""

from __future__ import annotations

import collections
import heapq
import itertools
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.utils.logger import get_logger
from core.security.atomic import atomic_json_write, load_json
from core.security.redaction import redact

__all__ = [
    "MissionStatus",
    "TriggerKind",
    "MissedTriggerPolicy",
    "MissionTrigger",
    "TaskEvent",
    "EventBus",
    "Mission",
    "TaskRuntime",
    "new_mission_id",
    "ALL_EVENT_TYPES",
    "EVENT_TASK_STARTED",
    "EVENT_ACKNOWLEDGED",
    "EVENT_PLAN_READY",
    "EVENT_STEP_STARTED",
    "EVENT_STEP_COMPLETED",
    "EVENT_TOOL_CALLED",
    "EVENT_TOOL_RESULT",
    "EVENT_VERIFICATION",
    "EVENT_REPAIR_STARTED",
    "EVENT_REPAIR_COMPLETED",
    "EVENT_DELEGATED",
    "EVENT_STREAM_CHUNK",
    "EVENT_STREAM_END",
    "EVENT_CONFIRMATION_REQUIRED",
    "EVENT_ERROR",
    "EVENT_TASK_COMPLETED",
    "EVENT_TASK_FAILED",
    "EVENT_TASK_PROGRESS",
    "EVENT_TRIGGERED",
]

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
#  Жизненный цикл задачи
# --------------------------------------------------------------------------- #

class MissionStatus(str, Enum):
    """Состояния жизненного цикла миссии (§4)."""

    PENDING = "pending"
    WAITING = "waiting"
    TRIGGERED = "triggered"
    QUEUED = "queued"
    ACKNOWLEDGING = "acknowledging"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REPAIRING = "repairing"
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (
            MissionStatus.COMPLETED, MissionStatus.CANCELLED,
            MissionStatus.FAILED, MissionStatus.EXPIRED,
        )

    @property
    def is_active(self) -> bool:
        return not self.is_terminal and self != MissionStatus.PAUSED


class TriggerKind(str, Enum):
    TIME = "time"
    WORLD = "world"
    MANUAL = "manual"
    EVENT = "event"


class MissedTriggerPolicy(str, Enum):
    NOTIFY_LATE = "notify_late"
    EXECUTE_IF_FRESH = "execute_if_fresh"
    SKIP = "skip"


@dataclass(frozen=True)
class MissionTrigger:
    """Serializable trigger contract; contains data, never executable code."""

    kind: TriggerKind
    trigger_id: str = field(default_factory=lambda: f"trigger-{uuid.uuid4().hex}")
    due_at: Optional[str] = None
    domain: str = ""
    path: str = ""
    operator: str = "equals"
    expected: Any = None
    observation_options: Dict[str, Any] = field(default_factory=dict)
    poll_interval_sec: float = 5.0
    event_type: str = ""
    missed_policy: MissedTriggerPolicy = MissedTriggerPolicy.EXECUTE_IF_FRESH
    max_lateness_sec: float = 60.0

    @classmethod
    def at(
        cls, due_at: datetime | str, *,
        missed_policy: MissedTriggerPolicy | str = MissedTriggerPolicy.EXECUTE_IF_FRESH,
        max_lateness_sec: float = 60.0,
    ) -> "MissionTrigger":
        return cls(
            kind=TriggerKind.TIME, due_at=_datetime_iso(due_at),
            missed_policy=MissedTriggerPolicy(missed_policy),
            max_lateness_sec=max(0.0, float(max_lateness_sec)),
        )

    @classmethod
    def condition(
        cls, domain: str, path: str, operator: str, expected: Any,
        *, poll_interval_sec: float = 5.0,
        observation_options: Optional[Dict[str, Any]] = None,
    ) -> "MissionTrigger":
        if not domain.strip() or not path.strip():
            raise ValueError("world trigger requires domain and path")
        if operator not in {
            "equals", "not_equals", "exists", "missing", "lt", "lte", "gt", "gte",
            "contains", "any_match", "none_match",
        }:
            raise ValueError(f"unsupported world operator: {operator}")
        return cls(
            kind=TriggerKind.WORLD, domain=domain.casefold().strip(), path=path.strip(),
            operator=operator, expected=expected,
            poll_interval_sec=max(0.1, float(poll_interval_sec)),
            observation_options=dict(observation_options or {}),
        )

    @classmethod
    def manual(cls) -> "MissionTrigger":
        return cls(kind=TriggerKind.MANUAL)

    @classmethod
    def event(cls, event_type: str) -> "MissionTrigger":
        if not event_type.strip():
            raise ValueError("event trigger requires event_type")
        return cls(kind=TriggerKind.EVENT, event_type=event_type.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value, "trigger_id": self.trigger_id,
            "due_at": self.due_at, "domain": self.domain, "path": self.path,
            "operator": self.operator, "expected": self.expected,
            "observation_options": dict(self.observation_options),
            "poll_interval_sec": self.poll_interval_sec, "event_type": self.event_type,
            "missed_policy": self.missed_policy.value,
            "max_lateness_sec": self.max_lateness_sec,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MissionTrigger":
        if not isinstance(raw, Mapping):
            raise TypeError("trigger must be a mapping")
        trigger = cls(
            kind=TriggerKind(str(raw.get("kind") or "")),
            trigger_id=str(raw.get("trigger_id") or f"trigger-{uuid.uuid4().hex}"),
            due_at=str(raw["due_at"]) if raw.get("due_at") else None,
            domain=str(raw.get("domain") or ""), path=str(raw.get("path") or ""),
            operator=str(raw.get("operator") or "equals"), expected=raw.get("expected"),
            observation_options=dict(raw.get("observation_options") or {}),
            poll_interval_sec=max(0.1, float(raw.get("poll_interval_sec", 5.0))),
            event_type=str(raw.get("event_type") or ""),
            missed_policy=MissedTriggerPolicy(str(raw.get("missed_policy") or "execute_if_fresh")),
            max_lateness_sec=max(0.0, float(raw.get("max_lateness_sec", 60.0))),
        )
        if trigger.kind is TriggerKind.TIME and _parse_datetime(trigger.due_at) is None:
            raise ValueError("time trigger requires valid due_at")
        if trigger.kind is TriggerKind.WORLD and (not trigger.domain or not trigger.path):
            raise ValueError("world trigger requires domain and path")
        if trigger.kind is TriggerKind.WORLD and trigger.operator not in {
            "equals", "not_equals", "exists", "missing", "lt", "lte", "gt", "gte",
            "contains", "any_match", "none_match",
        }:
            raise ValueError(f"unsupported world operator: {trigger.operator}")
        if trigger.kind is TriggerKind.EVENT and not trigger.event_type:
            raise ValueError("event trigger requires event_type")
        return trigger


# Типы событий (§23, §36)
EVENT_TASK_STARTED = "task_started"
EVENT_ACKNOWLEDGED = "acknowledged"
EVENT_PLAN_READY = "plan_ready"
EVENT_STEP_STARTED = "step_started"
EVENT_STEP_COMPLETED = "step_completed"
EVENT_TOOL_CALLED = "tool_called"
EVENT_TOOL_RESULT = "tool_result"
EVENT_VERIFICATION = "verification"
EVENT_REPAIR_STARTED = "repair_started"
EVENT_REPAIR_COMPLETED = "repair_completed"
EVENT_DELEGATED = "delegated"
EVENT_STREAM_CHUNK = "stream_chunk"
EVENT_STREAM_END = "stream_end"
EVENT_CONFIRMATION_REQUIRED = "confirmation_required"
EVENT_ERROR = "error"
EVENT_TASK_COMPLETED = "task_completed"
EVENT_TASK_FAILED = "task_failed"
EVENT_TASK_PROGRESS = "task_progress"
EVENT_TRIGGERED = "triggered"

#: Полный словарь событий задачи (§23) — для подписчиков и UI.
ALL_EVENT_TYPES: tuple[str, ...] = (
    EVENT_TASK_STARTED,
    EVENT_ACKNOWLEDGED,
    EVENT_PLAN_READY,
    EVENT_STEP_STARTED,
    EVENT_STEP_COMPLETED,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    EVENT_VERIFICATION,
    EVENT_REPAIR_STARTED,
    EVENT_REPAIR_COMPLETED,
    EVENT_DELEGATED,
    EVENT_STREAM_CHUNK,
    EVENT_STREAM_END,
    EVENT_CONFIRMATION_REQUIRED,
    EVENT_ERROR,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_PROGRESS,
    EVENT_TRIGGERED,
)


# --------------------------------------------------------------------------- #
#  События и шина событий
# --------------------------------------------------------------------------- #

@dataclass
class TaskEvent:
    """Одно наблюдаемое событие миссии (§35, §36)."""

    task_id: str
    event_type: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "phase": self.phase,
            "payload": self.payload,
        }


class EventBus:
    """Потокобезопасная шина событий для подписки UI / логов (§35, §36).

    Подписчики получают каждое опубликованное событие. Исключения внутри
    подписчиков НЕ должны ронять рантайм — они логируются и глотаются.
    """

    def __init__(self) -> None:
        self._subscribers: List[Callable[[TaskEvent], None]] = []
        self._lock = threading.RLock()

    def subscribe(self, callback: Callable[[TaskEvent], None]) -> Callable[[], None]:
        """Подписывает callback на все события. Возвращает unsubscribe()."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, event: TaskEvent) -> None:
        """Публикует событие всем подписчикам (copy-on-read для безопасности)."""
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception as exc:  # подписчик не должен ронять рантайм
                log.debug("EventBus subscriber упал на %s: %s", event.event_type, exc)


# --------------------------------------------------------------------------- #
#  Миссия
# --------------------------------------------------------------------------- #

@dataclass
class Mission:
    """Одна задача пользователя со своим жизненным циклом и историей событий (§6)."""

    task_id: str
    goal: str
    status: MissionStatus = MissionStatus.QUEUED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    result: Optional[str] = None
    error: Optional[str] = None
    plan: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    events: List[TaskEvent] = field(default_factory=list)

    # --- поля состояния задачи (§6) ---
    progress: float = 0.0                                  # 0.0 .. 1.0
    current_step: Optional[str] = None
    model_used: Optional[str] = None
    tools_used: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    verification: Optional[Dict[str, Any]] = None
    acknowledgement: Optional[str] = None
    trigger: Optional[Dict[str, Any]] = None
    context: Dict[str, Any] = field(default_factory=dict)
    latest_evidence: Dict[str, Any] = field(default_factory=dict)
    attempt_state: Dict[str, Any] = field(default_factory=dict)
    completion_criteria: Dict[str, Any] = field(default_factory=dict)
    expires_at: Optional[str] = None
    execution_id: Optional[str] = None
    trigger_id: Optional[str] = None
    executed_trigger_ids: List[str] = field(default_factory=list)

    # Внутреннее: события публикуются и в шину, и сохраняются здесь.
    _bus: Optional[EventBus] = field(default=None, repr=False, compare=False)
    _clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc), repr=False, compare=False,
    )

    def _touch(self) -> None:
        self.updated_at = self._clock().isoformat()

    @property
    def cancelled(self) -> bool:
        return self.status == MissionStatus.CANCELLED

    def set_status(self, status: MissionStatus, reason: Optional[str] = None) -> None:
        self.status = status
        self._touch()
        self.emit(EVENT_TASK_PROGRESS, phase=status.value,
                  payload={"status": status.value, "reason": reason, "progress": self.progress})

    def set_progress(self, progress: float, step: Optional[str] = None) -> None:
        """Обновляет прогресс задачи и публикует событие (§23)."""
        self.progress = max(0.0, min(1.0, float(progress)))
        if step:
            self.current_step = step
        self._touch()
        self.emit(EVENT_TASK_PROGRESS,
                  payload={"progress": self.progress, "current_step": self.current_step})

    def note_tool(self, tool_name: str) -> None:
        """Фиксирует использованный инструмент (§6)."""
        if tool_name and tool_name not in self.tools_used:
            self.tools_used.append(tool_name)

    def note_error(self, message: str) -> None:
        """Фиксирует ошибку, не прерывая задачу (§10 — ошибка не финал)."""
        if message:
            self.errors.append(message)
            self._touch()

    def add_step(self, description: str, tool: Optional[str] = None,
                 args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Добавляет шаг плана и публикует plan_ready при первом шаге."""
        step = {
            "index": len(self.plan),
            "description": description,
            "tool": tool,
            "args": args or {},
            "status": "pending",
        }
        self.plan.append(step)
        if len(self.plan) == 1:
            self.emit(EVENT_PLAN_READY, payload={"plan": self.plan})
        return step

    def emit(self, event_type: str, phase: Optional[str] = None,
             payload: Optional[Dict[str, Any]] = None) -> TaskEvent:
        event = TaskEvent(
            task_id=self.task_id,
            event_type=event_type,
            phase=phase or self.status.value,
            payload=payload or {},
        )
        self.events.append(event)
        self._touch()
        if self._bus is not None:
            self._bus.publish(event)
        return event

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress,
            "current_step": self.current_step,
            "model_used": self.model_used,
            "tools_used": list(self.tools_used),
            "result": self.result,
            "error": self.error,
            "errors": list(self.errors),
            "verification": self.verification,
            "acknowledgement": self.acknowledgement,
            "trigger": self.trigger,
            "context": self.context,
            "latest_evidence": self.latest_evidence,
            "attempt_state": self.attempt_state,
            "completion_criteria": self.completion_criteria,
            "expires_at": self.expires_at,
            "execution_id": self.execution_id,
            "trigger_id": self.trigger_id,
            "executed_trigger_ids": list(self.executed_trigger_ids),
            "plan": self.plan,
            "metadata": self.metadata,
            "events": [e.to_dict() for e in self.events],
        }


#: Счётчик задач в рамках процесса — для человекочитаемой нумерации (§6).
_MISSION_COUNTER = itertools.count(1)


def new_mission_id(now: Optional[datetime] = None) -> str:
    """Generates a restart-safe human-readable mission ID."""
    year = (now or datetime.now(timezone.utc)).year
    return f"JARVIS-{year}-{next(_MISSION_COUNTER):05d}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
#  Task Runtime
# --------------------------------------------------------------------------- #

# Функция, исполняющая миссию. Получает Mission (для публикации событий и
# установки статуса) и threading.Event отмены. Возвращает финальный результат
# (строку) или бросает исключение (рантайм ловит и помечает failed).
MissionRunner = Callable[[Mission, threading.Event], str]


class TaskRuntime:
    """Асинхронный рантайм миссий (§3, §4, §30, §33).

    Запускает ``MissionRunner`` в отдельном потоке, ведёт жизненный цикл,
    публикует события в ``EventBus`` и предоставляет управление:
    cancel / resume / wait / status / list.

    НИКАКИХ ограничений на "время размышления" модели. Единственный
    времени-ограничитель — опциональный ``default_watchdog_sec`` (реальный
    потолок на ВСЮ миссию, по умолчанию None = безлимит) + явная отмена.
    """

    def __init__(self, default_watchdog_sec: Optional[float] = None,
                 *, max_concurrent: int = 2,
                 persistence_dir: Optional[str | Path] = None,
                 durable_runner: Optional[MissionRunner] = None,
                 world_state: Any = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 scheduler_poll_sec: float = 30.0,
                 auto_start_scheduler: bool = False) -> None:
        """
        Args:
            default_watchdog_sec: реальный потолок на ВСЮ миссию (сек).
                None = безлимит (§33). Это НЕ latency-бюджет и НЕ "3 секунды".
            max_concurrent: HARD CAP на число одновременно исполняемых
                миссий (§1.3 П1). Третья и последующие миссии встают в
                очередь (QUEUED) и НЕ отбрасываются, НЕ падают.
            ВНИМАНИЕ: ``max_concurrent`` — ТОЛЬКО keyword-аргумент, чтобы
            не сломать существующие call-сайты, передающие позиционно
            ``default_watchdog_sec`` (например TaskRuntime(30)).
        """
        if max_concurrent < 1:
            raise ValueError("max_concurrent должен быть >= 1")
        self._bus = EventBus()
        self._missions: Dict[str, Mission] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._cancel: Dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._default_watchdog = default_watchdog_sec
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._durable_runner = durable_runner
        self._world_state = world_state
        self._runners: Dict[str, MissionRunner] = {}
        # --- HARD CAP параллельных миссий (§1.3) ---
        self._max_concurrent = max_concurrent
        self._active_count = 0
        # Очередь ожидающих миссий: (mission, runner, cancel, watchdog)
        self._queue: "collections.deque" = collections.deque()
        self._schedule_heap: List[tuple[float, int, str, str]] = []
        self._heap_counter = itertools.count()
        self._scheduler_poll_sec = max(0.05, float(scheduler_poll_sec))
        self._scheduler_stop = threading.Event()
        self._scheduler_condition = threading.Condition(self._lock)
        self._scheduler_thread: Optional[threading.Thread] = None
        self._scheduler_metrics = {
            "wakeups": 0, "evaluations": 0, "executions": 0,
            "world_observations": 0,
        }
        self._shutdown_preserve: set[str] = set()
        self._persistence_dir = Path(persistence_dir) if persistence_dir else None
        if self._persistence_dir is not None:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            self._restore_persisted()
            self._bus.subscribe(self._persist_event)
            self._rebuild_schedule()
        if auto_start_scheduler:
            self.start_scheduler()

    # ------------------------------------------------------------------ #
    #  Доступ к шине событий
    # ------------------------------------------------------------------ #
    @property
    def bus(self) -> EventBus:
        return self._bus

    def subscribe(self, callback: Callable[[TaskEvent], None]) -> Callable[[], None]:
        return self._bus.subscribe(callback)

    # ------------------------------------------------------------------ #
    #  Создание и запуск
    # ------------------------------------------------------------------ #
    def submit(self, goal: str, runner: MissionRunner,
               watchdog_sec: Optional[float] = None,
               metadata: Optional[Dict[str, Any]] = None) -> Mission:
        """Создаёт миссию и запускает её в фоновом потоке.

        НЕ блокирует вызывающий поток — сразу возвращает ``Mission``
        (статус queued/acknowledging), пока реальная работа идёт асинхронно.

        Args:
            goal: цель пользователя.
            runner: исполнитель миссии (получает Mission + cancel Event).
            watchdog_sec: реальный потолок на миссию; None = брать дефолт.
            metadata: произвольные метаданные (risk, intent, и т.п.).

        Returns:
            Mission — немедленно (до завершения работы).
        """
        now = self._clock().isoformat()
        mission = Mission(
            task_id=new_mission_id(self._clock()), goal=goal, status=MissionStatus.QUEUED,
            created_at=now, updated_at=now, _clock=self._clock,
        )
        mission._bus = self._bus
        mission.metadata = dict(metadata or {})
        cancel = threading.Event()

        watchdog = watchdog_sec if watchdog_sec is not None else self._default_watchdog

        with self._lock:
            self._missions[mission.task_id] = mission
            self._cancel[mission.task_id] = cancel
            self._runners[mission.task_id] = runner
            if self._active_count < self._max_concurrent:
                self._active_count += 1
                start_now = True
            else:
                self._queue.append((mission, runner, cancel, watchdog))
                start_now = False

        mission.emit(EVENT_TASK_STARTED, phase=MissionStatus.QUEUED.value,
                     payload={"goal": goal, "watchdog_sec": watchdog})

        if start_now:
            self._start_thread(mission, runner, cancel, watchdog)
        else:
            # HARD CAP достигнут: миссия ЖДЁТ в очереди (§1.3 П1) — НЕ падает.
            mission.set_status(MissionStatus.QUEUED, "ожидает свободного слота (HARD CAP)")
            log.info("TaskRuntime: HARD CAP=%d достигнут, миссия %s в очереди",
                     self._max_concurrent, mission.task_id)

        return mission

    def schedule(
        self, goal: str, trigger: MissionTrigger | Mapping[str, Any], *,
        runner: Optional[MissionRunner] = None,
        context: Optional[Dict[str, Any]] = None,
        completion_criteria: Optional[Dict[str, Any]] = None,
        expires_at: datetime | str | None = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Mission:
        """Persist a waiting mission and register its structured trigger."""
        clean_goal = " ".join((goal or "").split()).strip()
        if not clean_goal:
            raise ValueError("mission goal is required")
        spec = trigger if isinstance(trigger, MissionTrigger) else MissionTrigger.from_dict(trigger)
        now = self._clock()
        mission = Mission(
            task_id=new_mission_id(now), goal=clean_goal, status=MissionStatus.WAITING,
            created_at=now.isoformat(), updated_at=now.isoformat(),
            metadata={**dict(metadata or {}), "durable": True, "requires_verification": True},
            trigger=spec.to_dict(), context=dict(context or {}),
            completion_criteria=dict(completion_criteria or {"verified": True}),
            expires_at=_datetime_iso(expires_at) if expires_at is not None else None,
            trigger_id=spec.trigger_id, _bus=self._bus, _clock=self._clock,
            attempt_state={"attempts": 0, "schedule_revision": 1},
        )
        with self._scheduler_condition:
            self._missions[mission.task_id] = mission
            self._cancel[mission.task_id] = threading.Event()
            if runner is not None:
                self._runners[mission.task_id] = runner
            self._enqueue_trigger_locked(mission, spec, now=now)
            try:
                self._persist(mission)
            except Exception:
                self._missions.pop(mission.task_id, None)
                self._cancel.pop(mission.task_id, None)
                self._runners.pop(mission.task_id, None)
                raise
            self._scheduler_condition.notify_all()
        mission.emit(
            EVENT_TASK_STARTED, phase=MissionStatus.WAITING.value,
            payload={"goal": clean_goal, "trigger": spec.to_dict()},
        )
        return mission

    def set_durable_runner(self, runner: MissionRunner) -> None:
        with self._scheduler_condition:
            self._durable_runner = runner
            now = self._clock()
            for mission in self._missions.values():
                if mission.status is not MissionStatus.WAITING:
                    continue
                spec = self._mission_trigger(mission)
                if spec is not None and spec.kind in {TriggerKind.TIME, TriggerKind.WORLD}:
                    self._enqueue_trigger_locked(mission, spec, now=now, explicit_time=True)
            self._scheduler_condition.notify_all()

    def set_world_state(self, world_state: Any) -> None:
        self._world_state = world_state

    def reschedule(self, task_id: str, trigger: MissionTrigger | Mapping[str, Any]) -> bool:
        spec = trigger if isinstance(trigger, MissionTrigger) else MissionTrigger.from_dict(trigger)
        with self._scheduler_condition:
            mission = self._missions.get(task_id)
            if mission is None or mission.status.is_terminal:
                return False
            previous = (
                dict(mission.trigger or {}), mission.trigger_id, mission.execution_id,
                dict(mission.attempt_state), mission.status,
            )
            mission.trigger = spec.to_dict()
            mission.trigger_id = spec.trigger_id
            mission.execution_id = None
            mission.attempt_state["schedule_revision"] = int(mission.attempt_state.get("schedule_revision", 0)) + 1
            mission.attempt_state.pop("scheduled_at", None)
            if mission.status is not MissionStatus.PAUSED:
                mission.status = MissionStatus.WAITING
            self._enqueue_trigger_locked(mission, spec, now=self._clock())
            mission._touch()
            try:
                self._persist(mission)
            except Exception:
                mission.trigger, mission.trigger_id, mission.execution_id, mission.attempt_state, mission.status = previous
                return False
            self._scheduler_condition.notify_all()
        mission.emit(EVENT_TASK_PROGRESS, phase=mission.status.value, payload={
            "status": mission.status.value, "reason": "schedule updated",
        })
        return True

    def trigger_manual(self, task_id: str) -> bool:
        with self._lock:
            mission = self._missions.get(task_id)
            if mission is None or mission.status is not MissionStatus.WAITING:
                return False
            spec = self._mission_trigger(mission)
            if spec is None or spec.kind is not TriggerKind.MANUAL:
                return False
        return self._claim_and_dispatch(task_id, spec.trigger_id, dedupe_id=spec.trigger_id)

    def notify_event(
        self, event_type: str, payload: Optional[Dict[str, Any]] = None,
        *, event_id: Optional[str] = None,
    ) -> int:
        """Extension point for future event sources; no transport is embedded here."""
        dedupe_id = str(event_id or f"event-{uuid.uuid4().hex}")
        with self._lock:
            candidates = []
            for mission in self._missions.values():
                if mission.status is not MissionStatus.WAITING:
                    continue
                spec = self._mission_trigger(mission)
                if spec and spec.kind is TriggerKind.EVENT and spec.event_type == event_type:
                    candidates.append((mission.task_id, spec.trigger_id))
        fired = 0
        for task_id, trigger_id in candidates:
            mission = self.get(task_id)
            if mission is not None:
                mission.latest_evidence = {
                    "event_type": event_type, "event_id": dedupe_id,
                    "payload": dict(payload or {}), "observed_at": self._clock().isoformat(),
                }
            if self._claim_and_dispatch(task_id, trigger_id, dedupe_id=dedupe_id):
                fired += 1
        return fired

    def notify_world_changed(self, domain: str) -> int:
        """Wake only conditions targeting a changed World Model domain."""
        target = str(domain or "").casefold().strip()
        queued = 0
        with self._scheduler_condition:
            now = self._clock()
            for mission in self._missions.values():
                if mission.status is not MissionStatus.WAITING:
                    continue
                spec = self._mission_trigger(mission)
                if spec is None or spec.kind is not TriggerKind.WORLD or spec.domain != target:
                    continue
                self._enqueue_trigger_locked(mission, spec, now=now, explicit_time=True)
                queued += 1
            if queued:
                self._scheduler_condition.notify_all()
        return queued

    def start_scheduler(self) -> None:
        with self._scheduler_condition:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return
            self._scheduler_stop.clear()
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop, name="TaskRuntimeScheduler", daemon=False,
            )
            self._scheduler_thread.start()

    def stop_scheduler(self, timeout: float = 5.0) -> None:
        with self._scheduler_condition:
            self._scheduler_stop.set()
            self._scheduler_condition.notify_all()
            thread = self._scheduler_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def scheduler_stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._scheduler_metrics)

    def runtime_thread_names(self) -> List[str]:
        return [thread.name for thread in threading.enumerate()]

    def run_scheduler_once(self) -> Optional[float]:
        """Evaluate currently due triggers once; deterministic tests call this directly."""
        now = self._clock()
        due: List[tuple[str, str]] = []
        with self._lock:
            self._scheduler_metrics["wakeups"] += 1
            self._discard_stale_heap_locked()
            while self._schedule_heap and self._schedule_heap[0][0] <= now.timestamp():
                _when, _order, task_id, trigger_id = heapq.heappop(self._schedule_heap)
                mission = self._missions.get(task_id)
                if not self._schedule_entry_current(mission, trigger_id, _when):
                    continue
                mission.attempt_state.pop("scheduled_at", None)
                due.append((task_id, trigger_id))
        for task_id, trigger_id in due:
            self._evaluate_trigger(task_id, trigger_id, now)
        with self._lock:
            self._discard_stale_heap_locked()
            return self._schedule_heap[0][0] if self._schedule_heap else None

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                next_due = self.run_scheduler_once()
            except Exception as exc:
                log.error("TaskRuntime scheduler evaluation failed: %s", exc)
                next_due = None
            now_ts = self._clock().timestamp()
            timeout = self._scheduler_poll_sec
            if next_due is not None:
                timeout = min(timeout, max(0.01, next_due - now_ts))
            with self._scheduler_condition:
                if not self._scheduler_stop.is_set():
                    self._scheduler_condition.wait(timeout=timeout)

    def _evaluate_trigger(self, task_id: str, trigger_id: str, now: datetime) -> None:
        with self._lock:
            mission = self._missions.get(task_id)
            if mission is None or mission.status is not MissionStatus.WAITING:
                return
            spec = self._mission_trigger(mission)
            if spec is None or spec.trigger_id != trigger_id:
                return
            self._scheduler_metrics["evaluations"] += 1
            if mission.expires_at and (_parse_datetime(mission.expires_at) or now) <= now:
                mission.set_status(MissionStatus.EXPIRED, "mission expiry reached")
                return
        if spec.kind is TriggerKind.TIME:
            due = _parse_datetime(spec.due_at)
            if due is None:
                self._fail_trigger(mission, "invalid persisted due_at")
                return
            lateness = max(0.0, (now - due).total_seconds())
            if spec.missed_policy is MissedTriggerPolicy.SKIP and lateness > 0:
                mission.set_status(MissionStatus.EXPIRED, "missed trigger policy=skip")
                return
            if (
                spec.missed_policy is MissedTriggerPolicy.EXECUTE_IF_FRESH
                and lateness > spec.max_lateness_sec
            ):
                mission.set_status(MissionStatus.EXPIRED, "missed trigger exceeded freshness window")
                return
            mission.latest_evidence = {
                "source": "scheduler_clock", "trigger_id": trigger_id,
                "due_at": due.isoformat(), "observed_at": now.isoformat(),
                "lateness_sec": lateness, "missed_policy": spec.missed_policy.value,
            }
            self._claim_and_dispatch(task_id, trigger_id, dedupe_id=trigger_id)
            return
        if spec.kind is TriggerKind.WORLD:
            if self._world_condition_met(mission, spec, now):
                self._claim_and_dispatch(task_id, trigger_id, dedupe_id=trigger_id)
            else:
                with self._scheduler_condition:
                    current = self._missions.get(task_id)
                    if current is not None and current.status is MissionStatus.WAITING:
                        self._enqueue_trigger_locked(
                            current, spec,
                            now=now + timedelta(seconds=spec.poll_interval_sec),
                            explicit_time=True,
                        )
                        self._persist(current)
                        self._scheduler_condition.notify_all()

    def _world_condition_met(self, mission: Mission, spec: MissionTrigger, now: datetime) -> bool:
        world = self._world_state
        if world is None:
            mission.latest_evidence = {"error": "world state unavailable", "freshness": "unknown"}
            self._persist(mission)
            return False
        try:
            fact = world.observe_domain(spec.domain, force=True, **dict(spec.observation_options))
            self._scheduler_metrics["world_observations"] += 1
            freshness = fact.freshness(now)
            observed = str(getattr(fact, "fact_type", "")) == "observed"
            actual, found = _resolve_path(getattr(fact, "value", None), spec.path)
            if spec.operator == "missing":
                condition_matches = (not found) or actual is None
            elif spec.operator == "exists":
                condition_matches = found and actual is not None
            else:
                condition_matches = found and _compare(actual, spec.operator, spec.expected)
            matched = bool(observed and freshness == "fresh" and not fact.error and condition_matches)
            mission.latest_evidence = {
                "domain": spec.domain, "path": spec.path, "actual": actual if found else None,
                "fact_type": getattr(fact, "fact_type", ""), "source": getattr(fact, "source", ""),
                "observed_at": getattr(fact, "observed_at", ""), "freshness": freshness,
                "evidence": list(getattr(fact, "evidence", []) or []),
                "error": getattr(fact, "error", None), "matched": matched,
            }
            self._persist(mission)
            return matched
        except Exception as exc:
            mission.latest_evidence = {
                "domain": spec.domain, "freshness": "unknown",
                "error": f"{type(exc).__name__}: {exc}", "matched": False,
            }
            self._persist(mission)
            return False

    def _claim_and_dispatch(self, task_id: str, trigger_id: str, *, dedupe_id: str) -> bool:
        with self._lock:
            mission = self._missions.get(task_id)
            if mission is None or mission.status is not MissionStatus.WAITING:
                return False
            now = self._clock()
            expiry = _parse_datetime(mission.expires_at)
            if expiry is not None and expiry <= now:
                mission.set_status(MissionStatus.EXPIRED, "mission expiry reached")
                self._persist(mission)
                return False
            if dedupe_id in mission.executed_trigger_ids:
                return False
            runner = self._runners.get(task_id) or self._durable_runner
            if runner is None:
                mission.latest_evidence = {**mission.latest_evidence, "error": "durable runner unavailable"}
                spec = self._mission_trigger(mission)
                if spec is not None and spec.kind in {TriggerKind.TIME, TriggerKind.WORLD}:
                    self._enqueue_trigger_locked(
                        mission, spec, now=self._clock() + timedelta(seconds=self._scheduler_poll_sec),
                        explicit_time=True,
                    )
                self._persist(mission)
                return False
            cancel = self._cancel.setdefault(task_id, threading.Event())
            if cancel.is_set():
                return False
            if self._active_count >= self._max_concurrent:
                self._enqueue_trigger_locked(
                    mission, self._mission_trigger(mission),
                    now=self._clock() + timedelta(seconds=0.1), explicit_time=True,
                )
                return False
            mission.executed_trigger_ids.append(dedupe_id)
            mission.execution_id = f"execution-{uuid.uuid4().hex}"
            mission.trigger_id = trigger_id
            mission.attempt_state["attempts"] = int(mission.attempt_state.get("attempts", 0)) + 1
            mission.status = MissionStatus.TRIGGERED
            mission._touch()
            self._active_count += 1
            self._scheduler_metrics["executions"] += 1
            try:
                self._persist(mission)
            except Exception as exc:
                mission.status = MissionStatus.WAITING
                mission.execution_id = None
                if mission.executed_trigger_ids and mission.executed_trigger_ids[-1] == dedupe_id:
                    mission.executed_trigger_ids.pop()
                self._active_count = max(0, self._active_count - 1)
                self._scheduler_metrics["executions"] = max(0, self._scheduler_metrics["executions"] - 1)
                mission.latest_evidence = {
                    **mission.latest_evidence,
                    "error": f"durable claim persistence failed: {type(exc).__name__}",
                }
                spec = self._mission_trigger(mission)
                if spec is not None and spec.kind in {TriggerKind.TIME, TriggerKind.WORLD}:
                    self._enqueue_trigger_locked(
                        mission, spec,
                        now=self._clock() + timedelta(seconds=self._scheduler_poll_sec),
                        explicit_time=True,
                    )
                log.error("TaskRuntime refused unpersisted trigger claim for %s", task_id)
                return False
            mission.emit(EVENT_TRIGGERED, phase=MissionStatus.TRIGGERED.value, payload={
                "trigger_id": trigger_id, "execution_id": mission.execution_id,
            })
            self._start_thread(mission, runner, cancel, self._default_watchdog)
        return True

    def _enqueue_trigger_locked(
        self, mission: Mission, spec: Optional[MissionTrigger], *, now: datetime,
        explicit_time: bool = False,
    ) -> None:
        if spec is None or mission.status is MissionStatus.PAUSED or mission.status.is_terminal:
            return
        if spec.kind is TriggerKind.TIME and not explicit_time:
            due = _parse_datetime(spec.due_at)
        elif spec.kind is TriggerKind.WORLD or explicit_time:
            due = now
        else:
            due = None
        if due is None:
            mission.attempt_state.pop("scheduled_at", None)
            return
        when = due.timestamp()
        mission.attempt_state["scheduled_at"] = when
        heapq.heappush(
            self._schedule_heap,
            (when, next(self._heap_counter), mission.task_id, spec.trigger_id),
        )

    def _discard_stale_heap_locked(self) -> None:
        while self._schedule_heap:
            when, _order, task_id, trigger_id = self._schedule_heap[0]
            if self._schedule_entry_current(self._missions.get(task_id), trigger_id, when):
                break
            heapq.heappop(self._schedule_heap)

    @staticmethod
    def _schedule_entry_current(mission: Optional[Mission], trigger_id: str, when: float) -> bool:
        if mission is None or mission.status is not MissionStatus.WAITING:
            return False
        return (
            mission.trigger_id == trigger_id
            and float(mission.attempt_state.get("scheduled_at", -1.0)) == float(when)
        )

    @staticmethod
    def _mission_trigger(mission: Mission) -> Optional[MissionTrigger]:
        try:
            return MissionTrigger.from_dict(mission.trigger or {})
        except (TypeError, ValueError):
            return None

    def _fail_trigger(self, mission: Mission, error: str) -> None:
        mission.error = error
        mission.set_status(MissionStatus.FAILED, error)

    # ------------------------------------------------------------------ #
    #  Служебное: старт потока миссии
    # ------------------------------------------------------------------ #

    def _start_thread(self, mission: Mission, runner: MissionRunner,
                      cancel: threading.Event, watchdog: Optional[float]) -> None:
        """Создаёт и стартует поток исполнения миссии (вне блокировки _lock)."""
        if mission.metadata.get("durable"):
            mission.set_status(MissionStatus.EXECUTING, "durable trigger claimed")
        else:
            mission.set_status(MissionStatus.ACKNOWLEDGING, "mission accepted")
        thread = threading.Thread(
            target=self._run_wrapper,
            args=(mission, runner, cancel, watchdog),
            name=f"mission-{mission.task_id}",
            daemon=False,
        )
        with self._lock:
            self._threads[mission.task_id] = thread
        thread.start()

    def _run_wrapper(self, mission: Mission, runner: MissionRunner,
                     cancel: threading.Event, watchdog: Optional[float]) -> None:
        """Обертка потока: ловит всё, управляет статусом и watchdog."""
        start = time.perf_counter()
        mission.emit(EVENT_ACKNOWLEDGED, phase=MissionStatus.ACKNOWLEDGING.value,
                     payload={"ack": mission.acknowledgement or "Принято, сэр. Разбираюсь."})

        # Опциональный реальный watchdog на ВСЮ миссию (не на размышление!).
        if watchdog is not None and watchdog > 0:
            def _watcher() -> None:
                if not cancel.wait(timeout=watchdog):
                    if mission.status.is_active:
                        log.warning("Watchdog: миссия %s превысила реальный лимит %ss",
                                    mission.task_id, watchdog)
                        cancel.set()
                        mission.set_status(MissionStatus.FAILED,
                                           f"real watchdog timeout ({watchdog}s)")
            wthread = threading.Thread(target=_watcher, name=f"watchdog-{mission.task_id}",
                                       daemon=False)
            wthread.start()

        try:
            result = runner(mission, cancel)
            if cancel.is_set() and mission.status != MissionStatus.COMPLETED:
                if task_id := getattr(mission, "task_id", ""):
                    preserving = task_id in self._shutdown_preserve
                else:
                    preserving = False
                if preserving and mission.metadata.get("durable"):
                    mission.set_status(MissionStatus.PAUSED, "runtime shutdown interrupted execution")
                elif mission.status is not MissionStatus.CANCELLED:
                    mission.set_status(MissionStatus.CANCELLED, "cancel requested")
                mission.emit(EVENT_TASK_COMPLETED, payload={"status": mission.status.value})
                mission.error = mission.error or (
                    "Execution interrupted by runtime shutdown."
                    if preserving else "Отменено пользователем."
                )
                return
            # Подтверждение (HIGH-risk): миссия приостановлена и ждёт ответа
            # пользователя. Финализировать НЕЛЬЗЯ — иначе confirmation
            # теряется, и никто не сможет его подтвердить/отклонить.
            if mission.status == MissionStatus.PAUSED:
                mission.result = result
                log.info("Миссия %s ожидает подтверждения пользователя", mission.task_id)
                return
            mission.result = result
            if mission.metadata.get("requires_verification"):
                verified = bool(
                    isinstance(mission.verification, Mapping)
                    and mission.verification.get("verified") is True
                )
                if not verified:
                    mission.error = "desired state was not verified"
                    mission.set_status(MissionStatus.FAILED, mission.error)
                    mission.emit(EVENT_TASK_FAILED, payload={
                        "error": mission.error, "verification": mission.verification,
                    })
                    mission.emit(EVENT_TASK_COMPLETED, payload={
                        "status": "failed", "error": mission.error,
                    })
                    return
            mission.set_status(MissionStatus.COMPLETED)
            mission.emit(EVENT_TASK_COMPLETED, payload={"status": "completed", "result": result})
            log.info("Миссия %s завершена за %.1fs", mission.task_id,
                     time.perf_counter() - start)
        except Exception as exc:  # верхний уровень миссии
            if cancel.is_set():
                if mission.task_id in self._shutdown_preserve and mission.metadata.get("durable"):
                    mission.status = MissionStatus.PAUSED
                    mission.error = "Execution interrupted by runtime shutdown."
                else:
                    mission.status = MissionStatus.CANCELLED
                    mission.error = "Отменено пользователем."
            else:
                mission.status = MissionStatus.FAILED
                mission.error = f"{type(exc).__name__}: {exc}"
                mission.note_error(mission.error)
                mission.emit(EVENT_ERROR, payload={"error": mission.error})
                mission.emit(EVENT_TASK_FAILED, payload={"error": mission.error})
                log.exception("Миссия %s упала: %s", mission.task_id, exc)
            mission.emit(EVENT_TASK_COMPLETED,
                         payload={"status": mission.status.value, "error": mission.error})
        finally:
            # Слот освобождается СТРОГО в finally — даже при отмене/падении/
            # паузе (§1.3 П1). Иначе очередь зависнет навсегда.
            self._release_slot_and_drain()
            self._shutdown_preserve.discard(mission.task_id)

    def _release_slot_and_drain(self) -> None:
        """Освобождает слот и стартует следующую миссию из очереди (§1.3 П1).

        Вызывается из finally каждой завершившейся миссии. Поток следующей
        миссии стартует ВНЕ блокировки, чтобы не держать _lock долго и не
        дедлокнуть подписчиков шины событий.
        """
        nxt = None
        with self._lock:
            self._active_count = max(0, self._active_count - 1)
            while self._queue:
                mission, runner, cancel, watchdog = self._queue.popleft()
                # Отменённая ещё в очереди — просто выбрасываем, не стартуем.
                if cancel.is_set() or mission.status == MissionStatus.CANCELLED:
                    continue
                self._active_count += 1
                nxt = (mission, runner, cancel, watchdog)
                break
        if nxt is not None:
            self._start_thread(*nxt)

    # ------------------------------------------------------------------ #
    #  Управление
    # ------------------------------------------------------------------ #
    def cancel(self, task_id: str) -> bool:
        """Отменяет миссию (выставляет флаг отмены исполнителю).

        Работает и для миссий, ещё ЖДУЩИХ в очереди (§1.3 П1): такие
        никогда не стартуют — выбрасываются при дренаже слота.
        """
        with self._lock:
            ev = self._cancel.get(task_id)
            mission = self._missions.get(task_id)
            # Если миссия ещё в очереди — помечаем отменённой, чтобы drain
            # её выбросил, а не запустил.
            queued = False
            if mission is not None and mission.status == MissionStatus.QUEUED:
                new_q = collections.deque()
                for item in self._queue:
                    if item[0].task_id == task_id:
                        queued = True
                        continue
                    new_q.append(item)
                self._queue = new_q
        if ev is None:
            return False
        ev.set()
        if mission is not None and not mission.status.is_terminal:
            mission.set_status(
                MissionStatus.CANCELLED,
                "cancel requested" + (" (в очереди)" if queued else ""),
            )
            try:
                self._persist(mission)
            except Exception as exc:
                log.error("TaskRuntime cancellation persistence failed for %s: %s", task_id, exc)
                return False
        return True

    def resume(self, task_id: str) -> bool:
        """Resume a paused mission, re-registering its durable trigger."""
        mission = self.get(task_id)
        if mission is None:
            return False
        if mission.status == MissionStatus.PAUSED:
            if mission.metadata.get("durable"):
                spec = self._mission_trigger(mission)
                if spec is None:
                    return False
                mission.set_status(MissionStatus.WAITING, "resumed")
                with self._scheduler_condition:
                    self._enqueue_trigger_locked(mission, spec, now=self._clock())
                    try:
                        self._persist(mission)
                    except Exception as exc:
                        mission.status = MissionStatus.PAUSED
                        log.error("TaskRuntime resume persistence failed for %s: %s", task_id, exc)
                        return False
                    self._scheduler_condition.notify_all()
                if spec.kind is TriggerKind.MANUAL:
                    return self.trigger_manual(task_id)
            else:
                mission.set_status(MissionStatus.EXECUTING, "resumed")
            return True
        return False

    def pause(self, task_id: str) -> bool:
        """Marks a mission paused; cooperative runners inspect this state."""
        mission = self.get(task_id)
        if mission is None or mission.status.is_terminal:
            return False
        mission.set_status(MissionStatus.PAUSED, "paused by user")
        try:
            self._persist(mission)
        except Exception as exc:
            log.error("TaskRuntime pause persistence failed for %s: %s", task_id, exc)
            return False
        return True

    def skip_step(self, task_id: str) -> bool:
        """Skips only the current pending plan step and preserves the mission."""
        mission = self.get(task_id)
        if mission is None:
            return False
        for step in mission.plan:
            if step.get("status") == "pending":
                step["status"] = "skipped"
                mission._touch()
                mission.emit(EVENT_STEP_COMPLETED, payload={
                    "step": step.get("description"), "status": "skipped",
                })
                return True
        return False

    def explain_current_step(self, task_id: str) -> str:
        """Returns an action trace item, never hidden model reasoning."""
        mission = self.get(task_id)
        if mission is None:
            return "mission not found"
        for step in mission.plan:
            if step.get("status") == "pending":
                return str(step.get("description") or step.get("tool") or "pending")
        return "completed"

    def restore_mission(self, mission: Mission) -> Mission:
        """Registers an externally reconstructed mission for continuation."""
        mission._bus = self._bus
        mission._clock = self._clock
        with self._lock:
            self._missions[mission.task_id] = mission
            self._cancel.setdefault(mission.task_id, threading.Event())
            if mission.metadata.get("durable") and mission.status is MissionStatus.WAITING:
                spec = self._mission_trigger(mission)
                if spec is not None:
                    self._enqueue_trigger_locked(mission, spec, now=self._clock())
        self._persist(mission)
        return mission

    def wait(self, task_id: str, timeout: Optional[float] = None) -> Optional[Mission]:
        """Блокирует до терминального статуса миссии или до timeout.

        ВАЖНО (§33): timeout здесь — это сколько ВЫЗЫВАЮЩИЙ готов ждать,
        а НЕ лимит на саму задачу. Если timeout=None — ждём сколько угодно.
        """
        mission = self.get(task_id)
        if mission is None:
            return None
        deadline = None if timeout is None else time.perf_counter() + timeout
        with self._scheduler_condition:
            while mission.status.is_active:
                remaining = None if deadline is None else max(0.0, deadline - time.perf_counter())
                if remaining is not None and remaining <= 0:
                    return mission
                self._scheduler_condition.wait(timeout=remaining)
        # A terminal status becomes externally observable only with a durable
        # checkpoint. This also closes a restart race with the final event emit.
        self._persist(mission)
        with self._lock:
            thread = self._threads.get(task_id)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            remaining = None if deadline is None else max(0.0, deadline - time.perf_counter())
            thread.join(timeout=remaining)
        return mission

    def get(self, task_id: str) -> Optional[Mission]:
        with self._lock:
            return self._missions.get(task_id)

    def list_missions(self, include_terminal: bool = True) -> List[Mission]:
        with self._lock:
            items = list(self._missions.values())
        if not include_terminal:
            items = [m for m in items if m.status.is_active]
        return items

    def persistence_path(self, task_id: str) -> Optional[Path]:
        if self._persistence_dir is None:
            return None
        return self._persistence_dir / f"{task_id}.json"

    def update_context(self, task_id: str, updates: Mapping[str, Any]) -> bool:
        """Atomically persist semantic/AskOnce context without re-registering triggers."""
        with self._lock:
            mission = self._missions.get(task_id)
            if mission is None or mission.status.is_terminal:
                return False
            mission.context.update(dict(updates))
            mission._touch()
            self._persist(mission)
            return True

    def shutdown(self) -> None:
        """Stop runtime while preserving durable waiting/paused missions."""
        self.stop_scheduler()
        with self._lock:
            # Очистить очередь ожидания — миссии в ней не должны стартовать.
            self._queue.clear()
        # Отменить активные и queued.
        for tid, mission in list(self._missions.items()):
            if mission.metadata.get("durable"):
                if mission.status in {MissionStatus.WAITING, MissionStatus.PAUSED}:
                    self._persist(mission)
                    continue
                if not mission.status.is_terminal:
                    self._shutdown_preserve.add(tid)
                    event = self._cancel.get(tid)
                    if event is not None:
                        event.set()
                    continue
            self.cancel(tid)
        # Дождаться завершения потоков (best-effort).
        for tid, th in list(self._threads.items()):
            if th.is_alive():
                th.join(timeout=2.0)

    def is_alive(self, task_id: str) -> bool:
        with self._lock:
            th = self._threads.get(task_id)
        return th is not None and th.is_alive()

    # ------------------------------------------------------------------ #
    #  Sprint 9 — local mission persistence
    # ------------------------------------------------------------------ #
    def _persist_event(self, event: TaskEvent) -> None:
        mission = self.get(event.task_id)
        if mission is not None:
            self._persist(mission)
        with self._scheduler_condition:
            self._scheduler_condition.notify_all()

    def _persist(self, mission: Mission) -> None:
        if self._persistence_dir is None:
            return
        path = self._persistence_dir / f"{mission.task_id}.json"
        atomic_json_write(path, redact(_json_safe(mission.to_dict())))

    def _restore_persisted(self) -> None:
        if self._persistence_dir is None:
            return
        for path in self._persistence_dir.glob("*.json"):
            try:
                data = load_json(path, default={})
                mission = Mission(
                    task_id=data["task_id"], goal=data["goal"],
                    status=MissionStatus(data.get("status", "queued")),
                    created_at=data.get("created_at", _utcnow_iso()),
                    updated_at=data.get("updated_at", _utcnow_iso()),
                    result=data.get("result"), error=data.get("error"),
                    plan=list(data.get("plan") or []), metadata=dict(data.get("metadata") or {}),
                    progress=float(data.get("progress", 0.0)),
                    current_step=data.get("current_step"), model_used=data.get("model_used"),
                    tools_used=list(data.get("tools_used") or []),
                    errors=list(data.get("errors") or []), verification=data.get("verification"),
                    acknowledgement=data.get("acknowledgement"),
                    trigger=dict(data["trigger"]) if isinstance(data.get("trigger"), dict) else None,
                    context=dict(data.get("context") or {}),
                    latest_evidence=dict(data.get("latest_evidence") or {}),
                    attempt_state=dict(data.get("attempt_state") or {}),
                    completion_criteria=dict(data.get("completion_criteria") or {}),
                    expires_at=data.get("expires_at"), execution_id=data.get("execution_id"),
                    trigger_id=data.get("trigger_id"),
                    executed_trigger_ids=list(data.get("executed_trigger_ids") or []),
                    _clock=self._clock,
                )
                mission._bus = self._bus
                if mission.metadata.get("durable"):
                    if mission.status is MissionStatus.PENDING:
                        mission.status = MissionStatus.WAITING
                    elif mission.status in {
                        MissionStatus.TRIGGERED, MissionStatus.ACKNOWLEDGING,
                        MissionStatus.ANALYZING, MissionStatus.PLANNING,
                        MissionStatus.EXECUTING, MissionStatus.VERIFYING,
                        MissionStatus.REPAIRING,
                    }:
                        mission.status = MissionStatus.PAUSED
                        mission.latest_evidence = {
                            **mission.latest_evidence,
                            "recovery": "execution interrupted by restart; manual resume required",
                        }
                self._missions[mission.task_id] = mission
                self._cancel[mission.task_id] = threading.Event()
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def _rebuild_schedule(self) -> None:
        with self._lock:
            now = self._clock()
            for mission in self._missions.values():
                mission._clock = self._clock
                if not mission.metadata.get("durable") or mission.status is not MissionStatus.WAITING:
                    continue
                spec = self._mission_trigger(mission)
                if spec is None:
                    mission.status = MissionStatus.FAILED
                    mission.error = "invalid persisted trigger"
                    self._persist(mission)
                    continue
                self._enqueue_trigger_locked(mission, spec, now=now)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: datetime | str | None) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _datetime_iso(value: datetime | str) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError(f"invalid datetime: {value}")
    return parsed.astimezone(timezone.utc).isoformat()


def _resolve_path(value: Any, path: str) -> tuple[Any, bool]:
    current = value
    for part in [item for item in str(path).split(".") if item]:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return None, False
    return current, True


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "exists":
        return actual is not None
    if operator == "missing":
        return actual is None
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "contains":
        try:
            return expected in actual
        except TypeError:
            return False
    if operator in {"any_match", "none_match"}:
        if not isinstance(actual, list):
            return operator == "none_match"
        if isinstance(expected, Mapping):
            matches = any(
                isinstance(item, Mapping)
                and all(item.get(str(key)) == value for key, value in expected.items())
                for item in actual
            )
        else:
            matches = expected in actual
        return matches if operator == "any_match" else not matches
    try:
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
    except TypeError:
        return False
    return False


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if not str(key).startswith("_") and not callable(item)
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value if not callable(item)]
    return f"<{type(value).__name__}>"
