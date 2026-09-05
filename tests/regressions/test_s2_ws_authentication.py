"""Regression S2 — WS-мост аутентифицируется с двух сторон и fail-closed по Origin.

Аудит 2026-09-05 нашёл в ``core/ws_server.py`` три дефекта, каждый — «ложное
разрешение»:

1. Токен был опционален: ``auth_token=os.environ.get("JARVIS_WS_TOKEN") or None``
   в ``run_server``. Пустая переменная → сервер стартовал полностью открытым, и
   любой локальный процесс мог отправить ``command``/``screen_capture``/
   ``settings:update``.
2. ``_origin_allowed`` был fail-open: ``if not self._allowed_origins: return
   True``. Пустой список означал «пускать всех», а конструктор по умолчанию
   получал именно пустой список.
3. ``_authenticate`` принимал токен из query string (``?token=…``). Адрес
   соединения попадает в access-логи, историю и телеметрию прокси — токен
   утекал вместе с ним.

Тесты ниже закрепляют: (1) неаутентифицированный клиент не может выполнить ни
одну из четырёх опасных команд, (2) после ``{"type":"auth"}`` может, (3) до
аутентификации разрешён только ``ping``, (4) query-string путь удалён
структурно и функционально, (5) fail-closed Origin, (6) ``run_server`` не
стартует без токена, (7) токен случайный, стабильный и на диске не в открытом
виде.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from core.security.ws_token import (
    ENV_DEV_NOAUTH,
    ENV_TOKEN,
    load_or_create_token,
    read_token,
    resolve_server_token,
    token_fingerprint,
    token_path,
)
from core.ws_server import _DEFAULT_ALLOWED_ORIGINS, JarvisWSServer

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WS_SRC = _REPO_ROOT / "core" / "ws_server.py"

_TOKEN = "s2-test-token-value"
_ORIGIN = "http://localhost:1420"

#: Команды, которые аудит требует закрыть от неаутентифицированного клиента.
_DANGEROUS_MESSAGES = (
    {"type": "command", "text": "удали всё"},
    {"type": "interrupt"},
    {"type": "screen_capture", "permission": True},
    {"type": "settings:update", "settings": {"api_key": "leak-me"}},
)


class _Mission:
    task_id = "mission-1"


class _Orchestrator:
    """Тот же публичный контракт, что у Orchestrator (как в tests/test_ws_server.py)."""

    def __init__(self) -> None:
        self._output_callback = lambda text: None
        self.inputs: list[str] = []
        self.cancelled: list[str] = []
        self.settings = Settings()
        self.settings.launcher.greeting_enabled = False
        self._settings = self.settings

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def handle_input(self, text: str, **_kwargs: Any) -> dict:
        self.inputs.append(text)
        return {"needs_confirmation": False, "response": f"Ответ на: {text}"}

    def answer_confirmation(self, confirmation_id: str, approved: bool) -> dict:
        return {"response": "ок"}

    def subscribe_events(self, callback) -> Any:
        return lambda: None

    def list_missions(self, include_terminal: bool = True) -> list[_Mission]:
        return [_Mission()]

    def cancel_mission(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True


class _FakeWS:
    """Двойник соединения для проверки guard в ``_on_message`` напрямую."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _server(port: int, *, auth_token: str | None = _TOKEN,
            allowed_origins: set[str] | None = None) -> tuple[JarvisWSServer, _Orchestrator]:
    orch = _Orchestrator()
    server = JarvisWSServer(
        orch, host="127.0.0.1", port=port,
        auth_token=auth_token,
        allowed_origins={_ORIGIN} if allowed_origins is None else allowed_origins,
    )
    return server, orch


async def _open(port: int, *, origin: str = _ORIGIN, path: str = "", **kwargs: Any):
    """Ждёт, пока поток сервера начнёт слушать порт, и подключается."""
    import websockets

    deadline = time.monotonic() + 4.0
    while True:
        try:
            return await websockets.connect(
                f"ws://127.0.0.1:{port}{path}", origin=origin, **kwargs)
        except OSError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.03)


async def _drain(ws, deadline: float = 0.5) -> list[dict]:
    """Собирает всё, что сервер успел прислать, не падая на закрытии."""
    out: list[dict] = []
    end = time.monotonic() + deadline
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except Exception:
            break
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return out


def _run(port: int, scenario, **server_kwargs) -> tuple[Any, _Orchestrator]:
    pytest.importorskip("websockets")
    server, orch = _server(port, **server_kwargs)
    server.start()
    try:
        result = asyncio.run(scenario(port))
    finally:
        server.shutdown()
        server.join(timeout=3.0)
    return result, orch


