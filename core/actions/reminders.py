"""Reminder tools backed by canonical durable missions in the main runtime.

Инструмент:
- ``AddReminderTool`` — добавляет напоминание через N минут.
- ``TaskManager`` — управляет таймерами, вызывает callback при срабатывании.

``TaskManager`` remains a compatibility fallback for isolated callers that do
not provide ``TaskRuntime``; production Agent context always provides it.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from config.settings import Settings
from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.utils.logger import get_logger

__all__ = ["TaskManager", "AddReminderTool", "ListRemindersTool", "CancelReminderTool"]

log = get_logger(__name__)


@dataclass(slots=True)
class Reminder:
    """Напоминание с таймером."""

    id: str
    text: str
    due_at: float  # unix timestamp
    timer: threading.Timer
    created_at: float = field(default_factory=time.time)


class TaskManager:
    """Менеджер напоминаний: фасад над TaskRuntime с fallback на threading.Timer.

    Когда TaskRuntime подключён, напоминания сохраняются как durable missions,
    переживают рестарт и исполняются через единый планировщик runtime.
    При отсутствии TaskRuntime используется threading.Timer (для изолированных тестов).

    Args:
        callback: функция, вызываемая при срабатывании напоминания.
            Сигнатура: callback(reminder_id: str, text: str) -> None.
        task_runtime: экземпляр TaskRuntime (единый источник правды).
    """

    def __init__(
        self,
        callback: Optional[Callable[[str, str], None]] = None,
        task_runtime: Optional[Any] = None,
    ) -> None:
        self._reminders: Dict[str, Reminder] = {}
        self._lock = threading.RLock()
        self._callback = callback
        self._task_runtime = task_runtime

    def set_runtime(self, runtime: Any) -> None:
        """Подключает TaskRuntime к менеджеру напоминаний."""
        with self._lock:
            self._task_runtime = runtime

    @property
    def runtime(self) -> Optional[Any]:
        return self._task_runtime

    def add_reminder_in_minutes(self, text: str, minutes: int) -> str:
        """Добавляет напоминание.

        Args:
            text: текст напоминания.
            minutes: через сколько минут сработать (>= 1).

        Returns:
            ID напоминания (task_id миссии или uuid).
        """
        if not text or not text.strip():
            raise ValueError("Текст напоминания не может быть пустым")
        if minutes < 1:
            raise ValueError("Минимум 1 минута")

        with self._lock:
            runtime = self._task_runtime

        if runtime is not None:
            from core.task_runtime import MissedTriggerPolicy, MissionTrigger

            clock = getattr(runtime, "_clock", None)
            now_dt = clock() if callable(clock) else datetime.now(timezone.utc)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            due_at = now_dt + timedelta(minutes=int(minutes))

            def _reminder_runner(mission, cancel_ev):
                text_to_notify = str(mission.context.get("notification_text") or mission.goal)
                if self._callback:
                    try:
                        self._callback(mission.task_id, text_to_notify)
                    except Exception as exc:
                        log.error("Ошибка в callback напоминания %s: %s", mission.task_id, exc)
                mission.verification = {
                    "verified": True,
                    "method": "task_manager_notification",
                    "detail": "dispatched",
                    "strict": True,
                }
                return f"Напоминание: {text_to_notify}"

            mission = runtime.schedule(
                f"Напомнить пользователю: {text.strip()}",
                MissionTrigger.at(
                    due_at, missed_policy=MissedTriggerPolicy.NOTIFY_LATE,
                    max_lateness_sec=0,
                ),
                runner=_reminder_runner,
                context={"notification_text": text.strip()},
                completion_criteria={"notification_dispatched": True},
                metadata={"durable_kind": "reminder", "source": "add_reminder"},
            )
            log.info("Добавлено напоминание (TaskRuntime) #%s через %d мин: %s", mission.task_id, minutes, text[:50])
            return mission.task_id

        reminder_id = uuid.uuid4().hex[:8]
        due_at = time.time() + minutes * 60

        def _fire() -> None:
            with self._lock:
                rem = self._reminders.pop(reminder_id, None)
            if rem and self._callback:
                try:
                    self._callback(rem.id, rem.text)
                except Exception as exc:
                    log.error("Ошибка в callback напоминания %s: %s", reminder_id, exc)

        timer = threading.Timer(minutes * 60, _fire)
        timer.daemon = True
        timer.start()

        reminder = Reminder(id=reminder_id, text=text.strip(), due_at=due_at, timer=timer)
        with self._lock:
            self._reminders[reminder_id] = reminder

        log.info("Добавлено напоминание #%s через %d мин: %s", reminder_id, minutes, text[:50])
        return reminder_id

    def list_reminders(self) -> List[Dict[str, Any]]:
        """Возвращает список активных напоминаний."""
        with self._lock:
            runtime = self._task_runtime

        if runtime is not None:
            clock = getattr(runtime, "_clock", None)
            now_dt = clock() if callable(clock) else datetime.now(timezone.utc)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            now_ts = now_dt.timestamp()

            result = []
            for mission in runtime.list_missions(include_terminal=False):
                if mission.metadata.get("durable_kind") != "reminder":
                    continue
                due_at_str = str((mission.trigger or {}).get("due_at") or "")
                due_ts = now_ts
                if due_at_str:
                    try:
                        due_dt = datetime.fromisoformat(due_at_str)
                        if due_dt.tzinfo is None:
                            due_dt = due_dt.replace(tzinfo=timezone.utc)
                        due_ts = due_dt.timestamp()
                    except Exception:
                        due_ts = now_ts

                remaining = max(0, int(due_ts - now_ts))
                created_ts = now_ts
                if mission.created_at:
                    try:
                        created_dt = datetime.fromisoformat(mission.created_at)
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        created_ts = created_dt.timestamp()
                    except Exception:
                        created_ts = now_ts

                result.append(
                    {
                        "id": mission.task_id,
                        "text": mission.context.get("notification_text", mission.goal),
                        "due_at": due_ts,
                        "remaining_sec": remaining,
                        "created_at": created_ts,
                    }
                )
            return sorted(result, key=lambda x: x["due_at"])

        with self._lock:
            now = time.time()
            result = []
            for rem in self._reminders.values():
                remaining = max(0, int(rem.due_at - now))
                result.append(
                    {
                        "id": rem.id,
                        "text": rem.text,
                        "due_at": rem.due_at,
                        "remaining_sec": remaining,
                        "created_at": rem.created_at,
                    }
                )
            return sorted(result, key=lambda x: x["due_at"])

    def cancel_reminder(self, reminder_id: str) -> bool:
        """Отменяет напоминание по ID.

        Returns:
            True — было отменено, False — не найдено.
        """
        with self._lock:
            runtime = self._task_runtime

        if runtime is not None:
            mission = runtime.get(reminder_id)
            if mission is not None and mission.metadata.get("durable_kind") == "reminder":
                canceled = runtime.cancel(reminder_id)
                if canceled:
                    log.info("Отменено напоминание (TaskRuntime) #%s", reminder_id)
                    return True

        with self._lock:
            rem = self._reminders.pop(reminder_id, None)
        if rem:
            rem.timer.cancel()
            log.info("Отменено напоминание #%s", reminder_id)
            return True
        return False

    def clear_all(self) -> int:
        """Отменяет все напоминания. Возвращает число отменённых."""
        count = 0
        with self._lock:
            runtime = self._task_runtime

        if runtime is not None:
            for mission in runtime.list_missions(include_terminal=False):
                if mission.metadata.get("durable_kind") == "reminder":
                    if runtime.cancel(mission.task_id):
                        count += 1

        with self._lock:
            count += len(self._reminders)
            for rem in self._reminders.values():
                rem.timer.cancel()
            self._reminders.clear()
        return count

    def shutdown(self) -> None:
        """Корректно останавливает все таймеры (при выходе приложения)."""
        self.clear_all()


# Глобальный экземпляр для удобства (оркестратор может использовать свой)
_DEFAULT_MANAGER: Optional[TaskManager] = None


def get_default_manager() -> TaskManager:
    """Возвращает глобальный TaskManager (лениво создаёт)."""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = TaskManager()
    return _DEFAULT_MANAGER


# --------------------------------------------------------------------------- #
# Tool-обёртки
# --------------------------------------------------------------------------- #


class AddReminderTool(Tool):
    """Инструмент: добавить напоминание."""

    @property
    def name(self) -> str:
        return "add_reminder"

    @property
    def description(self) -> str:
        return (
            "Создаёт напоминание, которое сработает через указанное число минут. "
            "При подключённом TaskRuntime хранится как durable mission и переживает restart; "
            "legacy callback используется только вне основного runtime."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Текст напоминания (что напомнить).",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Через сколько минут сработать (минимум 1).",
                    "minimum": 1,
                    "maximum": 10080,  # неделя
                },
            },
            "required": ["text", "minutes"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        text = args["text"]
        minutes = args["minutes"]

        runtime = context.extra.get("task_runtime") if context.extra else None
        if runtime is not None:
            try:
                from core.task_runtime import MissedTriggerPolicy, MissionTrigger
                due_at = datetime.now(timezone.utc) + timedelta(minutes=int(minutes))
                mission = runtime.schedule(
                    f"Напомнить пользователю: {text}",
                    MissionTrigger.at(
                        due_at, missed_policy=MissedTriggerPolicy.NOTIFY_LATE,
                        max_lateness_sec=0,
                    ),
                    context={"notification_text": text},
                    completion_criteria={"notification_dispatched": True},
                    metadata={"durable_kind": "reminder", "source": "add_reminder"},
                )
                path = runtime.persistence_path(mission.task_id)
                return ActionResult(
                    tool=self.name, args=args, ok=True,
                    output={
                        "summary": f"Напоминание создано (ID: {mission.task_id}): через {minutes} мин — «{text}»",
                        "mission_id": mission.task_id, "trigger_id": mission.trigger_id,
                        "due_at": due_at.isoformat(), "status": mission.status.value,
                        "durable": True, "persistence_path": str(path) if path else "",
                    },
                )
            except Exception as exc:
                return ActionResult(tool=self.name, args=args, ok=False, error=str(exc))

        manager = get_default_manager()
        # Если у контекста есть callback — используем его (оркестратор прокидывает)
        callback = context.extra.get("reminder_callback") if context.extra else None
        if callback and manager._callback is None:
            manager._callback = callback

        try:
            reminder_id = manager.add_reminder_in_minutes(text, minutes)
            return ActionResult(
                tool=self.name,
                args=args,
                ok=True,
                output=f"Напоминание создано (ID: {reminder_id}): через {minutes} мин — «{text}»",
            )
        except Exception as exc:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=False,
                error=str(exc),
            )


class ListRemindersTool(Tool):
    """Инструмент: список активных напоминаний."""

    @property
    def name(self) -> str:
        return "list_reminders"

    @property
    def description(self) -> str:
        return "Возвращает список всех активных напоминаний с оставшимся временем."

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        runtime = context.extra.get("task_runtime") if context.extra else None
        if runtime is not None:
            reminders = [
                mission for mission in runtime.list_missions(include_terminal=False)
                if mission.metadata.get("durable_kind") == "reminder"
            ]
            if not reminders:
                return ActionResult(tool=self.name, args=args, ok=True, output="Активных напоминаний нет.")
            lines = ["Активные напоминания:"]
            for mission in sorted(reminders, key=lambda item: str((item.trigger or {}).get("due_at") or "")):
                due_at = str((mission.trigger or {}).get("due_at") or "")
                lines.append(f"  #{mission.task_id} — {mission.context.get('notification_text', mission.goal)} ({due_at})")
            return ActionResult(tool=self.name, args=args, ok=True, output="\n".join(lines))

        manager = get_default_manager()
        reminders = manager.list_reminders()

        if not reminders:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=True,
                output="Активных напоминаний нет.",
            )

        lines = ["Активные напоминания:"]
        for r in reminders:
            mins = r["remaining_sec"] // 60
            secs = r["remaining_sec"] % 60
            lines.append(f"  #{r['id']} — {r['text']} (через {mins} мин {secs} сек)")
        return ActionResult(
            tool=self.name,
            args=args,
            ok=True,
            output="\n".join(lines),
        )


class CancelReminderTool(Tool):
    """Инструмент: отменить напоминание по ID."""

    @property
    def name(self) -> str:
        return "cancel_reminder"

    @property
    def description(self) -> str:
        return "Отменяет активное напоминание по его ID."

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "string",
                    "description": "ID напоминания (возвращается при создании).",
                },
            },
            "required": ["reminder_id"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        reminder_id = args["reminder_id"]
        runtime = context.extra.get("task_runtime") if context.extra else None
        if runtime is not None:
            mission = runtime.get(reminder_id)
            if mission is not None and mission.metadata.get("durable_kind") == "reminder" and runtime.cancel(reminder_id):
                return ActionResult(
                    tool=self.name, args=args, ok=True,
                    output=f"Напоминание #{reminder_id} отменено.",
                )
            return ActionResult(
                tool=self.name, args=args, ok=False,
                error=f"Напоминание #{reminder_id} не найдено.",
            )

        manager = get_default_manager()

        if manager.cancel_reminder(reminder_id):
            return ActionResult(
                tool=self.name,
                args=args,
                ok=True,
                output=f"Напоминание #{reminder_id} отменено.",
            )
        else:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=False,
                error=f"Напоминание #{reminder_id} не найдено.",
            )


# Авто-регистрация
DEFAULT_REGISTRY.register(AddReminderTool())
DEFAULT_REGISTRY.register(ListRemindersTool())
DEFAULT_REGISTRY.register(CancelReminderTool())
