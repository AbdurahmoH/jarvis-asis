"""Validated, bounded and cancellable tool execution."""
from __future__ import annotations

import multiprocessing
import pickle
import queue
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import jsonschema
from jsonschema import ValidationError

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import ToolRegistry
from core.security.redaction import redact_args
from core.utils.logger import get_logger

__all__ = [
    "ToolExecutor",
    "execute_tool",
    "validate_args",
    "tool_timeout_for",
    "tool_timeout_class",
    "leaked_execution_stats",
    "leaked_execution_count",
    "reset_leaked_executions",
]

log = get_logger(__name__)

_WEB_TOOLS = frozenset({
    "web_search", "web_fetch", "weather", "public_data",
})
# S4: браузер и computer-use — отдельный класс. Раньше браузерные жили в
# _WEB_TOOLS, а computer_* вообще не были перечислены и падали в файловый
# класс (10 c): навигация со скриптами и физический ввод в такой потолок не
# укладываются и получали ложный таймаут.
_BROWSER_TOOLS = frozenset({
    "browser_bridge", "browser_automation",
    "browser_open", "browser_click", "browser_type", "browser_scroll",
    "browser_close", "browser_screenshot",
    "computer_mouse", "computer_keyboard", "computer_screenshot",
})
_SYSTEM_TOOLS = frozenset({"system_status", "volume", "open_app", "close_app", "add_reminder", "list_reminders", "cancel_reminder", "current_time", "play_music", "screenshot", "clipboard_read", "clipboard_write", "key_press", "type_text", "screen_capture"})
# S4: класс для инструментов, которые внутри себя ходят в модель. Сейчас
# ПУСТ и это проверенный факт, а не заготовка: ни один зарегистрированный
# инструмент не вызывает LLM (computer_use работает через CUA-бэкенд ввода).
# Множество объявлено, чтобы бюджет брался из settings.limits, а не из
# соседнего класса, если такой инструмент появится.
_LLM_TOOLS: frozenset = frozenset()

# S4: класс -> (поле в settings.limits, код-дефолт). Код-дефолт срабатывает
# ТОЛЬКО когда настройки нет или она непригодна (None / не число / <= 0):
# живой Settings всегда выигрывает, config/settings.json не требуется.
_TIMEOUT_CLASSES: Dict[str, Tuple[str, float]] = {
    "web": ("tool_timeout_web_sec", 15.0),
    "system": ("tool_timeout_system_sec", 10.0),
    "file": ("tool_timeout_file_sec", 10.0),
    "browser": ("tool_timeout_browser_sec", 60.0),
    "llm": ("response_timeout_sec", 15.0),
}


def _passport(tool_name: str) -> Any:
    """Паспорт инструмента из CAPABILITIES или None.

    Импорт ЛОКАЛЬНЫЙ и это обязательно, а не стиль. ``core/actions/__init__``
    импортирует этот модуль ДО регистрации инструментов, а
    ``core/capabilities`` импортирует ``core.actions.registry``. Модульный
    ``from core.capabilities import CAPABILITIES`` здесь падает ImportError,
    а ``import core.capabilities`` — что хуже — МОЛЧА строит реестр паспортов
    на пустом DEFAULT_REGISTRY и теряет авто-паспорта (screen_capture,
    file_copy, file_move, list_files_recursive). Тот же приём:
    core/actions/screen_capture.py.

    Отсутствие паспорта деградирует безопасно: неидемпотентно + класс-дефолт.
    """
    try:
        from core.capabilities import CAPABILITIES
    except ImportError:
        return None
    try:
        return CAPABILITIES.get(tool_name)
    except (AttributeError, KeyError, TypeError):
        return None


def _limit_sec(limits: Any, attr: str, fallback: float) -> float:
    """Значение таймаута из settings.limits или код-дефолт класса."""
    try:
        value = float(getattr(limits, attr, None))
    except (TypeError, ValueError):
        return float(fallback)
    return value if value > 0 else float(fallback)


