"""S2 — токен WebSocket-аутентификации: генерация, шифрованное хранение, выдача.

Аудит 2026-09-05: WS-мост слушал 127.0.0.1:8771 без аутентификации с обеих
сторон. Любой локальный процесс (в том числе страница в браузере с
разрешённым Origin) мог отправить ``command``/``screen_capture``/
``settings:update``. Токен в ``JARVIS_WS_TOKEN`` был опционален: пустая
переменная → сервер стартовал полностью открытым.

Этот модуль — единственный источник токена:

* генерируется случайно (``secrets.token_urlsafe(32)``) один раз на установку;
* хранится **не в открытом виде**: тот же контракт at-rest, что у
  ``core/brain/secrets.DPAPISecretStore`` (Windows DPAPI, иначе
  аутентифицированный per-user fallback), файл ``data/security/ws-token.dpapi``;
* выдаётся лаунчеру через ``python -m core.ws_server --print-ws-token``
  (stdout, единственная строка), а лаунчер отдаёт его webview по
  ``invoke("ws_auth_token")`` — не параметром URL, который попал бы в логи.

Токен НИКОГДА не пишется в лог: для диагностики используется отпечаток
``token_fingerprint`` (первые 8 hex sha256).
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Mapping, Optional

from core.brain.secrets import DPAPISecretStore
from core.utils.logger import get_logger

__all__ = [
    "WS_TOKEN_REFERENCE",
    "ENV_TOKEN",
    "ENV_DEV_NOAUTH",
    "LocalTokenStore",
    "token_path",
    "token_fingerprint",
    "read_token",
    "load_or_create_token",
    "resolve_server_token",
]

log = get_logger(__name__)

#: Ключ внутри зашифрованного словаря (файл хранит dict, как и брайн-стор).
WS_TOKEN_REFERENCE = "ws_auth_token"

#: Явно переданный токен (лаунчер/CI/интеграции). Имеет приоритет над стором.
ENV_TOKEN = "JARVIS_WS_TOKEN"

#: Единственный способ запустить сервер без аутентификации (dev).
ENV_DEV_NOAUTH = "JARVIS_WS_DEV_NOAUTH"

#: Длина в байтах до base64url; 32 байта ≈ 43 символа.
_TOKEN_BYTES = 32


class LocalTokenStore(DPAPISecretStore):
    """``DPAPISecretStore``, работающий и вне Windows.

    Базовый класс поднимает ``RuntimeError`` при ``os.name != "nt"``, потому
    что предназначен для DPAPI. Здесь нужен тот же файл-формат и тот же
    запрет на открытый текст, но модуль обязан работать в тестах и на CI под
    Linux, поэтому вне Windows используется штатный аутентифицированный
    fallback базового класса (PBKDF2 от машинно-пользовательской
    идентичности + HMAC), а не отсутствие шифрования.
    """

    @staticmethod
    def _protect(data: bytes) -> bytes:
        if os.name == "nt":
            return DPAPISecretStore._protect(data)
        return DPAPISecretStore._fallback_protect(data)

    @staticmethod
    def _unprotect(data: bytes) -> bytes:
        if os.name == "nt":
            return DPAPISecretStore._unprotect(data)
        return DPAPISecretStore._fallback_unprotect(data)


def _data_dir(settings: object | None) -> Path:
    if settings is None:
        from config import load_config

        settings = load_config()
    data_dir = getattr(settings, "data_dir", None)
    return Path(data_dir) if data_dir is not None else Path("data")


def token_path(settings: object | None = None) -> Path:
    """Путь к зашифрованному файлу токена (``data/security/ws-token.dpapi``)."""
    return _data_dir(settings) / "security" / "ws-token.dpapi"


def token_fingerprint(token: str | None) -> str:
    """Короткий отпечаток для логов. Сам токен в лог не попадает никогда."""
    if not token:
        return "none"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _store(path: Path) -> LocalTokenStore:
    path.parent.mkdir(parents=True, exist_ok=True)
    return LocalTokenStore(path)


def read_token(settings: object | None = None, *, path: Path | None = None) -> Optional[str]:
    """Возвращает сохранённый токен или None, если его ещё нет."""
    target = Path(path) if path is not None else token_path(settings)
    if not target.exists():
        return None
    try:
        return _store(target).get(WS_TOKEN_REFERENCE)
    except Exception as exc:  # noqa: BLE001 — расшифровка чужого профиля/битый файл
        log.warning("Не удалось прочитать WS-токен из %s: %s", target, exc)
        return None


def load_or_create_token(settings: object | None = None, *,
                         path: Path | None = None) -> str:
    """Читает токен, а при первом запуске генерирует и сохраняет новый.

    Raises:
        RuntimeError: если токен не удаётся сохранить в зашифрованном виде.
            Сервер обязан в этом случае не стартовать, а не работать открытым.
    """
    target = Path(path) if path is not None else token_path(settings)
    existing = read_token(path=target)
    if existing:
        return existing
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    try:
        _store(target).set(WS_TOKEN_REFERENCE, token)
    except Exception as exc:  # noqa: BLE001 — включает RuntimeError из DPAPI
        raise RuntimeError(
            f"не удалось сохранить WS-токен в {target}: {exc}"
        ) from exc
    stored = read_token(path=target)
    if stored != token:
        raise RuntimeError(
            f"WS-токен записан в {target}, но не читается обратно — "
            "хранилище неработоспособно"
        )
    log.info("Сгенерирован новый WS-токен (fp=%s), файл: %s",
             token_fingerprint(token), target)
    return token


def resolve_server_token(settings: object | None = None, *,
                         env: Mapping[str, str] | None = None,
                         path: Path | None = None) -> tuple[Optional[str], str]:
    """Решает, с каким токеном стартовать сервер.

    Порядок (первое сработавшее правило побеждает):

    1. ``JARVIS_WS_TOKEN`` непустой → используем его (лаунчер/CI).
    2. ``JARVIS_WS_DEV_NOAUTH=1`` → dev-режим без аутентификации, громкий
       warning. Проверяется ДО стора: иначе флаг был бы недостижим, ведь
       стор всегда может создать токен.
    3. Иначе — токен из ``data/security/ws-token.dpapi`` (создаётся при
       первом запуске).

    Returns:
        ``(token, mode)``, где mode ∈ {"env", "dev-noauth", "store"}.
        ``token is None`` возможен только в режиме "dev-noauth".

    Raises:
        RuntimeError: стор недоступен и dev-флаг не выставлен.
    """
    environ = os.environ if env is None else env
    explicit = (environ.get(ENV_TOKEN) or "").strip()
    if explicit:
        log.info("WS-аутентификация: токен получен из %s (fp=%s)",
                 ENV_TOKEN, token_fingerprint(explicit))
        return explicit, "env"
    if (environ.get(ENV_DEV_NOAUTH) or "").strip() == "1":
        log.warning(
            "!!! WS-СЕРВЕР СТАРТУЕТ БЕЗ АУТЕНТИФИКАЦИИ: %s=1. "
            "Любой локальный процесс может отправлять команды, читать экран и "
            "менять настройки. Это режим ТОЛЬКО для разработки — снимите флаг.",
            ENV_DEV_NOAUTH,
        )
        return None, "dev-noauth"
    token = load_or_create_token(settings, path=path)
    log.info("WS-аутентификация включена, токен из хранилища (fp=%s)",
             token_fingerprint(token))
    return token, "store"
