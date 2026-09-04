# -*- coding: utf-8 -*-
"""Скрипт калибровки порогов уверенности и серой зоны семантического роутера Jarvis.

Калибровка проводится строго на dev-выборке eval-сета (h % 100 < 55).
Критерий калибровки:
  - Минимизация harmful_error_rate = (wrong_tool_execution + negatives_fp) / total_dev
  - Ограничения: clarify_rate <= 15%, High-Risk False Negatives == 0, Negatives FP <= 10%
  - Tie-breaker: максимизация kind_accuracy, затем tool_accuracy.
Результат сохраняется в core/routing/thresholds.json.

Использование:
    python scripts/routing_calibrate.py
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.capabilities import CAPABILITIES
from core.routing.semantic_router import (
    RoutingContext,
    RoutingDecision,
    SemanticRouter,
    assess_risk,
    normalize_text,
)

DEV_SPLIT_MODULO = 55
DEFAULT_DATASET = PROJECT_ROOT / "tests" / "routing" / "routing_eval_set.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "core" / "routing" / "thresholds.json"


def is_dev_item(text: str) -> bool:
    """Хеш-сплит для dev (55%) и holdout (45%)."""
    h = int(hashlib.sha256(text.strip().encode("utf-8")).hexdigest(), 16)
    return (h % 100) < DEV_SPLIT_MODULO


def is_negative_trap(item: Dict[str, Any]) -> bool:
    note = str(item.get("note", "")).lower()
    exp_k = item.get("expected_kind")
    exp_t = item.get("expected_tool")
    intent = str(item.get("intent", "none")).lower()
    return "негатив" in note or "но " in note or (exp_k in ("chat", "question") and exp_t is None and intent != "none")


def precompute_dev(router: SemanticRouter, dev_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Предрасчет k-NN кандидатов для dev-выборки для ускорения сеточного поиска."""
    router.ensure_index()
    assert router._index_vectors is not None

    precomputed = []
    t0 = time.perf_counter()
    print(f"Предрасчет векторов для {len(dev_items)} dev-запросов...")

    for it in dev_items:
        raw = str(it["text"]).strip()
        norm = normalize_text(raw)
        risk_level, needs_conf = assess_risk(None, None, raw)

        is_shortcut = norm in router.CANONICAL_SHORTCUTS
        s_kind, s_tool = router.CANONICAL_SHORTCUTS.get(norm, ("", None))

        q_vec = router.embed_text(raw)
        sims = np.dot(router._index_vectors, q_vec)

        top_k_idx = np.argsort(sims)[::-1][: router.k]
        s_max = float(sims[top_k_idx[0]])

        top_sims = np.array([float(sims[i]) for i in top_k_idx])
        weights = np.exp((top_sims - s_max) / router.tau)
        weights /= weights.sum()

        tool_scores: Dict[str, float] = {}
        kind_scores: Dict[str, float] = {}

        for rank, idx in enumerate(top_k_idx):
            w = float(weights[rank])
            s = float(sims[idx])
            t = router._index_tools[idx]
            kd = router._index_kinds[idx]
            cf = router._index_counter_for[idx]

            if t is not None:
                tool_scores[t] = tool_scores.get(t, 0.0) + w * s
                act_k = "fresh_data" if t in ("weather", "public_data") else "action"
                kind_scores[act_k] = kind_scores.get(act_k, 0.0) + w * s
            else:
                kind_scores[kd] = kind_scores.get(kd, 0.0) + w * s

            if cf is not None:
                tool_scores[cf] = max(0.0, tool_scores.get(cf, 0.0) - router.alpha * w * s)

        candidates: List[Tuple[str, Optional[str], float]] = []
        for t, sc in tool_scores.items():
            if sc > 0.001:
                kd = "fresh_data" if t in ("weather", "public_data") else "action"
                candidates.append((kd, t, sc))
        for kd, sc in kind_scores.items():
            if sc > 0.001:
                if kd not in ("action", "fresh_data"):
                    candidates.append((kd, None, sc))
                elif kd == "action":
                    unsup_sc = sum(
                        float(weights[r]) * float(sims[idx])
                        for r, idx in enumerate(top_k_idx)
                        if router._index_kinds[idx] == "action" and router._index_tools[idx] is None
                    )
                    if unsup_sc > 0.001:
                        candidates.append(("action", None, unsup_sc))

        candidates.sort(key=lambda x: -x[2])
        if not candidates:
            candidates = [("chat", None, s_max)]

        top1_kind, top1_tool, top1_score = candidates[0]
        top2_score = candidates[1][2] if len(candidates) > 1 else 0.0
        margin_agg = top1_score - top2_score

        tool_risk, tool_conf = assess_risk(top1_tool, None, raw)

        precomputed.append({
            "item": it,
            "raw": raw,
            "is_shortcut": is_shortcut,
            "s_kind": s_kind,
            "s_tool": s_tool,
            "s_max": s_max,
            "top1_kind": top1_kind,
            "top1_tool": top1_tool,
            "margin_agg": margin_agg,
            "needs_conf": tool_conf if top1_tool else needs_conf,
        })

    print(f"Предрасчет завершен за {time.perf_counter() - t0:.2f} с")
    return precomputed


