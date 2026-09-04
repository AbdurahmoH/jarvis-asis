from datetime import datetime

import pytest

from core.triggers.engine import SystemTriggerEngine, TriggerContext
from core.vision.screen import ScreenCapture
from core.voice.stt import VoiceActivityDetector
from core.voice.wake_word import NoOpWakeWord


def test_vad_silence_and_nonempty_fallback(monkeypatch):
    vad = VoiceActivityDetector()
    monkeypatch.setattr(vad, "_vad", None)
    monkeypatch.setattr(vad, "_silero", None)
    assert vad.is_speech(b"\x00" * 32) is False
    assert vad.is_speech(b"\x01" * 32) is True


def test_vad_webrtcvad_with_valid_frame():
    vad = VoiceActivityDetector(prefer_silero=False)
    if vad._vad is not None:
        silence_frame = b"\x00" * 640
        assert vad.is_speech(silence_frame, sample_rate=16000) is False


def test_trigger_fires_once_until_cooldown():
    now = [1000.0]
    events = []
    engine = SystemTriggerEngine([{"id": "late", "enabled": True, "condition": {"time_after": "23:00"}, "cooldown_hours": 1, "messages": ["late"]}], emit=lambda event, text: events.append((event, text)), clock=lambda: now[0])
    context = TriggerContext(now=datetime(2026, 1, 1, 23, 30))
    assert engine.check(context) == ["late"]
    assert engine.check(context) == []
    now[0] += 3601
    assert engine.check(context) == ["late"]


def test_trigger_three_ignores_disables_it():
    engine = SystemTriggerEngine([{"id": "x", "enabled": True, "condition": {}, "max_ignores": 3, "messages": ["x"]}])
    for _ in range(3): engine.record_ignored("x")
    assert engine.check(TriggerContext()) == []


def test_screen_capture_requires_permission():
    with pytest.raises(PermissionError): ScreenCapture().capture()


def test_wake_word_noop_is_safe():
    detector = NoOpWakeWord(enabled=True, phrase="JARVIS")
    detector.start(); detector.stop()
    assert detector.available is False
