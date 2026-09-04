"""Regression: БАГ 3 — напоминания реально срабатывают через durable-планировщик.

Часы подменяются (фейковый clock), поэтому тест детерминирован:
1. До наступления времени планировщик НЕ запускает runner.
2. По наступлении — runner вызван ровно один раз, миссия COMPLETED.
3. Повторный проход планировщика не срабатывает повторно (dedupe).
4. Orchestrator._durable_mission_runner отдаёт typed AssistantOutput
   (не raw str) и один раз дергает output_callback.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


class _FakeClock:
    """Подменяемые часы для TaskRuntime (UTC)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


def _make_runtime(clock: _FakeClock):
    from core.task_runtime import TaskRuntime

    return TaskRuntime(clock=clock, scheduler_poll_sec=10.0)


def test_reminder_fires_once_at_due_time():
    from core.task_runtime import MissionTrigger, MissionStatus

    clock = _FakeClock()
    runtime = _make_runtime(clock)

    calls: list[str] = []

    def runner(mission, cancel: threading.Event) -> str:
        calls.append(mission.goal)
        mission.verification = {
            "verified": True,
            "method": "test_notification",
            "detail": "dispatched",
            "strict": True,
        }
        return "Напоминание: выпить воды"

    due = clock.now + timedelta(seconds=60)
    mission = runtime.schedule(
        "выпить воды",
        MissionTrigger.at(due),
        runner=runner,
        context={"notification_text": "выпить воды"},
        metadata={"durable_kind": "reminder"},
    )
    assert mission.status is MissionStatus.WAITING

    # 1) До наступления времени — тишина.
    runtime.run_scheduler_once()
    assert calls == [], "Напоминание сработало раньше времени"
    assert mission.status is MissionStatus.WAITING

    # 2) Время наступило — срабатывает.
    clock.now = due + timedelta(seconds=1)
    runtime.run_scheduler_once()
    done = runtime.wait(mission.task_id, timeout=5.0)
    assert calls == ["выпить воды"], f"runner вызван {len(calls)} раз(а)"
    assert done is not None and done.status is MissionStatus.COMPLETED

    # 3) Повторный проход планировщика — повторного запуска нет.
    clock.now = due + timedelta(seconds=30)
    runtime.run_scheduler_once()
    time.sleep(0.1)
    assert calls == ["выпить воды"], "Напоминание сработало повторно"


def test_reminder_output_is_typed_assistant_output():
    """_durable_mission_runner кладёт в TTS typed AssistantOutput, а не str."""
    from core.orchestrator import Orchestrator
    from core.voice import AssistantOutput

    orch = Orchestrator.__new__(Orchestrator)
    printed: list[str] = []
    queued: list[object] = []
    orch._output_callback = lambda text: printed.append(text)
    orch._queue_assistant_output = lambda output: queued.append(output)

    mission = MagicMock()
    mission.metadata = {"durable_kind": "reminder"}
    mission.context = {"notification_text": "выпить воды"}
    mission.goal = "напомни выпить воды"

    result = orch._durable_mission_runner(mission, threading.Event())

    assert printed == ["Напоминание: выпить воды"]
    assert len(queued) == 1 and isinstance(queued[0], AssistantOutput), (
        "Напоминание должно идти в TTS через typed AssistantOutput, не raw str"
    )
    assert result == "Напоминание: выпить воды"
    assert mission.verification["verified"] is True