def tool_timeout_class(tool_name: str, passport: Any = None) -> str:
    """Класс таймаута инструмента (S4)."""
    if tool_name in _BROWSER_TOOLS:
        return "browser"
    if tool_name in _LLM_TOOLS:
        return "llm"
    if tool_name in _SYSTEM_TOOLS:
        return "system"
    if tool_name in _WEB_TOOLS:
        return "web"
    if passport is not None and bool(getattr(passport, "internet_required", False)):
        return "web"
    return "file"


def tool_timeout_for(tool_name: str, context: ToolContext) -> float:
    """Потолок времени: явный timeout_sec паспорта, иначе класс-дефолт."""
    passport = _passport(tool_name)
    explicit = getattr(passport, "timeout_sec", None) if passport is not None else None
    if explicit is not None:
        try:
            declared = float(explicit)
        except (TypeError, ValueError):
            declared = 0.0
        if declared > 0:
            return declared
    limits = getattr(getattr(context, "settings", None), "limits", None)
    attr, fallback = _TIMEOUT_CLASSES[tool_timeout_class(tool_name, passport)]
    return _limit_sec(limits, attr, fallback)


def _truncate_output(output: Any, context: ToolContext) -> Any:
    if not isinstance(output, str):
        return output
    cap = int(getattr(getattr(getattr(context, "settings", None), "limits", None), "tool_output_max_bytes", 50 * 1024) or 0)
    if cap <= 0 or len(output.encode("utf-8", errors="replace")) <= cap:
        return output
    text = output.encode("utf-8", errors="replace")[:cap].decode("utf-8", errors="ignore")
    return f"{text}\n… [вывод усечён: {len(output)} символов, потолок {cap} байт — resource limit]"


# S4: реестр «утёкших» исполнений — тех, где watchdog отдал управление, но
# побочные эффекты НЕ остановлены (поток проигнорировал cancel_event, процесс
# не удалось убить). Такое исполнение перестаёт быть просто ошибкой: оно
# продолжает менять мир после ответа. Замок нужен из-за нескольких ВЫЗЫВАЮЩИХ
# потоков на одном ToolExecutor (core/agent.py:456), запись делает вызывающий,
# не утёкший поток. deque ограничен: диагностика не должна расти без предела.
_LEGACY_CANCEL_GRACE_SEC = 0.25
_LEAK_HISTORY_LIMIT = 20
_LEAK_REPORT_LIMIT = 5
_LEAK_LOCK = threading.Lock()
_LEAKED_TOTAL = 0
_LEAKED_RECENT: Deque[Dict[str, Any]] = deque(maxlen=_LEAK_HISTORY_LIMIT)


def _arg_keys(args: Any) -> List[str]:
    """Только ИМЕНА аргументов для машинной диагностики.

    Значения не кладём сознательно: payload уходит в runtime_diagnostics и
    сериализуется json.dumps (scripts/_live_probe.py), а среди значений
    бывают несериализуемые объекты. Плюс утёкший поток продолжает держать
    ссылку на тот же dict и может его менять — отсюда list() под try.
    """
    if not isinstance(args, dict):
        return []
    try:
        return sorted(str(key) for key in list(args.keys()))
    except RuntimeError:
        return ["<изменяется утёкшим потоком>"]


def _safe_redacted_args(args: Any) -> Any:
    """Замаскированная копия аргументов для ЛОГА (не для payload)."""
    try:
        return redact_args(dict(args) if isinstance(args, dict) else args)
    except (RuntimeError, TypeError, ValueError):
        return {"<args>": "недоступны: изменяются утёкшим потоком"}


