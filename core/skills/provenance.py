"""Provenance tracking for forged skills.

Every generated skill carries a provenance record: which model generated it,
which prompt was used, which tests passed, and when.  Users can inspect and
delete skills via the /skills WS events.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.utils.logger import get_logger

log = get_logger(__name__)

_PROVENANCE_FILE = "provenance.json"


@dataclass
class SkillProvenance:
    """Immutable record attached to every forged skill."""
    skill_id:       str
    skill_name:     str
    model:          str                  # e.g. "deepseek-chat"
    prompt_hash:    str                  # sha256[:16] of the generation prompt
    tests_passed:   List[str]            = field(default_factory=list)
    tests_failed:   List[str]            = field(default_factory=list)
    created_at:     str                  = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    profile_id:     str                  = "default"
    goal:           str                  = ""
    sandbox_ok:     bool                 = False
    metadata:       Dict[str, Any]       = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id":     self.skill_id,
            "skill_name":   self.skill_name,
            "model":        self.model,
            "prompt_hash":  self.prompt_hash,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "created_at":   self.created_at,
            "profile_id":   self.profile_id,
            "goal":         self.goal,
            "sandbox_ok":   self.sandbox_ok,
            "metadata":     self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillProvenance":
        return cls(
            skill_id     = str(d.get("skill_id") or uuid.uuid4().hex),
            skill_name   = str(d.get("skill_name") or ""),
            model        = str(d.get("model") or "unknown"),
            prompt_hash  = str(d.get("prompt_hash") or ""),
            tests_passed = list(d.get("tests_passed") or []),
            tests_failed = list(d.get("tests_failed") or []),
            created_at   = str(d.get("created_at") or datetime.now(timezone.utc).isoformat()),
            profile_id   = str(d.get("profile_id") or "default"),
            goal         = str(d.get("goal") or ""),
            sandbox_ok   = bool(d.get("sandbox_ok", False)),
            metadata     = dict(d.get("metadata") or {}),
        )


class ProvenanceStore:
    """Persists skill provenance records to data/skills/user_forged/."""

    def __init__(self, skills_dir: Optional[Path] = None) -> None:
        if skills_dir is None:
            from core.utils.paths import PROJECT_ROOT
            skills_dir = PROJECT_ROOT / "data" / "skills" / "user_forged"
        self._dir = Path(skills_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._prov_path = self._dir / _PROVENANCE_FILE
        self._records: Dict[str, SkillProvenance] = {}
        self._load()

    def _load(self) -> None:
        if not self._prov_path.exists():
            return
        try:
            data = json.loads(self._prov_path.read_text(encoding="utf-8"))
            for item in data:
                p = SkillProvenance.from_dict(item)
                self._records[p.skill_id] = p
        except Exception as exc:
            log.warning("ProvenanceStore: failed to load: %s", exc)

    def _save(self) -> None:
        try:
            payload = json.dumps(
                [p.to_dict() for p in self._records.values()],
                ensure_ascii=False, indent=2,
            )
            self._prov_path.write_text(payload, encoding="utf-8")
        except Exception as exc:
            log.error("ProvenanceStore: failed to save: %s", exc)

    def record(self, prov: SkillProvenance) -> None:
        self._records[prov.skill_id] = prov
        self._save()

    def get(self, skill_id: str) -> Optional[SkillProvenance]:
        return self._records.get(skill_id)

    def list_all(self, profile_id: Optional[str] = None) -> List[SkillProvenance]:
        records = list(self._records.values())
        if profile_id is not None:
            records = [r for r in records if r.profile_id == profile_id]
        return records

    def delete(self, skill_id: str) -> bool:
        if skill_id not in self._records:
            return False
        del self._records[skill_id]
        self._save()
        # Also remove the skill file
        skill_file = self._dir / f"{skill_id}.py"
        if skill_file.exists():
            try:
                skill_file.unlink()
            except OSError:
                pass
        return True
