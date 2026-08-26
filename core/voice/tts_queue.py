"""Thread-safe FIFO TTS queue with cooperative lifecycle controls."""
from __future__ import annotations

import collections
import threading
import time
from typing import Optional

from core.utils.logger import get_logger
from core.voice.output import AssistantOutput
from core.voice.speech_renderer import RenderedSpeech, SpeechRenderer

__all__ = ["TTSQueue"]
log = get_logger(__name__)


class TTSQueue:
    def __init__(self, tts_engine, *, renderer: Optional[SpeechRenderer] = None) -> None:
        self._tts = tts_engine
        self._renderer = renderer or SpeechRenderer()
        self._items: collections.deque[tuple[int, RenderedSpeech, float]] = collections.deque()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = False
        self._active = False
        self._condition = threading.Condition(threading.RLock())
        self._stopped = threading.Event()
        self._stopped.set()
        self._generation = 0
        self._spoken: collections.deque[str] = collections.deque(maxlen=64)
        self._last_telemetry: dict[str, float] = {}

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            self._running = True
            self._paused = False
            self._stopped.clear()
            self._thread = threading.Thread(target=self._worker, daemon=False, name="TTSQueue")
            self._thread.start()

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        with self._condition:
            if not self._running and self._thread is None:
                return
            self._running = False
            self._items.clear()
            self._condition.notify_all()
        try:
            self._tts.stop_speaking()
        except Exception:
            pass
        if wait:
            self.join(timeout=timeout)

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        return bool(thread is None or not thread.is_alive())

    @property
    def stopped(self) -> threading.Event:
        return self._stopped

    def add_output(self, output: AssistantOutput) -> bool:
        rendered = self._renderer.render(output)
        if rendered is None:
            return False
        with self._condition:
            if not self._running:
                return False
            enqueued_at = round(time.time_ns() / 1_000_000.0, 3)
            self._last_telemetry = {"enqueued_at_ms": enqueued_at}
            self._items.append((self._generation, rendered, enqueued_at))
            self._condition.notify()
        return True

    def add_to_queue(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        return self.add_output(AssistantOutput.natural(text.strip()))

    def clear(self) -> int:
        with self._condition:
            count = len(self._items)
            self._items.clear()
            self._condition.notify_all()
            return count

    def interrupt(self) -> None:
        with self._condition:
            self._generation += 1
            self._items.clear()
            self._condition.notify_all()
        try:
            self._tts.stop_speaking()
        except Exception:
            pass

    def wait_until_done(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._items or self._active:
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(timeout=remaining)
            return True

    def pause(self) -> None:
        with self._condition:
            self._paused = True

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._running

    @property
    def queue_size(self) -> int:
        with self._condition:
            return len(self._items)

    @property
    def generation(self) -> int:
        """Monotonic turn generation; interruption invalidates older audio."""
        with self._condition:
            return self._generation

    @property
    def spoken_text(self) -> tuple[str, ...]:
        """Text whose blocking playback completed in the current process."""
        with self._condition:
            return tuple(self._spoken)

    @property
    def last_telemetry(self) -> dict[str, float]:
        with self._condition:
            return dict(self._last_telemetry)

    def _worker(self) -> None:
        try:
            while True:
                with self._condition:
                    while self._running and (self._paused or not self._items):
                        self._condition.wait()
                    if not self._running and not self._items:
                        break
                    if not self._items:
                        continue
                    generation, item, enqueued_at = self._items.popleft()
                    if generation != self._generation:
                        continue
                    self._active = True
                    self._last_telemetry = {
                        "enqueued_at_ms": enqueued_at,
                        "first_audio_at_ms": round(time.time_ns() / 1_000_000.0, 3),
                    }
                try:
                    speak_rendered = getattr(self._tts, "speak_rendered", None)
                    if callable(speak_rendered):
                        speak_rendered(item)
                    else:
                        self._tts.speak(item.text, blocking=True)
                    with self._condition:
                        if generation == self._generation:
                            self._spoken.append(item.text)
                except Exception as exc:
                    log.error("TTSQueue worker ошибка: %s", exc)
                finally:
                    with self._condition:
                        self._active = False
                        self._condition.notify_all()
        finally:
            with self._condition:
                self._thread = None
                self._running = False
                self._active = False
                self._condition.notify_all()
            self._stopped.set()
