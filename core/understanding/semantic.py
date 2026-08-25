"""Canonical natural-language interpretation over P0/P1/P2A/P2B boundaries.

The interpreter emits serializable proposals only.  The coordinator validates
those proposals and delegates scheduling to TaskRuntime and authority issuance
to AuthorityStore; model output never executes directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from core.actions.app_control import resolve_app
from core.authority import AuthorityProposal, AuthorityStore, ProvenanceKind
from core.capabilities import RiskLevel
from core.executive import AskOncePolicy
from core.task_runtime import Mission, MissionStatus, MissionTrigger, TaskRuntime, TriggerKind

__all__ = [
    "NaturalMissionCoordinator", "NaturalMissionInterpreter", "NaturalMissionResult",
    "SemanticInterpretation", "SemanticMode",
]


class SemanticMode(str, Enum):
    CONVERSATION = "conversation"
    IMMEDIATE_ACTION = "immediate_action"
    SCHEDULED_MISSION = "scheduled_mission"
    CONDITIONAL_MISSION = "conditional_mission"
    DELEGATED_MISSION = "delegated_mission"
    MISSION_CONTROL = "mission_control"
    FOLLOW_UP = "follow_up"
    CLARIFY = "clarify"


@dataclass
class SemanticInterpretation:
    mode: SemanticMode
    goal: str = ""
    trigger: Optional[dict[str, Any]] = None
    authority_proposal: Optional[AuthorityProposal] = None
    temporary_context: dict[str, Any] = field(default_factory=dict)
    referenced_mission_id: Optional[str] = None
    missing_required_information: list[str] = field(default_factory=list)
    reporting_policy: str = "on_trigger_and_completion"
    required_integration: Optional[str] = None
    integration_configured: Optional[bool] = None
    control_action: Optional[str] = None
    updates: dict[str, Any] = field(default_factory=dict)
    natural_response: str = ""
    source: str = "local_unambiguous"
    llm_calls: int = 0


@dataclass
class NaturalMissionResult:
    interpretation: SemanticInterpretation
    response: str
    mission: Optional[Mission] = None
    grant: Any = None
    clarification: str = ""


_RELATIVE = re.compile(
    r"через\s+(?:(\d+|од(?:ин|ну)|дв[ае]|три|четыре|пять|шесть|семь|восемь|девять|десять)\s*)?"
    r"(минут\w*|час\w*|дн\w*)", re.I,
)
_ABSOLUTE = re.compile(r"(?:^|\s)в\s+(\d{1,2})[:.](\d{2})(?:\s|$)", re.I)
_DELEGATION = re.compile(r"(?:если|когда)\s+(.+?)\s+напиш\w*", re.I)
_RESOURCE = re.compile(r"напиш\w*\s+в\s+([\w-]+)", re.I)
_SUBJECT_CORRECTION = re.compile(r"^не\s+(.+?)[,\s]+а\s+(.+?)\s*$", re.I)


def _clean(text: str) -> str:
    return " ".join(str(text or "").replace("—", " ").split()).strip(" ,.!?")


def _at(now: datetime, hour: int, minute: int, *, tomorrow: bool = False) -> datetime:
    zone = now.tzinfo or timezone.utc
    day = now.date() + timedelta(days=1 if tomorrow else 0)
    value = datetime.combine(day, clock_time(hour, minute), tzinfo=zone)
    if not tomorrow and value <= now:
        value += timedelta(days=1)
    return value


class NaturalMissionInterpreter:
    """One semantic contract with an optional single structured Brain pass."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None,
                 integrations: set[str] | None = None,
                 structured_backend: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.integrations = {_clean(item).casefold() for item in (integrations or set())}
        self.structured_backend = structured_backend

    def interpret(self, text: str, *, source_role: str = "user",
                  context: Mapping[str, Any] | None = None) -> SemanticInterpretation:
        raw = _clean(text)
        lowered = raw.casefold()
        if not raw:
            return SemanticInterpretation(SemanticMode.CLARIFY,
                                          missing_required_information=["goal"])

        correction = _SUBJECT_CORRECTION.match(raw)
        if correction:
            return SemanticInterpretation(
                SemanticMode.FOLLOW_UP, goal=raw,
                updates={"subject_from": _clean(correction.group(1)).casefold(),
                         "subject_to": _clean(correction.group(2)).casefold()},
                natural_response="Изменение адресата распознано.",
            )

        if lowered.startswith(("нет,", "нет ")):
            due = self._parse_time(lowered)
            return SemanticInterpretation(
                SemanticMode.FOLLOW_UP, goal=raw,
                trigger=MissionTrigger.at(due).to_dict() if due else None,
                missing_required_information=[] if due else ["updated_schedule"],
                updates={"schedule": True}, natural_response="Время задачи обновлено.",
            )

        controls = {
            "отмени": "cancel", "поставь": "pause", "приостанов": "pause",
            "продолж": "resume", "возобнов": "resume", "перенеси": "reschedule",
        }
        for marker, action in controls.items():
            if marker in lowered and any(word in lowered for word in ("это", "задач", "последн", "пока", "продолж")):
                return SemanticInterpretation(SemanticMode.MISSION_CONTROL, goal=raw,
                                              control_action=action,
                                              natural_response="Управление задачей применено.")

        delegated = bool(_DELEGATION.search(raw)) and any(
            word in lowered for word in ("отвеч", "поговор", "от моего имени", "разберись")
        )
        if delegated:
            return self._delegated(raw, source_role)

        due = self._parse_time(lowered)
        if due is not None and any(word in lowered for word in ("напом", "скажи", "пни", "не дай забыть")):
            response, calls = self._brain_response(
                raw, {"mode": "scheduled", "due_at": due.isoformat()},
                self._scheduled_response(due),
            )
            return SemanticInterpretation(
                SemanticMode.SCHEDULED_MISSION, goal=self._goal_after_time(raw),
                trigger=MissionTrigger.at(due).to_dict(),
                natural_response=response, llm_calls=calls,
                source="structured_brain" if calls else "local_unambiguous",
            )

        if any(word in lowered for word in ("как только", "когда", "если")):
            condition = self._condition(raw)
            if condition is not None:
                response, calls = self._brain_response(
                    raw, {"mode": "conditional", "domain": condition.domain},
                    "Условие принято; сообщу после свежего наблюдения.",
                )
                return SemanticInterpretation(
                    SemanticMode.CONDITIONAL_MISSION, goal=raw, trigger=condition.to_dict(),
                    natural_response=response, llm_calls=calls,
                    source="structured_brain" if calls else "local_unambiguous",
                )

        if lowered in {"привет", "здравствуй", "добрый день"} or "?" in raw or lowered.startswith(("почему", "что такое", "как ")):
            return SemanticInterpretation(SemanticMode.CONVERSATION, goal=raw, llm_calls=0)

        if any(lowered.startswith(word) for word in ("открой", "закрой", "включи", "выключи", "поставь")):
            return SemanticInterpretation(SemanticMode.IMMEDIATE_ACTION, goal=raw)

        if self.structured_backend is not None:
            proposal = dict(self.structured_backend(raw, context or {}))
            return self._validate_backend(proposal)
        return SemanticInterpretation(SemanticMode.CONVERSATION, goal=raw, llm_calls=0)

    def _parse_time(self, text: str) -> Optional[datetime]:
        now = self.clock()
        relative = _RELATIVE.search(text)
        if relative:
            number_words = {
                "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3,
                "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
                "восемь": 8, "девять": 9, "десять": 10,
            }
            token = (relative.group(1) or "1").casefold()
            amount = int(token) if token.isdigit() else number_words[token]
            unit = relative.group(2).casefold()
            delta = timedelta(minutes=amount) if unit.startswith("минут") else (
                timedelta(hours=amount) if unit.startswith("час") else timedelta(days=amount)
            )
            return now + delta
        absolute = _ABSOLUTE.search(f" {text} ")
        if absolute:
            hour, minute = int(absolute.group(1)), int(absolute.group(2))
            if hour > 23 or minute > 59:
                return None
            return _at(now, hour, minute)
        if "завтра утром" in text:
            return _at(now, 9, 0, tomorrow=True)
        if "сегодня вечером" in text or "до вечера" in text:
            return _at(now, 19, 0)
        return None

    def _goal_after_time(self, text: str) -> str:
        value = _RELATIVE.sub("", text)
        value = _ABSOLUTE.sub("", value)
        value = re.sub(r"\b(напомни|скажи|пни меня|не дай забыть)\b", "", value, flags=re.I)
        return _clean(value) or _clean(text)

    def _condition(self, text: str) -> Optional[MissionTrigger]:
        lowered = text.casefold()
        if any(word in lowered for word in ("файл", "pdf", "скача")):
            extension = ".pdf" if "pdf" in lowered else ""
            return MissionTrigger.condition(
                "filesystem", "files", "any_match", {},
                observation_options={
                    "roots": ["downloads"], "extension": extension, "limit": 50,
                    "modified_after": self.clock().astimezone(timezone.utc).isoformat(),
                },
            )
        closed = re.search(r"(?:если|когда)\s+(.+?)\s+закро\w*", text, re.I)
        if closed:
            spoken_name = _clean(closed.group(1)).casefold()
            resolved = resolve_app(spoken_name)
            process_name = resolved.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if resolved else spoken_name
            return MissionTrigger.condition(
                "processes", "processes", "none_match",
                {"name": process_name}, poll_interval_sec=5,
            )
        return None

    def _delegated(self, text: str, source_role: str) -> SemanticInterpretation:
        subject_match = _DELEGATION.search(text)
        subject = _clean(subject_match.group(1)).casefold() if subject_match else ""
        resource_match = _RESOURCE.search(text)
        resource = _clean(resource_match.group(1)).casefold() if resource_match else ""
        # Remove a trailing resource phrase from the subject captured before "writes".
        subject = re.sub(r"\s+в\s+[\w-]+$", "", subject, flags=re.I).strip()
        now = self.clock()
        expires = self._parse_time(text.casefold()) or (now + timedelta(hours=2))
        llm_calls = 0
        proposed: dict[str, Any] = {}
        if self.structured_backend is not None and source_role.casefold() == "user":
            try:
                proposed = dict(self.structured_backend(text, {
                    "schema": {
                        "subject": "string", "resource": "string",
                        "purpose": "string", "duration_minutes": "integer 1..1440",
                        "natural_response": "short user-facing acknowledgement without IDs",
                    },
                    "rule": "interpret only; never claim or issue authority",
                }))
                llm_calls = 1
                candidate_subject = _clean(proposed.get("subject", "")).casefold()
                candidate_resource = _clean(proposed.get("resource", "")).casefold()
                if candidate_subject:
                    subject = candidate_subject
                if candidate_resource:
                    resource = candidate_resource
                duration = int(proposed.get("duration_minutes", 120))
                expires = now + timedelta(minutes=max(1, min(duration, 1440)))
            except (TypeError, ValueError, KeyError):
                llm_calls = 1
        temporary = {
            "availability": "away", "observed_at": now.isoformat(),
            "expires_at": expires.isoformat(), "storage": "mission_context",
        }
        integration = resource or None
        configured = bool(integration and integration in self.integrations)
        if source_role.casefold() != "user":
            return SemanticInterpretation(
                SemanticMode.DELEGATED_MISSION, goal=text,
                missing_required_information=["real_user_instruction"],
                required_integration=integration, integration_configured=configured,
                temporary_context=temporary,
            )
        missing = []
        if not subject:
            missing.append("subject")
        if not resource:
            missing.append("resource")
        proposal = None
        if not missing:
            proposal = AuthorityProposal(
                principal="local-user", delegate="jarvis", subjects=[subject],
                resources=[resource], allowed_actions=["read_message", "send_message"],
                capability_families=["conversation"], allowed_effects=["conversation"],
                denied_actions=["send_money", "reveal_secret", "change_security", "destructive_operation"],
                purposes=["temporary conversation while user unavailable"],
                risk_ceiling=RiskLevel.HIGH, valid_from=now, expires_at=expires,
                constraints={"ordinary_interaction_only": True},
            )
        response = (
            f"Для канала {resource} требуется настройка интеграции."
            if integration and not configured else "Временное поручение подготовлено."
        )
        if llm_calls and self._safe_response(proposed.get("natural_response", "")):
            response = _clean(proposed["natural_response"])
        return SemanticInterpretation(
            SemanticMode.DELEGATED_MISSION, goal=text,
            trigger=MissionTrigger.event(f"integration.{resource}.message_received").to_dict() if resource else None,
            authority_proposal=proposal, temporary_context=temporary,
            missing_required_information=missing, required_integration=integration,
            integration_configured=configured, natural_response=response,
            reporting_policy="summarize_on_return_or_scope_violation",
            source="structured_brain" if llm_calls else "local_unambiguous",
            llm_calls=llm_calls,
        )

    def _validate_backend(self, raw: Mapping[str, Any]) -> SemanticInterpretation:
        try:
            mode = SemanticMode(str(raw["mode"]))
        except (KeyError, ValueError):
            return SemanticInterpretation(SemanticMode.CLARIFY,
                                          missing_required_information=["valid_interpretation"],
                                          source="structured_brain", llm_calls=1)
        return SemanticInterpretation(mode, goal=_clean(raw.get("goal", "")),
                                      source="structured_brain", llm_calls=1)

    def _brain_response(self, text: str, facts: Mapping[str, Any], fallback: str) -> tuple[str, int]:
        if self.structured_backend is None:
            return fallback, 0
        try:
            raw = dict(self.structured_backend(text, {
                "schema": {"natural_response": "short string without internal IDs"},
                "validated_facts": dict(facts),
                "rule": "word only these facts; do not invent execution success",
            }))
            value = raw.get("natural_response", "")
            return (_clean(value), 1) if self._safe_response(value) else (fallback, 1)
        except (TypeError, ValueError, KeyError):
            return fallback, 1

    @staticmethod
    def _safe_response(value: Any) -> bool:
        text = _clean(value)
        lowered = text.casefold()
        return bool(text and len(text) <= 300 and "{" not in text and "}" not in text
                    and "mission-" not in lowered and "jarvis-" not in lowered
                    and "trigger" not in lowered and "schema" not in lowered)

    @staticmethod
    def _scheduled_response(due: datetime) -> str:
        return f"Напомню {due.astimezone().strftime('%d.%m в %H:%M')}."


