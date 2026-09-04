# -*- coding: utf-8 -*-
"""Стенд измерения качества маршрутизации Jarvis (Baseline & Semantic k-NN Evaluation).

Поддерживает:
  - Baseline (Keyword only & capabilities embedding)
  - Semantic k-NN (Tier-0 Shortcut + Tier-0B Semantic + Tier-1 Fast LLM mock)
  - Сплиты: dev (55%), holdout (45%), all (100%)
  - Метрики: Kind Accuracy, Tool Accuracy (supported), Negatives FP, High-Risk FN,
    Unsupported wrong substitution rate, Clarify share, Latency p50/p95, Product Backlog.

Использование:
    python scripts/routing_eval.py --router semantic --split dev
    python scripts/routing_eval.py --router semantic --split dev --llm
    python scripts/routing_eval.py --router semantic --split holdout
    python scripts/routing_eval.py --router semantic --split holdout --llm
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings
from core.agent import Agent, AgentOutcome
from core.capabilities import CAPABILITIES, Capability
from core.model_router import classify_conversation
from core.orchestrator import Orchestrator
from core.router.intent_router import resolve_keyword_tool, split_compound_commands
from core.routing.semantic_router import RoutingContext, RoutingDecision, SemanticRouter
from core.safety import assess_risk as baseline_assess_risk
from core.understanding.layer import _MISSION_MARKERS, _QUESTION_MARKERS

DEV_SPLIT_MODULO = 55


def is_dev_item(text: str) -> bool:
    """Детерминированное разделение на dev (55%) и holdout (45%)."""
    h = int(hashlib.sha256(text.strip().encode("utf-8")).hexdigest(), 16)
    return (h % 100) < DEV_SPLIT_MODULO


def is_negative_trap(item: Dict[str, Any]) -> bool:
    note = str(item.get("note", "")).lower()
    exp_k = item.get("expected_kind")
    exp_t = item.get("expected_tool")
    intent = str(item.get("intent", "none")).lower()
    return "негатив" in note or "но " in note or (exp_k in ("chat", "question") and exp_t is None and intent != "none")


@dataclass
class EvalResult:
    text: str
    expected_kind: str
    expected_tool: Optional[str]
    expected_high_risk: bool
    predicted_kind: str
    predicted_tool: Optional[str]
    predicted_high_risk: bool
    kind_correct: bool
    tool_correct: bool
    is_negative_trap: bool
    is_negative_fp: bool
    is_high_risk_fn: bool
    is_unsupported: bool
    is_unsupported_wrong_sub: bool
    requested_unsupported_tool: Optional[str]
    latency_ms: float
    gate_taken: str
    intent: str
    clarify_question: Optional[str] = None
    note: str = ""


class RoutingEvaluator:
    """Baseline Evaluator: текущий агент и эвристики."""

    def __init__(self, settings: Optional[Settings] = None, use_embedding: bool = False) -> None:
        self.settings = settings or Settings()
        self.agent = Agent(self.settings)
        self.use_embedding = use_embedding
        if self.use_embedding:
            CAPABILITIES._ensure_embedder()

    def evaluate_item(self, item: Dict[str, Any]) -> EvalResult:
        text = str(item.get("text", "")).strip()
        expected_kind = item.get("expected_kind", "chat")
        expected_tool = item.get("expected_tool")
        expected_high_risk = bool(item.get("high_risk", False))
        is_unsup = bool(item.get("unsupported", False))
        req_unsup = item.get("requested_unsupported_tool")
        note = item.get("note", "")

        t0 = time.perf_counter()

        intent = resolve_keyword_tool(text, text)
        risk = baseline_assess_risk(text)
        predicted_high_risk = risk.needs_confirmation

        predicted_kind = ""
        predicted_tool: Optional[str] = None
        gate_taken = ""

        lowered = " ".join(text.casefold().replace("ё", "е").split())
        is_weather = any(w in lowered for w in ("погода", "температура", "осадки", "дождь", "снег"))
        is_currency = any(w in lowered for w in ("курс доллара", "курс евро", "курс валют", "биткоин", "стоимость акций"))

        if is_weather:
            predicted_kind = "fresh_data"
            predicted_tool = "weather"
            gate_taken = "fresh_information"
        elif is_currency:
            predicted_kind = "fresh_data"
            predicted_tool = "public_data"
            gate_taken = "fresh_information"
        else:
            classified_kind = classify_conversation(text, intent)
            tokens = text.split()
            token_count = len(tokens)

            if intent == "none" and token_count <= 8 and classified_kind in ("chit_chat", "general"):
                predicted_kind = "chat"
                gate_taken = "short_chit_chat_heuristic"
            elif _QUESTION_MARKERS.search(lowered) and intent == "none":
                predicted_kind = "question"
                gate_taken = "question_marker"
            elif _MISSION_MARKERS.search(lowered):
                predicted_kind = "mission"
                gate_taken = "mission_marker"
            elif intent != "none":
                predicted_kind = "action"
                predicted_tool = intent
                gate_taken = "intent_router_keyword"
            elif self.use_embedding:
                candidates = CAPABILITIES.retrieve(text, threshold=0.45, limit=1)
                if candidates:
                    predicted_kind = "action"
                    predicted_tool = candidates[0].name
                    gate_taken = "capability_embedding"
                else:
                    predicted_kind = "chat"
                    gate_taken = "fallback_chat"
            else:
                predicted_kind = "chat" if classified_kind == "chit_chat" else "question"
                gate_taken = "model_router_fallback"

        latency_ms = (time.perf_counter() - t0) * 1000.0

        tool_correct = (predicted_tool == expected_tool)
        if not tool_correct and expected_tool:
            if expected_tool in ("search_files", "list_files") and predicted_tool in ("search_files", "list_files"):
                tool_correct = True
            elif expected_tool in ("screen_capture", "computer_screenshot") and predicted_tool in ("screen_capture", "computer_screenshot"):
                tool_correct = True

        kind_correct = (predicted_kind == expected_kind)

        is_neg = is_negative_trap(item)
        is_negative_fp = is_neg and (predicted_kind in ("action", "fresh_data") or predicted_tool is not None)
        is_high_risk_fn = expected_high_risk and not predicted_high_risk
        is_unsup_wrong_sub = is_unsup and (predicted_tool is not None)

        return EvalResult(
            text=text,
            expected_kind=expected_kind,
            expected_tool=expected_tool,
            expected_high_risk=expected_high_risk,
            predicted_kind=predicted_kind,
            predicted_tool=predicted_tool,
            predicted_high_risk=predicted_high_risk,
            kind_correct=kind_correct,
            tool_correct=tool_correct,
            is_negative_trap=is_neg,
            is_negative_fp=is_negative_fp,
            is_high_risk_fn=is_high_risk_fn,
            is_unsupported=is_unsup,
            is_unsupported_wrong_sub=is_unsup_wrong_sub,
            requested_unsupported_tool=req_unsup,
            latency_ms=latency_ms,
            gate_taken=gate_taken,
            intent=intent,
            note=note,
        )


class SemanticEvaluator:
    """Semantic Evaluator: каскадный роутер с поддержкой серой зоны и FAST LLM мока."""

    def __init__(self, llm_available: bool = False, router: Optional[SemanticRouter] = None) -> None:
        self.router = router or SemanticRouter()
        self.router.warmup()
        self.llm_available = llm_available

    def _mock_fast_llm(self, text: str, candidates: List[Tuple[str, float]], exp_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Мок Fast LLM для серой зоны: 85% корректных, 10% некорректных, 5% невалидный JSON (пустой dict)."""
        h = int(hashlib.sha256(text.strip().encode("utf-8")).hexdigest(), 16) % 100

        # 5% невалидный JSON (пустой словарь)
        if h >= 95:
            return {}

        # 85% корректное разрешение серой зоны
        if h < 85 and exp_item is not None:
            exp_k = exp_item.get("expected_kind", "chat")
            exp_t = exp_item.get("expected_tool")
            return {"kind": exp_k, "tool": exp_t, "confidence": 0.88}

        # 10% некорректное разрешение (берем топ-кандидата без проверки)
        if candidates:
            top_label, score = candidates[0]
            if top_label in ("chat", "question", "mission"):
                return {"kind": top_label, "tool": None, "confidence": 0.75}
            kind = "fresh_data" if top_label in ("weather", "public_data") else "action"
            return {"kind": kind, "tool": top_label, "confidence": 0.75}

        return {"kind": "chat", "tool": None, "confidence": 0.70}

    def evaluate_item(self, item: Dict[str, Any]) -> EvalResult:
        text = str(item.get("text", "")).strip()
        expected_kind = item.get("expected_kind", "chat")
        expected_tool = item.get("expected_tool")
        expected_high_risk = bool(item.get("high_risk", False))
        is_unsup = bool(item.get("unsupported", False))
        req_unsup = item.get("requested_unsupported_tool")
        note = item.get("note", "")
        intent = resolve_keyword_tool(text, text)

        fast_llm_fn = (lambda t, cands: self._mock_fast_llm(t, cands, item)) if self.llm_available else None

        ctx = RoutingContext(
            llm_available=self.llm_available,
            fast_llm_fn=fast_llm_fn,
            allow_clarify=True,
        )

        t0 = time.perf_counter()
        dec = self.router.route(text, ctx)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        predicted_kind = dec.kind
        predicted_tool = dec.tool
        predicted_high_risk = dec.needs_confirmation

        tool_correct = (predicted_tool == expected_tool)
        if not tool_correct and expected_tool:
            if expected_tool in ("search_files", "list_files") and predicted_tool in ("search_files", "list_files"):
                tool_correct = True
            elif expected_tool in ("screen_capture", "computer_screenshot") and predicted_tool in ("screen_capture", "computer_screenshot"):
                tool_correct = True

        kind_correct = (predicted_kind == expected_kind)

        is_neg = is_negative_trap(item)
        is_negative_fp = is_neg and (predicted_kind in ("action", "fresh_data") or predicted_tool is not None)
        is_high_risk_fn = expected_high_risk and not predicted_high_risk
        is_unsup_wrong_sub = is_unsup and (predicted_tool is not None)

        return EvalResult(
            text=text,
            expected_kind=expected_kind,
            expected_tool=expected_tool,
            expected_high_risk=expected_high_risk,
            predicted_kind=predicted_kind,
            predicted_tool=predicted_tool,
            predicted_high_risk=predicted_high_risk,
            kind_correct=kind_correct,
            tool_correct=tool_correct,
            is_negative_trap=is_neg,
            is_negative_fp=is_negative_fp,
            is_high_risk_fn=is_high_risk_fn,
            is_unsupported=is_unsup,
            is_unsupported_wrong_sub=is_unsup_wrong_sub,
            requested_unsupported_tool=req_unsup,
            latency_ms=latency_ms,
            gate_taken=dec.tier,
            intent=intent,
            clarify_question=dec.clarify_question,
            note=note,
        )


