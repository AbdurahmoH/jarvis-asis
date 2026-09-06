"""Secret references for provider credentials; ordinary config stores no key values."""
from __future__ import annotations

import json
import os
import threading
import ctypes
import getpass
import hashlib
import hmac
import platform
import secrets as _secrets
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from core.security.atomic import atomic_write_bytes


class SecretStore(ABC):
    @abstractmethod
    def get(self, reference: str) -> str | None: ...


class MemorySecretStore(SecretStore):
    """Process-local store useful for runtime injection and tests."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._lock = threading.RLock()
        self._values = dict(values or {})

    def get(self, reference: str) -> str | None:
        with self._lock:
            value = self._values.get(reference, "").strip()
            return value or None

    def set(self, reference: str, secret: str) -> None:
        with self._lock:
            self._values[str(reference)] = str(secret)

    def delete(self, reference: str) -> None:
        with self._lock:
            self._values.pop(reference, None)


class EnvironmentSecretStore(SecretStore):
    def get(self, reference: str) -> str | None:
        normalized = reference.upper().replace('-', '_')
        aliases = {
            "DEEPINFRA": "DEEPINFRA_API_KEY",
            "DEEPINFRA_API_KEY": "DEEPINFRA_API_KEY",
        }
        names = (
            reference,
            aliases.get(normalized, ""),
            f"ATLAS_SECRET_{normalized}",
        )
        for name in names:
            if not name:
                continue
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return None


class CompositeSecretStore(SecretStore):
    def __init__(self, *stores: SecretStore) -> None:
        self._stores = stores

    def get(self, reference: str) -> str | None:
        for store in self._stores:
            value = store.get(reference)
            if value:
                return value
        return None


class DPAPISecretStore(SecretStore):
    """User-scoped encrypted local storage backed by Windows DPAPI."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _protect(data: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI secret storage requires Windows")
        # pywin32 is the preferred binding, but the bundled runtime may not
        # ship its optional extension.  Keep the same user-scoped DPAPI
        # contract through the Windows API as a dependency-free fallback.
        try:
            try:
                import win32crypt
            except ImportError:
                return DPAPISecretStore._crypt32_protect(data)
            return bytes(win32crypt.CryptProtectData(data, "ATLAS Brain Fabric", None, None, None, 0))
        except Exception:
            # Some service/CI Windows accounts do not have a DPAPI profile.
            # Keep the at-rest invariant with an authenticated, per-user
            # fallback rather than ever writing the secret in clear text.
            return DPAPISecretStore._fallback_protect(data)

    @staticmethod
    def _unprotect(data: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI secret storage requires Windows")
        if data.startswith(DPAPISecretStore._FALLBACK_MAGIC):
            return DPAPISecretStore._fallback_unprotect(data)
        try:
            try:
                import win32crypt
            except ImportError:
                return DPAPISecretStore._crypt32_unprotect(data)
            return bytes(win32crypt.CryptUnprotectData(data, None, None, None, 0)[1])
        except Exception:
            return DPAPISecretStore._fallback_unprotect(data)

    _FALLBACK_MAGIC = b"ATLAS-DPAPI-FALLBACK\x01"

    @staticmethod
    def _fallback_identity() -> bytes:
        identity = "|".join(
            (
                os.environ.get("USERDOMAIN", ""),
                os.environ.get("USERNAME", ""),
                os.environ.get("COMPUTERNAME", ""),
                platform.node(),
                getpass.getuser(),
            )
        )
        return identity.encode("utf-8", "replace")

    @staticmethod
    def _fallback_key(salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", DPAPISecretStore._fallback_identity(), salt, 120_000, dklen=32
        )

    @staticmethod
    def _fallback_protect(data: bytes) -> bytes:
        salt = _secrets.token_bytes(16)
        key = DPAPISecretStore._fallback_key(salt)
        stream = bytearray()
        counter = 0
        while len(stream) < len(data):
            stream.extend(hashlib.sha256(key + counter.to_bytes(8, "big")).digest())
            counter += 1
        cipher = bytes(a ^ b for a, b in zip(data, stream))
        tag = hmac.new(key, cipher, hashlib.sha256).digest()
        return DPAPISecretStore._FALLBACK_MAGIC + salt + tag + cipher

    @staticmethod
    def _fallback_unprotect(data: bytes) -> bytes:
        header = DPAPISecretStore._FALLBACK_MAGIC
        if not data.startswith(header) or len(data) < len(header) + 16 + 32:
            raise ValueError("invalid encrypted secret payload")
        offset = len(header)
        salt, tag, cipher = data[offset:offset + 16], data[offset + 16:offset + 48], data[offset + 48:]
        key = DPAPISecretStore._fallback_key(salt)
        if not hmac.compare_digest(tag, hmac.new(key, cipher, hashlib.sha256).digest()):
            raise ValueError("secret payload authentication failed")
        stream = bytearray()
        counter = 0
        while len(stream) < len(cipher):
            stream.extend(hashlib.sha256(key + counter.to_bytes(8, "big")).digest())
            counter += 1
        return bytes(a ^ b for a, b in zip(cipher, stream))

    @staticmethod
    def _crypt32_protect(data: bytes) -> bytes:
        """Call CryptProtectData directly when pywin32 is unavailable."""
        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        protect = crypt32.CryptProtectData
        protect.argtypes = [
            ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)
        ]
        protect.restype = ctypes.c_int
        source = ctypes.create_string_buffer(data)
        result = _Blob()
        description = ctypes.c_wchar_p("ATLAS Brain Fabric")
        source_blob = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
        if not protect(
            ctypes.byref(source_blob), description, None, None, None, 0, ctypes.byref(result)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(result.pbData)

    @staticmethod
    def _crypt32_unprotect(data: bytes) -> bytes:
        """Call CryptUnprotectData directly when pywin32 is unavailable."""
        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        unprotect = crypt32.CryptUnprotectData
        unprotect.argtypes = [
            ctypes.POINTER(_Blob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)
        ]
        unprotect.restype = ctypes.c_int
        source = ctypes.create_string_buffer(data)
        result = _Blob()
        source_blob = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
        description = ctypes.c_wchar_p()
        if not unprotect(
            ctypes.byref(source_blob), ctypes.byref(description), None, None, None, 0,
            ctypes.byref(result)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(result.pbData)
            if description:
                kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))

    def _load(self) -> dict[str, str]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(self._unprotect(raw).decode("utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return ({str(key): str(value) for key, value in payload.items()}
                if isinstance(payload, dict) else {})

    def _save(self, values: dict[str, str]) -> None:
        plain = json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
        atomic_write_bytes(self.path, self._protect(plain))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, reference: str) -> str | None:
        with self._lock:
            value = self._load().get(reference, "").strip()
            return value or None

    def set(self, reference: str, secret: str) -> None:
        if not reference.strip() or not secret:
            raise ValueError("secret reference and value are required")
        with self._lock:
            values = self._load()
            values[reference] = secret
            self._save(values)

    def delete(self, reference: str) -> None:
        with self._lock:
            values = self._load()
            values.pop(reference, None)
            self._save(values)


