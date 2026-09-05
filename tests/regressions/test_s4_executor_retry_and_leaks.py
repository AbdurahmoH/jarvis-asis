"""Regression S4: политика повторов, классы таймаутов и реестр утечек.

До фикса ``execute_tool`` по умолчанию повторял ЛЮБОЙ упавший инструмент до
трёх раз. Значит ``write_file``, ``file_copy``, ``add_reminder``,
``computer_keyboard`` и прочие пишущие/отправляющие вызовы исполнялись трижды
на одной ошибке — тройная запись, тройное нажатие, тройное напоминание.
Плюс legacy-поток, проигнорировавший ``cancel_event``, помечался как
``terminated`` и нигде не учитывался: побочные эффекты продолжались молча.

Закрепляем:
1. Дефолт ``max_retries`` == 0 у ``ToolExecutor.execute`` и ``execute_tool``.
2. Неидемпотентный инструмент вызывается РОВНО один раз, даже если вызывающий
   просит повторы (зажим стоит на границе безопасности, а не у вызывающих).
3. Идемпотентный ``read_file`` при ``max_retries=2`` вызывается 3 раза.
4. Паспорта: ``read_file.idempotent`` True, ``write_file.idempotent`` False;
   ``Capability.from_tool(..., idempotent=True)`` реально ставит поле.
5. Утечка legacy-потока: ``terminated=True``, ``side_effects_contained=False``,
   счётчик растёт, значения аргументов в payload не попадают.
6. Поток, уважающий ``cancel_event``: contained, счётчик НЕ растёт.
7. Успешный инструмент с ``side_effects_contained=False`` (как play_music)
   счётчик НЕ трогает — ключ именно ветка таймаута.
8. Классы таймаутов: browser/computer 60 c, явный ``timeout_sec`` паспорта
   выигрывает, web/system/file берутся из settings.
9. Payload диагностики сериализуем в JSON (его дампит scripts/_live_probe.py).
10. Импорт ``core.actions`` не ломает реестр паспортов (tripwire на молчаливую
    деградацию CAPABILITIES из-за кругового импорта).
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from config.settings import Settings
from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.executor import (
    ToolExecutor,
    execute_tool,
    leaked_execution_count,
    leaked_execution_stats,
    reset_leaked_executions,
    tool_timeout_class,
    tool_timeout_for,
)
from core.actions.registry import ToolRegistry


class _CountingTool(Tool):
    """Инструмент, который всегда падает и считает вызовы."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"stub {self._name}"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        self.calls += 1
        return ActionResult(tool=self._name, args=args, ok=False, error="stub failure")


class _HungLegacyTool(Tool):
    """Legacy-инструмент, который игнорирует cancel_event."""

    @property
    def name(self) -> str:
        return "s4_hung_legacy"

    @property
    def description(self) -> str:
        return "spins past cancellation"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        time.sleep(3.0)
        return ActionResult(tool=self.name, args=args, ok=True)


class _PoliteLegacyTool(Tool):
    """Legacy-инструмент, который проверяет cancel_event."""

    @property
    def name(self) -> str:
        return "s4_polite_legacy"

    @property
    def description(self) -> str:
        return "honours cancellation"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            if context.cancel_event.is_set():
                return ActionResult(tool=self.name, args=args, ok=False, error="отменён")
            time.sleep(0.01)
        return ActionResult(tool=self.name, args=args, ok=True)


class _UncontainedSuccessTool(Tool):
    """Успех с side_effects_contained=False — форма play_music."""

    @property
    def name(self) -> str:
        return "s4_uncontained_success"

    @property
    def description(self) -> str:
        return "leaves a player running"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        return ActionResult(tool=self.name, args=args, ok=True, output="играет",
                            side_effects_contained=False)


@pytest.fixture()
def context() -> ToolContext:
    return ToolContext(settings=Settings())


@pytest.fixture()
def clean_leaks():
    reset_leaked_executions()
    yield
    reset_leaked_executions()


def test_default_max_retries_is_zero() -> None:
    """Дефолт — ноль попыток повтора у обоих входов."""
    assert inspect.signature(ToolExecutor.execute).parameters["max_retries"].default == 0
    assert inspect.signature(execute_tool).parameters["max_retries"].default == 0


def test_default_call_does_not_retry(context: ToolContext) -> None:
    registry = ToolRegistry()
    tool = _CountingTool("read_file")
    registry.register(tool)

    execute_tool(registry, "read_file", {}, context)

    assert tool.calls == 1, "без явного max_retries повторов быть не должно"


def test_write_file_error_called_exactly_once(context: ToolContext) -> None:
    """Пишущий инструмент не повторяется, даже если вызывающий просит."""
    registry = ToolRegistry()
    tool = _CountingTool("write_file")
    registry.register(tool)

    result = execute_tool(registry, "write_file", {"path": "x", "content": "y"},
                          context, max_retries=2, retry_delay=0.0)

    assert tool.calls == 1, f"write_file исполнен {tool.calls} раз — двойная запись"
    assert not result.ok
    assert result.error == "stub failure", "ошибка должна дойти до repair-цикла как есть"


