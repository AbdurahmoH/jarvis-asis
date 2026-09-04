"""Repair Loop — самоисправление J.A.R.V.I.S. (§8, §9).

Если J.A.R.V.I.S. написал код и он упал — НЕ сдавайся сразу. Цикл:

    EXECUTE -> ERROR -> READ ERROR -> DIAGNOSE -> PATCH -> RETRY -> VERIFY

Возможности repair loop (§9):
    * повторить;
    * изменить аргументы;
    * выбрать другой инструмент (fallback);
    * искать документацию / web;
    * создать новый skill;
    * откатить частично сделанное;
    * обратиться к пользователю, если нужно решение человека.

Количество попыток ограничено политикой безопасности, но НЕ равняется одному.

Модуль чистый: не зависит от тяжёлых LLM. Диагностика — эвристики по
тексту ошибки + опциональный LLM-callback (``reasoner``) для генерации
патча аргументов. Каждый шаг публикуется в Mission (события repairing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.actions.base import ActionResult, ToolContext
from core.actions.executor import execute_tool
from core.actions.registry import ToolRegistry
from core.task_runtime import EVENT_ERROR, Mission, MissionStatus
from core.redact import redact_args
from core.utils.logger import get_logger

__all__ = ["RepairResult", "RepairLoop", "diagnose_error"]

log = get_logger(__name__)


# Reasoner — опциональный LLM-колбэк: (error_text, args, context) -> новые args
Reasoner = Callable[[str, Dict[str, Any], ToolContext], Optional[Dict[str, Any]]]

#: Risk gate — (tool, args) -> причина блокировки или None, если вызов разрешён.
#: Sprint 3 STEP 4: КАЖДАЯ повторная попытка repair (патч аргументов,
#: fallback-инструмент, повтор) проходит тот же риск-гейт, что и первый
#: вызов (§21). Переформулировка НЕ должна обходить подтверждение.
RiskGate = Callable[[str, Dict[str, Any]], Optional[str]]


@dataclass
class RepairResult:
    """Итог repair loop для одного инструмента."""

    ok: bool
    attempts: int
    final_result: Optional[ActionResult] = None
    trace: List[str] = field(default_factory=list)
    escalated_to_skill_forge: bool = False
    needs_human: bool = False
    human_message: str = ""


def diagnose_error(error_text: str) -> str:
    """Эвристическая классификация ошибки по тексту (для выбора патча)."""
    e = (error_text or "").lower()
    if "not found" in e or "no such file" in e or "файл не найден" in e or "не найден" in e:
        return "path_not_found"
    if "permission" in e or "access is denied" in e or "отказано" in e:
        return "permission_denied"
    if "timed out" in e or "timeout" in e or "время ожидания" in e:
        return "timeout"
    if "validation" in e or "schema" in e or "валидац" in e:
        return "invalid_args"
    if "not a directory" in e or "is a directory" in e:
        return "path_is_directory"
    if "traceback" in e or "error" in e:
        return "generic_error"
    return "unknown"


# Категория ошибки -> нужно ли человеческое подтверждение (HIGH risk §27).
_HUMAN_REQUIRED = {"permission_denied"}


class RepairLoop:
    """Цикл самоисправления для одного вызова инструмента (§8, §9)."""

    def __init__(
        self,
        registry: ToolRegistry,
        reasoner: Optional[Reasoner] = None,
        fallback_tools: Optional[Dict[str, List[str]]] = None,
        max_attempts: int = 3,
    ) -> None:
        """
        Args:
            registry: реестр инструментов.
            reasoner: опциональный LLM-колбэк для генерации патча аргументов.
            fallback_tools: отображение tool -> список fallback-инструментов.
            max_attempts: максимум попыток (НЕ 1, по §8; по умолчанию 3).
        """
        self._registry = registry
        self._reasoner = reasoner
        self._fallback = fallback_tools or {}
        self._max = max(1, int(max_attempts))

    def run(
        self,
        tool_name: str,
        args: Dict[str, Any],
        context: ToolContext,
        mission: Optional[Mission] = None,
        verification: Optional[Callable[[ActionResult], bool]] = None,
        risk_gate: Optional[RiskGate] = None,
    ) -> RepairResult:
        """Прогоняет EXECUTE -> ERROR -> DIAGNOSE -> PATCH -> RETRY -> VERIFY.

        Args:
            tool_name: имя стартового инструмента.
            args: аргументы.
            context: контекст выполнения.
            mission: опциональная Mission для публикации событий (repairing).
            verification: опциональный предикат "результат успешно проверен".
            risk_gate: опциональный повторный риск-гейт (Sprint 3 STEP 4):
                ``(tool, args) -> причина блокировки | None``. Вызывается
                перед КАЖДОЙ попыткой (включая патч/fallback); HIGH-risk
                повтор останавливает цикл с запросом решения человека.

        Returns:
            RepairResult со следом действий.
        """
        trace: List[str] = []
        last_result: Optional[ActionResult] = None
        current_tool = tool_name
        current_args = dict(args)
        mission_id = getattr(mission, "task_id", "-")

        if mission is not None and mission.status.is_active:
            mission.set_status(MissionStatus.REPAIRING, "вход в repair loop")

        for attempt in range(1, self._max + 1):
            # ---- Sprint 3 STEP 4: повторный риск-гейт ----
            if risk_gate is not None:
                try:
                    blocked_reason = risk_gate(current_tool, current_args)
                except Exception as exc:  # гейт не должен ронять repair
                    log.warning("risk_gate упал (пропускаю проверку): %s", exc)
                    blocked_reason = None
                if blocked_reason:
                    trace.append(
                        f"RISK GATE: попытка {attempt} заблокирована — {blocked_reason}"
                    )
                    log.warning(
                        "Repair [%s] попытка %d/%d заблокирована риск-гейтом: %s",
                        mission_id, attempt, self._max, blocked_reason,
                    )
                    return RepairResult(
                        ok=False, attempts=attempt, final_result=last_result,
                        trace=trace, needs_human=True,
                        human_message=(
                            "Первая попытка не удалась, но повторение (с изменёнными "
                            f"аргументами или другим инструментом) заблокировано контролём "
                            f"безопасности: {blocked_reason}. Требуется ваше решение."
                        ),
                    )

            if mission is not None:
                mission.emit(EVENT_ERROR if attempt == 1 else EVENT_ERROR,
                             payload={"repair_attempt": attempt, "tool": current_tool})
            log.info("Repair [%d/%d] [%s]: %s(%s)", attempt, self._max, mission_id,
                     current_tool, redact_args(current_args))

            # Sprint 3: repair loop САМ является механизмом повторов —
            # executor-retry внутри попытки умножал бы вызовы (3 repair ×
            # 3 retry = 9 обращений к падающему инструменту).
            result = execute_tool(self._registry, current_tool, current_args,
                                  context, max_retries=0)
            last_result = result
            trace.append(f"попытка {attempt}: {current_tool}({current_args}) -> ok={result.ok}")

            # Успех? Проверяем ФАКТИЧЕСКИ даже при ok=True (§14: «готово» только
            # после настоящей верификации, а не просто отсутствия исключения).
            if result.ok:
                verified = True
                if verification is not None:
                    try:
                        verified = bool(verification(result))
                    except Exception as exc:
                        log.warning("verification упал: %s", exc)
                        verified = True
                if verified:
                    trace.append("успех подтверждён")
                    return RepairResult(ok=True, attempts=attempt, final_result=result, trace=trace)
                trace.append("ok=True, но фактическая проверка не прошла — продолжаем repair")

            # Неудача: диагностируем
            diag = diagnose_error(result.error or "")
            trace.append(f"диагноз: {diag} ({result.error})")

            if diag in _HUMAN_REQUIRED:
                # Требуется решение человека (HIGH risk §27)
                return RepairResult(
                    ok=False, attempts=attempt, final_result=result, trace=trace,
                    needs_human=True,
                    human_message=(
                        f"Не удалось выполнить '{current_tool}': {result.error}. "
                        f"Требуется ваше решение (недостаточно прав / подтверждение)."
                    ),
                )

            # 1) Пробуем LLM-патч аргументов
            if self._reasoner is not None and attempt < self._max:
                try:
                    patched = self._reasoner(result.error or "", current_args, context)
                except Exception as exc:
                    log.warning("reasoner упал: %s", exc)
                    patched = None
                if patched:
                    current_args = patched
                    trace.append(f"LLM-патч аргументов: {patched}")
                    continue

            # 2) Эвристический патч путей (path_not_found)
            if diag == "path_not_found" and attempt < self._max:
                patched = self._heal_path_args(current_args)
                if patched != current_args:
                    current_args = patched
                    trace.append(f"эвристический патч пути: {patched}")
                    continue

            # 3) Fallback-инструмент
            if attempt < self._max:
                switched = False
                for fb_tool in self._fallback.get(current_tool, []):
                    if fb_tool != current_tool and fb_tool in self._registry:
                        adapted = self._adapt_args(current_tool, fb_tool, current_args)
                        if adapted is None:
                            # Аргументы не переносятся на этот инструмент —
                            # слепой вызов гарантированно провалит валидацию.
                            trace.append(
                                f"fallback '{fb_tool}' пропущен: аргументы {list(current_args)} "
                                f"не отображаются на его схему"
                            )
                            continue
                        current_tool = fb_tool
                        current_args = adapted
                        trace.append(
                            f"переключение на fallback-инструмент: {fb_tool}({adapted})"
                        )
                        switched = True
                        break
                if switched:
                    continue

            # Исчерпали этот такт — если ещё есть попытки, повторяем как есть.
            if attempt < self._max:
                if diag in {"invalid_args", "path_not_found", "path_is_directory"}:
                    trace.append("детерминированная ошибка без нового патча — повтор тех же аргументов остановлен")
                    return RepairResult(
                        ok=False, attempts=attempt, final_result=last_result, trace=trace,
                    )
                trace.append("повтор без изменений")
                continue

        # Все попытки исчерпаны
        trace.append("все попытки исчерпаны")
        return RepairResult(
            ok=False, attempts=self._max, final_result=last_result, trace=trace,
            escalated_to_skill_forge=False,
        )

    def _adapt_args(self, from_tool: str, to_tool: str,
                    args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Переносит аргументы на схему fallback-инструмента (§11).

        Слепо передавать аргументы другому инструменту нельзя: у него другая
        JSON Schema, и вызов провалится на валидации, впустую сжигая попытку.

        Стратегия:
            1. общие ключи переносим как есть;
            2. недостающие обязательные ключи пытаемся заполнить осмысленным
               значением из исходных аргументов (напр. name -> query);
            3. если обязательный ключ заполнить нечем — возвращаем None
               (fallback пропускается).

        Returns:
            Новый словарь аргументов или ``None``, если перенос невозможен.
        """
        tool = self._registry.get(to_tool)
        if tool is None:
            return None
        schema = getattr(tool, "input_schema", {}) or {}
        props: Dict[str, Any] = schema.get("properties", {}) or {}
        required: List[str] = list(schema.get("required", []) or [])

        adapted: Dict[str, Any] = {k: v for k, v in args.items() if k in props}

        # Текстовое значение из исходных аргументов — кандидат для query/поиска.
        text_value = ""
        for key in ("query", "name", "path", "url", "text"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                text_value = val.strip()
                break

        for key in required:
            if key in adapted:
                continue
            if key in ("query", "text", "name") and text_value:
                adapted[key] = text_value
            elif key == "path" and text_value and not text_value.startswith("http"):
                adapted[key] = text_value
            elif key == "url" and text_value.startswith("http"):
                adapted[key] = text_value
            else:
                # Обязательный аргумент заполнить нечем — fallback бесполезен.
                return None

        return adapted

    @staticmethod
    def _heal_path_args(args: Dict[str, Any]) -> Dict[str, Any]:
        """Эвристика: пытается «починить» аргументы с путями.

        - раскрывает ~ и переменные среды;
        - для относительных путей оставляет как есть (executor разрешит по проекту).

        ВАЖНО (безопасность): mkdir намеренно убран. Создание директорий —
        это side effect с потенциальным выходом за пределы scope. Если
        родительская директория отсутствует — это ошибка «путь недоступен»,
        а не то, что repair loop должен молча исправлять. Создание директорий
        разрешено только явным инструментом внутри documents_dir scope.
        """
        healed = dict(args)
        for key, val in list(args.items()):
            if not isinstance(val, str):
                continue
            if "\\" not in val and "/" not in val:
                continue
            p = Path(val).expanduser()
            if p.is_absolute():
                # Раскрываем переменные окружения, но не создаём директории.
                expanded = str(p)
                if expanded != val:
                    healed[key] = expanded
            # относительный — оставляем как есть (executor разрешит по проекту)
        return healed
