"""Structured identity, response style selection, and restrained wording."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.personality.communication import CommunicationAdapter
from core.personality.humor import HumorPolicy
from core.personality.models import IdentityProfile, PersonalityProfile, StyleProfile, UserProfile
from core.utils.paths import PROJECT_ROOT


def _load(path: Path | str | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


class PersonalityEngine:
    def __init__(self, *, identity_path: Path | str | None = None,
                 personality_path: Path | str | None = None) -> None:
        identity_path = identity_path or (PROJECT_ROOT / "persona" / "identity.json")
        personality_path = personality_path or (PROJECT_ROOT / "persona" / "personality.json")
        self.identity = IdentityProfile.from_mapping(_load(identity_path))
        self.profile = PersonalityProfile.from_mapping(_load(personality_path))
        self.humor = HumorPolicy(self.profile.humor)
        self.communication = CommunicationAdapter(self.profile, self.humor)

    def style_for(self, *, user_context: Any = None, urgency: str = "normal",
                  task_type: str = "conversation",
                  user_preference: UserProfile | Mapping[str, Any] | None = None,
                  risk: str = "low", is_error: bool = False) -> StyleProfile:
        return self.communication.adapt(
            user_context, urgency, task_type, user_preference, risk=risk, is_error=is_error,
        )

    @staticmethod
    def infer_task_type(text: str, *, mode: str = "") -> str:
        value = " ".join((text or "").casefold().replace("ё", "е").split())
        if mode == "conversation":
            if re.search(r"\b(объясни|научи|как работает|почему)\b", value):
                return "learning"
            return "conversation"
        if re.search(r"\b(отчет|сводк|результат|итог)\w*", value):
            return "report"
        if re.search(r"\b(объясни|научи|инструкц|по шагам)\w*", value):
            return "learning"
        return "work"

    def contextual_greeting(self_or_context: Any, return_context: Any = None) -> str | None:
        if isinstance(self_or_context, PersonalityEngine):
            ctx = return_context
            addr = (self_or_context.profile.address or "").strip()
        else:
            ctx = self_or_context
            try:
                addr = (PersonalityEngine().profile.address or "").strip()
            except Exception:
                addr = ""
        confidence = float(getattr(ctx, "confidence", 0.0) or 0.0)
        evidence = tuple(getattr(ctx, "evidence", ()) or ())
        message = " ".join(str(getattr(ctx, "message", "") or "").split())
        if confidence < 0.7 or not evidence or not message:
            return None
        prefix = f"{addr.capitalize()}, " if addr else ""
        sprint11 = re.match(r"(?i)^продолжим\s+(.+?)\?\s*остановились\s+на\s+.+", message)
        if sprint11:
            subject = sprint11.group(1).strip(" .?!")
            res = f"{prefix}продолжим {subject}?"
            return res[0].upper() + res[1:]
        task = re.sub(r"(?i)^вы\s+остановились\s+на\s+", "", message).strip(" .?!")
        words = task.split()
        if words and re.search(r"(?i)(ке|те)$", words[0]):
            words[0] = words[0][:-1] + "у"
        task = " ".join(words)
        res = f"{prefix}продолжим: {task}?" if task else f"{prefix}продолжим прошлую задачу?"
        return res[0].upper() + res[1:]

    def prompt_fragment(self, style: StyleProfile,
                        memories: Iterable[str] = ()) -> str:
        relevant = [" ".join(str(item).split())[:180] for item in memories if str(item).strip()][:4]
        parts = [
            f"Identity: {self.identity.name}; role: {self.identity.role}; mission: {self.identity.mission}.",
            ("Стиль ответа: "
             f"tone={style.tone}; verbosity={style.verbosity}; "
             f"max_sentences={style.max_sentences}; structure={'yes' if style.structured else 'no'}; "
             f"humor={style.humor_level:.2f}; address={style.address}."),
            "Сначала точность и результат; сохраняй единый характер без постоянного театра.",
        ]
        if relevant:
            parts.append("Релевантные предпочтения: " + " | ".join(relevant))
        return "\n".join(parts)[:1199]

    def naturalize(self, text: str, *, verified: bool, task_type: str = "work") -> str:
        value = (text or "").strip()
        generic = value.casefold().rstrip(".! ") in {
            "task completed successfully", "completed successfully", "выполнено", "задача выполнена",
        }
        if not generic:
            return value
        if verified:
            addr = (self.profile.address or "").strip()
            if addr:
                return f"Готово, {addr}. Проверил результат — всё применилось."
            return "Готово. Проверил результат — всё применилось."
        return "Результат пока не подтверждён проверкой."

    @staticmethod
    def adapt_response(text: str, style: StyleProfile) -> str:
        """Applies a hard brevity bound only to plain natural conversation."""
        value = (text or "").strip()
        list_lines = [line for line in value.splitlines()
                      if line.lstrip().startswith(("-", "*", "1."))]
        if style.verbosity != "short" or style.structured or "```" in value or len(list_lines) > 1:
            return value
        sentences = [part.strip() for part in re.findall(
            r"[^.!?]+(?:[.!?]+(?=\s|$)|$)", value,
        ) if part.strip()]
        if len(sentences) <= style.max_sentences:
            return value
        return " ".join(sentences[:style.max_sentences])