def test_read_file_error_retried_up_to_three_times(context: ToolContext) -> None:
    """Идемпотентное чтение повторяется: 1 + 2 повтора."""
    registry = ToolRegistry()
    tool = _CountingTool("read_file")
    registry.register(tool)

    execute_tool(registry, "read_file", {"path": "x"}, context,
                 max_retries=2, retry_delay=0.0)

    assert tool.calls == 3, f"read_file исполнен {tool.calls} раз, ожидалось 3"


@pytest.mark.parametrize(
    "tool_name,expected",
    [
        ("read_file", True),
        ("list_files", True),
        ("search_files", True),
        ("web_search", True),
        ("web_fetch", True),
        ("weather", True),
        ("public_data", True),
        ("current_time", True),
        ("system_status", True),
        ("list_reminders", True),
        ("write_file", False),
        ("file_copy", False),
        ("file_move", False),
        ("add_reminder", False),
        ("cancel_reminder", False),
        ("open_app", False),
        ("close_app", False),
        ("volume", False),
        ("play_music", False),
        ("screen_capture", False),
        ("computer_mouse", False),
        ("computer_keyboard", False),
        ("computer_screenshot", False),
        ("browser_automation", False),
        ("browser_bridge", False),
        ("list_files_recursive", False),
    ],
)
def test_passport_idempotency_flags(tool_name: str, expected: bool) -> None:
    """Флаг идемпотентности стоит ровно на читающих инструментах."""
    from core.capabilities import CAPABILITIES

    passport = CAPABILITIES.get(tool_name)
    assert passport is not None, f"паспорт '{tool_name}' пропал из реестра"
    assert bool(passport.idempotent) is expected


def test_from_tool_override_sets_idempotent_field() -> None:
    """Поле объявлено на dataclass, иначе from_tool молча его теряет."""
    from core.actions import DEFAULT_REGISTRY
    from core.capabilities import Capability

    tool = DEFAULT_REGISTRY.get("read_file")
    assert tool is not None
    cap = Capability.from_tool(tool, idempotent=True, timeout_sec=7.5)
    assert cap.idempotent is True
    assert cap.timeout_sec == 7.5
    payload = cap.to_dict()
    assert payload["idempotent"] is True
    assert payload["timeout_sec"] == 7.5


def test_hung_legacy_tool_is_registered_as_leak(context: ToolContext, clean_leaks) -> None:
    registry = ToolRegistry()
    registry.register(_HungLegacyTool())
    before = leaked_execution_count()

    result = execute_tool(registry, "s4_hung_legacy",
                          {"path": "x", "api_key": "sk-live-should-not-leak"},
                          context, timeout_sec=0.2)

    assert not result.ok
    assert "Таймаут выполнения" in (result.error or "")
    assert result.terminated is True
    assert result.side_effects_contained is False
    assert leaked_execution_count() == before + 1, "утечка не зарегистрирована"

    stats = leaked_execution_stats()
    assert stats["leaked_executions"] == before + 1
    record = stats["recent"][-1]
    assert record["tool"] == "s4_hung_legacy"
    assert record["execution_mode"] == "legacy_thread"
    assert record["arg_keys"] == ["api_key", "path"]
    assert "sk-live-should-not-leak" not in json.dumps(stats, ensure_ascii=False), (
        "значения аргументов не должны попадать в диагностику"
    )


def test_polite_legacy_tool_is_contained_and_not_leaked(context: ToolContext, clean_leaks) -> None:
    registry = ToolRegistry()
    registry.register(_PoliteLegacyTool())
    before = leaked_execution_count()

    result = execute_tool(registry, "s4_polite_legacy", {}, context, timeout_sec=0.2)

    assert not result.ok
    assert result.side_effects_contained is True
    assert leaked_execution_count() == before, "инструмент остановился, утечки нет"


def test_successful_uncontained_tool_does_not_count_as_leak(context: ToolContext, clean_leaks) -> None:
    """play_music-подобный успех не должен раздувать счётчик утечек."""
    registry = ToolRegistry()
    registry.register(_UncontainedSuccessTool())
    before = leaked_execution_count()

    result = execute_tool(registry, "s4_uncontained_success", {}, context)

    assert result.ok
    assert result.side_effects_contained is False
    assert leaked_execution_count() == before


def test_leak_registry_is_thread_safe(context: ToolContext, clean_leaks) -> None:
    """Несколько ВЫЗЫВАЮЩИХ потоков делят один executor (core/agent.py:456)."""
    registry = ToolRegistry()
    registry.register(_HungLegacyTool())
    executor = ToolExecutor(4)
    context.extra["tool_executor"] = executor
    before = leaked_execution_count()

    threads = [
        threading.Thread(target=execute_tool,
                         args=(registry, "s4_hung_legacy", {"n": index}, context),
                         kwargs={"timeout_sec": 0.2})
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)

    assert leaked_execution_count() == before + 4


