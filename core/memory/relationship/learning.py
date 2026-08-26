"""Explicit preference and suggestion-outcome learning for relationship memory."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from core.memory.relationship.store import RelationshipMemoryStore
from core.memory.secret_filter import contains_secret_or_raw
from core.personality.models import UserProfile
from core.security.atomic import atomic_json_write, load_json


class PreferenceLearner:
    def __init__(self, store: RelationshipMemoryStore) -> None:
        self.store = store
        self.path = Path(store.directory) / "user_profile.json"
        self._lock = threading.RLock()

    def observe_user_message(self, message: str) -> dict[str, Any]:
        """Learns only explicit interaction preferences, never arbitrary facts."""
        text = " ".join((message or "").casefold().replace("ё", "е").split())
        changes: dict[str, Any] = {}
        if re.search(r"\b(кратко|короче|без длинных|без подробност)\w*", text):
            changes["communication_style"] = "short"
        if re.search(r"\b(подробно|подробнее|по шагам|развернут)\w*", text):
            changes["communication_style"] = "detailed"
        if re.search(r"\b(просто сделай|действуй сразу|меньше объясн)\w*", text):
            changes["prefers_action_over_explanation"] = True
        if re.search(r"\b(сначала объясни|объясняй перед|не делай сразу)\w*", text):
            changes["prefers_action_over_explanation"] = False
        if re.search(r"\b(без шуток|никакого юмора|не шути)\b", text):
            changes["humor_preference"] = 0.0
        elif re.search(r"\b(можно шутить|с юмором|больше юмора)\b", text):
            changes["humor_preference"] = 0.35
        if re.search(r"\b(не спрашивай подтвержден|без подтвержден)\w*", text):
            changes["likes_confirmation"] = False
        elif re.search(r"\b(спрашивай подтвержден|подтверждай перед)\w*", text):
            changes["likes_confirmation"] = True
        if re.search(r"\b(разработчик|технически|продвинутый|advanced)\w*", text):
            changes["technical_level"] = "advanced"
        elif re.search(r"\b(новичок|новичку|простыми словами|без терминов)\w*", text):
            changes["technical_level"] = "beginner"
        if not changes:
            return {}

        with self._lock:
            data = self._load_data()
            for key, value in changes.items():
                data[key] = value
                self.store.remember(
                    self._fact_for(key, value), source="user_explicit", confidence=0.95,
                    importance=0.9, category="preference", key=key, ttl_days=730,
                )
            self._save_data(data)
        return changes

    def record_suggestion_outcome(self, task_type: str, outcome: str) -> float:
        topic = self._topic(task_type)
        result = str(outcome or "ignored").casefold()
        if result not in {"accepted", "useful", "rejected", "ignored", "failed"}:
            result = "ignored"
        with self._lock:
            data = self._load_data()
            counts = dict(data.get("delegation_counts") or {})
            topic_counts = dict(counts.get(topic) or {"accepted": 0, "rejected": 0, "ignored": 0})
            bucket = "accepted" if result in {"accepted", "useful"} else (
                "rejected" if result in {"rejected", "failed"} else "ignored"
            )
            topic_counts[bucket] = int(topic_counts.get(bucket, 0)) + 1
            counts[topic] = topic_counts
            accepted = int(topic_counts.get("accepted", 0))
            rejected = int(topic_counts.get("rejected", 0))
            ignored = int(topic_counts.get("ignored", 0))
            confidence = (accepted + 1) / (accepted + rejected + 0.5 * ignored + 2)
            confidence = round(max(0.0, min(1.0, confidence)), 3)
            affinity = dict(data.get("delegation_affinity") or {})
            affinity[topic] = confidence
            data["delegation_counts"] = counts
            data["delegation_affinity"] = affinity
            self._save_data(data)
            if accepted + rejected + ignored >= 2:
                tendency = "обычно делегирует" if confidence >= 0.6 else "редко делегирует"
                self.store.remember(
                    f"Пользователь {tendency} задачи типа {topic}", source="interaction_outcome",
                    confidence=max(0.5, abs(confidence - 0.5) * 2), importance=0.7,
                    category="delegation", key=f"delegation:{topic}", ttl_days=365,
                )
            return confidence

    def delegation_confidence(self, task_type: str) -> float:
        topic = self._topic(task_type)
        return float((self._load_data().get("delegation_affinity") or {}).get(topic, 0.5))

    def should_offer(self, task_type: str) -> bool:
        topic = self._topic(task_type)
        data = self._load_data()
        counts = (data.get("delegation_counts") or {}).get(topic) or {}
        return not (int(counts.get("rejected", 0)) >= 2 or int(counts.get("ignored", 0)) >= 3)

    def profile(self) -> UserProfile:
        data = self._load_data()
        style = str(data.get("communication_style") or "adaptive")
        if style not in {"adaptive", "short", "detailed"}:
            style = "adaptive"
        technical = str(data.get("technical_level") or "adaptive")
        if technical not in {"adaptive", "beginner", "advanced"}:
            technical = "adaptive"
        try:
            humor_value = float(data["humor_preference"])
            humor = humor_value if 0.0 <= humor_value <= 1.0 else None
        except (KeyError, TypeError, ValueError):
            humor = None
        affinity: dict[str, float] = {}
        for key, value in (data.get("delegation_affinity") or {}).items():
            try:
                score = float(value)
            except (TypeError, ValueError):
                continue
            if 0.0 <= score <= 1.0:
                affinity[str(key)[:100]] = score
        return UserProfile(
            communication_style=style,
            technical_level=technical,
            prefers_action_over_explanation=(
                data.get("prefers_action_over_explanation")
                if isinstance(data.get("prefers_action_over_explanation"), bool) else False
            ),
            likes_confirmation=(
                data.get("likes_confirmation")
                if isinstance(data.get("likes_confirmation"), bool) else True
            ),
            humor_preference=humor,
            preferred_address=str(data.get("preferred_address") or ""),
            delegation_affinity=affinity,
        )

    @staticmethod
    def _fact_for(key: str, value: Any) -> str:
        labels = {
            "communication_style": f"Пользователь предпочитает стиль ответов: {value}",
            "prefers_action_over_explanation": (
                "Пользователь предпочитает действие вместо объяснения" if value else
                "Пользователь предпочитает объяснение перед действием"
            ),
            "humor_preference": "Пользователь задал уровень юмора в общении",
            "likes_confirmation": (
                "Пользователь предпочитает подтверждения" if value else
                "Пользователь не любит лишние подтверждения"
            ),
            "technical_level": f"Пользователь предпочитает технический уровень: {value}",
        }
        return labels.get(key, f"Пользователь задал предпочтение {key}")

    @staticmethod
    def _topic(task_type: str) -> str:
        raw = " ".join((task_type or "general").casefold().split())[:200]
        if contains_secret_or_raw(raw) or re.search(
            r"(?i)([\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:\+?\d[\d\s()\-]{7,}\d)|"
            r"диагноз|паспорт|банковск|медицинск)", raw,
        ):
            return "private_task"
        compact = re.sub(r"[^\wа-яё-]+", "_", raw, flags=re.IGNORECASE).strip("_")
        return compact[:100] or "general"

    def _load_data(self) -> dict[str, Any]:
        try:
            data = load_json(self.path, default={})
            return data if isinstance(data, dict) else {}
        except (OSError, TypeError, ValueError):
            return {}

    def _save_data(self, data: dict[str, Any]) -> None:
        atomic_json_write(self.path, data)
