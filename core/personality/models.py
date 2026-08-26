"""Typed contracts for the ATLAS personality and communication layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class IdentityProfile:
    name: str = "ATLAS"
    role: str = "digital intelligence"
    mission: str = "understand and advance user goals safely"
    values: tuple[str, ...] = ("accuracy", "privacy", "initiative", "reliability")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IdentityProfile":
        values = tuple(str(item) for item in data.get("values", cls().values) if str(item).strip())
        return cls(
            name=str(data.get("name") or cls.name),
            role=str(data.get("role") or cls.role),
            mission=str(data.get("mission") or cls.mission),
            values=values or cls().values,
        )


@dataclass(frozen=True)
class PersonalityProfile:
    name: str = "ATLAS"
    tone: str = "professional_friendly"
    humor: float = 0.35
    verbosity: str = "adaptive"
    initiative: str = "assistant"
    respect_level: str = "high"
    address: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PersonalityProfile":
        return cls(
            name=str(data.get("name") or cls.name),
            tone=str(data.get("tone") or cls.tone),
            humor=_clamp(data.get("humor"), cls.humor),
            verbosity=str(data.get("verbosity") or cls.verbosity),
            initiative=str(data.get("initiative") or cls.initiative),
            respect_level=str(data.get("respect_level") or cls.respect_level),
            address=str(data.get("address") or cls.address),
        )


@dataclass
class UserProfile:
    """Non-sensitive learned preferences only."""

    communication_style: str = "adaptive"
    technical_level: str = "adaptive"
    prefers_action_over_explanation: bool = False
    likes_confirmation: bool = True
    humor_preference: float | None = None
    preferred_address: str = ""
    delegation_affinity: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class StyleProfile:
    tone: str
    verbosity: str
    max_sentences: int
    explanation_depth: str
    structured: bool
    formality: str
    humor_level: float
    initiative: str
    address: str