class NaturalMissionCoordinator:
    """Apply validated interpretations through the existing P2A/P2B owners."""

    def __init__(self, runtime: TaskRuntime, authority: AuthorityStore,
                 interpreter: NaturalMissionInterpreter) -> None:
        self.runtime = runtime
        self.authority = authority
        self.interpreter = interpreter
        self.ask_policy = AskOncePolicy()

    def _active(self) -> list[Mission]:
        return sorted(self.runtime.list_missions(include_terminal=False),
                      key=lambda item: item.updated_at, reverse=True)

    def handle(self, text: str, *, source_role: str = "user",
               source_id: str = "user-input",
               interpretation: SemanticInterpretation | None = None) -> NaturalMissionResult:
        item = interpretation or self.interpreter.interpret(text, source_role=source_role)
        if item.mode in {SemanticMode.CONVERSATION, SemanticMode.IMMEDIATE_ACTION, SemanticMode.CLARIFY}:
            return NaturalMissionResult(item, item.natural_response)

        if item.mode is SemanticMode.MISSION_CONTROL:
            candidates = self._active()
            if len(candidates) != 1:
                item.missing_required_information = ["referenced_mission"]
                return NaturalMissionResult(item, "Какую именно задачу?", clarification="Какую именно задачу?")
            mission = candidates[0]
            action = item.control_action
            if action == "cancel":
                self.runtime.cancel(mission.task_id)
            elif action == "pause":
                self.runtime.pause(mission.task_id)
            elif action == "resume":
                self.runtime.resume(mission.task_id)
            return NaturalMissionResult(item, "Готово.", mission=self.runtime.get(mission.task_id))

        if item.mode is SemanticMode.FOLLOW_UP:
            candidates = self._active()
            if len(candidates) != 1:
                item.missing_required_information = ["referenced_mission"]
                return NaturalMissionResult(item, "Какую именно задачу изменить?",
                                            clarification="Какую именно задачу изменить?")
            mission = candidates[0]
            if item.trigger:
                self.runtime.reschedule(mission.task_id, item.trigger)
                return NaturalMissionResult(item, item.natural_response, mission=mission)
            if "subject_to" in item.updates:
                old = [
                    g for g in self.authority.list(include_terminal=False)
                    if g.mission_id == mission.task_id
                ]
                for grant in old:
                    self.authority.revoke(grant.grant_id, "subject corrected by user")
                proposal_raw = mission.context.get("authority_proposal") or {}
                proposal = AuthorityProposal(**{**proposal_raw, "subjects": [item.updates["subject_to"]],
                                                 "mission_id": mission.task_id})
                grant = self.authority.issue(
                    proposal, source_kind=ProvenanceKind.USER_INSTRUCTION,
                    source_role=source_role, source_text=text, source_id=source_id,
                )
                mission.context["authority_proposal"] = self._proposal_dict(proposal)
                mission.context["authority_request"]["subject"] = item.updates["subject_to"]
                self.runtime.update_context(mission.task_id, mission.context)
                return NaturalMissionResult(item, "Адресат обновлён.", mission=mission, grant=grant)

        if item.missing_required_information:
            question = self.ask_once_for_interpretation(item)
            return NaturalMissionResult(item, question, clarification=question)
        if item.trigger is None:
            item.missing_required_information = ["valid_trigger"]
            return NaturalMissionResult(item, "Нужно уточнить условие запуска.",
                                        clarification="Нужно уточнить условие запуска.")

        context = {
            "semantic_mode": item.mode.value, "temporary_context": item.temporary_context,
            "reporting_policy": item.reporting_policy,
            "required_integration": item.required_integration,
            "integration_configured": item.integration_configured,
        }
        metadata = {"semantic_interpretation": True, "requires_integration": item.required_integration}
        if item.mode is SemanticMode.SCHEDULED_MISSION:
            context["notification_text"] = item.goal
            metadata["durable_kind"] = "reminder"
        if item.authority_proposal is not None:
            proposal = item.authority_proposal
            context["authority_proposal"] = self._proposal_dict(proposal)
            context["authority_request"] = {
                "subject": proposal.subjects[0], "resource": proposal.resources[0],
                "action": "send_message", "capability_family": "conversation",
                "purpose": proposal.purposes[0], "constraints": dict(proposal.constraints),
            }
        mission = self.runtime.schedule(
            item.goal, item.trigger, context=context,
            metadata=metadata,
        )
        grant = None
        if item.authority_proposal is not None and item.integration_configured:
            item.authority_proposal.mission_id = mission.task_id
            mission.context["authority_proposal"] = self._proposal_dict(item.authority_proposal)
            grant = self.authority.issue(
                item.authority_proposal, source_kind=ProvenanceKind.USER_INSTRUCTION,
                source_role=source_role, source_text=text, source_id=source_id,
            )
            self.runtime.update_context(mission.task_id, mission.context)
        return NaturalMissionResult(item, item.natural_response, mission=mission, grant=grant)

    def ask_once_for_interpretation(self, item: SemanticInterpretation) -> str:
        mapping = {
            "subject": "Кому именно передать это поручение?",
            "resource": "В каком канале это должно работать?",
            "updated_schedule": "На какое время перенести?",
            "real_user_instruction": "Подтвердите это поручение своей репликой.",
        }
        key = item.missing_required_information[0]
        return mapping.get(key, "Какой именно параметр нужно использовать?")

    def ask_once(self, mission: Mission, uncertainties: list[str]) -> Optional[str]:
        selected = self.ask_policy.choose_for(mission.context, uncertainties)
        self.runtime.update_context(mission.task_id, mission.context)
        return selected

    def answer_once(self, mission: Mission, question: str, answer: Any) -> None:
        self.ask_policy.record_answer(mission.context, question, answer)
        self.runtime.update_context(mission.task_id, mission.context)

    @staticmethod
    def _proposal_dict(proposal: AuthorityProposal) -> dict[str, Any]:
        return {
            "principal": proposal.principal, "delegate": proposal.delegate,
            "subjects": list(proposal.subjects), "resources": list(proposal.resources),
            "allowed_actions": list(proposal.allowed_actions),
            "capability_families": list(proposal.capability_families),
            "allowed_effects": list(proposal.allowed_effects),
            "denied_actions": list(proposal.denied_actions), "purposes": list(proposal.purposes),
            "risk_ceiling": RiskLevel(proposal.risk_ceiling).value,
            "valid_from": proposal.valid_from.isoformat() if isinstance(proposal.valid_from, datetime) else proposal.valid_from,
            "expires_at": proposal.expires_at.isoformat() if isinstance(proposal.expires_at, datetime) else proposal.expires_at,
            "mission_id": proposal.mission_id, "commitment_id": proposal.commitment_id,
            "constraints": dict(proposal.constraints),
        }
