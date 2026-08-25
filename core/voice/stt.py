"""Speech-to-Text (STT) — минимальная рабочая версия (P5 §5.9).

Реализация поверх ``faster-whisper`` (MIT, быстрая локальная интеграция).
Все тяжёлые зависимости импортируются LAZY — модуль не падает при
отсутствии библиотек, а честно сообщает, что нужно установить.

Поведение:
    * ``settings.voice.stt_enabled = False`` (по умолчанию) ->
      ``is_available()`` возвращает False, вызовы кидают ``NotImplementedError``
      с понятным сообщением (обратная совместимость со старой заглушкой).
    * ``stt_enabled = True`` и ``faster-whisper`` установлен ->
      РЕАЛЬНОЕ распознавание (``transcribe_file`` / ``listen_once``).
    * ``stt_enabled = True``, но библиотека не стоит ->
      ``is_available()`` = False, вызовы кидают ``RuntimeError`` с инструкцией
      по установке (а не молча «не реализовано»).

Бэкенд выбирается автоматически:
    ``faster-whisper`` (приоритет, MIT, локально, без юр. рисков).
"""

from __future__ import annotations

import io
import numbers
import tempfile
import time
from typing import List, Optional, Tuple

from config.settings import Settings
from core.utils.logger import get_logger

__all__ = ["STTEngine", "STT_DISABLED_MSG", "STT_NEED_BACKEND_MSG", "VoiceActivityDetector"]

log = get_logger(__name__)

#: Сообщение, когда STT отключён флагом.
STT_DISABLED_MSG = (
    "STT отключён. Установите settings.voice.stt_enabled=True "
    "для работы распознавания речи."
)

#: Инструкция, когда включено, но библиотека не установлена.
STT_NEED_BACKEND_MSG = (
    "STT включён (settings.voice.stt_enabled=True), но не найден движок. "
    "Установите: pip install faster-whisper sounddevice scipy\n"
    "(faster-whisper — MIT, локальное распознавание без юридических рисков)."
)

#: Модель faster-whisper по умолчанию (small — баланс точность/скорость).
DEFAULT_WHISPER_MODEL = "small"


