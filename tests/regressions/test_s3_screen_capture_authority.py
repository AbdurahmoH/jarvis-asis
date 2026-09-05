"""Regression S3 — снимок экрана разрешает пользователь, а не клиент.

Аудит 2026-09-05 нашёл две поверхности, каждая из которых позволяла выдать
разрешение самому себе:

1. ``core/actions/screen_capture.py``: схема инструмента требовала
   ``permission: boolean`` и передавала флаг прямо в драйвер. Значит модель
   (или любой, кто формирует план) объявляла себе разрешение аргументом.
2. ``core/ws_server.py``: ветка ``screen_capture`` проверяла
   ``msg.get("permission") is not True`` — то есть верила полю в сообщении
   клиента. Продуктовый UI это сообщение вообще не отправляет, так что ветка
   была чистой поверхностью атаки для любого локального процесса.

Теперь единственный источник разрешения — грант ``core.authority`` с
``effect=read_screen``, выданный на реальном подтверждении пользователя:
``once`` (2 минуты, отзывается сразу после кадра), ``session`` (12 часов,
отзывается при отключении клиента), ``permanent`` (365 дней). Литерального
«навсегда» не существует.

Тесты закрепляют: (1) флага в схеме нет и он структурно отвергается,
(2) без гранта — ``confirmation_required``, а не кадр, (3) с грантом —
исполняется, (4) истёкший и отозванный гранты снова требуют подтверждения,
(5) риск выше ceiling подтверждением не разрешается, (6) флаг
``permission`` в WS-сообщении больше ничего не значит.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config.settings import Settings
from core.actions.base import ToolContext
from core.actions.executor import validate_args
from core.actions.screen_capture import NEEDS_AUTHORIZATION, ScreenCaptureTool
from core.authority import (
    SCREEN_ACTION,
    SCREEN_EFFECT,
    SCREEN_SCOPE_TTL,
    SCREEN_SCOPES,
    AuthorityStatus,
    AuthorityStore,
    ProvenanceKind,
    classify_effect,
    screen_capture_proposal,
    screen_capture_request,
)
from core.capabilities import RiskLevel
from core.safety import assess_risk
from core.ws_server import JarvisWSServer

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WS_SRC = _REPO_ROOT / "core" / "ws_server.py"
_TOOL_SRC = _REPO_ROOT / "core" / "actions" / "screen_capture.py"


class _Clock:
    """Управляемое время: TTL-гранты иначе не проверить честно.

    Стартует в реальном «сейчас» с небольшим опережением. Мост строит
    предложение через ``screen_capture_proposal(scope)`` без ``now=``, то есть
    по системному времени — значит хранилище обязано считать текущим то же
    самое время, иначе свежий грант оказался бы «not yet valid». В продукте
    это одно и то же время (Orchestrator строит AuthorityStore с часами по
    умолчанию); управляемость оставлена только чтобы перескочить TTL.
    """

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc) + timedelta(seconds=5)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


class _Frame:
    """То, что возвращает ScreenCapture().capture()."""

    text = "то, что было на экране"
    active_window = "Блокнот"
    url = None


def _fake_capture_class(calls: list[bool]):
    """Двойник ScreenCapture, который считает каждый реальный захват."""

    class _Capture:
        def capture(self, *, permission: bool = False) -> _Frame:
            calls.append(permission)
            if not permission:
                raise PermissionError("Screen capture requires explicit permission")
            return _Frame()

    return _Capture


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def store(tmp_path: Path, clock: _Clock) -> AuthorityStore:
    return AuthorityStore(tmp_path / "authority", clock=clock)


def _issue(store: AuthorityStore, clock: _Clock, scope: str = "once") -> Any:
    """Грант, выданный так, как его выдаёт подтверждение пользователя."""
    return store.issue(
        screen_capture_proposal(scope, now=clock.now),
        source_kind=ProvenanceKind.USER_INSTRUCTION,
        source_role="user",
        source_text="покажи, что у меня на экране",
        source_id=f"confirmation-{scope}",
    )


def _context(store: Any = None, risk_level: str | None = None) -> ToolContext:
    extra: dict[str, Any] = {}
    if store is not None:
        extra["authority"] = store
    if risk_level:
        extra["risk_level"] = risk_level
    return ToolContext(settings=Settings(), extra=extra)


def _run_tool(monkeypatch: pytest.MonkeyPatch, context: ToolContext) -> tuple[Any, list[bool]]:
    calls: list[bool] = []
    monkeypatch.setattr(
        "core.actions.screen_capture.ScreenCapture", _fake_capture_class(calls),
    )
    return ScreenCaptureTool().run({}, context), calls


# ---------------------------------------------------------------------------
# 1. Инструмент: разрешение не является аргументом
# ---------------------------------------------------------------------------

def test_permission_flag_is_gone_from_the_schema() -> None:
    """Схема пуста: подделывать нечего."""
    schema = ScreenCaptureTool().input_schema
    assert schema.get("properties") == {}, f"в схеме снова есть аргументы: {schema}"
    assert "permission" not in schema.get("required", []), "permission снова обязателен"
    assert schema.get("additionalProperties") is False, (
        "схема разрешает лишние поля — permission можно дослать"
    )


def test_permission_argument_is_structurally_rejected() -> None:
    """Валидация аргументов отвергает флаг, а не молча его игнорирует."""
    schema = ScreenCaptureTool().input_schema
    assert validate_args(schema, {}) is None
    error = validate_args(schema, {"permission": True})
    assert error and "permission" in error, (
        f"аргумент permission прошёл валидацию: {error!r}"
    )


def test_tool_without_authority_store_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет хранилища полномочий — нет разрешения (так работает subprocess-путь)."""
    result, calls = _run_tool(monkeypatch, _context())
    assert result.ok is False and result.error == NEEDS_AUTHORIZATION
    assert calls == [], "экран сняли без хранилища полномочий"


