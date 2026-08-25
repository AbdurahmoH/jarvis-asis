"""Сборка системного промпта для LLM.

``build_system_prompt`` комбинирует:
1. Базовую персону (``persona/persona.md``)
2. Профиль пользователя (``get_profile_context``)
3. Найденный контекст памяти (``RetrievedContext``)
в единый system prompt для передачи в LLM.

Sprint 4 добавляет ``build_agent_system_prompt`` — сборку для агентного
WS-пути (``Agent._answer_conversation``): персона по тирам, факты
профиля, тон разговора, время суток, degraded-режим TIER 4.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

from config.settings import Settings
from core.memory.profile import get_profile_context
from core.state import RetrievedContext
from core.utils.logger import get_logger
from core.utils.paths import PROJECT_ROOT

__all__ = [
    "build_system_prompt",
    "load_persona_text",
    "persona_core",
    "build_agent_system_prompt",
    "time_of_day_hint",
]

log = get_logger(__name__)

# Путь к файлу персоны
PERSONA_PATH = PROJECT_ROOT / "persona" / "persona.md"

# Кэш персоны
_PERSONA_CACHE: Optional[str] = None

#: Компактное ядро персоны для FAST-тира (TIER 1 бюджет — 2000 токенов,
#: полный persona.md туда не влезает вместе с историей).
_PERSONA_CORE = (
    "Ты — АТЛАС, единый непрерывный цифровой разум пользователя: точный, "
    "надёжный и профессионально-дружелюбный. В обычном разговоре звучишь "
    "естественно и знакомо, как давний собеседник из Discord, но без навязчивого "
    "театра. Длина, подробность, структура, обращение и уровень юмора задаются "
    "структурированным профилем стиля ниже; следуй ему. Сначала результат и "
    "точность; не выдумывай факты; при ошибке сохраняй спокойствие; при риске "
    "исключай юмор. Прежнее имя «Джарвис» распознавай как совместимый alias, "
    "но представляйся как АТЛАС. Сохраняй один и тот же голос в разговоре, "
    "уточнении, выполнении и ошибке. Не употребляй «сэр» без явной текущей "
    "настройки пользователя. Не начинай каждый ответ с обращения или "
    "подтверждения; не повторяй уже показанный ACK. Результат действия называй "
    "выполненным только при verified=true. Если интеграция известна, но не "
    "настроена, говори именно что она не подключена, без выдуманного отсутствия "
    "возможностей и без внутренних терминов runtime/tool/schema."
)

#: Persona-строка для планировщика (TIER 2) — тон без размытия фокуса на JSON.
_PERSONA_PLANNER = (
    "Ты — АТЛАС (legacy alias: Джарвис): единый когнитивный координатор. Сейчас твоя задача — "
    "точное решение с инструментом: строго валидный JSON, без лишних слов."
)

#: Persona-строка для глубоких тиров (TIER 3).
_PERSONA_DEEP = (
    "Ты — АТЛАС (legacy alias: Джарвис). Задача серьёзная (код/анализ): собранный режим, максимум "
    "структуры и точности; юмор — по остаточному принципу."
)

#: TIER 4 (офлайн, ограниченный режим).
_PERSONA_OFFLINE = (
    "Ты — АТЛАС (legacy alias: Джарвис), работаешь в ограниченном офлайн-режиме. Скажи об этом "
    "коротко и всё равно помоги, чем можешь."
)


def load_persona_text() -> str:
    """Загружает текст персоны из markdown-файла (с кэшированием)."""
    global _PERSONA_CACHE
    if _PERSONA_CACHE is not None:
        return _PERSONA_CACHE

    try:
        text = PERSONA_PATH.read_text(encoding="utf-8")
        _PERSONA_CACHE = text
        log.debug("Персона загружена из %s (%d символов)", PERSONA_PATH, len(text))
        return text
    except Exception as exc:
        log.error("Не удалось загрузить персону из %s: %s", PERSONA_PATH, exc)
        # Фоллбэк — минимальная персона
        fallback = _PERSONA_CORE
        _PERSONA_CACHE = fallback
        return fallback


def persona_core() -> str:
    """Компактное ядро персоны (для быстрых тиров с малым бюджетом)."""
    return _PERSONA_CORE


def time_of_day_hint(now: Optional[_dt.datetime] = None) -> str:
    """Подсказка времени суток для живого приветствия («Доброе утро» и пр.)."""
    hour = (now or _dt.datetime.now()).hour
    if 5 <= hour < 12:
        return "Сейчас утро."
    if 12 <= hour < 18:
        return "Сейчас день."
    if 18 <= hour < 23:
        return "Сейчас вечер."
    return "Сейчас ночь."


def build_agent_system_prompt(
    settings: Settings,
    tier: str = "fast",
    profile_ctx: str = "",
    memory_ctx: str = "",
    tone: str = "default",
    offline: bool = False,
    personality_context: str = "",
) -> str:
    """Собирает system prompt для агентного пути (Sprint 4, STEP 3).

    Персона по тирам (STEP 3.2):
        fast     — максимум персоны (компактное ядро + факты + тон);
        plan     — persona-строка + фокус на точность JSON;
        deep     — persona-строка + собранность;
        offline  — persona + честная пометка об ограниченном режиме.

    Args:
        settings: конфигурация (persona.name/address/language).
        tier: 'fast' | 'plan' | 'deep' | 'offline'.
        profile_ctx: выжимка профиля пользователя (``get_profile_context``).
        memory_ctx: контекст из графа-памяти (agento ``_retrieve_context``).
        tone: 'casual' | 'serious' | 'default' (``core.memory.facts.detect_tone``).
        offline: TIER 4 — офлайн-режим.
    """
    persona_cfg = getattr(settings, "persona", None)
    name = getattr(persona_cfg, "name", "АТЛАС")
    language = getattr(persona_cfg, "language", "ru")

    if tier == "plan":
        return f"{_PERSONA_PLANNER}\nЯзык ответа: {language}."
    if tier == "deep":
        return f"{_PERSONA_DEEP}\nЯзык ответа: {language}."

    parts: list[str] = [_PERSONA_CORE]
    if offline:
        parts.append(_PERSONA_OFFLINE)

    parts.append(f"Язык ответа: {language}.")

    if profile_ctx:
        parts.append(f"Что ты знаешь о пользователе: {profile_ctx}")
    else:
        parts.append(
            "Имени пользователя ты ещё не знаешь — если уместно, можешь "
            "один раз коротко спросить, как его зовут."
        )

    if memory_ctx:
        parts.append(f"Контекст из памяти: {memory_ctx}")

    if tone == "casual":
        parts.append("Пользователь настроен неформально — подыграй, можно подколоть.")
    elif tone == "serious":
        parts.append("Пользователь настроен серьёзно — по делу, юмор минимизируй.")

    if not offline:
        parts.append(time_of_day_hint())

    if personality_context:
        parts.append(personality_context[:1200])

    return "\n".join(parts)


def build_system_prompt(
    settings: Settings,
    retrieved_context: Optional[RetrievedContext] = None,
) -> str:
    """Собирает полный системный промпт.

    Args:
        settings: конфигурация (для имени/обращения).
        retrieved_context: контекст из памяти (профиль, долгая память, документы, граф).

    Returns:
        Готовый system prompt строка.
    """
    parts: list[str] = []

    # 1. Базовая персона
    persona_text = load_persona_text()
    parts.append(persona_text.strip())

    # 2. Имя и обращение из настроек
    persona_name = getattr(getattr(settings, "persona", None), "name", "АТЛАС")
    address = getattr(getattr(settings, "persona", None), "address", "сёр")
    language = getattr(getattr(settings, "persona", None), "language", "ru")

    parts.append(f"\n---\nИмя: {persona_name}. Обращение: «{address}». Язык ответа: {language}.")

    # 3. Профиль пользователя
    if retrieved_context and retrieved_context.get("profile"):
        profile_ctx = retrieved_context["profile"].strip()
        if profile_ctx:
            parts.append(f"\n---\nПрофиль пользователя:\n{profile_ctx}")

    # 4. Долгая память (релевантные факты)
    if retrieved_context and retrieved_context.get("long_term"):
        lt = retrieved_context["long_term"]
        if lt:
            parts.append("\n---\nРелевантные факты из долгой памяти:")
            for i, item in enumerate(lt, 1):
                parts.append(f"  {i}. {item}")

    # 5. Документы (RAG)
    if retrieved_context and retrieved_context.get("documents"):
        docs = retrieved_context["documents"]
        if docs:
            parts.append("\n---\nРелевантные фрагменты из документов:")
            for i, doc in enumerate(docs, 1):
                preview = doc[:300] + ("…" if len(doc) > 300 else "")
                parts.append(f"  [{i}] {preview}")

    # 6. Граф знаний
    if retrieved_context and retrieved_context.get("graph"):
        graph = retrieved_context["graph"]
        if graph:
            parts.append("\n---\nСвязи из графа знаний:")
            for node in graph:
                parts.append(f"  • {node}")

    # 7. Направляющие для ответа
    parts.append(
        "\n---\n"
        "ПРАВИЛА ОТВЕТА:\n"
        "1. Обращайся «сёр».\n"
        "2. Отвечай кратко (1-3 предложения или список). Никаких эссе.\n"
        "3. Используй контекст выше — не выдумывай факты.\n"
        "4. Если не знаешь — скажи «не знаю, сёр» или «проверю».\n"
        "5. Предупреждай перед опасными действиями.\n"
        "6. Можешь отказать, если запрос небезопасен или абсурден.\n"
        "7. Юмор сухой, по месту. Не спамь."
    )

    return "\n".join(parts)