def _register_leak(tool_name: str, args: Any, *, mode: str, timeout_sec: float, detail: str) -> None:
    """Фиксирует утечку: счётчик + история + warning в лог.

    Ключ — именно ветка таймаута, а НЕ ``side_effects_contained is False``:
    core/actions/media.py:83 отдаёт этот флаг False на успешном пути
    (play_music оставляет играющий процесс), и счётчик по флагу считал бы
    каждое удачное воспроизведение.
    """
    global _LEAKED_TOTAL
    record = {
        "tool": str(tool_name),
        "execution_mode": str(mode),
        "timeout_sec": round(float(timeout_sec), 3),
        "detail": str(detail),
        "arg_keys": _arg_keys(args),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _LEAK_LOCK:
        _LEAKED_TOTAL += 1
        _LEAKED_RECENT.append(record)
        total = _LEAKED_TOTAL
    log.warning(
        "УТЕЧКА ИСПОЛНЕНИЯ #%d: %s (%s) не остановлен после %.1f c — %s; args=%s",
        total, tool_name, mode, float(timeout_sec), detail, _safe_redacted_args(args),
    )


def leaked_execution_count() -> int:
    """Сколько исполнений утекло за жизнь процесса."""
    with _LEAK_LOCK:
        return _LEAKED_TOTAL


def leaked_execution_stats() -> Dict[str, Any]:
    """Срез для runtime_diagnostics. Только JSON-примитивы."""
    with _LEAK_LOCK:
        recent = [dict(item) for item in list(_LEAKED_RECENT)[-_LEAK_REPORT_LIMIT:]]
        return {"leaked_executions": _LEAKED_TOTAL, "recent": recent}


def reset_leaked_executions() -> None:
    """Сброс реестра. Нужен тестам, в проде не вызывается."""
    global _LEAKED_TOTAL
    with _LEAK_LOCK:
        _LEAKED_TOTAL = 0
        _LEAKED_RECENT.clear()


def _process_worker(tool: Tool, args: Dict[str, Any], settings: Any, result_queue: Any) -> None:
    context = ToolContext(settings=settings)
    try:
        result = tool.run(args, context)
        if not isinstance(result, ActionResult):
            result = ActionResult(tool=tool.name, args=args, ok=False, error=f"Инструмент вернул не ActionResult: {type(result)}")
        result.execution_mode = "subprocess"
        result.side_effects_contained = True
        result_queue.put(result)
    except BaseException as exc:  # process boundary converts all failures
        result_queue.put(ActionResult(tool=tool.name, args=args, ok=False, error=f"{type(exc).__name__}: {exc}", execution_mode="subprocess", side_effects_contained=True))


def _terminate_process(process: Any) -> bool:
    if not process.is_alive():
        return True
    process.terminate()
    process.join(timeout=0.3)
    if process.is_alive():
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        except OSError:
            pass
        process.join(timeout=2.0)
    return not process.is_alive()


class ToolExecutor:
    """Runtime-owned executor; no mutable global semaphore lifecycle."""

    def __init__(self, max_parallel: int = 0) -> None:
        self.capacity = max(0, int(max_parallel))
        self.semaphore = threading.Semaphore(self.capacity) if self.capacity else None

    def _can_spawn(self, tool: Tool, args: Dict[str, Any], context: ToolContext) -> bool:
        try:
            pickle.dumps((tool, args, getattr(context, "settings", None)))
            return True
        except Exception:
            return False

    def _run_subprocess(self, tool: Tool, args: Dict[str, Any], context: ToolContext, timeout_sec: float) -> ActionResult | None:
        if not getattr(tool, "supports_hard_cancellation", False) and not getattr(tool, "generated_by_shadow", False):
            return None
        if not self._can_spawn(tool, args, context):
            return None
        mp = multiprocessing.get_context("spawn")
        result_queue = mp.Queue()
        process = mp.Process(target=_process_worker, args=(tool, args, getattr(context, "settings", None), result_queue), name=f"tool-process:{tool.name}")
        try:
            process.start()
        except Exception as exc:
            result_queue.close()
            return ActionResult(tool=tool.name, args=args, ok=False,
                                error=f"Не удалось запустить изолированный worker: {type(exc).__name__}: {exc}",
                                execution_mode="subprocess", side_effects_contained=True)
        try:
            result = result_queue.get(timeout=max(0.1, timeout_sec))
        except queue.Empty:
            killed = _terminate_process(process)
            if not killed:
                # S4: живой процесс после watchdog — утечка хуже потоковой:
                # его не остановит даже завершение агента.
                _register_leak(tool.name, args, mode="subprocess", timeout_sec=timeout_sec,
                               detail=f"процесс pid={process.pid} не остановлен ни terminate, ни taskkill")
            suffix = "" if killed else f"; процесс pid={process.pid} ЖИВ — побочные эффекты НЕ остановлены"
            return ActionResult(tool=tool.name, args=args, ok=False,
                                error=f"Таймаут выполнения: инструмент '{tool.name}' остановлен watchdog{suffix}",
                                duration_sec=timeout_sec, terminated=True,
                                side_effects_contained=killed, execution_mode="subprocess")
        process.join(timeout=1.0)
        if result is None:
            result = ActionResult(tool=tool.name, args=args, ok=False,
                                  error=f"worker завершился с кодом {process.exitcode}",
                                  execution_mode="subprocess", side_effects_contained=True)
        result_queue.close()
        result_queue.join_thread()
        result.execution_mode = "subprocess"
        result.side_effects_contained = True
        return result

    def _run_legacy(self, tool: Tool, args: Dict[str, Any], context: ToolContext, timeout_sec: float) -> ActionResult:
        box: "queue.Queue[ActionResult]" = queue.Queue()
        context.cancel_event.clear()

        def worker() -> None:
            start = time.perf_counter()
            try:
                result = tool.run(args, context)
                if not isinstance(result, ActionResult):
                    result = ActionResult(tool=tool.name, args=args, ok=False, error=f"Инструмент вернул не ActionResult: {type(result)}")
                result.duration_sec = time.perf_counter() - start
                box.put(result)
            except Exception as exc:
                box.put(ActionResult(tool=tool.name, args=args, ok=False, error=f"{type(exc).__name__}: {exc}", duration_sec=time.perf_counter() - start))

        thread = threading.Thread(target=worker, name=f"legacy-tool:{tool.name}", daemon=True)
        thread.start()
        try:
            return box.get(timeout=max(0.1, timeout_sec))
        except queue.Empty:
            context.cancel_event.set()
            thread.join(timeout=_LEGACY_CANCEL_GRACE_SEC)
            contained = not thread.is_alive()
            if not contained:
                # S4: поток проигнорировал cancel_event за отведённую отсрочку.
                # Он daemon, живёт до конца процесса и продолжает менять мир.
                _register_leak(tool.name, args, mode="legacy_thread", timeout_sec=timeout_sec,
                               detail=(f"поток {thread.name} проигнорировал cancel_event "
                                       f"за {_LEGACY_CANCEL_GRACE_SEC:.2f} c и продолжает работу"))
            suffix = "" if contained else "; поток ЖИВ — побочные эффекты НЕ остановлены"
            return ActionResult(tool=tool.name, args=args, ok=False,
                                error=(f"Таймаут выполнения: legacy-инструмент '{tool.name}' "
                                       f"не поддерживает hard cancellation{suffix}"),
                                duration_sec=timeout_sec, terminated=True,
                                side_effects_contained=contained, execution_mode="legacy_thread")

    def run(self, tool: Tool, args: Dict[str, Any], context: ToolContext, timeout_sec: float) -> ActionResult:
        result = self._run_subprocess(tool, args, context, timeout_sec)
        return result if result is not None else self._run_legacy(tool, args, context, timeout_sec)

    def _retry_budget(self, tool: Tool, requested: int) -> int:
        """S4: автоповтор разрешён ТОЛЬКО идемпотентному инструменту.

        Зажим стоит здесь, на границе безопасности, а не у вызывающих: любой
        путь (agent, research_gateway, capability_engine, тест) получает одну
        и ту же политику. Инструмент без паспорта считается неидемпотентным —
        неизвестный побочный эффект нельзя повторять «на всякий случай».
        Ошибка не теряется: она уходит вызывающему и дальше в repair-цикл.
        """
        budget = max(0, int(requested))
        if budget == 0:
            return 0
        passport = _passport(tool.name)
        if passport is not None and bool(getattr(passport, "idempotent", False)):
            return budget
        log.debug(
            "S4: автоповтор подавлен для '%s' (запрошено %d) — паспорт%s идемпотентным; ошибка уйдёт в repair",
            tool.name, budget, " не найден, не считаем" if passport is None else " не помечен",
        )
        return 0

    def execute(self, registry: ToolRegistry, tool_name: str, args: Dict[str, Any], context: ToolContext,
                max_retries: int = 0, retry_delay: float = 0.5, timeout_sec: Optional[float] = None) -> ActionResult:
        tool = registry.get(tool_name)
        if tool is None:
            return ActionResult(tool=tool_name, args=args, ok=False, error=f"Инструмент '{tool_name}' не найден в реестре")
        validation_error = validate_args(tool.input_schema, args)
        if validation_error is not None:
            return ActionResult(tool=tool_name, args=args, ok=False, error=validation_error)
        if timeout_sec is None:
            timeout_sec = tool_timeout_for(tool_name, context)
        max_retries = self._retry_budget(tool, max_retries)
        last_error: Optional[str] = None
        for attempt in range(max_retries + 1):
            acquired = False
            if self.semaphore is not None:
                self.semaphore.acquire()
                acquired = True
            try:
                result = self.run(tool, args, context, float(timeout_sec))
            finally:
                if acquired:
                    self.semaphore.release()
            # S4: watchdog сработал — повтор запрещён вне зависимости от
            # идемпотентности. terminated ставит только executor, инструменты
            # его не выставляют; подстрока остаётся префиксом сообщения, она
            # закреплена в tests/test_sprint3.py:131.
            if result.terminated or (result.error and "Таймаут выполнения" in result.error):
                return result
            if not result.ok and attempt < max_retries:
                last_error = result.error
                time.sleep(max(0.0, retry_delay))
                continue
            result.output = _truncate_output(result.output, context)
            return result
        return ActionResult(tool=tool_name, args=args, ok=False, error=last_error or "Неизвестная ошибка после всех попыток")


def _executor_for(context: ToolContext) -> ToolExecutor:
    limits = getattr(getattr(context, "settings", None), "limits", None)
    capacity = int(getattr(limits, "max_parallel_tools", 0) or 0)
    # БАГ 9/17 FIX: агент владеет ОДНИМ executor'ом на процесс и передаёт
    # его через extra. Если владелец не вложил — fallback на кэш в extra
    # (по одному executor'у на контекст, семафор не разделяется).
    injected = context.extra.get("tool_executor") if isinstance(context.extra, dict) else None
    if isinstance(injected, ToolExecutor):
        return injected
    current = context.extra.get("_tool_executor") if isinstance(context.extra, dict) else None
    if not isinstance(current, ToolExecutor) or current.capacity != max(0, capacity):
        current = ToolExecutor(capacity)
        if isinstance(context.extra, dict):
            context.extra["_tool_executor"] = current
    return current


def validate_args(schema: Dict[str, Any], args: Dict[str, Any]) -> Optional[str]:
    try:
        jsonschema.validate(instance=args, schema=schema)
    except ValidationError as exc:
        path = " -> ".join(str(p) for p in exc.path) if exc.path else "корень"
        return f"Валидация аргументов не прошла ({path}): {exc.message}"
    except Exception as exc:
        return f"Ошибка валидатора: {exc}"
    return None


def execute_tool(registry: ToolRegistry, tool_name: str, args: Dict[str, Any], context: ToolContext,
                 max_retries: int = 0, retry_delay: float = 0.5, timeout_sec: Optional[float] = None) -> ActionResult:
    return _executor_for(context).execute(registry, tool_name, args, context, max_retries, retry_delay, timeout_sec)
