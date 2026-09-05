"""Deterministic, durable and scoped delegated authority.

The language model may prepare :class:`AuthorityProposal` data.  Only a real
user-message provenance can issue a grant, and every later action is matched
locally across subject, resource, action, purpose, validity and risk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from core.capabilities import RiskLevel
from core.security.atomic import atomic_json_write, load_json
from core.security.redaction import redact
from core.task_runtime import MissionStatus, TaskEvent

__all__ = [
    "AuthorityDecision", "AuthorityGrant", "AuthorityProposal",
    "AuthorityRequest", "AuthorityStatus", "AuthorityStore", "ProvenanceKind",
    "SCREEN_ACTION", "SCREEN_EFFECT", "SCREEN_FAMILY", "SCREEN_PURPOSE",
    "SCREEN_RESOURCE", "SCREEN_SCOPES", "SCREEN_SCOPE_TTL", "SCREEN_SUBJECT",
    "classify_effect", "screen_capture_proposal", "screen_capture_request",
]


class AuthorityStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    CLOSED = "closed"


class ProvenanceKind(str, Enum):
    USER_INSTRUCTION = "user_instruction"
    ASSISTANT_TEXT = "assistant_text"
    MEMORY = "memory"
    INFERENCE = "inference"


_RISK_ORDER = {
    RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _keys(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({_key(item) for item in values if _key(item)}))


def _safe_constraints(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in (value or {}).items():
        name = _key(raw_key)
        if not name or name in {"password", "secret", "token", "credential", "api_key"}:
            continue
        if raw_value is None or isinstance(raw_value, (str, int, float, bool)):
            result[name] = raw_value
    return result


#: S3: чтение экрана — отдельный эффект, а не «какой-то инструмент».  Имя
#: инструмента/действия — структурное поле, поэтому это НЕ текстовый
#: классификатор: свободный текст цели здесь не участвует.
_SCREEN_TOOLS = frozenset({"screen_capture", "computer_screenshot", "screenshot"})

SCREEN_EFFECT = "read_screen"
SCREEN_SUBJECT = "local user"
SCREEN_RESOURCE = "screen"
SCREEN_ACTION = "screen_capture"
SCREEN_FAMILY = "vision"
SCREEN_PURPOSE = "screen context for the assistant"
_SCREEN_PRINCIPAL = "user"
_SCREEN_DELEGATE = "jarvis"

#: Область действия разрешения → срок жизни гранта.  «Навсегда» как отдельной
#: сущности не существует: это тоже TTL-грант, просто долгий.  «Один раз» —
#: короткий грант, который вызывающая сторона отзывает сразу после снимка.
SCREEN_SCOPE_TTL: Mapping[str, timedelta] = {
    "once": timedelta(minutes=2),
    "session": timedelta(hours=12),
    "permanent": timedelta(days=365),
}
SCREEN_SCOPES: tuple[str, ...] = tuple(SCREEN_SCOPE_TTL)


def classify_effect(goal: str, action: str, tool: str = "",
                    arguments: Mapping[str, Any] | None = None) -> str:
    """Classify the concrete effect without trusting a model-supplied label."""
    text = " ".join((goal, action, tool, " ".join(str(v) for v in (arguments or {}).values()))).casefold()
    if any(word in text for word in ("password", "credential", "парол", "secret", "api_key", "token")):
        return "credential"
    if any(word in text for word in ("payment", "purchase", "transfer", "send_money", "оплат", "купи", "деньг")):
        return "financial"
    if any(word in text for word in ("firewall", "defender", "uac", "registry", "security", "брандмауэр", "реестр")):
        return "security"
    if any(word in text for word in ("delete", "remove", "wipe", "format", "удал", "сотри", "формат")):
        return "destructive"
    # Проверка ПОСЛЕ перечисленных выше: цель, явно направленная на секреты или
    # разрушение, остаётся за пределами гранта на чтение экрана (fail-closed).
    if _key(tool) in _SCREEN_TOOLS or _key(action) in _SCREEN_TOOLS:
        return SCREEN_EFFECT
    if _key(action) in {"send_message", "read_message", "reply", "conversation"}:
        return "conversation"
    return _key(tool or action or "unknown")


@dataclass
class AuthorityProposal:
    principal: str
    delegate: str
    subjects: list[str]
    resources: list[str]
    allowed_actions: list[str]
    capability_families: list[str]
    allowed_effects: list[str]
    denied_actions: list[str]
    purposes: list[str]
    risk_ceiling: RiskLevel | str
    valid_from: datetime | str
    expires_at: datetime | str
    mission_id: Optional[str] = None
    commitment_id: Optional[str] = None
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorityRequest:
    subject: str
    resource: str
    action: str
    capability_family: str
    effect: str
    purpose: str
    risk: RiskLevel | str
    mission_id: Optional[str] = None
    commitment_id: Optional[str] = None
    constraints: Mapping[str, Any] = field(default_factory=dict)


def screen_capture_request(
    *, risk: RiskLevel | str = RiskLevel.MEDIUM, mission_id: Optional[str] = None,
    commitment_id: Optional[str] = None,
) -> "AuthorityRequest":
    """Запрос на чтение экрана — одинаковый для WS-моста и для инструмента.

    ``risk`` по умолчанию равен паспортному уровню возможности (S1: medium).
    Более высокая оценка НЕ покрывается грантом с ceiling=medium — это
    сознательный fail-closed: «покажи пароли с экрана» снова спросит.
    """
    return AuthorityRequest(
        subject=SCREEN_SUBJECT, resource=SCREEN_RESOURCE, action=SCREEN_ACTION,
        capability_family=SCREEN_FAMILY, effect=SCREEN_EFFECT,
        purpose=SCREEN_PURPOSE, risk=risk,
        mission_id=mission_id, commitment_id=commitment_id,
    )


def screen_capture_proposal(scope: str, *, now: datetime | None = None) -> "AuthorityProposal":
    """Предложение гранта на чтение экрана для одной из ``SCREEN_SCOPES``.

    Неизвестная область действия — ``KeyError``: молчаливого «разрешим на всякий
    случай навсегда» не бывает.
    """
    ttl = SCREEN_SCOPE_TTL[str(scope or "").strip().casefold()]
    start = now or _now()
    return AuthorityProposal(
        principal=_SCREEN_PRINCIPAL, delegate=_SCREEN_DELEGATE,
        subjects=[SCREEN_SUBJECT], resources=[SCREEN_RESOURCE],
        allowed_actions=[SCREEN_ACTION], capability_families=[SCREEN_FAMILY],
        allowed_effects=[SCREEN_EFFECT], denied_actions=[],
        purposes=[SCREEN_PURPOSE], risk_ceiling=RiskLevel.MEDIUM,
        valid_from=start, expires_at=start + ttl,
    )


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    requires_confirmation: bool
    reason: str
    grant_id: Optional[str] = None


@dataclass(frozen=True)
class AuthorityGrant:
    grant_id: str
    principal: str
    delegate: str
    created_at: str
    valid_from: str
    expires_at: str
    subjects: tuple[str, ...]
    resources: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    capability_families: tuple[str, ...]
    allowed_effects: tuple[str, ...]
    denied_actions: tuple[str, ...]
    purposes: tuple[str, ...]
    risk_ceiling: RiskLevel
    constraints: Mapping[str, Any]
    mission_id: Optional[str]
    commitment_id: Optional[str]
    status: AuthorityStatus
    provenance: Mapping[str, Any]
    revoked_at: Optional[str] = None
    closed_at: Optional[str] = None
    status_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "grant_id": self.grant_id, "principal": self.principal,
            "delegate": self.delegate, "created_at": self.created_at,
            "valid_from": self.valid_from, "expires_at": self.expires_at,
            "subjects": list(self.subjects), "resources": list(self.resources),
            "allowed_actions": list(self.allowed_actions),
            "capability_families": list(self.capability_families),
            "allowed_effects": list(self.allowed_effects),
            "denied_actions": list(self.denied_actions), "purposes": list(self.purposes),
            "risk_ceiling": self.risk_ceiling.value,
            "constraints": dict(self.constraints), "mission_id": self.mission_id,
            "commitment_id": self.commitment_id, "status": self.status.value,
            "provenance": dict(self.provenance), "revoked_at": self.revoked_at,
            "closed_at": self.closed_at, "status_reason": self.status_reason,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthorityGrant":
        if int(raw.get("schema_version", 0)) != 1:
            raise ValueError("unsupported authority schema")
        grant = cls(
            grant_id=str(raw["grant_id"]), principal=_key(raw["principal"]),
            delegate=_key(raw["delegate"]), created_at=_dt(raw["created_at"]).isoformat(),
            valid_from=_dt(raw["valid_from"]).isoformat(),
            expires_at=_dt(raw["expires_at"]).isoformat(), subjects=_keys(raw["subjects"]),
            resources=_keys(raw["resources"]), allowed_actions=_keys(raw["allowed_actions"]),
            capability_families=_keys(raw["capability_families"]),
            allowed_effects=_keys(raw["allowed_effects"]),
            denied_actions=_keys(raw.get("denied_actions") or []),
            purposes=_keys(raw["purposes"]), risk_ceiling=RiskLevel(raw["risk_ceiling"]),
            constraints=_safe_constraints(raw.get("constraints")),
            mission_id=str(raw["mission_id"]) if raw.get("mission_id") else None,
            commitment_id=str(raw["commitment_id"]) if raw.get("commitment_id") else None,
            status=AuthorityStatus(raw["status"]), provenance=dict(raw["provenance"]),
            revoked_at=str(raw["revoked_at"]) if raw.get("revoked_at") else None,
            closed_at=str(raw["closed_at"]) if raw.get("closed_at") else None,
            status_reason=str(raw.get("status_reason") or ""),
        )
        if not all((grant.grant_id, grant.principal, grant.delegate, grant.subjects,
                    grant.resources, grant.allowed_actions, grant.capability_families,
                    grant.allowed_effects, grant.purposes)):
            raise ValueError("incomplete authority grant")
        return grant


class AuthorityStore:
    """Local policy boundary and integrity-protected durable grant store."""

    def __init__(self, root: str | Path, *, clock: Callable[[], datetime] | None = None,
                 mission_resolver: Callable[[str], Any] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or _now
        self._mission_resolver = mission_resolver
        self._lock = threading.RLock()
        self._grants: dict[str, AuthorityGrant] = {}
        self._key = self._load_or_create_key()
        self.integrity_failures = 0
        self._checks = 0
        self._confirmations = 0
        self._load()

    def _load_or_create_key(self) -> bytes:
        path = self.root / ".authority.key"
        if path.is_file():
            value = path.read_bytes()
            if len(value) == 32:
                return value
            raise ValueError("invalid authority integrity key")
        value = secrets.token_bytes(32)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        return value

    def path(self, grant_id: str) -> Path:
        return self.root / f"grant-{grant_id}.json"

    @staticmethod
    def _canonical(value: Mapping[str, Any]) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    def _mac(self, value: Mapping[str, Any]) -> str:
        return hmac.new(self._key, self._canonical(value), hashlib.sha256).hexdigest()

    def _persist(self, grant: AuthorityGrant) -> None:
        raw = redact(grant.to_dict())
        atomic_json_write(self.path(grant.grant_id), {"grant": raw, "integrity": self._mac(raw)})

    def _load(self) -> None:
        with self._lock:
            for path in sorted(self.root.glob("grant-*.json")):
                try:
                    envelope = load_json(path, default={})
                    raw = envelope.get("grant") if isinstance(envelope, Mapping) else None
                    signature = envelope.get("integrity") if isinstance(envelope, Mapping) else None
                    if not isinstance(raw, Mapping) or not isinstance(signature, str):
                        raise ValueError("invalid authority envelope")
                    if not hmac.compare_digest(signature, self._mac(raw)):
                        raise ValueError("authority integrity mismatch")
                    grant = AuthorityGrant.from_dict(raw)
                    self._grants[grant.grant_id] = grant
                except Exception:
                    self.integrity_failures += 1
            self.expire()
            if self._mission_resolver is not None:
                for grant in list(self._grants.values()):
                    if grant.status is not AuthorityStatus.ACTIVE or not grant.mission_id:
                        continue
                    mission = self._mission_resolver(grant.mission_id)
                    if mission is None or mission.status.is_terminal:
                        self._replace_status(grant, AuthorityStatus.CLOSED, "bound mission inactive on recovery")

    def issue(self, proposal: AuthorityProposal, *, source_kind: ProvenanceKind | str,
              source_role: str, source_text: str, source_id: str) -> AuthorityGrant:
        kind = ProvenanceKind(source_kind)
        if kind is not ProvenanceKind.USER_INSTRUCTION or _key(source_role) != "user" or not source_text.strip():
            raise ValueError("authority requires a real user instruction")
        start, end = _dt(proposal.valid_from), _dt(proposal.expires_at)
        now = self._clock()
        if end <= start or end <= now:
            raise ValueError("authority validity must end in the future")
        normalized = {
            "principal": _key(proposal.principal), "delegate": _key(proposal.delegate),
            "subjects": _keys(proposal.subjects), "resources": _keys(proposal.resources),
            "allowed_actions": _keys(proposal.allowed_actions),
            "capability_families": _keys(proposal.capability_families),
            "allowed_effects": _keys(proposal.allowed_effects),
            "denied_actions": _keys(proposal.denied_actions), "purposes": _keys(proposal.purposes),
            "risk_ceiling": RiskLevel(proposal.risk_ceiling),
            "constraints": _safe_constraints(proposal.constraints),
            "mission_id": str(proposal.mission_id) if proposal.mission_id else None,
            "commitment_id": str(proposal.commitment_id) if proposal.commitment_id else None,
        }
        if not all((normalized["principal"], normalized["delegate"], normalized["subjects"],
                    normalized["resources"], normalized["allowed_actions"],
                    normalized["capability_families"], normalized["allowed_effects"],
                    normalized["purposes"])):
            raise ValueError("authority proposal is incomplete")
        if set(normalized["allowed_actions"]) & set(normalized["denied_actions"]):
            raise ValueError("an action cannot be both allowed and denied")
        provenance = {
            "kind": kind.value, "source_role": "user", "source_id": str(source_id),
            "instruction_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        }
        fingerprint_data = {
            **{k: (v.value if isinstance(v, Enum) else v) for k, v in normalized.items()},
            "valid_from": start.isoformat(), "expires_at": end.isoformat(),
            "provenance": provenance,
        }
        grant_id = hashlib.sha256(self._canonical(fingerprint_data)).hexdigest()[:24]
        with self._lock:
            existing = self._grants.get(grant_id)
            if existing is not None:
                return existing
            grant = AuthorityGrant(
                grant_id=grant_id, created_at=now.isoformat(), valid_from=start.isoformat(),
                expires_at=end.isoformat(), status=AuthorityStatus.ACTIVE,
                provenance=provenance, **normalized,
            )
            self._persist(grant)
            self._grants[grant_id] = grant
            return grant

    def _replace_status(self, grant: AuthorityGrant, status: AuthorityStatus,
                        reason: str = "") -> AuthorityGrant:
        data = grant.to_dict()
        data["status"] = status.value
        data["status_reason"] = reason
        if status is AuthorityStatus.REVOKED:
            data["revoked_at"] = self._clock().isoformat()
        if status is AuthorityStatus.CLOSED:
            data["closed_at"] = self._clock().isoformat()
        updated = AuthorityGrant.from_dict(data)
        self._persist(updated)
        self._grants[updated.grant_id] = updated
        return updated

    def expire(self) -> int:
        count = 0
        with self._lock:
            now = self._clock()
            for grant in list(self._grants.values()):
                if grant.status is AuthorityStatus.ACTIVE and now >= _dt(grant.expires_at):
                    self._replace_status(grant, AuthorityStatus.EXPIRED, "validity ended")
                    count += 1
        return count

    def revoke(self, grant_id: str, reason: str = "user revoked") -> bool:
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None or grant.status is not AuthorityStatus.ACTIVE:
                return False
            self._replace_status(grant, AuthorityStatus.REVOKED, reason)
            return True

    def close_for_mission(self, mission_id: str, reason: str = "mission terminal") -> int:
        count = 0
        with self._lock:
            for grant in list(self._grants.values()):
                if grant.status is AuthorityStatus.ACTIVE and grant.mission_id == mission_id:
                    self._replace_status(grant, AuthorityStatus.CLOSED, reason)
                    count += 1
        return count

    def bind_runtime(self, runtime: Any) -> Callable[[], None]:
        if self._mission_resolver is None:
            self._mission_resolver = runtime.get

        def observe(event: TaskEvent) -> None:
            status = _key(event.payload.get("status") or event.phase)
            if status in {"completed", "cancelled", "failed", "expired"}:
                self.close_for_mission(event.task_id, f"mission {status}")

        return runtime.subscribe(observe)

    def _mismatch(self, grant: AuthorityGrant, request: AuthorityRequest) -> Optional[str]:
        action, effect = _key(request.action), _key(request.effect)
        if grant.mission_id and grant.mission_id != request.mission_id:
            return "mission"
        if grant.commitment_id and grant.commitment_id != request.commitment_id:
            return "commitment"
        if _key(request.subject) not in grant.subjects:
            return "subject"
        if _key(request.resource) not in grant.resources:
            return "resource"
        if action in grant.denied_actions:
            return "action denied"
        if action not in grant.allowed_actions:
            return "action"
        if _key(request.capability_family) not in grant.capability_families:
            return "capability"
        if effect not in grant.allowed_effects:
            return "effect outside scope"
        if _key(request.purpose) not in grant.purposes:
            return "purpose"
        try:
            if _RISK_ORDER[RiskLevel(request.risk)] > _RISK_ORDER[grant.risk_ceiling]:
                return "risk ceiling exceeded"
        except (ValueError, KeyError):
            return "invalid risk"
        supplied = _safe_constraints(request.constraints)
        if any(supplied.get(key) != value for key, value in grant.constraints.items()):
            return "constraint"
        return None

    def check(self, request: AuthorityRequest) -> AuthorityDecision:
        with self._lock:
            self._checks += 1
            self.expire()
            active = [item for item in self._grants.values() if item.status is AuthorityStatus.ACTIVE]
            first_reason = "no delegated authority"
            for grant in active:
                if self._clock() < _dt(grant.valid_from):
                    first_reason = "not yet valid"
                    continue
                if grant.mission_id and self._mission_resolver is not None:
                    mission = self._mission_resolver(grant.mission_id)
                    if mission is None or mission.status.is_terminal:
                        self._replace_status(grant, AuthorityStatus.CLOSED, "bound mission inactive")
                        first_reason = "mission inactive"
                        continue
                mismatch = self._mismatch(grant, request)
                if mismatch is None:
                    return AuthorityDecision(True, False, "delegated authority matched", grant.grant_id)
                if first_reason == "no delegated authority":
                    first_reason = mismatch
            return AuthorityDecision(False, True, first_reason)

    def execute_authorized(self, request: AuthorityRequest, callback: Callable[[], Any]) -> tuple[AuthorityDecision, Any]:
        """Revalidate and start an effect atomically with respect to revocation."""
        with self._lock:
            decision = self.check(request)
            if not decision.allowed:
                return decision, None
            return decision, callback()

    def get(self, grant_id: str) -> Optional[AuthorityGrant]:
        with self._lock:
            self.expire()
            return self._grants.get(grant_id)

    def list(self, *, include_terminal: bool = True) -> list[AuthorityGrant]:
        with self._lock:
            self.expire()
            values = sorted(self._grants.values(), key=lambda item: item.grant_id)
            return values if include_terminal else [g for g in values if g.status is AuthorityStatus.ACTIVE]

    def record_confirmation(self, confirmation_id: str, *, action: str) -> None:
        del confirmation_id, action
        with self._lock:
            self._confirmations += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "grants": len(self._grants), "checks": self._checks,
                "integrity_failures": self.integrity_failures,
                "confirmations": self._confirmations, "llm_calls": 0, "threads": 0,
            }
