"""Typed contracts for the Tutor Mode subsystem."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class TutorIntent(str, Enum):
    """What the user wants from the tutor."""
    SOLVE  = "solve"   # give the answer, show working
    LEARN  = "learn"   # Socratic mode — teach the concept
    AMBIGUOUS = "ambiguous"


class MasteryLevel(str, Enum):
    UNKNOWN    = "unknown"
    NOVICE     = "novice"
    DEVELOPING = "developing"
    PROFICIENT = "proficient"
    MASTERED   = "mastered"


@dataclass
class SubtopicMastery:
    """Per-subtopic mastery record stored in user memory."""
    subtopic:       str
    level:          MasteryLevel     = MasteryLevel.UNKNOWN
    correct_count:  int              = 0
    wrong_count:    int              = 0
    last_seen:      str              = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subtopic":      self.subtopic,
            "level":         self.level.value,
            "correct_count": self.correct_count,
            "wrong_count":   self.wrong_count,
            "last_seen":     self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SubtopicMastery":
        return cls(
            subtopic      = str(d.get("subtopic") or ""),
            level         = MasteryLevel(d.get("level", "unknown")),
            correct_count = int(d.get("correct_count", 0)),
            wrong_count   = int(d.get("wrong_count", 0)),
            last_seen     = str(d.get("last_seen") or datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class TutorSession:
    """Active tutoring session state."""
    session_id:     str              = field(default_factory=lambda: f"tutor-{uuid.uuid4().hex[:8]}")
    profile_id:     str              = "default"
    topic:          str              = ""
    intent:         TutorIntent      = TutorIntent.AMBIGUOUS
    user_level:     str              = "adaptive"   # grade 9, university, etc.
    turn_count:     int              = 0
    mastery:        Dict[str, SubtopicMastery] = field(default_factory=dict)
    history:        List[Dict[str, str]]       = field(default_factory=list)
    created_at:     str              = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at:     str              = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    fatigue_signals: int             = 0   # count of short/disengaged answers

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def add_turn(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text})
        self.turn_count += 1
        self.touch()

    def update_mastery(self, subtopic: str, correct: bool) -> None:
        if subtopic not in self.mastery:
            self.mastery[subtopic] = SubtopicMastery(subtopic=subtopic)
        m = self.mastery[subtopic]
        if correct:
            m.correct_count += 1
            if m.correct_count >= 3 and m.wrong_count == 0:
                m.level = MasteryLevel.MASTERED
            elif m.correct_count >= 2:
                m.level = MasteryLevel.PROFICIENT
            elif m.correct_count >= 1:
                m.level = MasteryLevel.DEVELOPING
        else:
            m.wrong_count += 1
            if m.level == MasteryLevel.MASTERED:
                m.level = MasteryLevel.PROFICIENT
        m.last_seen = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id":     self.session_id,
            "profile_id":     self.profile_id,
            "topic":          self.topic,
            "intent":         self.intent.value,
            "user_level":     self.user_level,
            "turn_count":     self.turn_count,
            "mastery":        {k: v.to_dict() for k, v in self.mastery.items()},
            "history":        self.history,
            "created_at":     self.created_at,
            "updated_at":     self.updated_at,
            "fatigue_signals": self.fatigue_signals,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TutorSession":
        obj = cls(
            session_id     = str(d.get("session_id") or f"tutor-{uuid.uuid4().hex[:8]}"),
            profile_id     = str(d.get("profile_id") or "default"),
            topic          = str(d.get("topic") or ""),
            intent         = TutorIntent(d.get("intent", "ambiguous")),
            user_level     = str(d.get("user_level") or "adaptive"),
            turn_count     = int(d.get("turn_count", 0)),
            history        = list(d.get("history") or []),
            created_at     = str(d.get("created_at") or datetime.now(timezone.utc).isoformat()),
            updated_at     = str(d.get("updated_at") or datetime.now(timezone.utc).isoformat()),
            fatigue_signals = int(d.get("fatigue_signals", 0)),
        )
        for k, v in (d.get("mastery") or {}).items():
            obj.mastery[k] = SubtopicMastery.from_dict(v)
        return obj


@dataclass
class TutorResponse:
    """What the tutor returns to the orchestrator."""
    text:           str
    voice_text:     str              = ""   # SSML-annotated for TTS
    deliverable:    Optional[str]    = None  # path to generated file (konspekt/pptx)
    check_question: Optional[str]    = None  # follow-up check question
    intent:         TutorIntent      = TutorIntent.LEARN
    session_id:     str              = ""
    mastery_update: Optional[Dict[str, Any]] = None
