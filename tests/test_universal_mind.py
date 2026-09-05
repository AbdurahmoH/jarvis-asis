from __future__ import annotations

import json
from pathlib import Path

from core.intelligence import (
    EvidenceRecord,
    LatencyBudget,
    TutorEngine,
    TutorMode,
    UniversalIntake,
    choose_provider,
)
from core.memory.document_rag import read_document
from core.state import new_state
from core.llm.factory import clear_backend_cache, get_llm_backend
from config import Settings


def test_intake_is_fast_and_normalizes_modes():
    contract = UniversalIntake().classify("Установи тестовую программу и настрой как в инструкции")
    assert contract.intent_family == "install"
    assert contract.mode == "setup"
    assert contract.confidence >= 0.8


def test_intake_tutor_and_attachment_descriptors(tmp_path: Path):
    image = tmp_path / "homework.png"
    image.write_bytes(b"fixture")
    contract = UniversalIntake().classify("помоги с дз", attachments=[image])
    assert contract.mode == "tutor"
    assert contract.inputs[0]["kind"] == "image"


def test_latency_budget_and_evidence_are_serializable():
    result = LatencyBudget("fast", 600, 1000, 1500).check([100, 500, 800])
    assert result["pass"] is True
    evidence = EvidenceRecord("done", "fixture", latency_ms=12.3).to_dict()
    assert evidence["latency_ms"] == 12.3
    assert json.loads(json.dumps(evidence, ensure_ascii=False))["source"] == "fixture"


def test_tutor_default_is_socratic_and_check_is_bounded():
    engine = TutorEngine()
    session = __import__("core.intelligence", fromlist=["TeachingSession"]).TeachingSession("entropy")
    result = engine.teach("entropy", session=session)
    assert result.mode == TutorMode.SOCRATIC.value
    assert result.check_questions
    assert engine.check(session, "Entropy measures uncertainty in a system")["passed"] is True


def test_provider_priority_rejects_coordinates_by_default():
    assert choose_provider(["vision", "uia", "native_api"]).provider == "native_api"
    assert choose_provider(["coordinates"]) is None


def test_document_text_adapter(tmp_path: Path):
    path = tmp_path / "note.md"
    path.write_text("local knowledge", encoding="utf-8")
    assert read_document(path) == "local knowledge"


def test_offline_roles_share_one_local_backend_object(tmp_path: Path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    settings = Settings(offline_mode=True, warmup_local_on_start=False)
    settings.local_model.gguf_path = str(model)
    settings.model_tiers.fast = "qwen"
    settings.model_tiers.analyst = "qwen"
    settings.tier_providers.fast = "local"
    settings.tier_providers.analyst = "local"
    clear_backend_cache()
    fast = get_llm_backend(settings, "fast")
    analyst = get_llm_backend(settings, "analyst")
    assert fast is analyst


def test_state_has_task_contract_and_evidence_slots():
    state = new_state("объясни энтропию")
    # R2 (позиция 2): keyword-классификатор удалён из new_state — intent
    # проставляется оркестратором из решения semantic-роутера.
    assert state["intent"] is None
    assert state["task_contract"] == {}
    assert state["evidence"] == []
