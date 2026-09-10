"""Pre-step world-state verification for the Executive Planner.

Checks whether the preconditions of a PlanStep are still satisfied before
execution begins.  Uses the existing WorldState / UnifiedWorldState from
core/executive/ — no new dependencies.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.utils.logger import get_logger

log = get_logger(__name__)


class WorldCheck:
    """Evaluates PlanStep preconditions against the current world state.

    The world_state object is duck-typed: it only needs to expose
    ``observe_domain(domain, force=True)`` returning an object with a
    ``value`` attribute.  This matches both ``UnifiedWorldState`` and test
    stubs.
    """

    def __init__(self, world_state: Optional[Any] = None) -> None:
        self._world = world_state

    def set_world_state(self, world_state: Any) -> None:
        self._world = world_state

    def check_preconditions(
        self,
        preconditions: List[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, List[str]]:
        """Evaluate a list of precondition strings.

        Preconditions are plain-English strings like:
          - "blender_installed"
          - "internet_available"
          - "file_exists:C:/Users/user/Desktop/setup.exe"
          - "process_running:blender"

        Returns (all_ok, list_of_failed_conditions).
        """
        if not preconditions:
            return True, []

        failed: List[str] = []
        ctx = context or {}

        for cond in preconditions:
            ok = self._evaluate(cond.strip(), ctx)
            if not ok:
                failed.append(cond)

        return len(failed) == 0, failed

    def _evaluate(self, condition: str, ctx: Dict[str, Any]) -> bool:
        """Evaluate a single precondition string."""
        # Context overrides (for testing / plan metadata)
        if condition in ctx:
            return bool(ctx[condition])

        # Structured conditions: "key:value"
        if ":" in condition:
            key, _, value = condition.partition(":")
            return self._check_structured(key.strip(), value.strip())

        # World-state domain checks
        if self._world is not None:
            return self._check_world_domain(condition)

        # Unknown condition without world state — optimistic (don't block)
        log.debug("WorldCheck: no world state, assuming '%s' is satisfied", condition)
        return True

    def _check_structured(self, key: str, value: str) -> bool:
        """Handle structured preconditions like file_exists:path."""
        import os
        if key == "file_exists":
            return os.path.exists(value)
        if key == "process_running":
            return self._process_running(value)
        if key == "env_var":
            return bool(os.environ.get(value))
        if key == "world":
            # world:domain.path=expected
            if "=" in value:
                path, _, expected = value.partition("=")
                return self._world_path_equals(path.strip(), expected.strip())
        # Unknown structured condition — optimistic
        log.debug("WorldCheck: unknown structured condition '%s:%s'", key, value)
        return True

    def _process_running(self, name: str) -> bool:
        try:
            import psutil  # type: ignore[import]
            for proc in psutil.process_iter(["name"]):
                if proc.info["name"] and name.lower() in proc.info["name"].lower():
                    return True
            return False
        except Exception:
            # psutil not available or access denied — optimistic
            return True

    def _check_world_domain(self, condition: str) -> bool:
        """Check a world-state domain by name."""
        if self._world is None:
            return True
        try:
            fact = self._world.observe_domain(condition, force=False)
            if fact is None:
                return True  # domain unknown — optimistic
            error = getattr(fact, "error", None)
            if error:
                log.debug("WorldCheck: domain '%s' has error: %s", condition, error)
                return True  # can't determine — optimistic
            value = getattr(fact, "value", None)
            return bool(value)
        except Exception as exc:
            log.debug("WorldCheck: domain '%s' check failed: %s", condition, exc)
            return True

    def _world_path_equals(self, path: str, expected: str) -> bool:
        """Check world.domain.subpath == expected."""
        if self._world is None:
            return True
        parts = path.split(".", 1)
        domain = parts[0]
        subpath = parts[1] if len(parts) > 1 else ""
        try:
            fact = self._world.observe_domain(domain, force=False)
            if fact is None:
                return True
            value = getattr(fact, "value", None)
            if subpath and isinstance(value, dict):
                for part in subpath.split("."):
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        return True  # path not found — optimistic
            return str(value) == expected
        except Exception:
            return True