def test_tool_without_grant_denies(monkeypatch: pytest.MonkeyPatch,
                                   store: AuthorityStore) -> None:
    """Хранилище есть, гранта нет — отказ с внятной причиной."""
    result, calls = _run_tool(monkeypatch, _context(store))
    assert result.ok is False
    assert result.error and result.error.startswith(NEEDS_AUTHORIZATION)
    assert "no delegated authority" in result.error, result.error
    assert calls == [], "экран сняли без гранта"


def test_tool_with_grant_captures(monkeypatch: pytest.MonkeyPatch,
                                  store: AuthorityStore, clock: _Clock) -> None:
    """С действующим грантом инструмент отдаёт текст и id гранта."""
    grant = _issue(store, clock, "session")
    result, calls = _run_tool(monkeypatch, _context(store))
    assert result.ok is True, result.error
    assert calls == [True], f"драйвер вызван не один раз или без permission: {calls}"
    assert result.output["text"] == _Frame.text
    assert result.output["active_window"] == _Frame.active_window
    assert result.output["grant_id"] == grant.grant_id


def test_expired_grant_denies_again(monkeypatch: pytest.MonkeyPatch,
                                    store: AuthorityStore, clock: _Clock) -> None:
    """TTL истёк — снова нужен человек (третий случай из спецификации S3)."""
    _issue(store, clock, "once")
    clock.advance(seconds=SCREEN_SCOPE_TTL["once"].total_seconds() + 1)
    result, calls = _run_tool(monkeypatch, _context(store))
    assert result.ok is False and calls == []
    assert result.error and result.error.startswith(NEEDS_AUTHORIZATION)


def test_revoked_grant_denies(monkeypatch: pytest.MonkeyPatch,
                              store: AuthorityStore, clock: _Clock) -> None:
    """Отозванный грант не «почти действующий»."""
    grant = _issue(store, clock, "session")
    assert store.revoke(grant.grant_id, "тест") is True
    result, calls = _run_tool(monkeypatch, _context(store))
    assert result.ok is False and calls == []


def test_risk_above_ceiling_is_not_covered_by_the_grant(
        monkeypatch: pytest.MonkeyPatch, store: AuthorityStore, clock: _Clock) -> None:
    """Грант на чтение экрана имеет ceiling=medium: HIGH им не разрешается."""
    _issue(store, clock, "session")
    result, calls = _run_tool(monkeypatch, _context(store, risk_level="high"))
    assert result.ok is False and calls == []
    assert "risk ceiling exceeded" in (result.error or ""), result.error