# ---------------------------------------------------------------------------
# 1. Неаутентифицированный клиент не может ничего опасного
# ---------------------------------------------------------------------------

def test_unauthenticated_client_cannot_run_dangerous_commands() -> None:
    """Ни command, ни interrupt, ни screen_capture, ни settings:update."""
    async def scenario(port: int) -> list[dict]:
        ws = await _open(port)
        try:
            for message in _DANGEROUS_MESSAGES:
                try:
                    await ws.send(json.dumps(message))
                except Exception:
                    break  # сервер уже закрыл соединение — правильный итог
            seen = await _drain(ws)
        finally:
            await ws.close()
        return seen

    seen, orch = _run(8821, scenario)
    assert orch.inputs == [], f"команда дошла до ядра без аутентификации: {orch.inputs}"
    assert orch.cancelled == [], "interrupt отменил миссии без аутентификации"
    types = [item.get("type") for item in seen]
    assert "screen_capture" not in types, "снимок экрана отдан без аутентификации"
    assert "settings" not in types and "settings:saved" not in types, (
        f"настройки отданы/сохранены без аутентификации: {seen}"
    )
    assert "state" not in types, (
        "сервер начал сессию (state:idle) до аутентификации — приветственные "
        f"кадры уходят неавторизованному клиенту: {seen}"
    )


def test_authenticated_client_can_run_commands() -> None:
    """С правильным токеном первым сообщением команда доходит до ядра."""
    async def scenario(port: int) -> list[dict]:
        ws = await _open(port)
        await ws.send(json.dumps({"type": "auth", "token": _TOKEN}))
        await ws.send(json.dumps({"type": "command", "text": "привет"}))
        seen = await _drain(ws, 1.5)
        await ws.close()
        return seen

    seen, orch = _run(8822, scenario)
    assert orch.inputs == ["привет"], (
        f"аутентифицированная команда не дошла до ядра: inputs={orch.inputs}, кадры={seen}"
    )
    assert any(item.get("type") == "state" for item in seen), (
        f"после аутентификации сервер не начал сессию: {seen}"
    )


def test_wrong_token_is_rejected() -> None:
    """Неверный токен — не «почти верный»: команда не исполняется."""
    async def scenario(port: int) -> None:
        ws = await _open(port)
        await ws.send(json.dumps({"type": "auth", "token": _TOKEN + "x"}))
        try:
            await ws.send(json.dumps({"type": "command", "text": "удали всё"}))
        except Exception:
            pass
        await _drain(ws)
        await ws.close()

    _, orch = _run(8823, scenario)
    assert orch.inputs == [], "команда исполнена с неверным токеном"


def test_bearer_header_authenticates() -> None:
    """Заголовок Authorization: Bearer — второй разрешённый способ."""
    async def scenario(port: int) -> list[dict]:
        ws = await _open(port, additional_headers={"Authorization": f"Bearer {_TOKEN}"})
        await ws.send(json.dumps({"type": "command", "text": "привет"}))
        seen = await _drain(ws, 1.5)
        await ws.close()
        return seen

    _, orch = _run(8824, scenario)
    assert orch.inputs == ["привет"], "Bearer-заголовок не аутентифицировал клиента"


def test_only_ping_is_allowed_before_authentication() -> None:
    """До auth разрешён ровно ping: на него pong, аутентификация не ломается."""
    async def scenario(port: int) -> list[dict]:
        ws = await _open(port)
        await ws.send(json.dumps({"type": "ping"}))
        seen = await _drain(ws, 0.6)
        await ws.send(json.dumps({"type": "auth", "token": _TOKEN}))
        seen += await _drain(ws, 1.2)
        await ws.close()
        return seen

    seen, orch = _run(8825, scenario)
    types = [item.get("type") for item in seen]
    assert "pong" in types, f"ping до аутентификации не отвечен: {seen}"
    assert "state" in types, f"после ping аутентификация перестала работать: {seen}"


def test_non_ping_frame_before_auth_closes_connection() -> None:
    """Любой другой кадр до auth — отказ и закрытие, а не «подождём auth»."""
    async def scenario(port: int) -> list[dict]:
        ws = await _open(port)
        await ws.send(json.dumps({"type": "hotkey_pressed"}))
        seen = await _drain(ws, 0.8)
        try:
            await ws.send(json.dumps({"type": "auth", "token": _TOKEN}))
            await ws.send(json.dumps({"type": "command", "text": "поздний вход"}))
            seen += await _drain(ws, 0.8)
        except Exception:
            pass
        await ws.close()
        return seen

    seen, orch = _run(8831, scenario)
    assert orch.inputs == [], (
        f"после неверного первого кадра клиент всё же исполнил команду: {seen}"
    )
    assert any(item.get("type") == "error" for item in seen), (
        f"сервер не сообщил об отказе: {seen}"
    )


