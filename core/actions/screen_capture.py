"""Снимок экрана: разрешение — решение сервера, а не флаг из аргументов.

Аудит S3: схема инструмента требовала ``permission: boolean`` и передавала его
прямо в драйвер. Значит модель (или любой, кто формирует план) могла выдать
разрешение сама себе — «клиент утверждает, что ему можно». Теперь единственный
источник разрешения — грант ``core.authority`` с ``effect=read_screen``,
выданный на реальном подтверждении пользователя.

Флага ``permission`` в схеме больше нет: нечего подделывать.
"""
from __future__ import annotations

from typing import Any, Dict

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.vision.screen import ScreenCapture

# ``core.authority`` и ``core.capabilities`` импортируются внутри функций:
# цепочка core.capabilities -> core.actions -> screen_capture -> core.authority
# -> core.capabilities замыкается в цикл, если тянуть её на уровне модуля.

#: Отказ формулируется одинаково во всех путях — по нему же ориентируется UI.
NEEDS_AUTHORIZATION = "screen capture requires an explicit user authorization"

#: Порядок уровней риска строками — чтобы не тащить enum в момент импорта.
_RISK_ORDER = ("low", "medium", "high", "critical")
#: Паспортный уровень возможности (S1). Ниже него запрос не опускается никогда:
#: иначе занижением риска можно было бы пролезть под чужой низкий ceiling.
_PASSPORT_RISK = "medium"


def _requested_risk(context: ToolContext) -> Any:
    from core.capabilities import RiskLevel

    extra = context.extra if isinstance(context.extra, dict) else {}
    level = str(extra.get("risk_level") or _PASSPORT_RISK).strip().casefold()
    if level not in _RISK_ORDER:
        level = _PASSPORT_RISK
    if _RISK_ORDER.index(level) < _RISK_ORDER.index(_PASSPORT_RISK):
        level = _PASSPORT_RISK
    return RiskLevel(level)


class ScreenCaptureTool(Tool):
    @property
    def name(self) -> str:
        return "screen_capture"

    @property
    def description(self) -> str:
        return (
            "Capture the screen for local OCR. Authorization is decided by the "
            "user through a confirmation; it cannot be requested in arguments. "
            "Never uploads the image."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        # Пустая схема — сознательно: разрешение не является аргументом.
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        from core.authority import screen_capture_request

        extra = context.extra if isinstance(context.extra, dict) else {}
        authority = extra.get("authority")
        if authority is None:
            # Нет хранилища полномочий — нет и разрешения (fail-closed).
            return ActionResult(self.name, args, False, error=NEEDS_AUTHORIZATION)

        request = screen_capture_request(risk=_requested_risk(context))

        def _capture() -> Any:
            return ScreenCapture().capture(permission=True)

        try:
            decision, captured = authority.execute_authorized(request, _capture)
        except Exception as exc:
            return ActionResult(self.name, args, False, error=str(exc))
        if not decision.allowed:
            return ActionResult(self.name, args, False,
                                error=f"{NEEDS_AUTHORIZATION}: {decision.reason}")
        return ActionResult(self.name, args, True, {
            "text": captured.text,
            "active_window": captured.active_window,
            "url": captured.url,
            "grant_id": decision.grant_id,
        })


DEFAULT_REGISTRY.register(ScreenCaptureTool())