def test_risk_never_drops_below_the_passport_level(
        monkeypatch: pytest.MonkeyPatch, store: AuthorityStore, clock: _Clock) -> None:
    """Занижением риска нельзя пролезть под чужой низкий ceiling (S1)."""
    _issue(store, clock, "session")
    result, calls = _run_tool(monkeypatch, _context(store, risk_level="low"))
    assert result.ok is True and calls == [True], result.error
    # Паспортный medium остаётся: запрос собран не по присланному "low".
    assert store.check(screen_capture_request(risk=RiskLevel.MEDIUM)).allowed is True


def test_garbage_risk_level_does_not_open_the_gate(
        monkeypatch: pytest.MonkeyPatch, store: AuthorityStore, clock: _Clock) -> None:
    """Мусор в risk_level трактуется как medium, а не как «нет риска»."""
    _issue(store, clock, "session")
    result, _ = _run_tool(monkeypatch, _context(store, risk_level="не-уровень"))
    assert result.ok is True, result.error


# ---------------------------------------------------------------------------
# 2. Классификация эффекта: read_screen выводится из инструмента, не из текста
# ---------------------------------------------------------------------------

def test_effect_of_the_screen_tools_is_read_screen() -> None:
    for tool in ("screen_capture", "computer_screenshot", "screenshot"):
        assert classify_effect("посмотри на экран", "", tool, {}) == SCREEN_EFFECT
    assert classify_effect("", SCREEN_ACTION, "", {}) == SCREEN_EFFECT


def test_credential_goal_falls_outside_the_screen_grant() -> None:
    """«Покажи пароли с экрана» — не read_screen: грант его не покрывает."""
    effect = classify_effect("покажи пароли с экрана", "", "screen_capture", {})
    assert effect != SCREEN_EFFECT, (
        "цель про секреты классифицирована как обычное чтение экрана"
    )


# ---------------------------------------------------------------------------
# 3. WS-мост: разрешение спрашивается у пользователя, а не у клиента
# ---------------------------------------------------------------------------


class _FakeWS:
    """Двойник соединения: складывает отправленные кадры."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _BareOrch:
    """Ядро без хранилища полномочий — разрешать нечем (fail-closed путь)."""

    def __init__(self) -> None:
        self._settings = Settings()
        self._output_callback = lambda text: None


class _Orch(_BareOrch):
    """Полномочия ядра ровно теми методами, которыми их знает мост.

    Хранилище настоящее: двойник AuthorityStore проверял бы двойника,
    а не правило.
    """

    def __init__(self, store: AuthorityStore) -> None:
        super().__init__()
        self._store = store
        self.answered: list[tuple[str, bool]] = []

    def check_authority(self, request: Any) -> Any:
        return self._store.check(request)

    def issue_authority(self, proposal: Any, *, user_instruction: str,
                        source_id: str, source_role: str = "user") -> Any:
        return self._store.issue(
            proposal, source_kind=ProvenanceKind.USER_INSTRUCTION,
            source_role=source_role, source_text=user_instruction,
            source_id=source_id,
        )

    def revoke_authority(self, grant_id: str, reason: str = "user revoked") -> bool:
        return self._store.revoke(grant_id, reason)

    def execute_authorized(self, request: Any, callback: Any) -> Any:
        return self._store.execute_authorized(request, callback)

    def answer_confirmation(self, confirmation_id: str, approve: bool) -> dict:
        self.answered.append((confirmation_id, approve))
        return {}


@pytest.fixture()
def ws_capture(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Мост берёт драйвер из ``core.vision.screen`` внутри колбэка."""
    calls: list[bool] = []
    monkeypatch.setattr("core.vision.screen.ScreenCapture", _fake_capture_class(calls))
    return calls


def _server(orch: Any) -> JarvisWSServer:
    return JarvisWSServer(orch, auth_token=None)


def _send(server: JarvisWSServer, ws: _FakeWS, payload: dict[str, Any]) -> None:
    asyncio.run(server._on_message(ws, json.dumps(payload)))


def _frames(ws: _FakeWS) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in ws.sent]


def _ask(server: JarvisWSServer, ws: _FakeWS) -> str:
    """Запрос снимка без гранта: возвращает confirmation_id из вопроса."""
    _send(server, ws, {"type": "screen_capture"})
    frame = _frames(ws)[-1]
    assert frame["type"] == "confirmation_required", frame
    return str(frame["confirmation_id"])


