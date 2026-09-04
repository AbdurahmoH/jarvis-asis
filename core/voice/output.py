"""Typed boundary between assistant results, UI diagnostics and speech."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from core.llm.backend import strip_reasoning_blocks


class ErrorCategory(str, Enum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NETWORK_PROBLEM = "network_problem"
    MODEL_FAILURE = "model_failure"
    TOOL_FAILURE = "tool_failure"
    PERMISSION_REQUIRED = "permission_required"
    TTS_FAILURE = "tts_failure"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass(frozen=True)
class ErrorInfo:
    category: ErrorCategory
    technical_message: str = ""
    recovered: bool = False
    provider: Optional[str] = None
    request_id: Optional[str] = None
    status_code: Optional[int] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data


@dataclass(frozen=True)
class AssistantOutput:
    display_text: str
    speech_text: Optional[str] = None
    debug: Optional[dict[str, Any]] = None
    error: Optional[ErrorInfo] = None
    speak: bool = True
    speech_mode: str = "normal"

    @property
    def text(self) -> str:
        return self.speech_text or self.display_text or ""

    @classmethod
    def natural(cls, text: str, *, speech_mode: str = "normal",
                debug: Optional[dict[str, Any]] = None) -> "AssistantOutput":
        return cls(text, text, debug=debug, speech_mode=speech_mode)

    @classmethod
    def failure(cls, *, display_text: str, error: ErrorInfo,
                debug: Optional[dict[str, Any]] = None) -> "AssistantOutput":
        # Technical failure data is deliberately not copied into speech_text.
        return cls(display_text, None, debug=debug, error=error, speak=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_text": self.display_text,
            "speech_text": self.speech_text,
            "debug": self.debug,
            "error": self.error.to_dict() if self.error else None,
            "speak": self.speak,
            "speech_mode": self.speech_mode,
        }


class UserFriendlyErrorMapper:
    """Maps categorical terminal errors to short Russian voice responses."""

    _speech = {
        ErrorCategory.PROVIDER_UNAVAILABLE:
            "Сейчас этот сервис недоступен. Я попробую другим способом.",
        ErrorCategory.NETWORK_PROBLEM:
            "Сэр, возникла проблема со связью. Попробую ещё раз позже.",
        ErrorCategory.MODEL_FAILURE:
            "Сэр, здесь возникла проблема. Я пока не смог её обойти.",
        ErrorCategory.TOOL_FAILURE:
            "С этим возникла проблема. Сейчас попробую иначе.",
        ErrorCategory.PERMISSION_REQUIRED:
            "Сэр, здесь нужно ваше подтверждение.",
        ErrorCategory.TTS_FAILURE: "",
        ErrorCategory.UNKNOWN_FAILURE:
            "Сэр, здесь что-то пошло не так. Я пока не смог это обойти.",
    }

    def map(self, error: ErrorInfo, *, display_text: str = "") -> AssistantOutput:
        if error.recovered:
            return AssistantOutput(
                display_text=display_text,
                speech_text=None,
                error=error,
                speak=False,
            )
        speech = self._speech[error.category]
        visible = display_text or speech or "Голосовой вывод временно пропущен."
        return AssistantOutput(
            display_text=visible,
            speech_text=speech or None,
            error=error,
            speak=bool(speech),
        )


def assistant_output_from_outcome(outcome: Any) -> AssistantOutput:
    """Adapts AgentOutcome without importing the agent into the voice layer."""
    # The model's private reasoning is never a UI or speech payload.  Strip it
    # before both boundaries so the TTS sanitizer does not discard an otherwise
    # valid answer merely because a streaming provider emitted <think> first.
    display = strip_reasoning_blocks(str(getattr(outcome, "text", "") or "").strip())
    mode = str(getattr(outcome, "mode", "") or "")
    trace = list(getattr(outcome, "trace", []) or [])
    debug = {
        "mode": mode,
        "verified": bool(getattr(outcome, "verified", False)),
        "tool_used": getattr(outcome, "tool_used", None),
        "verification": (
            getattr(outcome, "verification").to_dict()
            if getattr(outcome, "verification", None) is not None
            else None
        ),
        "degraded": bool(getattr(outcome, "degraded", False)),
        "trace": trace,
    }
    technical = "\n".join(str(item) for item in trace)
    mapper = UserFriendlyErrorMapper()
    if mode == "model_error":
        mapped = mapper.map(
            ErrorInfo(ErrorCategory.MODEL_FAILURE, technical_message=technical),
            display_text=display,
        )
        return AssistantOutput(
            mapped.display_text, mapped.speech_text, debug=debug,
            error=mapped.error, speak=mapped.speak, speech_mode="normal",
        )
    if bool(getattr(outcome, "needs_confirmation", False)):
        mapped = mapper.map(
            ErrorInfo(ErrorCategory.PERMISSION_REQUIRED, technical_message=technical),
            display_text=display,
        )
        return AssistantOutput(
            mapped.display_text, mapped.speech_text, debug=debug,
            error=mapped.error, speak=mapped.speak, speech_mode="focused",
        )
    speech_mode = "focused" if mode in {"capability", "tool", "confirmation_required"} else "normal"
    return AssistantOutput.natural(display, speech_mode=speech_mode, debug=debug)


__all__ = [
    "AssistantOutput", "ErrorCategory", "ErrorInfo", "UserFriendlyErrorMapper",
    "assistant_output_from_outcome",
]
