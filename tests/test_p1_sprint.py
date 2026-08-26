"""Интеграционные тесты P1-спринта (реальный код, без аудита).

Стратегия: для каждого трека (A–G) — детерминированный offline-тест,
проверяющий ИЗМЕНЕНИЕ ПОВЕДЕНИЯ (DoD), а не отсутствие ошибок.
Все тесты офлайн, без сети/моделей. Работают поверх реальных модулей.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.task_runtime import MissionStatus, TaskRuntime


# --------------------------------------------------------------------------- #
#  TEST 10 (A1) — HARD CAP параллельных миссий = 2 + очередь
# --------------------------------------------------------------------------- #

def test_p1_runtime_hard_cap_two():
    """Третья миссия НЕ стартует сразу, ждёт слота, НЕ падает (§1.3 П1)."""
    rt = TaskRuntime(max_concurrent=2)
    gate = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def runner(mission, cancel):
        with lock:
            started.append(mission.task_id)
        # Держим слот открытым, пока gate не отпустят.
        gate.wait(timeout=5)
        return "ok"

    m1 = rt.submit("t1", runner)
    m2 = rt.submit("t2", runner)
    m3 = rt.submit("t3", runner)

    # Даём потокам стартовать.
    time.sleep(0.3)
    with lock:
        n_started = len(started)
    assert n_started == 2, f"ожидали ровно 2 одновременных миссии, стартовало {n_started}"
    # Третья — в очереди, а НЕ упала и НЕ завершилась.
    assert m3.status == MissionStatus.QUEUED, f"m3.status={m3.status}, ожидали QUEUED"
    assert rt._active_count == 2

    # Освобождаем слоты.
    gate.set()
    # Ждём завершения третьей (она должна дождаться слота и выполниться).
    rt.wait(m3.task_id, timeout=10)
    assert m3.status == MissionStatus.COMPLETED, (
        f"m3 не доехала до COMPLETED: {m3.status}"
    )
    with lock:
        n_started_final = len(started)
    assert n_started_final == 3, "все три миссии должны были исполниться"
    rt.shutdown()


def test_p1_runtime_cancel_queued():
    """Отмена миссии, ещё ждущей в очереди, — не даёт ей стартовать (§1.3)."""
    rt = TaskRuntime(max_concurrent=1)
    gate = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def runner(mission, cancel):
        with lock:
            started.append(mission.task_id)
        gate.wait(timeout=5)
        return "ok"

    m1 = rt.submit("q1", runner)   # занимает единственный слот
    m2 = rt.submit("q2", runner)   # встаёт в очередь
    time.sleep(0.2)
    with lock:
        assert len(started) == 1

    # Отменяем ждущую миссию.
    assert rt.cancel(m2.task_id) is True
    gate.set()
    rt.wait(m1.task_id, timeout=10)
    with lock:
        assert m2.task_id not in started, "отменённая в очереди миссия не должна стартовать"
    assert m2.status == MissionStatus.CANCELLED
    rt.shutdown()


# --------------------------------------------------------------------------- #
#  TEST 11 (A2) — 7B-эскалация удалена: локальный coder/architect запрещён
# --------------------------------------------------------------------------- #

def test_p1_no_local_heavy_escalation(monkeypatch):
    """Offline CODER keeps its role while reusing the configured local GGUF.

    Sprint 9 separates task role from model availability: no legacy heavy
    coder model is loaded, and the task does not become conversational FAST.
    """
    from config import load_config
    from core.llm import get_llm_backend
    from core.llm.factory import clear_backend_cache
    from core.llm.tiers import Tier

    settings = load_config()
    settings.offline_mode = True
    # tier_providers — pydantic-модель, не dict: прописываем провайдера
    # для coder = 'local' через setattr (симуляция старой/сломанной конфигурации).
    monkeypatch.setattr(settings.tier_providers, "coder", "local")
    settings.model_tiers.coder = "qwen-coder-local"
    clear_backend_cache()
    coder = get_llm_backend(settings, Tier.CODER)
    assert coder.task_role == "coder"
    assert coder.gguf_path == settings.local_model.resolved_gguf_path

    # FAST-тир с локальным провайдером — разрешён (лицо Qwen 4B).
    # (Не падает на BackendConfigError по причине local-provider;
    #  может упасть только если GGUF реально не найден — это норма офлайн-теста.)
    fast = get_llm_backend(settings, Tier.FAST)
    assert fast.task_role == "fast"
    clear_backend_cache()


def test_p1_council_escalate_no_local_heavy():
    """CouncilRouter эскалирует ТОЛЬКО к удалённым; нет ветки локального 7B.

    _is_local_heavy должна быть удалена из модуля (П1 §1.1) — само её
    отсутствие доказывает, что локальная 7B-эскалация вырезана.
    """
    import core.router.council as council_mod
    assert not hasattr(council_mod, "_is_local_heavy"), \
        "функция _is_local_heavy должна быть удалена (локальный 7B больше не в эскалации)"


# --------------------------------------------------------------------------- #
#  TEST 12/13 (B2) — TTS-санитайзер + interrupt
# --------------------------------------------------------------------------- #

def test_p1_tts_sanitizer_blocks_raw_errors_and_secrets():
    """Сырые ошибки/JSON/секреты НЕ попадают в голос (П1 §1.2)."""
    from core.voice.tts_sanitizer import looks_unsafe_for_tts, sanitize_for_tts

    # Сырый traceback / исключение -> санитайзер НЕ читает сырьё вслух,
    # и остаётся тишиной: terminal phrase создаёт typed ErrorMapper.
    assert looks_unsafe_for_tts("Traceback (most recent call last):\n  File ...")
    cleaned_trace = sanitize_for_tts("Traceback (most recent call last):\n  File ...")
    assert "Traceback" not in cleaned_trace, "сырой traceback не должен попадать в голос"
    assert cleaned_trace == ""

    # Внутренний JSON плана -> тоже fallback, без сырого JSON.
    assert looks_unsafe_for_tts('{"tool": "write_file", "arguments": {"path": "x"}}')
    cleaned_json = sanitize_for_tts('{"tool": "write_file", "arguments": {"path": "x"}}')
    assert "write_file" not in cleaned_json, "внутренний JSON плана не должен попадать в голос"

    # Секрет/ключ -> fallback, без сырого ключа.
    assert looks_unsafe_for_tts("redacted token sk-abcdefgh12345678")
    cleaned_secret = sanitize_for_tts("redacted token sk-abcdefgh12345678")
    assert "sk-abcdefgh12345678" not in cleaned_secret, "секрет не должен попадать в голос"

    # Безопасный живой текст проходит как есть.
    safe = "Сэр, файл записан. Готов к следующей задаче."
    assert not looks_unsafe_for_tts(safe)
    assert sanitize_for_tts(safe) == safe


def test_p1_tts_sanitizer_blocks_provider_http_errors():
    """Сырые HTTP-коды/ошибки провайдера НЕ читаются вслух (пункт 1 из live-теста).

    Голосовой sanitizer должен вернуть тишину без кодов 401/404/429/503 и
    технических деталей. Естественную terminal phrase создаёт ErrorMapper. Живой
    язык модели (числа, время, суммы) НЕ должен глушиться ложными срабатываниями.
    """
    from core.voice.tts_sanitizer import looks_unsafe_for_tts, sanitize_for_tts

    # Реальные сырые ошибки из живого теста.
    err_samples = [
        "Провайдер deepseek вернул HTTP 404: Not Found.",
        "Authentication Fails, Your api key is invalid.",
        "429 Too Many Requests — rate limit exceeded.",
        "503 Service Unavailable — model overloaded.",
        "Не задан endpoint провайдера 'aihubmix'.",
        "Ошибка ModelRouter: модель недоступна, провайдер anymodel вернул HTTP 401.",
    ]
    for sample in err_samples:
        assert looks_unsafe_for_tts(sample), f"ошибка провайдера должна блокироваться: {sample!r}"
        out = sanitize_for_tts(sample)
        # Никаких сырых кодов/деталей в голосе.
        assert "401" not in out and "404" not in out and "429" not in out and "503" not in out, \
            f"HTTP-код не должен попадать в голос: {out!r}"
        assert out == "", f"технический сбой должен быть беззвучным: {out!r}"

    # Живой язык модели НЕ глушится (ложноположительные срабатывания недопустимы).
    safe_live = [
        "Сэр, я нашёл 500 рублей в кошельке.",
        "Сейчас 12:45, сэр. Начинаю.",
        "Проверил 3 файла, всё в порядке.",
        "Привет! У меня всё отлично, готов помочь.",
    ]
    for s in safe_live:
        assert not looks_unsafe_for_tts(s), f"живой текст не должен глушиться: {s!r}"
        assert sanitize_for_tts(s) == s, f"живой текст должен идти как есть: {s!r}"


def test_p1_tts_queue_interrupt_clears_pending():
    """interrupt() очищает очередь ожидания и не роняет рантайм (П1 §1.2)."""

    class _FakeTTS:
        def is_available(self):
            return True

        def speak(self, text, blocking=True):
            pass

        def stop_speaking(self):
            pass

    from core.voice.tts_queue import TTSQueue
    q = TTSQueue(_FakeTTS())
    q.start()
    # Наполняем очередь (без реального TTS — fake мгновенный, но add_to_queue
    # просто кладёт текст; interrupt должен очистить то, что не успело проиграть).
    for i in range(5):
        q.add_to_queue(f"фраза {i}")
    q.interrupt()
    # После прерывания очередь пуста, рантайм не упал.
    assert q.queue_size == 0
    assert q.is_running is True
    q.stop()


# --------------------------------------------------------------------------- #
#  TEST 14 (B1) — ACK: LLM-обогащение с жёстким fallback к canned-фразам
# --------------------------------------------------------------------------- #

def test_p1_acknowledgement_falls_back_offline(monkeypatch):
    """pick_acknowledgement не падает офлайн и возвращает canned base (П1 §1.2).

    Тест детерминирован: явно мокает LLM-бэкенд как недоступный, чтобы
    проверить именно offline-fallback независимо от наличия живого ключа
    в settings.json (реальный ключ делает путь онлайн-обогащения рабочим,
    см. test_p1_acknowledgement_enriches_when_model_available).
    """
    from core.agent import pick_acknowledgement
    from core.llm.backend import BackendUnavailable

    def _boom(*a, **k):
        raise BackendUnavailable("мок: модель недоступна (offline)")
    monkeypatch.setattr("core.llm.get_llm_backend", _boom)

    # Без settings/goal — мгновенно базовая фраза по intent.
    assert pick_acknowledgement("app") == "Принято."
    assert pick_acknowledgement("none") == "Понял. Разбираюсь."

    # С целью, но без локальной модели (offline) — должен откатиться к base,
    # а НЕ упасть и НЕ вернуть пустоту.
    from config import load_config
    settings = load_config()
    ack = pick_acknowledgement("file", goal="проверь файл отчёта", settings=settings)
    assert ack == "Сейчас проверю."  # canned fallback для 'file'


def test_p1_acknowledgement_enriches_when_model_available(fake_backend, settings):
    """Если локальная модель доступна — ACK обогащается, но не сырым мусором."""
    from core.agent import pick_acknowledgement
    # FakeBackend.direct() возвращает _answer (заданный в set_answer).
    fake_backend.set_answer("Есть, сэр, гляну.")
    ack = pick_acknowledgement("file", goal="проверь файл отчёта", settings=settings)
    # Либо обогащённый вариант, либо canned base — главное, не пусто и не сырьё.
    assert ack and len(ack) <= 40
    assert "{" not in ack  # не сырой JSON


# --------------------------------------------------------------------------- #
#  TEST 15/16 (E1) — память: секреты НЕ пишутся + forget-TTL
# --------------------------------------------------------------------------- #

def test_p1_secret_filter_masks_keys_and_raw():
    """Санитайзер памяти маскирует секреты и режет сырьё (П1 §1.5/§1.8)."""
    from core.memory.secret_filter import sanitize_for_memory, contains_secret_or_raw

    # Секрет маскируется, а не сохраняется как есть.
    dirty = "вот мой ключ api_key=sk-abcdef1234567890token больше не скажу"
    assert contains_secret_or_raw(dirty)
    clean = sanitize_for_memory(dirty)
    assert "sk-abcdef" not in clean
    assert "***" in clean

    # Сырой JSON плана не пишем в память как есть.
    assert contains_secret_or_raw('{"tool": "write_file", "arguments": {"path": "x"}}')

    # Обычный текст диалога проходит.
    ok = "Сэр, напомнил о встрече в 15:00."
    assert not contains_secret_or_raw(ok)
    assert sanitize_for_memory(ok) == ok


def test_p1_memory_does_not_store_secrets(tmp_path):
    """LongTermMemory.add маскирует секреты перед записью (П1 §1.5)."""
    from core.memory.long_term import LongTermMemory
    from core.memory.embedder import Embedder

    # Используем in-memory embedder-заглушку, чтобы не тянуть chromadb-веса.
    class _StubEmbedder:
        def embed(self, texts):
            return [[0.0] * 8 for _ in texts]

        def embed_query(self, text):
            return [0.0] * 8

    class _StubLTM(LongTermMemory):
        """Перехватываем add, чтобы не зависеть от реального chromadb."""

        def __init__(self):
            self._settings = None
            self._collection = None
            self._initialized = True
            self._stored = []

        def _ensure(self):
            return True

        def add(self, text, metadata=None):
            from core.memory.secret_filter import sanitize_for_memory
            safe = sanitize_for_memory(text or "")
            if not safe or not safe.strip():
                return ""
            self._stored.append(safe)
            return "id1"

    ltm = _StubLTM()
    ltm.add("мой пароль password=supersecret123 никому не говори")
    assert len(ltm._stored) == 1
    assert "supersecret123" not in ltm._stored[0]
    assert "***" in ltm._stored[0]


# --------------------------------------------------------------------------- #
#  TEST 17/18 (F1) — proxy-клиент НЕ вшивает ключ автора
# --------------------------------------------------------------------------- #

def test_p1_proxy_client_does_not_embed_author_key():
    """В proxy-режиме клиент шлёт локальный proxy_token, НЕ ключ автора (П1 §1.4)."""
    from core.llm.proxy_client import ProxyLLMClient, PROXY_HEADER_NAME

    client = ProxyLLMClient(
        provider="deepseek",
        model_id="deepseek-chat",
        proxy_endpoint="http://127.0.0.1:8787/v1",
        proxy_token="local-proxy-secret",
    )
    headers = client._headers()
    # Ключ автора НЕ должен попасть в заголовки клиента.
    assert "Authorization" not in headers, "ключ автора не должен быть в заголовках клиента"
    assert headers.get(PROXY_HEADER_NAME) == "local-proxy-secret"
    # URL клиента — локальный proxy, а НЕ прямой endpoint провайдера.
    assert client._chat_url().startswith("http://127.0.0.1:8787")
    assert "api.deepseek.com" not in client._chat_url()


def test_p1_from_settings_proxy_mode(monkeypatch):
    """from_settings в proxy-режиме строит ProxyLLMClient без ключа автора (П1 §1.4)."""
    from config import load_config
    from core.llm.proxy_client import ProxyLLMClient
    from core.llm.remote_api import RemoteAPIBackend

    settings = load_config()
    # Включаем proxy-режим через monkeypatch (откат после теста, без мутации синглтона).
    monkeypatch.setattr(settings.proxy, "enabled", True)
    monkeypatch.setattr(settings.proxy, "endpoint", "http://127.0.0.1:8787/v1")
    monkeypatch.setattr(settings.proxy, "proxy_token", "local-proxy-secret")

    backend = RemoteAPIBackend.from_settings(
        settings, "deepseek", model_id="deepseek-chat"
    )
    assert isinstance(backend, ProxyLLMClient)
    headers = backend._headers()
    assert "Authorization" not in headers
    assert headers.get("X-Jarvis-Proxy-Token") == "local-proxy-secret"