def test_ws_without_grant_asks_the_user(ws_capture: list[bool],
                                        store: AuthorityStore) -> None:
    """Первый запрос снимка — вопрос пользователю с перечнем объёмов."""
    server, ws = _server(_Orch(store)), _FakeWS()
    _send(server, ws, {"type": "screen_capture"})
    frames = _frames(ws)
    assert len(frames) == 1, frames
    assert frames[0]["type"] == "confirmation_required"
    assert frames[0]["tool"] == "screen_capture"
    assert frames[0]["risk"]["level"] == "medium"
    # Объёмы перечисляет сервер: клиент не придумывает, на что соглашается.
    assert frames[0]["scopes"] == list(SCREEN_SCOPES)
    assert ws_capture == [], "экран сняли до ответа пользователя"
    assert list(server._pending_screen) == [frames[0]["confirmation_id"]]


def test_ws_permission_field_in_the_message_is_inert(ws_capture: list[bool],
                                                     store: AuthorityStore) -> None:
    """Главная дыра S3: ``permission: true`` в сообщении больше ничего не даёт."""
    server, ws = _server(_Orch(store)), _FakeWS()
    _send(server, ws, {"type": "screen_capture", "permission": True})
    assert _frames(ws)[0]["type"] == "confirmation_required"
    assert ws_capture == [], "поле permission из сообщения снова разрешает снимок"


def test_ws_confirm_once_captures_and_spends_the_grant(
        ws_capture: list[bool], store: AuthorityStore) -> None:
    """scope=once: кадр отдан, грант отозван сразу — «один раз» буквально."""
    server, ws = _server(_Orch(store)), _FakeWS()
    cid = _ask(server, ws)
    _send(server, ws, {"type": "confirm", "confirmation_id": cid,
                       "approve": True, "scope": "once"})
    frame = _frames(ws)[1]
    assert frame["type"] == "screen_capture", frame
    assert frame["text"] == _Frame.text
    assert frame["active_window"] == _Frame.active_window
    assert ws_capture == [True], f"драйвер вызван неверно: {ws_capture}"
    grant = store.get(frame["grant_id"])
    assert grant is not None and grant.status is AuthorityStatus.REVOKED, (
        "одноразовый грант остался действующим после снимка"
    )
    assert cid not in server._pending_screen, "подтверждение можно переиграть"
    # Следующий снимок снова требует человека.
    _send(server, ws, {"type": "screen_capture"})
    assert _frames(ws)[2]["type"] == "confirmation_required"
    assert ws_capture == [True], "второй снимок прошёл по истраченному гранту"


def test_ws_declined_confirmation_issues_no_grant(ws_capture: list[bool],
                                                  store: AuthorityStore) -> None:
    """Отказ пользователя не создаёт полномочия ни в каком объёме."""
    server, ws = _server(_Orch(store)), _FakeWS()
    cid = _ask(server, ws)
    _send(server, ws, {"type": "confirm", "confirmation_id": cid,
                       "approve": False, "scope": "permanent"})
    assert _frames(ws)[1] == {"type": "error",
                              "message": JarvisWSServer.SCREEN_DENIED}
    assert ws_capture == []
    assert store.list() == [], "отказ выдал грант"
    assert store.check(screen_capture_request()).allowed is False


def test_ws_unknown_scope_is_not_downgraded(ws_capture: list[bool],
                                            store: AuthorityStore) -> None:
    """Объёма «навсегда» в протоколе нет: это ошибка, а не «что-то поменьше»."""
    server, ws = _server(_Orch(store)), _FakeWS()
    cid = _ask(server, ws)
    _send(server, ws, {"type": "confirm", "confirmation_id": cid,
                       "approve": True, "scope": "навсегда"})
    assert _frames(ws)[1]["message"] == "unknown scope: навсегда"
    assert ws_capture == []
    assert store.list() == [], "неизвестный объём всё же выдал грант"