class STTEngine:
    """Минимальный, но РАБОЧИЙ STT-движок поверх faster-whisper."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = None
        voice_enabled = bool(getattr(getattr(settings, "voice", None), "stt_enabled", False))
        self._enabled = voice_enabled or bool(getattr(getattr(settings, "stt", None), "enabled", False))
        if not self._enabled:
            log.info("STT отключён (settings.voice.stt_enabled=False)")
        else:
            # Проверяем доступность бэкенда сразу, чтобы is_available() было
            # точным (а не падало в рантайме неожиданно).
            self._backend_ok = self._probe_backend()
            if not self._backend_ok:
                log.warning("STT включён, но движок недоступен: %s", STT_NEED_BACKEND_MSG)

    # ------------------------------------------------------------------ #
    #  Доступность
    # ------------------------------------------------------------------ #

    def is_available(self) -> bool:
        """True, если STT включён И движок реально доступен."""
        return self._enabled and getattr(self, "_backend_ok", False)

    def _probe_backend(self) -> bool:
        """Пробует лениво импортировать faster-whisper (без загрузки модели)."""
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception as exc:  # pragma: no cover - зависит от окружения
            log.debug("faster-whisper недоступен: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    #  Транскрипция файла
    # ------------------------------------------------------------------ #

    def transcribe_file(self, audio_path: str) -> str:
        """Транскрибирует аудиофайл в текст (реально, если движок доступен)."""
        if not self._enabled:
            raise NotImplementedError(STT_DISABLED_MSG)
        if not self.is_available():
            raise RuntimeError(STT_NEED_BACKEND_MSG)

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(STT_NEED_BACKEND_MSG) from exc

        stt_cfg = getattr(self._settings, "stt", None)
        voice_cfg = getattr(self._settings, "voice", None)
        model_size = getattr(stt_cfg, "model", "") or getattr(voice_cfg, "stt_model", DEFAULT_WHISPER_MODEL)
        if model_size.startswith("faster-whisper-"): model_size = model_size.removeprefix("faster-whisper-")
        device = getattr(voice_cfg, "stt_device", "cpu") or "cpu"
        language = (getattr(stt_cfg, "language", "") or getattr(voice_cfg, "language", "ru") or "ru").strip().lower()
        if language == "auto":
            language = "ru"

        if self._model is None:
            log.info("STT: загрузка модели '%s' на %s...", model_size, device)
            self._model = WhisperModel(model_size, device=device, compute_type="int8")
        model = self._model
        segments: List[Tuple[float, float, str]] = []
        confidences: list[float] = []
        vad_enabled = str(getattr(stt_cfg, "vad", "webrtc") or "").casefold() not in {"", "none", "off", "false"}
        transcribe_options = {
            "language": language,
            "beam_size": 8,
            "best_of": 5,
            "temperature": 0.0,
            "condition_on_previous_text": False,
            "vad_filter": vad_enabled,
        }
        if vad_enabled:
            transcribe_options["vad_parameters"] = {
                "min_silence_duration_ms": 300,
                "speech_pad_ms": 200,
            }
        for seg in model.transcribe(audio_path, **transcribe_options)[0]:
            segments.append((seg.start, seg.end, seg.text))
            if getattr(seg, "avg_logprob", None) is not None:
                import math
                confidences.append(max(0.0, min(1.0, math.exp(float(seg.avg_logprob)))))
        text = " ".join(s.strip() for _, _, s in segments if s and s.strip())
        log.info("STT: распознано %d сегментов, %d символов", len(segments), len(text))
        self.last_confidence = sum(confidences) / len(confidences) if confidences else 1.0
        return text.strip()

    def transcribe_with_confidence(self, audio_path: str) -> tuple[str, float]:
        """Return text plus Whisper's mean segment confidence when available."""
        text = self.transcribe_file(audio_path)
        confidence = 1.0
        # Keep the public legacy method stable; callers that need confidence
        # can use this optional metadata path.
        try:
            confidence = float(getattr(self, "last_confidence", confidence))
        except (TypeError, ValueError):
            pass
        return text, confidence

    # ------------------------------------------------------------------ #
    #  Потоковое / одноразовое прослушивание с микрофона
    # ------------------------------------------------------------------ #

    def listen_once(self, timeout: float = 5.0, sample_rate: int = 16000) -> str:
        """Одноразовое прослушивание с микрофона -> текст.

        Записывает ``timeout`` секунд через sounddevice, сохраняет во временный
        WAV и прогоняет через :meth:`transcribe_file`.
        """
        if not self._enabled:
            raise NotImplementedError(STT_DISABLED_MSG)
        if not self.is_available():
            raise RuntimeError(STT_NEED_BACKEND_MSG)

        try:
            import sounddevice as sd  # type: ignore
            import scipy.io.wavfile as wav  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "Для listen_once нужны sounddevice и scipy: "
                "pip install sounddevice scipy"
            ) from exc

        log.info("STT: слушаю микрофон %.1fс...", timeout)
        frames = int(timeout * sample_rate)
        recording = None
        last_error: Exception | None = None
        for device in self._input_device_candidates(sd):
            try:
                recording = sd.rec(
                    frames,
                    samplerate=sample_rate,
                    channels=1,
                    dtype="int16",
                    device=device,
                )
                sd.wait()
                self._active_input_device = device
                break
            except Exception as exc:
                last_error = exc
                log.debug("STT: вход %r недоступен: %s", device, exc)
        if recording is None:
            detail = str(last_error) if last_error else "нет входных устройств"
            raise RuntimeError(f"Микрофон недоступен: {detail}") from last_error

        # Normalize microphone level without changing speech timing.  The
        # previous path sent raw int16 capture straight to Whisper; quiet
        # Windows inputs then lose Russian consonants before decoding.
        try:
            import numpy as np  # type: ignore
            signal = np.asarray(recording, dtype=np.float32)
            signal -= float(signal.mean())
            peak = float(np.max(np.abs(signal))) if signal.size else 0.0
            if peak > 0.0:
                signal *= min(1.0, 30000.0 / peak)
            recording = np.clip(signal, -32768.0, 32767.0).astype(np.int16)
        except Exception as exc:
            log.debug("STT preprocessing skipped: %s", exc)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav.write(tmp.name, sample_rate, recording)
            tmp_path = tmp.name
        try:
            return self.transcribe_file(tmp_path)
        finally:
            import os

            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _input_device_candidates(self, sd) -> list[object]:
        configured = getattr(getattr(self._settings, "voice", None), "input_device_index", None)
        candidates: list[object] = []
        if isinstance(configured, numbers.Integral) and int(configured) >= 0:
            candidates.append(int(configured))
        try:
            default_input = sd.default.device[0]
            if isinstance(default_input, numbers.Integral) and int(default_input) >= 0:
                candidates.append(int(default_input))
        except Exception:
            pass
        try:
            for index, info in enumerate(sd.query_devices()):
                if int(info.get("max_input_channels", 0)) > 0:
                    candidates.append(index)
        except Exception:
            pass
        unique: list[object] = []
        for item in candidates:
            if item not in unique:
                unique.append(item)
        return unique

    def transcribe_with_confidence_from_mic(self, timeout: float = 5.0) -> tuple[str, float]:
        return self.listen_once(timeout=timeout), float(getattr(self, "last_confidence", 1.0))

    # ------------------------------------------------------------------ #
    #  Потоковая транскрипция (минимальный hook — делегирует в файл)
    # ------------------------------------------------------------------ #

    def transcribe_stream(self, audio_stream) -> str:
        """Транскрибирует аудиопоток.

        Минимальная версия (P5 §5.9): принимает уже записанный буфер
        (bytes/bytearray/.wav) и делегирует в ``transcribe_file`` через
        временный файл. Полноценный VAD-streaming — расширение на будущее.
        """
        if not self._enabled:
            raise NotImplementedError(STT_DISABLED_MSG)
        if not self.is_available():
            raise RuntimeError(STT_NEED_BACKEND_MSG)

        import os

        data = audio_stream.read() if hasattr(audio_stream, "read") else audio_stream
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            if isinstance(data, (bytes, bytearray)):
                tmp.write(bytes(data))
            elif isinstance(data, io.BytesIO):
                tmp.write(data.getvalue())
            else:
                raise ValueError("transcribe_stream: неподдерживаемый тип потока")
            tmp_path = tmp.name
        try:
            return self.transcribe_file(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class VoiceActivityDetector:
    """Optional VAD boundary: Silero ONNX first, WebRTC second, closed fallback."""
    def __init__(self, aggressiveness: int = 2, *, prefer_silero: bool = True,
                 threshold: float = 0.5, fail_closed: bool = False) -> None:
        self.aggressiveness = max(0, min(3, int(aggressiveness)))
        self.threshold = max(0.0, min(1.0, float(threshold)))
        self._fail_closed = bool(fail_closed)
        self._vad = None
        self._silero = None
        if prefer_silero:
            try:
                from silero_vad import load_silero_vad  # type: ignore
                self._silero = load_silero_vad(onnx=True)
            except Exception:
                self._silero = None
        try:
            import webrtcvad  # type: ignore
            self._vad = webrtcvad.Vad(self.aggressiveness)
        except Exception:
            pass
    @property
    def available(self) -> bool: return self._silero is not None or self._vad is not None
    def reset(self) -> None:
        reset = getattr(self._silero, "reset_states", None)
        if callable(reset):
            reset()
    def is_speech(self, frame: bytes, sample_rate: int = 16000) -> bool:
        if not frame:
            return False
        if self._silero is not None and sample_rate in {8000, 16000}:
            expected = 512 if sample_rate == 16000 else 256
            if len(frame) == expected * 2:
                try:
                    import numpy as np
                    import torch
                    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                    probability = float(self._silero(torch.from_numpy(samples), sample_rate).item())
                    return probability >= self.threshold
                except Exception:
                    pass
        if self._vad is None:
            return False if self._fail_closed else bool(any(frame))
        try:
            return bool(self._vad.is_speech(frame, sample_rate))
        except Exception:
            return False
