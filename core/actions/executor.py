"""Validated, bounded and cancellable tool execution."""
from __future__ import annotations

import multiprocessing
import pickle
import queue
import subprocess
import threading
import time
from typing import Any, Dict, Optional

import jsonschema
from jsonschema import ValidationError

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import ToolRegistry
from core.utils.logger import get_logger

__all__ = ["ToolExecutor", "execute_tool", "validate_args", "tool_timeout_for"]

log = get_logger(__name__)

_WEB_TOOLS = frozenset({
    "web_search", "web_fetch", "weather",
    "browser_bridge", "browser_automation",
    "browser_open", "browser_click", "browser_type", "browser_scroll",
    "browser_close", "browser_screenshot",
})
_SYSTEM_TOOLS = frozenset({"system_status", "volume", "open_app", "close_app", "add_reminder", "list_reminders", "cancel_reminder", "current_time", "play_music", "screenshot", "clipboard_read", "clipboard_write", "key_press", "type_text", "screen_capture"})


def tool_timeout_for(tool_name: str, context: ToolContext) -> float:
    limits = getattr(getattr(context, "settings", None), "limits", None)
    if tool_name in _WEB_TOOLS:
        return float(getattr(limits, "tool_timeout_web_sec", 30.0))
    if tool_name in _SYSTEM_TOOLS:
        return float(getattr(limits, "tool_timeout_system_sec", 5.0))
    return float(getattr(limits, "tool_timeout_file_sec", 10.0))


def _truncate_output(output: Any, context: ToolContext) -> Any:
    if not isinstance(output, str):
        return output
    cap = int(getattr(getattr(getattr(context, "settings", None), "limits", None), "tool_output_max_bytes", 50 * 1024) or 0)
    if cap <= 0 or len(output.encode("utf-8", errors="replace")) <= cap:
        return output
    text = output.encode("utf-8", errors="replace")[:cap].decode("utf-8", errors="ignore")
    return f"{text}\n… [вывод усечён: {len(output)} символов, потолок {cap} байт — resource limit]"


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
            terminated = _terminate_process(process)
            return ActionResult(tool=tool.name, args=args, ok=False,
                                error=f"Таймаут выполнения: инструмент '{tool.name}' остановлен watchdog",
                                duration_sec=timeout_sec, terminated=terminated,
                                side_effects_contained=terminated, execution_mode="subprocess")
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
            thread.join(timeout=0.25)
            contained = not thread.is_alive()
            return ActionResult(tool=tool.name, args=args, ok=False,
                                error=f"Таймаут выполнения: legacy-инструмент '{tool.name}' не поддерживает hard cancellation",
                                duration_sec=timeout_sec, terminated=contained,
                                side_effects_contained=contained, execution_mode="legacy_thread")

    def run(self, tool: Tool, args: Dict[str, Any], context: ToolContext, timeout_sec: float) -> ActionResult:
        result = self._run_subprocess(tool, args, context, timeout_sec)
        return result if result is not None else self._run_legacy(tool, args, context, timeout_sec)

    def execute(self, registry: ToolRegistry, tool_name: str, args: Dict[str, Any], context: ToolContext,
                max_retries: int = 2, retry_delay: float = 0.5, timeout_sec: Optional[float] = None) -> ActionResult:
        tool = registry.get(tool_name)
        if tool is None:
            return ActionResult(tool=tool_name, args=args, ok=False, error=f"Инструмент '{tool_name}' не найден в реестре")
        validation_error = validate_args(tool.input_schema, args)
        if validation_error is not None:
            return ActionResult(tool=tool_name, args=args, ok=False, error=validation_error)
        if timeout_sec is None:
            timeout_sec = tool_timeout_for(tool_name, context)
        last_error: Optional[str] = None
        for attempt in range(max(0, int(max_retries)) + 1):
            acquired = False
            if self.semaphore is not None:
                self.semaphore.acquire()
                acquired = True
            try:
                result = self.run(tool, args, context, float(timeout_sec))
            finally:
                if acquired:
                    self.semaphore.release()
            if result.error and "Таймаут выполнения" in result.error:
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
                 max_retries: int = 2, retry_delay: float = 0.5, timeout_sec: Optional[float] = None) -> ActionResult:
    return _executor_for(context).execute(registry, tool_name, args, context, max_retries, retry_delay, timeout_sec)