def bootstrap_from_local_secrets(settings, *, path=None) -> bool:
    """C5: одноразовый перенос ключа из ``config/secrets.local.json`` в DPAPI.

    Инсталлятор кладёт файл рядом с бинарьком/в корень проекта; при первом
    запуске ключ уезжает в DPAPI-хранилище (тот же credential store, что
    читают провайдеры мозга), файл удаляется после успешной проверки
    обратного чтения. Любая проблема — «переноса не было» (False), без
    исключений: продукт обязан стартовать и без ключа.
    """
    import logging
    from pathlib import Path

    log = logging.getLogger(__name__)
    try:
        candidate = path
        if candidate is None:
            candidate = Path("config") / "secrets.local.json"
        candidate = Path(candidate)
        if not candidate.is_file():
            return False
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        key = str(payload.get("deepinfra_api_key") or "").strip()
        if not key:
            log.warning("secrets.local.json без записи 'deepinfra_api_key' — перенос пропущен")
            return False

        store_cfg = getattr(settings, "credential_store", None)
        raw_path = str(getattr(store_cfg, "path", "") or "data/brain/provider-secrets.dpapi")
        store_path = Path(raw_path)
        if not store_path.is_absolute():
            data_dir = getattr(settings, "data_dir", "data")
            store_path = Path(data_dir).parent / store_path
        reference = str(getattr(store_cfg, "reference", "") or "DEEPINFRA_API_KEY").strip()

        store = DPAPISecretStore(store_path)
        store.set(reference, key)
        if store.get(reference) != key:
            log.warning("Проверка ключа после записи в DPAPI не удалась — файл сохранён")
            return False
        candidate.unlink()
        log.info("Ключ из secrets.local.json перенесён в DPAPI, файл удалён")
        return True
    except Exception as exc:
        log.warning("Бутстрап secrets.local.json не удался: %s", type(exc).__name__)
        return False


__all__ = [
    "SecretStore", "MemorySecretStore", "EnvironmentSecretStore", "CompositeSecretStore",
    "DPAPISecretStore", "bootstrap_from_local_secrets",
]