def evaluate_threshold_point(
    precomputed: List[Dict[str, Any]],
    conf_th: float,
    margin_th: float,
) -> Dict[str, Any]:
    """Быстрая оценка одной точки сетки порогов."""
    correct_kind = 0
    action_total = 0
    action_tool_correct = 0
    neg_total = 0
    neg_fp = 0
    hr_total = 0
    hr_fn = 0
    unsup_total = 0
    unsup_wrong_sub = 0
    clarify_count = 0
    tier_counts: Dict[str, int] = {}

    for p in precomputed:
        it = p["item"]
        exp_kind = it.get("expected_kind", "chat")
        exp_tool = it.get("expected_tool")
        hr = bool(it.get("high_risk", False))
        is_unsup = bool(it.get("unsupported", False))
        is_neg = is_negative_trap(it)

        if p["is_shortcut"]:
            tier = "shortcut"
            pred_kind = p["s_kind"]
            pred_tool = p["s_tool"]
        elif p["s_max"] >= conf_th and p["margin_agg"] >= margin_th:
            tier = "semantic"
            pred_kind = p["top1_kind"]
            pred_tool = p["top1_tool"]
        else:
            tier = "clarify"
            pred_kind = "clarify"
            pred_tool = None

        tier_counts[tier] = tier_counts.get(tier, 0) + 1

        if tier == "clarify":
            clarify_count += 1

        if pred_kind == exp_kind:
            correct_kind += 1

        if exp_kind in ("action", "fresh_data") and exp_tool and not is_unsup:
            action_total += 1
            t_ok = (pred_tool == exp_tool)
            if not t_ok:
                if exp_tool in ("search_files", "list_files") and pred_tool in ("search_files", "list_files"):
                    t_ok = True
                elif exp_tool in ("screen_capture", "computer_screenshot") and pred_tool in ("screen_capture", "computer_screenshot"):
                    t_ok = True
            if t_ok:
                action_tool_correct += 1

        if is_neg:
            neg_total += 1
            if pred_kind in ("action", "fresh_data") or pred_tool is not None:
                neg_fp += 1

        if is_unsup:
            unsup_total += 1
            if pred_tool is not None:
                unsup_wrong_sub += 1

        if hr:
            hr_total += 1
            if not p["needs_conf"]:
                hr_fn += 1

    total = len(precomputed)
    kind_acc = correct_kind / total if total else 0.0
    tool_acc = action_tool_correct / action_total if action_total else 0.0
    neg_fp_rate = neg_fp / neg_total if neg_total else 0.0
    unsup_wrong_rate = unsup_wrong_sub / unsup_total if unsup_total else 0.0
    clarify_rate = clarify_count / total if total else 0.0

    wrong_tool_exec = (action_total - action_tool_correct) + unsup_wrong_sub
    harmful_err_rate = (wrong_tool_exec + neg_fp) / total if total else 0.0

    # Ограничения: 5% <= clarify <= 15%, High-Risk FN == 0, Negatives FP <= 10%
    is_valid = (0.05 <= clarify_rate <= 0.15) and (hr_fn == 0) and (neg_fp_rate <= 0.10)

    return {
        "confidence_threshold": conf_th,
        "margin_threshold": margin_th,
        "kind_accuracy": round(kind_acc, 4),
        "tool_accuracy": round(tool_acc, 4),
        "negatives_fp_rate": round(neg_fp_rate, 4),
        "negatives_fp_count": neg_fp,
        "negatives_total": neg_total,
        "high_risk_fn": hr_fn,
        "unsupported_wrong_sub_rate": round(unsup_wrong_rate, 4),
        "unsupported_wrong_sub_count": unsup_wrong_sub,
        "unsupported_total": unsup_total,
        "clarify_rate": round(clarify_rate, 4),
        "clarify_count": clarify_count,
        "harmful_error_rate": round(harmful_err_rate, 4),
        "tier_counts": tier_counts,
        "is_valid": is_valid,
    }