def test_ws_session_scope_lasts_until_disconnect(ws_capture: list[bool],
                                                 store: AuthorityStore) -> None:
    """«На сеанс» — на это соединение: отключение закрывает грант."""
    server, ws = _server(_Orch(store)), _FakeWS()
    cid = _ask(server, ws)
    _send(server, ws, {"type": "confirm", "confirmation_id": cid,
                       "approve": True, "scope": "session"})
    assert _frames(ws)[1]["type"] == "screen_capture"
    # Второй снимок в том же соединении — без повторного вопроса.
    _send(server, ws, {"type": "screen_capture"})
    assert _frames(ws)[2]["type"] == "screen_capture"
    assert ws_capture == [True, True]
    grant_id = _frames(ws)[1]["grant_id"]
    assert server._session_screen_grants[id(ws)] == {grant_id}

    server._revoke_session_screen_grants(ws)
    grant = store.get(grant_id)
    assert grant is not None and grant.status is AuthorityStatus.REVOKED
    _send(server, ws, {"type": "screen_capture"})
    assert _frames(ws)[3]["type"] == "confirmation_required"
    assert ws_capture == [True, True], "снимок прошёл после отключения клиента"


def test_ws_expired_session_grant_asks_again(ws_capture: list[bool],
                                             store: AuthorityStore,
                                             clock: _Clock) -> None:
    """TTL сессионного гранта — 12 часов, а не «до перезапуска процесса»."""
    server, ws = _server(_Orch(store)), _FakeWS()
    cid = _ask(server, ws)
    _send(server, ws, {"type": "confirm", "confirmation_id": cid,
                       "approve": True, "scope": "session"})
    assert _frames(ws)[1]["type"] == "screen_capture"
    clock.advance(seconds=SCREEN_SCOPE_TTL["session"].total_seconds() + 1)
    _send(server, ws, {"type": "screen_capture"})
    assert _frames(ws)[2]["type"] == "confirmation_required"
    assert ws_capture == [True]


def test_ws_core_without_authority_api_denies(ws_capture: list[bool]) -> None:
    """Ядро без полномочий отказывает, а не «разрешает по умолчанию»."""
    server, ws = _server(_BareOrch()), _FakeWS()
    _send(server, ws, {"type": "screen_capture"})
    assert _frames(ws) == [{"type": "error",
                            "message": JarvisWSServer.SCREEN_DENIED}]
    assert ws_capture == []


def test_ws_non_screen_confirmation_still_reaches_the_core(
        store: AuthorityStore) -> None:
    """Перехват подтверждений снимка не сломал обычный путь подтверждений."""
    orch = _Orch(store)
    server, ws = _server(orch), _FakeWS()
    _send(server, ws, {"type": "confirm", "confirmation_id": "task-1",
                       "approve": True})
    assert orch.answered == [("task-1", True)]
    assert server._pending_screen == {}


# ---------------------------------------------------------------------------
# 4. Структурные проверки: удалённые поверхности не возвращаются молча
# ---------------------------------------------------------------------------

def test_ws_source_no_longer_trusts_a_permission_field() -> None:
    src = _WS_SRC.read_text(encoding="utf-8")
    assert 'msg.get("permission")' not in src, (
        "ветка screen_capture снова читает permission из сообщения клиента"
    )
    assert "Screen capture requires explicit permission" not in src, (
        "вернулся старый отказ, привязанный к флагу клиента"
    )


def test_tool_source_never_takes_permission_from_arguments() -> None:
    src = _TOOL_SRC.read_text(encoding="utf-8")
    assert 'args.get("permission")' not in src, "инструмент снова читает аргумент"
    assert "permission" not in json.dumps(ScreenCaptureTool().input_schema), (
        "флаг permission вернулся в схему"
    )
    # Драйверу разрешение передаётся константой — уже ПОСЛЕ проверки гранта.
    assert "permission=True" in src


# ---------------------------------------------------------------------------
# 5. Агент: полномочие рождается только на подтверждении человека
# ---------------------------------------------------------------------------

_AGENT_SRC = _REPO_ROOT / "core" / "agent.py"


def _agent(store: Any) -> Any:
    """Agent без конструктора: проверяемым помощникам нужны только полномочия."""
    from core.agent import Agent

    agent = Agent.__new__(Agent)
    agent._authority = store
    return agent


def test_screen_capture_risk_alone_never_asks_the_user() -> None:
    """Причина, по которой S3 не закрывался одним грантом.

    ``assess_risk`` даёт MEDIUM, а MEDIUM не требует подтверждения — значит
    без отдельного правила агент никогда бы не спросил человека, и инструмент
    отказывал бы навсегда.
    """
    risk = assess_risk("посмотри, что у меня на экране", "screen_capture",
                       arguments={})
    assert risk.level is RiskLevel.MEDIUM
    assert risk.needs_confirmation is False


