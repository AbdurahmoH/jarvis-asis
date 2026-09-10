"""Typed contracts for the Executive Planner subsystem.

All dataclasses here are serialisable to/from plain dicts so they can be
persisted by TaskRuntime and transmitted over the WS plan_event stream.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ExecutionMode(str, Enum):
    """How the user wants to participate in plan execution."""
    AUTONOMOUS = "autonomous"   # Jarvis executes silently, reports on done/fail
    TOGETHER   = "together"     # Jarvis narrates each step, asks before choices
    ESCORT     = "escort"       # User acts, Jarvis watches screen and guides


class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    WAITING   = "waiting"    # waiting for user action / external event
    DONE      = "done"
    SKIPPED   = "skipped"
    FAILED    = "failed"


class PlanStatus(str, Enum):
    DRAFT      = "draft"
    ACTIVE     = "active"
    PAUSED     = "paused"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class PlanEventKind(str, Enum):
    PLAN_CREATED    = "plan_created"
    STEP_STARTED    = "step_started"
    STEP_DONE       = "step_done"
    STEP_FAILED     = "step_failed"
    STEP_SKIPPED    = "step_skipped"
    WAITING_USER    = "waiting_user"
    PLAN_COMPLETED  = "plan_completed"
    PLAN_FAILED     = "plan_failed"
    PLAN_PAUSED     = "plan_paused"
    PLAN_RESUMED    = "plan_resumed"
    PLAN_CANCELLED  = "plan_cancelled"
    WORLD_CHANGED   = "world_changed"
    REPLANNED       = "replanned"
    VOICE_STATUS    = "voice_status"


# ---------------------------------------------------------------------------
# Core step model
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    """One atomic step inside a StructuredPlan.

    Steps can be nested: ``subplan`` holds a list of child PlanStep objects
    for compound goals.  Either ``tool_name`` or ``subplan`` is set, not both.
    """
    step_id:           str                        = field(default_factory=lambda: f"step-{uuid.uuid4().hex[:8]}")
    intent:            str                        = ""
    tool_name:         Optional[str]              = None
    args_template:     Dict[str, Any]             = field(default_factory=dict)
    preconditions:     List[str]                  = field(default_factory=list)
    verification:      List[str]                  = field(default_factory=list)
    on_success:        Optional[str]              = None   # step_id to jump to
    on_failure:        str                        = "ask"  # ask | retry | skip | abort
    requires_user:     bool                       = False  # must pause for user action
    estimated_seconds: Optional[int]              = None
    subplan:           List["PlanStep"]           = field(default_factory=list)
    status:            StepStatus                 = StepStatus.PENDING
    result:            Optional[str]              = None
    error:             Optional[str]              = None
    # voice narration for TOGETHER / ESCORT modes
    narration:         str                        = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id":           self.step_id,
            "intent":            self.intent,
            "tool_name":         self.tool_name,
            "args_template":     self.args_template,
            "preconditions":     self.preconditions,
            "verification":      self.verification,
            "on_success":        self.on_success,
            "on_failure":        self.on_failure,
            "requires_user":     self.requires_user,
            "estimated_seconds": self.estimated_seconds,
            "subplan":           [s.to_dict() for s in self.subplan],
            "status":            self.status.value,
            "result":            self.result,
            "error":             self.error,
            "narration":         self.narration,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PlanStep":
        return cls(
            step_id           = str(d.get("step_id") or f"step-{uuid.uuid4().hex[:8]}"),
            intent            = str(d.get("intent") or ""),
            tool_name         = d.get("tool_name"),
            args_template     = dict(d.get("args_template") or {}),
            preconditions     = list(d.get("preconditions") or []),
            verification      = list(d.get("verification") or []),
            on_success        = d.get("on_success"),
            on_failure        = str(d.get("on_failure") or "ask"),
            requires_user     = bool(d.get("requires_user", False)),
            estimated_seconds = d.get("estimated_seconds"),
            subplan           = [PlanStep.from_dict(s) for s in (d.get("subplan") or [])],
            status            = StepStatus(d.get("status", "pending")),
            result            = d.get("result"),
            error             = d.get("error"),
            narration         = str(d.get("narration") or ""),
        )


# ---------------------------------------------------------------------------
# Plan request / plan itself
# ---------------------------------------------------------------------------

@dataclass
class PlanRequest:
    """Input to Planner.plan()."""
    goal:          str
    profile_id:    str                   = "default"
    context:       Dict[str, Any]        = field(default_factory=dict)
    mode:          ExecutionMode         = ExecutionMode.TOGETHER
    capabilities:  List[str]             = field(default_factory=list)
    grants:        List[str]             = field(default_factory=list)
    world_snapshot: Dict[str, Any]       = field(default_factory=dict)


@dataclass
class StructuredPlan:
    """A complete, durable, executable plan."""
    plan_id:    str                  = field(default_factory=lambda: f"plan-{uuid.uuid4().hex[:12]}")
    goal:       str                  = ""
    profile_id: str                  = "default"
    mode:       ExecutionMode        = ExecutionMode.TOGETHER
    steps:      List[PlanStep]       = field(default_factory=list)
    status:     PlanStatus           = PlanStatus.DRAFT
    created_at: str                  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str                  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    mission_id: Optional[str]        = None   # linked TaskRuntime mission
    metadata:   Dict[str, Any]       = field(default_factory=dict)

    # index of the currently active step (flat, depth-first)
    current_step_index: int          = 0

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    @property
    def current_step(self) -> Optional[PlanStep]:
        flat = self._flat_steps()
        if 0 <= self.current_step_index < len(flat):
            return flat[self.current_step_index]
        return None

    def _flat_steps(self) -> List[PlanStep]:
        """Depth-first flattening of the step tree."""
        result: List[PlanStep] = []
        def _walk(steps: List[PlanStep]) -> None:
            for s in steps:
                result.append(s)
                if s.subplan:
                    _walk(s.subplan)
        _walk(self.steps)
        return result

    def progress(self) -> float:
        flat = self._flat_steps()
        if not flat:
            return 0.0
        done = sum(1 for s in flat if s.status in (StepStatus.DONE, StepStatus.SKIPPED))
        return done / len(flat)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id":            self.plan_id,
            "goal":               self.goal,
            "profile_id":         self.profile_id,
            "mode":               self.mode.value,
            "steps":              [s.to_dict() for s in self.steps],
            "status":             self.status.value,
            "created_at":         self.created_at,
            "updated_at":         self.updated_at,
            "mission_id":         self.mission_id,
            "metadata":           self.metadata,
            "current_step_index": self.current_step_index,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StructuredPlan":
        return cls(
            plan_id            = str(d.get("plan_id") or f"plan-{uuid.uuid4().hex[:12]}"),
            goal               = str(d.get("goal") or ""),
            profile_id         = str(d.get("profile_id") or "default"),
            mode               = ExecutionMode(d.get("mode", "together")),
            steps              = [PlanStep.from_dict(s) for s in (d.get("steps") or [])],
            status             = PlanStatus(d.get("status", "draft")),
            created_at         = str(d.get("created_at") or datetime.now(timezone.utc).isoformat()),
            updated_at         = str(d.get("updated_at") or datetime.now(timezone.utc).isoformat()),
            mission_id         = d.get("mission_id"),
            metadata           = dict(d.get("metadata") or {}),
            current_step_index = int(d.get("current_step_index", 0)),
        )


# ---------------------------------------------------------------------------
# Execution state
# ---------------------------------------------------------------------------

@dataclass
class PlanExecution:
    """Runtime state for an executing plan (not persisted separately — lives in StructuredPlan.metadata)."""
    plan_id:       str
    mode:          ExecutionMode
    paused:        bool                  = False
    cancelled:     bool                  = False
    repair_count:  int                   = 0
    last_error:    Optional[str]         = None


# ---------------------------------------------------------------------------
# Events emitted during execution
# ---------------------------------------------------------------------------

@dataclass
class PlanEvent:
    """One observable event from the planner, forwarded to WS clients."""
    plan_id:    str
    kind:       PlanEventKind
    step_id:    Optional[str]            = None
    payload:    Dict[str, Any]           = field(default_factory=dict)
    timestamp:  str                      = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type":      "plan_event",
            "plan_id":   self.plan_id,
            "kind":      self.kind.value,
            "step_id":   self.step_id,
            "payload":   self.payload,
            "timestamp": self.timestamp,
        }