class IntegratedEvaluator:
    """Интегрированный прогон: тот же датасет через реальный Orchestrator.handle_input.

    Замоканы:
      - инструменты: core.agent.execute_tool подменён на no-op ActionResult
        (реальных действий/IO нет);
      - провайдер: health-проба (кэш ``_llm_probe``) возвращает заданный
        llm_available, модель не вызывается (Agent.execute заменён стабом,
        который потребляет decision — единая точка решения).

    Предсказание читается из state["routing_decision"] (шаг 1), фактически
    выбранный инструмент — из state["tool"]. Расхождение с изолированным
    режимом = выживший старый гейт.
    """

    def __init__(self, llm_available: bool = False) -> None:
        import core.agent as agent_module
        from core.memory import SessionManager

        self.llm_available = llm_available
        self._agent_module = agent_module
        self._orig_execute_tool = agent_module.execute_tool
        agent_module.execute_tool = self._fake_execute_tool

        self.settings = Settings()
        self.settings.warmup_local_on_start = False
        self.orch = Orchestrator(self.settings)
        # handle_input не должен вызывать start() (провайдер/модель не грузим).
        self.orch._session = SessionManager(max_size=20)
        self.orch._running = True
        # Мок health-пробы: ключ в будущем, TTL-проверка всегда свежая.
        self.orch._llm_probe = (time.monotonic() + 10**9, llm_available)
        self.orch._model_router._fast_probe = (time.monotonic() + 10**9, llm_available)

        # Стаб Agent.execute: без модельных вызовов, но decision потребляется
        # и фактический инструмент отражается в outcome.tool_used.
        self._orig_agent_execute = self.orch._agent.execute

        def _stub_execute(goal: str, mission: Any = None, cancel: Any = None,
                          decision: Optional[RoutingDecision] = None,
                          **kwargs: Any) -> Any:
            dec = decision or RoutingDecision(
                kind="chat", tool=None, confidence=0.0, tier="stub",
                risk="low", needs_confirmation=False,
            )
            return AgentOutcome(
                text="ок",
                verified=True,
                tool_used=dec.tool,
                mode="fast_path" if dec.tool else "conversation",
            )

        self._stub_execute = _stub_execute
        self.orch._agent.execute = _stub_execute
        # В llm-режиме инжектируем тот же провайдер-мок, что и в изолированном
        # прогоне (реальный провайдер в eval не вызывается).
        if llm_available:
            self.orch._fast_llm_route = self._mock_fast_llm_route
            self.orch._mock_llm_item: Optional[Dict[str, Any]] = None

    def _mock_fast_llm_route(self, text: str,
                             candidates: List[Tuple[str, float]]) -> Dict[str, Any]:
        """Тот же мок FAST LLM, что в SemanticEvaluator (85/10/5)."""
        item = getattr(self.orch, "_mock_llm_item", None)
        h = int(hashlib.sha256(text.strip().encode("utf-8")).hexdigest(), 16) % 100
        if h >= 95:
            return {}
        if h < 85 and item is not None:
            return {
                "kind": item.get("expected_kind", "chat"),
                "tool": item.get("expected_tool"),
                "confidence": 0.88,
            }
        if candidates:
            top_label, _score = candidates[0]
            if top_label in ("chat", "question", "mission"):
                return {"kind": top_label, "tool": None, "confidence": 0.75}
            kind = "fresh_data" if top_label in ("weather", "public_data") else "action"
            return {"kind": kind, "tool": top_label, "confidence": 0.75}
        return {"kind": "chat", "tool": None, "confidence": 0.70}

    def _fake_execute_tool(self, registry: Any, tool_name: str, args: Dict[str, Any],
                           context: Any, max_retries: int = 2,
                           retry_delay: float = 0.5,
                           timeout_sec: Optional[float] = None) -> Any:
        from core.actions.base import ActionResult
        return ActionResult(
            tool=tool_name, args=dict(args or {}), ok=True,
            output=f"[integrated-mock] {tool_name}", error=None,
        )

    def close(self) -> None:
        self._agent_module.execute_tool = self._orig_execute_tool

    def evaluate_item(self, item: Dict[str, Any]) -> EvalResult:
        # Каждый элемент сета независим: clarify-сессия и memo маршрута
        # не должны перетекать между фразами.
        self.orch._clarification = None
        self.orch._route_memo = None
        if hasattr(self.orch, "_mock_llm_item"):
            self.orch._mock_llm_item = item
        text = str(item.get("text", "")).strip()
        expected_kind = item.get("expected_kind", "chat")
        expected_tool = item.get("expected_tool")
        expected_high_risk = bool(item.get("high_risk", False))
        is_unsup = bool(item.get("unsupported", False))
        req_unsup = item.get("requested_unsupported_tool")
        note = item.get("note", "")

        t0 = time.perf_counter()
        try:
            state = self.orch.handle_input(text)
        except Exception as exc:
            return EvalResult(
                text=text, expected_kind=expected_kind,
                expected_tool=expected_tool,
                expected_high_risk=expected_high_risk,
                predicted_kind="error", predicted_tool=None,
                predicted_high_risk=False, kind_correct=False,
                tool_correct=False, is_negative_trap=is_negative_trap(item),
                is_negative_fp=False, is_high_risk_fn=expected_high_risk,
                is_unsupported=is_unsup,
                is_unsupported_wrong_sub=False,
                requested_unsupported_tool=req_unsup,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                gate_taken="exception", intent=str(exc)[:80], note=note,
            )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        routing = (
            state.get("routing_decision")
            if isinstance(state, dict) else None
        ) or {}
        predicted_kind = str(routing.get("kind", "chat"))
        predicted_tool = routing.get("tool")
        predicted_high_risk = bool(routing.get("needs_confirmation", False))
        # Фактически исполненный инструмент (для wrong substitution).
        executed_tool = state.get("tool") if isinstance(state, dict) else None
        if predicted_tool is None and executed_tool:
            predicted_tool = str(executed_tool)

        tool_correct = (predicted_tool == expected_tool)
        if not tool_correct and expected_tool:
            if expected_tool in ("search_files", "list_files") and predicted_tool in ("search_files", "list_files"):
                tool_correct = True
            elif expected_tool in ("screen_capture", "computer_screenshot") and predicted_tool in ("screen_capture", "computer_screenshot"):
                tool_correct = True

        kind_correct = (predicted_kind == expected_kind)

        is_neg = is_negative_trap(item)
        is_negative_fp = is_neg and (predicted_kind in ("action", "fresh_data") or predicted_tool is not None)
        is_high_risk_fn = expected_high_risk and not predicted_high_risk
        is_unsup_wrong_sub = is_unsup and (predicted_tool is not None)

        return EvalResult(
            text=text,
            expected_kind=expected_kind,
            expected_tool=expected_tool,
            expected_high_risk=expected_high_risk,
            predicted_kind=predicted_kind,
            predicted_tool=predicted_tool,
            predicted_high_risk=predicted_high_risk,
            kind_correct=kind_correct,
            tool_correct=tool_correct,
            is_negative_trap=is_neg,
            is_negative_fp=is_negative_fp,
            is_high_risk_fn=is_high_risk_fn,
            is_unsupported=is_unsup,
            is_unsupported_wrong_sub=is_unsup_wrong_sub,
            requested_unsupported_tool=req_unsup,
            latency_ms=latency_ms,
            gate_taken=str(routing.get("tier", "integrated")),
            intent=str(routing.get("tier", "")),
            note=note,
        )


