"""C4: единственный переводчик сбоев модели в человеческие фразы.

Правило наряда: raw-текст исключения (ProviderUnavailable, NoRouteAvailable,
DeepInfra, capability discovery failed, любые traceback) — только в
trace/diagnostics/логи, НИКОГДА в ответ пользователю. Классификация по тексту
исключения устойчива к цепочкам вида
``BackendUnavailable: ... (deepinfra:ProviderUnavailable)``.

Использование: ``user_message_for(exc)`` принимает исключение или его текст.
"""
from __future__ import annotations

TRANSIENT = "transient"
AUTH = "auth"
RATE_LIMIT = "rate_limit"
NO_KEY = "no_key"
UNKNOWN = "unknown"

#: Фразы наряда C4. Пользователь видит только их; детали — в trace.
MESSAGES = {
    TRANSIENT: (
        "Секунду, связь пропала. Что-то простое могу сделать — открыть "
        "приложение, поставить напоминание, громкость."
    ),
    AUTH: "Ключ API не принят сервисом. Свяжись со мной, чтобы обновить сборку.",
    RATE_LIMIT: "Сервис ограничил запросы, через минуту попробую снова.",
    NO_KEY: "Ключ API не настроен в этой сборке. Свяжись со мной.",
    UNKNOWN: "Не получилось. Попробуй переформулировать или повторить.",
}

#: Подстроки (в нижнем регистре), по которым классифицируется сбой.
_AUTH_MARKERS = ("401", "unauthorized", "invalid api key", "invalid key",
                 "authentication", "auth failure", "ключ не принят")
_RATE_MARKERS = ("429", "rate limit", "too many requests")
_NO_KEY_MARKERS = ("credential is unavailable", "no api key", "missing api key",
                   "api key is not configured", "нет api-ключа", "нет ключа",
                   "api_key_ref")
_TRANSIENT_MARKERS = ("timed out", "timeout", "time-out", "connection", "unavailable",
                      "unreachable", "providererror", "backendunavailable",
                      "all routed providers", "no healthy provider", "norouteavailable",
                      "providerunavailable", "connection reset", "temporarily",
                      "502", "503", "504", "сеть", "связь",
                      "недоступен", "недоступна", "таймаут", "исчерпаны",
                      "превышен лимит времени")

#: Запрещённые к показу пользователю подстроки — закреплены тестом.
FORBIDDEN_IN_OUTPUT = (
    "ProviderUnavailable", "NoRouteAvailable", "DeepInfra", "DeepSeek runtime",
    "capability discovery", "BackendUnavailable", "ProviderResponseError",
    "Traceback", "Exception", "HTTP 4", "HTTP 5",
)


def classify(error: BaseException | str) -> str:
    """Категория сбоя по тексту исключения (цепочки разворачиваются целиком)."""
    if isinstance(error, BaseException):
        parts = [str(error)]
        current: BaseException | None = error
        while current is not None:
            cause = current.__cause__ or current.__context__
            if cause is not None and cause is not current:
                parts.append(str(cause))
            current = cause
        text = " | ".join(parts)
    else:
        text = str(error)
    lowered = text.casefold()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return AUTH
    if any(marker in lowered for marker in _RATE_MARKERS):
        return RATE_LIMIT
    if any(marker in lowered for marker in _NO_KEY_MARKERS):
        return NO_KEY
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return TRANSIENT
    return UNKNOWN


def user_message_for(error: BaseException | str) -> str:
    """Человеческая фраза для пользователя; raw-детали остаются вызывающему."""
    return MESSAGES[classify(error)]


__all__ = [
    "classify", "user_message_for", "MESSAGES", "FORBIDDEN_IN_OUTPUT",
    "TRANSIENT", "AUTH", "RATE_LIMIT", "NO_KEY", "UNKNOWN",
]