def test_query_string_token_no_longer_authenticates() -> None:
    """Путь ?token= удалён: токен не должен попадать в URL и логи."""
    src = _WS_SRC.read_text(encoding="utf-8")
    assert "parse_qs" not in src, (
        "core/ws_server.py снова разбирает query string — вернулся путь ?token="
    )
    assert 'getattr(ws, "path"' not in src, (
        "аутентификация снова смотрит на путь соединения"
    )

    async def scenario(port: int) -> None:
        ws = await _open(port, path=f"/?token={_TOKEN}")
        try:
            await ws.send(json.dumps({"type": "command", "text": "удали всё"}))
            await _drain(ws)
        except Exception:
            pass
        await ws.close()

    _, orch = _run(8826, scenario)
    assert orch.inputs == [], "токен из query string всё ещё аутентифицирует"


def test_on_message_guard_rejects_every_dangerous_type() -> None:
    """Второй эшелон: guard в _on_message отказывает по каждому типу.

    Проверяется напрямую, без сокета: даже если соединение как-то оказалось в
    ``_clients`` мимо ``_handler``, ни одна из четырёх команд не исполняется.
    """
    server, orch = _server(8827)
    for message in _DANGEROUS_MESSAGES:
        ws = _FakeWS()
        asyncio.run(server._on_message(ws, json.dumps(message)))
        assert ws.sent, f"{message['type']}: сервер ничего не ответил"
        assert ws.sent[0] == {"type": "error", "message": "authentication required"}, (
            f"{message['type']}: guard не сработал, ответ {ws.sent[0]}"
        )
    assert orch.inputs == [] and orch.cancelled == []


# ---------------------------------------------------------------------------
# 2. Origin: fail-closed
# ---------------------------------------------------------------------------

def test_empty_origin_allowlist_denies_everything() -> None:
    """Было fail-open (`if not self._allowed_origins: return True`)."""
    server, _ = _server(8828, allowed_origins=set())
    assert server._origin_allowed(MagicMock(request_headers={"Origin": _ORIGIN})) is False
    assert server._origin_allowed(MagicMock(request_headers={})) is False


def test_constructor_defaults_to_explicit_allowlist() -> None:
    """Дефолт — явный список, а не «пусто = всё разрешено»."""
    default_server = JarvisWSServer(_Orchestrator(), auth_token=_TOKEN)
    assert default_server._allowed_origins == set(_DEFAULT_ALLOWED_ORIGINS)
    assert default_server._allowed_origins, "дефолтный allowlist пуст — сервер мёртв"
    server, _ = _server(8829, allowed_origins=None)
    assert server._origin_allowed(MagicMock(request_headers={"Origin": _ORIGIN})) is True
    assert server._origin_allowed(
        MagicMock(request_headers={"Origin": "http://evil.example"})) is False
    assert server._origin_allowed(MagicMock(request_headers={})) is False


def test_foreign_origin_cannot_send_commands() -> None:
    """Живой сокет с чужим Origin закрывается до любой команды."""
    async def scenario(port: int) -> None:
        ws = await _open(port, origin="http://evil.example")
        try:
            await ws.send(json.dumps({"type": "auth", "token": _TOKEN}))
            await ws.send(json.dumps({"type": "command", "text": "удали всё"}))
            await _drain(ws)
        except Exception:
            pass
        finally:
            await ws.close()

    _, orch = _run(8830, scenario)
    assert orch.inputs == [], "клиент с чужим Origin исполнил команду"


# ---------------------------------------------------------------------------
# 3. run_server: без токена не стартует
# ---------------------------------------------------------------------------

def test_run_server_refuses_to_start_without_token(monkeypatch) -> None:
    """Нет токена и нет dev-флага → RuntimeError, а не открытый сервер."""
    import core.ws_server as ws_module

    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.delenv(ENV_DEV_NOAUTH, raising=False)
    created: list[dict] = []

    def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("хранилище недоступно")

    with patch("core.security.ws_token.load_or_create_token", _boom), \
            patch.object(ws_module, "Orchestrator", MagicMock()), \
            patch.object(ws_module, "load_config", MagicMock(return_value=MagicMock())), \
            patch.object(ws_module, "JarvisWSServer",
                         MagicMock(side_effect=lambda *a, **k: created.append(k))):
        with pytest.raises(RuntimeError, match="нет токена аутентификации"):
            ws_module.run_server()
    assert not created, "сервер был создан несмотря на отсутствие токена"