def run_evaluation(
    dataset_path: str,
    use_embedding: bool = False,
    router_mode: str = "baseline",
    split: str = "all",
    llm_available: bool = False,
) -> Tuple[List[EvalResult], Dict[str, Any]]:
    with open(dataset_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    if split == "dev":
        items = [it for it in items if is_dev_item(it["text"])]
    elif split == "holdout":
        items = [it for it in items if not is_dev_item(it["text"])]

    if router_mode == "semantic":
        evaluator: Any = SemanticEvaluator(llm_available=llm_available)
    elif router_mode == "integrated":
        evaluator = IntegratedEvaluator(llm_available=llm_available)
    else:
        evaluator = RoutingEvaluator(use_embedding=use_embedding)

    results: List[EvalResult] = []
    cold_start_lat = 0.0

    for idx, item in enumerate(items):
        res = evaluator.evaluate_item(item)
        if idx == 0:
            cold_start_lat = res.latency_ms
        results.append(res)

    total = len(results)
    kind_acc = sum(1 for r in results if r.kind_correct) / total if total else 0.0
    tool_acc = sum(1 for r in results if r.tool_correct) / total if total else 0.0

    # Метрика tool_accuracy только среди action/fresh_data с ожидаемым инструментом (supported)
    action_items = [r for r in results if r.expected_kind in ("action", "fresh_data") and r.expected_tool and not r.is_unsupported]
    action_tool_acc = sum(1 for r in action_items if r.tool_correct) / len(action_items) if action_items else 0.0

    negatives = [r for r in results if r.is_negative_trap]
    neg_fp = sum(1 for r in negatives if r.is_negative_fp)
    neg_fp_rate = (neg_fp / len(negatives)) if negatives else 0.0

    high_risks = [r for r in results if r.expected_high_risk]
    hr_fn = sum(1 for r in high_risks if r.is_high_risk_fn)
    hr_fn_rate = (hr_fn / len(high_risks)) if high_risks else 0.0

    unsup_items = [r for r in results if r.is_unsupported]
    unsup_wrong = sum(1 for r in unsup_items if r.is_unsupported_wrong_sub)
    unsup_wrong_rate = (unsup_wrong / len(unsup_items)) if unsup_items else 0.0

    # Замеры задержки прогретой модели (исключая холодный 1-й запуск)
    warm_latencies = sorted(r.latency_ms for r in (results[1:] if len(results) > 1 else results))
    p50_lat = warm_latencies[int(len(warm_latencies) * 0.50)] if warm_latencies else 0.0
    p95_lat = warm_latencies[int(len(warm_latencies) * 0.95)] if warm_latencies else 0.0

    # Распределение по tiers
    tier_counts: Dict[str, int] = {}
    for r in results:
        tier_counts[r.gate_taken] = tier_counts.get(r.gate_taken, 0) + 1
    clarify_count = tier_counts.get("clarify", 0)
    clarify_rate = clarify_count / total if total else 0.0

    # Сбор продуктового бэклога неподдерживаемых действий
    backlog_counts: Dict[str, int] = {}
    for r in unsup_items:
        t = r.requested_unsupported_tool or "unknown_unsupported"
        backlog_counts[t] = backlog_counts.get(t, 0) + 1
    sorted_backlog = sorted(backlog_counts.items(), key=lambda x: -x[1])

    # Confusion matrix
    kinds = ["action", "fresh_data", "question", "chat", "mission", "clarify"]
    conf_matrix = {exp: {pred: 0 for pred in kinds} for exp in kinds}
    for r in results:
        if r.expected_kind in conf_matrix and r.predicted_kind in conf_matrix[r.expected_kind]:
            conf_matrix[r.expected_kind][r.predicted_kind] += 1

    summary = {
        "total": total,
        "split": split,
        "router_mode": router_mode,
        "llm_available": llm_available,
        "use_embedding": use_embedding,
        "kind_accuracy": round(kind_acc, 4),
        "tool_accuracy": round(tool_acc, 4),
        "tool_accuracy_action_only": round(action_tool_acc, 4),
        "action_items_total": len(action_items),
        "negatives_total": len(negatives),
        "negatives_fp": neg_fp,
        "negatives_fp_rate": round(neg_fp_rate, 4),
        "high_risk_total": len(high_risks),
        "high_risk_fn": hr_fn,
        "high_risk_fn_rate": round(hr_fn_rate, 4),
        "unsupported_total": len(unsup_items),
        "unsupported_wrong_sub_count": unsup_wrong,
        "unsupported_wrong_sub_rate": round(unsup_wrong_rate, 4),
        "cold_start_latency_ms": round(cold_start_lat, 2),
        "latency_p50_ms": round(p50_lat, 2),
        "latency_p95_ms": round(p95_lat, 2),
        "tier_counts": tier_counts,
        "clarify_count": clarify_count,
        "clarify_rate": round(clarify_rate, 4),
        "product_backlog_unsupported": sorted_backlog,
        "confusion_matrix": conf_matrix,
    }

    return results, summary


def print_summary(summary: Dict[str, Any], results: List[EvalResult]) -> None:
    mode_str = f"Router: {summary['router_mode'].upper()} | Split: {summary['split'].upper()}"
    if summary["llm_available"]:
        mode_str += " (+FAST LLM Tier-1)"
    else:
        mode_str += " (Offline Tier-0 only)"

    print("\n" + "=" * 70)
    print(f"  {mode_str}")
    print("=" * 70)
    print(f"  Всего тестовых фраз        : {summary['total']}")
    print(f"  Kind Accuracy               : {summary['kind_accuracy']*100:.1f}%")
    print(f"  Tool Accuracy (поддержив.)  : {summary['tool_accuracy_action_only']*100:.1f}% ({summary['action_items_total']} действий)")
    print(f"  False Positives (негативы)  : {summary['negatives_fp_rate']*100:.1f}% ({summary['negatives_fp']}/{summary['negatives_total']})")
    print(f"  False Negatives (High-Risk) : {summary['high_risk_fn_rate']*100:.1f}% ({summary['high_risk_fn']}/{summary['high_risk_total']})")
    print(f"  Ложные подмены неподдержив. : {summary['unsupported_wrong_sub_rate']*100:.1f}% ({summary['unsupported_wrong_sub_count']}/{summary['unsupported_total']})")
    print(f"  Доля серой зоны (Clarify)   : {summary['clarify_rate']*100:.1f}% ({summary['clarify_count']}/{summary['total']})")
    print(f"  Распределение по Tier       : {summary['tier_counts']}")
    print(f"  Задержка (прогретая) p50/p95: {summary['latency_p50_ms']:.2f} ms / {summary['latency_p95_ms']:.2f} ms (холодный: {summary['cold_start_latency_ms']:.2f} ms)")
    print("-" * 70)

    # Вывод продуктового бэклога неподдерживаемых инструментов
    if summary["product_backlog_unsupported"]:
        print("  Бэклог неподдерживаемых возможностей (частотность запросов пользователей):")
        for tool_name, count in summary["product_backlog_unsupported"]:
            print(f"    - {tool_name:<25}: {count} запросов")
        print("-" * 70)

    # Примеры вопросов уточнения
    clarifies = [r for r in results if r.clarify_question]
    if clarifies:
        print(f"  Примеры сгенерированных вопросов уточнения ({min(len(clarifies), 5)} шт.):")
        for r in clarifies[:5]:
            print(f"    - «{r.text}» -> \"{r.clarify_question}\"")
        print("-" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description="Стенд оценки маршрутизации Jarvis")
    parser.add_argument("--dataset", default=str(PROJECT_ROOT / "tests" / "routing" / "routing_eval_set.json"), help="Путь к eval сету")
    parser.add_argument("--router", choices=["baseline", "semantic", "integrated"], default="semantic", help="Тип роутера")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev", help="Сплит выборки")
    parser.add_argument("--llm", action="store_true", help="Включить FAST LLM мок для серой зоны")
    parser.add_argument("--embedding", action="store_true", help="Для baseline: включить старый эмбеддер capabilities")
    parser.add_argument("--output-json", default=None, help="Куда сохранить JSON отчет")
    args = parser.parse_args()

    results, summary = run_evaluation(
        dataset_path=args.dataset,
        use_embedding=args.embedding,
        router_mode=args.router,
        split=args.split,
        llm_available=args.llm,
    )

    print_summary(summary, results)

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
