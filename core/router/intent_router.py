"""Быстрый keyword-роутер намерений (без обращения к LLM).

Задача: по ключевым словам отнести пользовательский запрос к одной из
категорий, чтобы «совет мудрецов» знал, какой обработчик/тир уместен.

Категории:
    * ``app``      — управление программами (открой/закрой/запусти)
    * ``media``    — музыка, видео, плеер
    * ``browser``  — открыть сайт, погуглить, работа в браузере
    * ``system``   — громкость, статус, питание, яркость
    * ``web``      — поиск информации, объяснения, вопросы
    * ``file``     — работа с файлами и документами
    * ``none``     — ничего не подошло (общий диалог / сложный запрос)

Работает мгновенно и офлайн. Не бросает исключений — на любой ввод
возвращает валидную категорию.
"""

from __future__ import annotations

import re
from typing import Dict, List

__all__ = ["resolve_keyword_tool", "split_compound_commands", "INTENT_NONE", "INTENT_APP", "INTENT_MEDIA",
           "INTENT_BROWSER", "INTENT_SYSTEM", "INTENT_WEB", "INTENT_FILE"]


# Константы категорий — чтобы не ловить опечатки по всему коду.
INTENT_APP = "app"
INTENT_MEDIA = "media"
INTENT_BROWSER = "browser"
INTENT_SYSTEM = "system"
INTENT_WEB = "web"
INTENT_FILE = "file"
INTENT_NONE = "none"


#: Приоритет проверки. Более специфичные категории идут первыми, чтобы
#: «открой файл» не попал в ``app``, а «открой сайт» — в ``browser``, а не в ``app``.
_PRIORITY: List[str] = [
    INTENT_FILE,
    INTENT_MEDIA,
    INTENT_SYSTEM,
    INTENT_BROWSER,
    INTENT_APP,
    INTENT_WEB,
]

_MEDIA_ACTION_MARKERS = (
    "поставь музыку", "поставь трек", "поставь песню", "включи музыку",
    "включи трек", "включи песню", "проиграй", "play music", "play track",
)
_BROWSER_ACTION_MARKERS = (
    "открой ютуб", "открой youtube", "открой сайт", "открой браузер",
    "перейди на сайт", "открой вкладку", "open youtube", "open site",
)
_COMPOUND_SEPARATOR = re.compile(r"\s+(?:и|and|затем|потом|после этого)\s+", re.IGNORECASE)

#: Ключевые слова (русские + английские) в нижнем регистре. Достаточно
#: вхождения любого слова из списка, чтобы отнести запрос к категории.
_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    INTENT_FILE: [
        "файл", "документ", "прочитай", "запиши", "сохрани", "открой файл",
        "открыть файл", "текстовый файл", "pdf", "excel", "word", "каталог",
        "папку", "папка", "директори", "file", "document", "read file",
        "save file", "open file",
    ],
    INTENT_BROWSER: [
        "сайт", "браузер", "в браузере", "открой сайт", "открыть сайт",
        "вкладк", "гугл", "google", "погугли", "загугли", "ютуб", "youtube",
        "интернете", "browser", "open site", "new tab", "tab",
    ],
    INTENT_MEDIA: [
        "музык", "трек", "песн", "видео", "ютуб", "youtube", "плеер",
        "spotify", "включи музыку", "поставь трек", "поставь песню",
        "музыку", "музыка", "music", "song", "video", "play track",
    ],
    INTENT_SYSTEM: [
        "громкость", "звук", "тише", "громче", "выключи звук", "без звука",
        "статус систем", "системный статус", "статус компьютера", "состояние системы",
        "память компьютера", "батаре", "аккумулятор",
        "перезагруз", "выключи компьютер", "яркость", "микрофон", "камер",
        "wifi", "процессор", "cpu", "диск", "volume", "mute", "shutdown",
        "reboot", "brightness", "который час", "который сейчас час", "сколько времени",
        "сколько сейчас времени", "текущее время", "точное время", "какой час",
        "какой сейчас час", "какое время", "какое сейчас время",
        "время", "дата", "time", "clock",
    ],
    INTENT_APP: [
        "открой", "открыть", "запусти", "запустить", "закрой", "закрыть",
        "включи программу", "приложение", "программ", "open", "launch",
        "close app", "start app",
    ],
    INTENT_WEB: [
        "найди", "поищи", "поиск", "что такое", "кто так", "почему", "зачем",
        "как работает", "расскажи о", "объясни", "узнай", "проверь", "search",
        "lookup", "find", "what is", "who is", "explain", "tell me about",
    ],
}


def resolve_keyword_tool(query: str, raw_query: str | None = None) -> str:
    """Определяет категорию запроса по ключевым словам.

    Args:
        query: нормализованный текст запроса (допускается пустая строка).
        raw_query: исходный текст пользователя «как есть». Если не передан —
            используется ``query``. Совпадение ищется по ``raw_query`` (с учётом
            регистра, приводим к нижнему).

    Returns:
        Одна из констант: ``app`` / ``media`` / ``browser`` / ``system`` /
        ``web`` / ``file`` / ``none``. Никогда не бросает исключений.
    """
    text = (raw_query if raw_query else query) or ""
    lowered = " ".join(text.casefold().split())

    # Action verbs disambiguate overlapping vocabulary: "открой YouTube"
    # is browser navigation, while "поставь музыку на YouTube" is media.
    if any(marker in lowered for marker in _MEDIA_ACTION_MARKERS):
        return INTENT_MEDIA
    if any(marker in lowered for marker in _BROWSER_ACTION_MARKERS):
        return INTENT_BROWSER

    for category in _PRIORITY:
        for keyword in _CATEGORY_KEYWORDS[category]:
            if keyword.casefold() in lowered:
                return category
    return INTENT_NONE


def split_compound_commands(query: str) -> List[str]:
    """Return independent action clauses, or an empty list for normal speech.

    A comma alone is deliberately not a separator: phrases such as
    ``"поставь музыку, настроения нет"`` are one request.  Only an explicit
    conjunction plus two recognizable action clauses becomes a batch.
    """
    clean = " ".join((query or "").strip().split())
    if not clean:
        return []
    parts = [part.strip(" ,;:") for part in _COMPOUND_SEPARATOR.split(clean) if part.strip(" ,;:")]
    if len(parts) < 2:
        return []
    categories = [resolve_keyword_tool(part, part) for part in parts]
    actionable = [category for category in categories if category != INTENT_NONE]
    if len(actionable) < 2:
        return []
    return parts
