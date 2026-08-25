from __future__ import annotations

import threading
import time
from pathlib import Path

from core.memory.document_rag import DocumentHit, StudySession
from core.voice.output import AssistantOutput
from core.voice.stt import VoiceActivityDetector
from core.voice.tts_queue import TTSQueue
from persona.system_prompt import persona_core


class _BlockingTTS:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.stop_calls = 0
        self.spoken: list[str] = []

    def speak(self, text: str, blocking: bool = True) -> None:
        self.started.set()
        self.release.wait(1)
        if self.stop_calls == 0:
            self.spoken.append(text)

    def stop_speaking(self) -> None:
        self.stop_calls += 1
        self.release.set()


def test_voice_interrupt_invalidates_unsaid_generation() -> None:
    engine = _BlockingTTS()
    queue = TTSQueue(engine)
    queue.start()
    assert queue.add_output(AssistantOutput.natural("первая фраза"))
    assert queue.add_output(AssistantOutput.natural("устаревший хвост"))
    assert engine.started.wait(1)
    generation = queue.generation
    queue.interrupt()
    assert queue.generation == generation + 1
    assert queue.queue_size == 0
    assert engine.stop_calls == 1
    queue.stop()
    assert "устаревший хвост" not in engine.spoken


def test_voice_queue_tracks_only_completed_spoken_text() -> None:
    class _TTS:
        def speak(self, text: str, blocking: bool = True) -> None:
            return None

        def stop_speaking(self) -> None:
            return None

    queue = TTSQueue(_TTS())
    queue.start()
    queue.add_output(AssistantOutput.natural("сказано"))
    assert queue.wait_until_done(1)
    assert queue.spoken_text == ("сказано",)
    queue.stop()


def test_vad_fails_closed_when_no_backend() -> None:
    vad = VoiceActivityDetector(prefer_silero=False, fail_closed=True)
    vad._vad = None
    assert vad.is_speech(b"\x01\x00" * 320) is False


def test_study_session_preserves_citations_and_retries_weak_topics(tmp_path: Path) -> None:
    source = tmp_path / "biology.txt"
    source.write_text("Клетка использует АТФ как переносчик энергии.", encoding="utf-8")
    hit = DocumentHit(
        text="Клетка использует АТФ как переносчик энергии.",
        source=str(source), chunk_index=0, score=0.94, sha256="abc",
    )
    session = StudySession.from_hits("Подготовка", [hit])
    assert session.citations() == [f"[{source.name}#chunk-0]"]
    question = session.next_question()
    session.record_answer(question.id, "не знаю", correct=False)
    assert session.weak_topics
    retry = session.next_question()
    assert retry.topic in session.weak_topics


def test_study_session_has_no_background_scan_or_model_call() -> None:
    started = time.perf_counter()
    session = StudySession.from_hits("Тест", [])
    assert session.next_question() is None
    assert time.perf_counter() - started < 0.05


def test_continuous_persona_contract_covers_all_response_paths() -> None:
    prompt = persona_core().casefold()
    assert "один и тот же голос" in prompt
    assert "не повторяй уже показанный ack" in prompt
    assert "verified=true" in prompt
    assert "не подключена" in prompt
