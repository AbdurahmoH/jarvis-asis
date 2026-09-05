"""Состояние графа Джарвиса — единый контракт между всеми узлами.

``JarvisState`` передаётся по цепочке узлов оркестратора (Часть 2):

    intake -> council_route -> memory_retrieve -> plan -> action_loop
           -> generate -> output

Каждый узел получает состояние, дописывает свои поля и возвращает его.
Правило: узлы НЕ удаляют чужие поля и НЕ меняют типы — иначе следующие
модули (router / memory / actions / voice / hud) сломаются.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, TypedDict

__all__ = [
    "Message",
    "PlanStep",
    "ActionResult",
    "RetrievedContext",
    "JarvisState",
    "new_state",
    "utc_timestamp",
    "push_message",
    "trim_short_memory",
]

Role = Literal["system", "user", "assistant", "tool"]


class Message(TypedDict, total=False):
    """Сообщение диалога (совместимо с форматом OpenAI chat messages)."""

    role: Role
    content: str
    name: str          # опционально: имя инструмента для role="tool"
    timestamp: str     # ISO-8601 UTC


class PlanStep(TypedDict, total=False):
    """Шаг плана, сформированного планировщиком (Часть 4)."""

    index: int
    description: str          # что нужно сделать, человекочитаемо
    tool: Optional[str]       # имя инструмента из реестра или None (=просто рассуждение)
    args: Dict[str, Any]      # аргументы инструмента
    status: Literal["pending", "running", "done", "failed", "skipped"]


class ActionResult(TypedDict, total=False):
    """Результат выполнения одного инструмента Action Engine."""

    tool: str
    args: Dict[str, Any]
    ok: bool
    output: str               # текстовый результат для подстановки в промпт
    error: Optional[str]
    duration_sec: float
    step_index: Optional[int]


class RetrievedContext(TypedDict, total=False):
    """Контекст, собранный слоями памяти (Часть 3).

    Ключи независимы: если слой недоступен, он просто отсутствует.
    """

    profile: str                     # выжимка profile.json
    long_term: List[str]             # факты из ChromaDB
    documents: List[str]             # фрагменты RAG по файлам
    graph: List[str]                 # узлы knowledge graph
    time_context: str                # дата/время/локация
    persona: str                     # текст persona.md


class JarvisState(TypedDict, total=False):
    """Полное состояние одного витка обработки запроса.

    total=False: узлы дописывают поля постепенно, а не все сразу.
    ``new_state()`` создаёт состояние со всеми ключами и безопасными
    значениями по умолчанию — используйте её, чтобы не проверять наличие
    ключей в каждом узле.
    """

    user_input: str                        # текст пользователя (микрофона нет)
    intent: Optional[str]                  # категория от keyword-роутера: app/media/web/...
    executive: Dict[str, Any]              # bounded Executive Mind snapshot
    tier: str                              # выбранный тир совета: fast/analyst/coder/architect
    short_memory: List[Message]            # последние N сообщений (ОЗУ)
    retrieved_context: RetrievedContext    # что подняли слои памяти
    plan: Optional[List[PlanStep]]         # план действий или None
    action_results: List[ActionResult]      # результаты инструментов
    response: Optional[str]                # финальный текст ответа
    tts_text: Optional[str]                # текст для озвучки (может быть короче response)
    assistant_output: Dict[str, Any]       # typed display/speech/debug/error contract
    timestamp: str                         # ISO-8601 UTC начала витка
    error: Optional[str]                   # человекочитаемая ошибка витка

    # --- служебные поля (не входят в обязательный контракт) ---
    source: str                            # 'user' | 'proactive' | 'reminder'
    escalations: List[str]                  # история эскалаций тиров за виток
    latency: Dict[str, float]               # тайминги узлов, сек
    task_contract: Dict[str, Any]           # normalized Universal Intake contract
    evidence: List[Dict[str, Any]]          # bounded claim/action evidence
    latency_budget: Dict[str, Any]          # active numerical budget metadata
    tool: str                               # selected tool, if any
    verified: bool                          # independent verification result
    mode: str                               # active execution mode


def utc_timestamp() -> str:
    """Текущее время в ISO-8601 UTC (с точностью до секунд)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_state(
    user_input: str,
    short_memory: Optional[List[Message]] = None,
    source: str = "user",
    tier: str = "fast",
) -> JarvisState:
    """Создаёт состояние с заполненными значениями по умолчанию.

    Args:
        user_input: текст пользователя (или текст-триггер для проактивного витка).
        short_memory: краткая память диалога; список НЕ копируется по значению —
            передавайте копию, если не хотите, чтобы узлы его мутировали.
        source: кто инициировал виток ('user' / 'proactive' / 'reminder').
        tier: стартовый тир (по умолчанию лицо Джарвиса — 'fast').
    """
    state = JarvisState(
        user_input=user_input,
        intent=None,
        executive={},
        tier=tier,
        short_memory=list(short_memory) if short_memory else [],
        retrieved_context=RetrievedContext(),
        plan=None,
        action_results=[],
        response=None,
        tts_text=None,
        assistant_output={},
        timestamp=utc_timestamp(),
        error=None,
        source=source,
        escalations=[],
        latency={},
        task_contract={},
        evidence=[],
        latency_budget={},
        tool="",
        verified=False,
        mode="conversation",
    )
    # (R2, позиция 2) keyword-классификатор resolve_keyword_tool удалён из
    # построения состояния: intent в живом пути проставляется оркестратором
    # из решения semantic-роутера (semantic_intent_category(decision)).
    return state


def push_message(state: JarvisState, role: Role, content: str,
                 name: Optional[str] = None, limit: Optional[int] = None) -> None:
    """Добавляет сообщение в краткую память состояния (мутирует state).

    Args:
        limit: если задан — краткая память сразу обрезается до ``limit``
            последних сообщений (``settings.limits.short_memory_size``).
    """
    message: Message = {"role": role, "content": content, "timestamp": utc_timestamp()}
    if name:
        message["name"] = name
    memory = state.setdefault("short_memory", [])
    memory.append(message)
    if limit is not None:
        trim_short_memory(state, limit)


def trim_short_memory(state: JarvisState, limit: int) -> None:
    """Обрезает краткую память до ``limit`` последних сообщений (мутирует state)."""
    if limit <= 0:
        state["short_memory"] = []
        return
    memory = state.get("short_memory") or []
    if len(memory) > limit:
        state["short_memory"] = memory[-limit:]