def calibrate(
    dataset_path: Path,
    output_path: Path,
    confidence_grid: Optional[List[float]] = None,
    margin_grid: Optional[List[float]] = None,
) -> Dict[str, Any]:
    with open(dataset_path, "r", encoding="utf-8") as f:
        all_items: List[Dict[str, Any]] = json.load(f)

    dev_items = [it for it in all_items if is_dev_item(it["text"])]
    print(f"Размер dev-выборки: {len(dev_items)} из {len(all_items)} фраз ({len(dev_items)/len(all_items)*100:.1f}%)")

    router = SemanticRouter()
    router.warmup()

    precomputed = precompute_dev(router, dev_items)

    conf_grid = confidence_grid or [0.38, 0.40, 0.42, 0.44, 0.46, 0.48, 0.50, 0.52]
    marg_grid = margin_grid or [0.03, 0.04, 0.05, 0.06, 0.07, 0.08]

    all_results: List[Dict[str, Any]] = []
    valid_results: List[Dict[str, Any]] = []

    for conf in conf_grid:
        for marg in marg_grid:
            res = evaluate_threshold_point(precomputed, conf, marg)
            all_results.append(res)
            if res["is_valid"]:
                valid_results.append(res)

    print(f"Исследовано точек сетки: {len(all_results)}, из них валидных: {len(valid_results)}")

    if valid_results:
        # Минимизируем harmful_error_rate, затем максимизируем kind_accuracy, tool_accuracy
        valid_results.sort(
            key=lambda r: (
                r["harmful_error_rate"],
                -r["kind_accuracy"],
                -r["tool_accuracy"],
                r["clarify_rate"],
            ),
        )
        best = valid_results[0]
    else:
        print("ВНИМАНИЕ: Нет точек, удовлетворяющих всем жестким ограничениям! Поиск наилучшей мягкой точки...")
        all_results.sort(
            key=lambda r: (
                r["high_risk_fn"] == 0,
                -r["negatives_fp_rate"],
                r["harmful_error_rate"],
                -r["kind_accuracy"],
            ),
            reverse=True,
        )
        best = all_results[0]

    is_edge_conf = best["confidence_threshold"] in (conf_grid[0], conf_grid[-1])
    is_edge_marg = best["margin_threshold"] in (marg_grid[0], marg_grid[-1])

    report = {
        "calibrated_at": datetime.datetime.now().isoformat(),
        "confidence_threshold": best["confidence_threshold"],
        "margin_threshold": best["margin_threshold"],
        "dev_kind_accuracy": best["kind_accuracy"],
        "dev_high_risk_fn": best["high_risk_fn"],
        "k": router.k,
        "alpha": router.alpha,
        "tau": router.tau,
        "metrics_on_dev": {
            "dev_samples": len(dev_items),
            "kind_accuracy": best["kind_accuracy"],
            "tool_accuracy": best["tool_accuracy"],
            "negatives_fp_rate": best["negatives_fp_rate"],
            "negatives_fp": f"{best['negatives_fp_count']}/{best['negatives_total']}",
            "high_risk_fn": best["high_risk_fn"],
            "unsupported_wrong_sub_rate": best["unsupported_wrong_sub_rate"],
            "unsupported_wrong_sub": f"{best['unsupported_wrong_sub_count']}/{best['unsupported_total']}",
            "clarify_rate": best["clarify_rate"],
            "clarify_count": f"{best['clarify_count']}/{len(dev_items)}",
            "harmful_error_rate": best["harmful_error_rate"],
            "tier_counts": best["tier_counts"],
        },
        "grid_boundary_check": {
            "conf_on_boundary": is_edge_conf,
            "marg_on_boundary": is_edge_marg,
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print(f"Калибровка успешно завершена! Результат сохранен в {output_path}")
    print(f"  Confidence Threshold: {best['confidence_threshold']}")
    print(f"  Margin Threshold    : {best['margin_threshold']}")
    print(f"  Kind Accuracy       : {best['kind_accuracy']*100:.1f}%")
    print(f"  Tool Accuracy (supp): {best['tool_accuracy']*100:.1f}%")
    print(f"  Negatives FP Rate   : {best['negatives_fp_rate']*100:.1f}% ({best['negatives_fp_count']}/{best['negatives_total']})")
    print(f"  High-Risk FN        : {best['high_risk_fn']}")
    print(f"  Unsupported Sub Rate: {best['unsupported_wrong_sub_rate']*100:.1f}% ({best['unsupported_wrong_sub_count']}/{best['unsupported_total']})")
    print(f"  Clarify Rate        : {best['clarify_rate']*100:.1f}% ({best['clarify_count']}/{len(dev_items)})")
    print(f"  Harmful Error Rate  : {best['harmful_error_rate']*100:.1f}%")
    print(f"  Tier Counts         : {best['tier_counts']}")
    print(f"  Положение на сетке  : conf на краю={is_edge_conf}, marg на краю={is_edge_marg}")
    print("=" * 60)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Калибровка порогов семантического роутера Jarvis")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="Путь к eval-сету")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Куда сохранить thresholds.json")
    args = parser.parse_args()

    calibrate(args.dataset, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
