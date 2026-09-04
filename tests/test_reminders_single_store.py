"""Тесты единого хранилища напоминаний (TaskManager + TaskRuntime).

Проверяет:
1. TaskManager делегирует в TaskRuntime при наличии runtime.
2. add_reminder, list_reminders, cancel_reminder оперируют одним реестром миссий.
3. E2E: «напомни через 1 минуту выпить воды» с _FakeClock и llm_available=False:
   через 1 виртуальную минуту формируется typed AssistantOutput и направляется в TTS.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config.settings import Settings
from core.actions.reminders import (
    AddReminderTool,
    CancelReminderTool,
    ListRemindersTool,
    TaskManager,
)
from core.task_runtime import MissionStatus, TaskRuntime
from core.voice import AssistantOutput


class _FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def test_task_manager_delegates_to_runtime(tmp_path: Path):
    clock = _FakeClock()
    runtime = TaskRuntime(persistence_dir=tmp_path / "missions", clock=clock)

    manager = TaskManager(task_runtime=runtime)
    assert manager.runtime is runtime

    # 1. Добавление через TaskManager попадает в TaskRuntime
    rem_id = manager.add_reminder_in_minutes("размять спину", 5)
    assert rem_id is not None

    mission = runtime.get(rem_id)
    assert mission is not None
    assert mission.metadata.get("durable_kind") == "reminder"
    assert mission.context.get("notification_text") == "размять спину"

    # 2. list_reminders читает из TaskRuntime
    listed = manager.list_reminders()
    assert len(listed) == 1
    assert listed[0]["id"] == rem_id
    assert listed[0]["text"] == "размять спину"
    assert listed[0]["remaining_sec"] == 300

    # 3. cancel_reminder отменяет в TaskRuntime
    canceled = manager.cancel_reminder(rem_id)
    assert canceled is True
    assert runtime.get(rem_id).status is MissionStatus.CANCELLED
    assert len(manager.list_reminders()) == 0


def test_reminders_single_store_tools_and_manager(tmp_path: Path):
    from core.actions.base import ToolContext

    clock = _FakeClock()
    runtime = TaskRuntime(persistence_dir=tmp_path / "missions", clock=clock)
    manager = TaskManager(task_runtime=runtime)

    context = ToolContext(
        settings=Settings(),
        extra={"task_runtime": runtime},
    )

    add_tool = AddReminderTool()
    res = add_tool.run({"text": "проверить почту", "minutes": 10}, context=context)
    assert res.ok is True
    mission_id = res.output["mission_id"]

    # Manager видит то же самое напоминание
    listed = manager.list_reminders()
    assert len(listed) == 1
    assert listed[0]["id"] == mission_id
    assert listed[0]["text"] == "проверить почту"

    # CancelTool отменяет его
    cancel_tool = CancelReminderTool()
    c_res = cancel_tool.run({"reminder_id": mission_id}, context=context)
    assert c_res.ok is True
    assert len(manager.list_reminders()) == 0


def test_e2e_reminder_fake_clock_tts_dispatch(tmp_path: Path):
    from core.orchestrator import Orchestrator

    clock = _FakeClock()

    settings = Settings()
    settings.paths.data_dir = str(tmp_path)
    settings.offline_mode = True
    settings.deepseek_brain_mode = False

    orch = Orchestrator(settings)
    orch._runtime._clock = clock
    orch._task_manager.set_runtime(orch._runtime)

    tts_outputs: list[AssistantOutput] = []
    plain_outputs: list[str] = []

    orch._queue_assistant_output = lambda output: tts_outputs.append(output)
    orch._output_callback = lambda text: plain_outputs.append(text)

    try:
        # 1. Запрос пользователя на создание напоминания
        outcome = orch.handle_input("напомни через 1 минуту выпить воды", channel="test")
        assert "напомн" in outcome.get("response", "").lower()
        # Orchestrator направляет первичное подтверждение (ACK) в TTS
        assert len(tts_outputs) == 1
        assert "напомн" in tts_outputs[0].text.lower()
        tts_outputs.clear()

        # Проверяем, что миссия создана в едином TaskRuntime
        reminders = orch._runtime.list_missions(include_terminal=False)
        rem_missions = [m for m in reminders if m.metadata.get("durable_kind") == "reminder"]
        assert len(rem_missions) == 1
        rem_mission = rem_missions[0]
        assert rem_mission.status is MissionStatus.WAITING
        assert "выпить воды" in str(rem_mission.context.get("notification_text", ""))

        # TaskManager также видит это напоминание
        mgr_reminders = orch._task_manager.list_reminders()
        assert len(mgr_reminders) == 1
        assert mgr_reminders[0]["id"] == rem_mission.task_id

        # До наступления времени — тишина
        orch._runtime.run_scheduler_once()
        assert len(tts_outputs) == 0

        # 2. Перемещаем виртуальные часы на 1 минуту и 1 секунду вперед
        clock.now = clock.now + timedelta(minutes=1, seconds=1)

        # Запускаем тик планировщика
        orch._runtime.run_scheduler_once()
        time.sleep(0.2)

        # Проверяем доставку в TTS
        assert len(tts_outputs) >= 1, "AssistantOutput не был направлен в TTS"
        last_tts = tts_outputs[-1]
        assert isinstance(last_tts, AssistantOutput)
        assert "выпить воды" in last_tts.text.lower()
        assert any("выпить воды" in msg.lower() for msg in plain_outputs)

        # Миссия перешла в COMPLETED
        done_mission = orch._runtime.get(rem_mission.task_id)
        assert done_mission is not None
        assert done_mission.status is MissionStatus.COMPLETED
    finally:
        orch.shutdown()