def test_dev_noauth_flag_is_the_only_way_to_run_open(monkeypatch, caplog) -> None:
    """JARVIS_WS_DEV_NOAUTH=1 → токена нет, но в логе громкое предупреждение."""
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.setenv(ENV_DEV_NOAUTH, "1")
    with caplog.at_level("WARNING"):
        token, mode = resolve_server_token(MagicMock())
    assert (token, mode) == (None, "dev-noauth")
    assert any("БЕЗ АУТЕНТИФИКАЦИИ" in record.getMessage() for record in caplog.records), (
        f"нет громкого предупреждения о dev-режиме: {[r.getMessage() for r in caplog.records]}"
    )


def test_dev_noauth_requires_exact_flag(monkeypatch, tmp_path) -> None:
    """Любое значение кроме "1" не включает открытый режим."""
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    settings = MagicMock()
    settings.data_dir = tmp_path
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv(ENV_DEV_NOAUTH, value)
        token, mode = resolve_server_token(settings)
        assert mode == "store" and token, f"{value!r} выключил аутентификацию"


def test_explicit_env_token_wins(monkeypatch) -> None:
    monkeypatch.setenv(ENV_TOKEN, " env-token ")
    assert resolve_server_token(MagicMock()) == ("env-token", "env")


# ---------------------------------------------------------------------------
# 4. Токен: случайный, стабильный, не в открытом виде
# ---------------------------------------------------------------------------

def test_token_is_generated_once_and_stored_encrypted(tmp_path) -> None:
    """Первый запуск создаёт токен; на диске его нет в открытом виде."""
    path = tmp_path / "security" / "ws-token.dpapi"
    first = load_or_create_token(path=path)
    second = load_or_create_token(path=path)
    assert first == second, "токен пересоздаётся на каждом запуске"
    assert len(first) >= 32, f"слишком короткий токен: {len(first)}"
    assert read_token(path=path) == first
    raw = path.read_bytes()
    assert first.encode("utf-8") not in raw, "токен лежит на диске в открытом виде"
    assert b"ws_auth_token" not in raw, "имя ключа видно — файл не зашифрован"


def test_token_is_random_per_install(tmp_path) -> None:
    a = load_or_create_token(path=tmp_path / "a" / "ws-token.dpapi")
    b = load_or_create_token(path=tmp_path / "b" / "ws-token.dpapi")
    assert a != b, "токен не случайный — одинаков для двух установок"


def test_token_path_lives_under_data_dir(tmp_path) -> None:
    settings = MagicMock()
    settings.data_dir = tmp_path
    assert token_path(settings) == tmp_path / "security" / "ws-token.dpapi"


def test_fingerprint_never_reveals_token() -> None:
    """В логах — только отпечаток, поэтому он не должен содержать токен."""
    token = "super-secret-token"
    fingerprint = token_fingerprint(token)
    assert token not in fingerprint and len(fingerprint) == 8
    assert token_fingerprint(None) == "none"


def test_server_never_logs_the_token() -> None:
    """Ни одна строка логирования не выводит сам токен."""
    for source in (_WS_SRC, _REPO_ROOT / "core" / "security" / "ws_token.py"):
        src = source.read_text(encoding="utf-8")
        for index, line in enumerate(src.splitlines(), 1):
            if "log." not in line:
                continue
            assert "_auth_token" not in line and "token)" not in line.replace(
                "token_fingerprint(token)", ""), (
                f"{source.name}:{index} — токен может попасть в лог: {line.strip()}"
            )


def test_client_sends_auth_before_anything_else() -> None:
    """Фронтенд: кадр auth первый (свойство закреплено и в JS-тесте)."""
    src = (_REPO_ROOT / "jarvis" / "src" / "integrations" / "wsBackend.ts").read_text(
        encoding="utf-8")
    assert "'auth'" in src and "authSent" in src, (
        "wsBackend.ts перестал отправлять кадр аутентификации"
    )
    assert "token=" not in src, "токен вернулся в URL соединения"
    test_src = (_REPO_ROOT / "jarvis" / "scripts" / "wsBackend.test.ts").read_text(
        encoding="utf-8")
    assert "sent[0]" in test_src and "type: 'auth'" in test_src, (
        "JS-тест больше не проверяет, что auth уходит первым кадром"
    )