def test_agent_asks_when_the_screen_grant_is_missing(store: AuthorityStore) -> None:
    risk = assess_risk("посмотри, что у меня на экране", "screen_capture",
                       arguments={})
    assert _agent(store)._screen_authority_missing("screen_capture", risk) is True


def test_agent_stops_asking_once_the_grant_exists(store: AuthorityStore,
                                                  clock: _Clock) -> None:
    _issue(store, clock, "session")
    risk = assess_risk("посмотри на экран", "screen_capture", arguments={})
    assert _agent(store)._screen_authority_missing("screen_capture", risk) is False


def test_agent_does_not_ask_for_unrelated_tools(store: AuthorityStore) -> None:
    risk = assess_risk("прочитай файл", "read_file", arguments={"path": "a.txt"})
    assert _agent(store)._screen_authority_missing("read_file", risk) is False


def test_agent_does_not_ask_when_the_grant_could_not_help(
        store: AuthorityStore) -> None:
    """HIGH грант с ceiling=medium не разрешит: спрашивать было бы нечестно."""
    high = SimpleNamespace(level=RiskLevel.HIGH)
    assert _agent(store)._screen_authority_missing("screen_capture", high) is False


def test_agent_without_authority_store_does_not_ask() -> None:
    """Без хранилища подтверждение бессмысленно: отказывает сам инструмент."""
    risk = assess_risk("посмотри на экран", "screen_capture", arguments={})
    assert _agent(None)._screen_authority_missing("screen_capture", risk) is False


def test_agent_grant_lives_only_for_the_confirmed_execution(
        store: AuthorityStore) -> None:
    """Подтверждение выдаёт грант, finally его отзывает — окна не остаётся."""
    agent = _agent(store)
    grant_id = agent._grant_screen_authority(
        "screen_capture", {}, "confirm-1", "покажи, что у меня на экране")
    assert grant_id, "подтверждение не выдало полномочие"
    assert store.check(screen_capture_request()).allowed is True
    agent._revoke_screen_authority(grant_id)
    assert store.check(screen_capture_request()).allowed is False
    revoked = store.get(grant_id)
    assert revoked is not None and revoked.status is AuthorityStatus.REVOKED


def test_agent_grant_carries_the_user_text_as_provenance(
        store: AuthorityStore) -> None:
    """Источник гранта — текст пользователя, а не выдумка инструмента.

    Сам текст в грант не пишется (в нём мог быть личный запрос) — пишется его
    sha256, и он должен совпадать с целью, которую подтвердил человек.
    """
    goal = "покажи, что у меня на экране"
    grant_id = _agent(store)._grant_screen_authority(
        "screen_capture", {}, "confirm-2", goal)
    grant = store.get(str(grant_id))
    assert grant is not None
    provenance = grant.to_dict()["provenance"]
    assert provenance["instruction_sha256"] == hashlib.sha256(
        goal.encode("utf-8")).hexdigest()
    assert provenance["source_id"] == "confirm-2"
    assert provenance["source_role"] == "user"
    assert provenance["kind"] == ProvenanceKind.USER_INSTRUCTION.value


def test_agent_issues_nothing_for_other_tools(store: AuthorityStore) -> None:
    assert _agent(store)._grant_screen_authority(
        "write_file", {}, "confirm-3", "запиши файл") is None
    assert store.list() == [], "полномочие на экран выдано чужому инструменту"


def test_agent_grant_follows_the_planned_tool_too(store: AuthorityStore) -> None:
    """Путь восстановления знает инструмент из решения, а не из аргумента."""
    pending = {"decision": SimpleNamespace(tool="screen_capture")}
    assert _agent(store)._grant_screen_authority(
        "", pending, "confirm-4", "посмотри на экран") is not None


def test_every_confirmation_gate_consults_the_screen_grant() -> None:
    """Четыре точки: основной гейт, восстановление, цикл и repair-гейт."""
    src = _AGENT_SRC.read_text(encoding="utf-8")
    assert src.count("self._screen_authority_missing(") == 4, (
        "число гейтов, спрашивающих про полномочие на экран, изменилось"
    )
    assert "self._revoke_screen_authority(screen_grant)" in src, (
        "грант, выданный подтверждением, больше не отзывается"
    )




