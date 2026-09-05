"""Structured local research gateway for the Universal Mind loop.

The gateway deliberately delegates network access to the existing
``ResearchEngine`` and action registry.  It adds a small stable API so the
cognitive kernel can persist ``research_pending`` without treating an empty
search page as a successful answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping

from config.settings import Settings
from core.research import ResearchEngine, ResearchReport
from core.security.atomic import atomic_json_write, load_json


@dataclass
class ResearchResult:
    query: str
    status: str = "research_pending"
    sources: list[dict[str, Any]] = field(default_factory=list)
    source_errors: list[str] = field(default_factory=list)
    cached_results: list[dict[str, Any]] = field(default_factory=list)
    local_fallback: list[dict[str, Any]] = field(default_factory=list)
    resume_task_id: str = ""
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == "completed" and bool(self.sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "sources": list(self.sources),
            "source_errors": list(self.source_errors),
            "cached_results": list(self.cached_results),
            "local_fallback": list(self.local_fallback),
            "resume_task_id": self.resume_task_id,
            "report": dict(self.report),
        }


class ResearchGateway:
    """One local-only boundary for online/offline research missions."""

    def __init__(self, settings: Settings, *, engine: ResearchEngine | None = None) -> None:
        self.settings = settings
        self.engine = engine or ResearchEngine(settings)
        data_dir = getattr(getattr(settings, "paths", None), "resolved", lambda _name: None)("data_dir")
        self._pending_path = Path(data_dir or getattr(settings, "data_dir", Path("data"))) / "research" / "pending.json"
        raw = load_json(self._pending_path, default={})
        self._pending: dict[str, str] = {
            str(key): str(value)
            for key, value in (raw.get("tasks", {}) if isinstance(raw, dict) else {}).items()
            if str(key).strip() and str(value).strip()
        }

    def _persist_pending(self) -> None:
        # The file is deliberately tiny and contains only query text/task IDs;
        # source payloads and user secrets never enter the resume store.
        atomic_json_write(self._pending_path, {"tasks": dict(self._pending)})

    def search(self, query: str, constraints: Mapping[str, Any] | None = None) -> ResearchResult:
        query = " ".join(str(query or "").split())
        report = self.engine.run(query)
        result = self._from_report(report)
        if result.resume_task_id:
            self._pending[result.resume_task_id] = query
            self._persist_pending()
        elif result.status == "completed":
            stale = [task_id for task_id, pending_query in self._pending.items() if pending_query == query]
            if stale:
                for task_id in stale:
                    self._pending.pop(task_id, None)
                self._persist_pending()
        return result

    def fetch(self, source_url: str) -> dict[str, Any]:
        """Fetch one source through the existing verified action registry."""
        from core.actions import DEFAULT_REGISTRY
        from core.actions.base import ToolContext
        from core.actions.executor import execute_tool

        url = str(source_url or "").strip()
        if not url:
            return {"ok": False, "error": "source_url is required"}
        tool = DEFAULT_REGISTRY.get("web_fetch")
        if tool is None:
            return {"ok": False, "error": "web_fetch capability unavailable"}
        # S4: явные 2 повтора вместо опоры на дефолт execute_tool — дефолт стал
        # 0. web_fetch идемпотентен (паспорт), поэтому зажим их пропускает и
        # число попыток по сети остаётся прежним (3).
        outcome = execute_tool(DEFAULT_REGISTRY, "web_fetch", {"url": url}, ToolContext(settings=self.settings),
                               max_retries=2)
        return {"ok": bool(outcome.ok), "url": url, "content": outcome.output if outcome.ok else "", "error": outcome.error}

    def verify_sources(self, result: ResearchResult | ResearchReport | Mapping[str, Any]) -> dict[str, Any]:
        data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        sources = data.get("sources") or data.get("sources_read") or []
        findings = data.get("findings") or []
        return {
            "verified": bool(data.get("status") == "completed" and sources and findings),
            "source_count": len(sources),
            "evidence": findings,
        }

    def resume(self, task_id: str) -> ResearchResult:
        query = self._pending.get(str(task_id), "")
        if not query:
            return ResearchResult(query="", resume_task_id=str(task_id), source_errors=["unknown resume task"])
        result = self.search(query)
        if result.status == "completed":
            self._pending.pop(str(task_id), None)
            self._persist_pending()
        elif result.resume_task_id and result.resume_task_id != str(task_id):
            # Keep the original handle stable across retries so the UI can
            # resume one mission instead of accumulating ghost research IDs.
            self._pending[str(task_id)] = query
            result.resume_task_id = str(task_id)
            self._persist_pending()
        return result

    @staticmethod
    def _from_report(report: ResearchReport) -> ResearchResult:
        data = report.to_dict()
        observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        findings = list(report.findings or [])
        source_confidence: dict[str, float] = {}
        for finding in findings:
            for url in finding.sources:
                source_confidence[url] = max(
                    source_confidence.get(url, 0.0),
                    1.0 if len(finding.sources) >= 2 else 0.72,
                )
        return ResearchResult(
            query=report.query,
            status=report.status,
            sources=[{
                "url": url,
                "observed_at": observed_at,
                "confidence": round(source_confidence.get(url, 0.72), 3),
                "status": "available",
            } for url in report.sources_read],
            source_errors=[f"{url}: unavailable" for url in report.sources_failed],
            local_fallback=list(report.local_fallback),
            resume_task_id=report.resume_task_id,
            report=data,
        )


__all__ = ["ResearchGateway", "ResearchResult"]
