"""Understanding Layer — единая точка понимания пользовательского ввода.

Один классификатор для консоли, WebSocket и фронта вместо рассогласованной
тройки needsMission() / intent_router / _should_run_background. Контракт:
на любой ввод — маршрут {reflex | quick_answer | action | mission |
clarify} с обоснованием; молчания и canned-шаблонов нет.

Пример:
    >>> from core.understanding import UnderstandingLayer
    >>> layer = UnderstandingLayer()
    >>> layer.understand("открой блокнот").route.value
    'action'
    >>> layer.understand("что такое энтропия?").route.value
    'quick_answer'
"""

from __future__ import annotations

from core.understanding.layer import UnderstandingLayer
from core.understanding.models import Route, Understanding
from core.understanding.quick_answer import QuickAnswerEngine, QuickAnswerResult
from core.understanding.semantic import (
    NaturalMissionCoordinator,
    NaturalMissionInterpreter,
    NaturalMissionResult,
    SemanticInterpretation,
    SemanticMode,
)

__all__ = [
    "UnderstandingLayer",
    "Route",
    "Understanding",
    "QuickAnswerEngine",
    "QuickAnswerResult",
    "NaturalMissionCoordinator",
    "NaturalMissionInterpreter",
    "NaturalMissionResult",
    "SemanticInterpretation",
    "SemanticMode",
]