def test_leak_stats_are_json_serialisable(context: ToolContext, clean_leaks) -> None:
    """scripts/_live_probe.py дампит весь diagnostics — только примитивы."""
    registry = ToolRegistry()
    registry.register(_HungLegacyTool())
    execute_tool(registry, "s4_hung_legacy", {"blob": object()}, context, timeout_sec=0.2)

    payload = {"tools": leaked_execution_stats()}
    encoded = json.dumps(payload, ensure_ascii=False)
    assert json.loads(encoded)["tools"]["leaked_executions"] >= 1


def test_leak_history_is_bounded(context: ToolContext, clean_leaks) -> None:
    """История ограничена: диагностика не растёт без предела."""
    from core.actions import executor as executor_module

    for index in range(executor_module._LEAK_HISTORY_LIMIT + 5):
        executor_module._register_leak("s4_synthetic", {"i": index}, mode="legacy_thread",
                                       timeout_sec=0.1, detail="synthetic")

    stats = leaked_execution_stats()
    assert stats["leaked_executions"] == executor_module._LEAK_HISTORY_LIMIT + 5
    assert len(stats["recent"]) <= executor_module._LEAK_REPORT_LIMIT


@pytest.mark.parametrize(
    "tool_name,expected_class",
    [
        ("browser_bridge", "browser"),
        ("browser_automation", "browser"),
        ("computer_mouse", "browser"),
        ("computer_keyboard", "browser"),
        ("computer_screenshot", "browser"),
        ("web_fetch", "web"),
        ("web_search", "web"),
        ("weather", "web"),
        ("public_data", "web"),
        ("system_status", "system"),
        ("screen_capture", "system"),
        ("read_file", "file"),
        ("write_file", "file"),
        ("s4_unknown_tool", "file"),
    ],
)
def test_timeout_classes(tool_name: str, expected_class: str) -> None:
    assert tool_timeout_class(tool_name) == expected_class


def test_browser_and_cua_get_sixty_seconds(context: ToolContext) -> None:
    """Дефолт браузерного класса — 60 c, а не файловые 10 c."""
    assert tool_timeout_for("browser_automation", context) == 60.0
    assert tool_timeout_for("computer_keyboard", context) == 60.0
    assert tool_timeout_for("computer_screenshot", context) == 60.0


def test_settings_still_win_for_web_system_file(context: ToolContext) -> None:
    """Код-дефолты класса не должны перебивать живой Settings."""
    limits = context.settings.limits
    assert tool_timeout_for("web_fetch", context) == limits.tool_timeout_web_sec
    assert tool_timeout_for("system_status", context) == limits.tool_timeout_system_sec
    assert tool_timeout_for("read_file", context) == limits.tool_timeout_file_sec


def test_non_positive_setting_falls_back_to_class_default() -> None:
    """Непригодная настройка даёт класс-дефолт, а не 0.1 c watchdog."""

    class _BrokenLimits:
        tool_timeout_file_sec = 0.0
        tool_timeout_web_sec = None

    class _BrokenSettings:
        limits = _BrokenLimits()

    context = ToolContext(settings=_BrokenSettings())
    assert tool_timeout_for("read_file", context) == 10.0
    assert tool_timeout_for("web_fetch", context) == 15.0


def test_passport_timeout_overrides_class(context: ToolContext, monkeypatch) -> None:
    """Явный timeout_sec паспорта сильнее класса."""
    from core.capabilities import CAPABILITIES

    passport = CAPABILITIES.get("read_file")
    assert passport is not None
    assert passport.timeout_sec is None, "по умолчанию переопределений нет"
    monkeypatch.setattr(passport, "timeout_sec", 3.5, raising=False)
    assert tool_timeout_for("read_file", context) == 3.5


def test_importing_actions_does_not_degrade_passport_registry() -> None:
    """Tripwire: круговой импорт не должен молча обнулять авто-паспорта.

    Проверяем в СВЕЖЕМ интерпретаторе: если core.actions.executor начнёт
    импортировать core.capabilities на уровне модуля, реестр паспортов
    построится на пустом DEFAULT_REGISTRY и авто-паспорта исчезнут.
    """
    code = (
        "import core.actions;"
        "from core.capabilities import CAPABILITIES;"
        "auto = ['screen_capture', 'file_copy', 'file_move', 'list_files_recursive'];"
        "missing = [n for n in auto if CAPABILITIES.get(n) is None];"
        "print('MISSING=' + ','.join(missing));"
        "print('TOTAL=%d' % len(CAPABILITIES.all()))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    assert completed.returncode == 0, (completed.stderr or "")[-2000:]
    lines = [line.strip() for line in (completed.stdout or "").splitlines()]
    assert "MISSING=" in lines, f"авто-паспорта потеряны: {completed.stdout}"
    total = [line for line in lines if line.startswith("TOTAL=")]
    assert total and int(total[0].split("=")[1]) >= 26, completed.stdout


def test_runtime_diagnostics_exposes_leak_count() -> None:
    """runtime_diagnostics отдаёт счётчик и остаётся JSON-сериализуемым."""
    import core.orchestrator as orchestrator_module

    source = inspect.getsource(orchestrator_module.Orchestrator.runtime_diagnostics)
    assert "leaked_execution_stats" in source
    assert '"tools"' in source
