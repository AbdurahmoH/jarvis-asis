"""Regression C5 — ключ поставки рядом с exe, не в коде.

* ``bootstrap_from_local_secrets``: secrets.local.json → DPAPI при первом
  запуске, затем файл удаляется (одноразовый бутстрап); битый JSON и
  отсутствие файла — не ошибки.
* упаковщик: ключ берётся из secrets.local.json, если env не задан; без
  обоих источников сборка падает с явным сообщением; копия файла кладётся
  в пакет.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.settings import Settings
from core.brain.secrets import DPAPISecretStore, bootstrap_from_local_secrets


def _settings(tmp_path: Path) -> Settings:
    return Settings(credential_store={
        "provider": "deepinfra",
        "reference": "DEEPINFRA_API_KEY",
        "path": str(tmp_path / "provider-secrets.dpapi"),
    })


def test_bootstrap_moves_key_to_dpapi_and_deletes_file(tmp_path: Path) -> None:
    secrets = tmp_path / "config" / "secrets.local.json"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text(json.dumps({"deepinfra_api_key": "shipped-key-32-chars-x"}), encoding="utf-8")
    store_path = tmp_path / "provider-secrets.dpapi"
    settings = _settings(tmp_path)

    assert bootstrap_from_local_secrets(settings, path=secrets) is True

    store = DPAPISecretStore(store_path)
    assert store.get("DEEPINFRA_API_KEY") == "shipped-key-32-chars-x"
    assert not secrets.exists(), "одноразовый бутстрап удаляет файл после проверки"


def test_bootstrap_without_file_is_a_noop(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert bootstrap_from_local_secrets(settings, path=tmp_path / "nope.json") is False


def test_bootstrap_keeps_file_on_broken_json(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.local.json"
    secrets.write_text("{not json", encoding="utf-8")
    settings = _settings(tmp_path)

    assert bootstrap_from_local_secrets(settings, path=secrets) is False
    assert secrets.exists(), "битый JSON — файл не теряем"
    store = DPAPISecretStore(tmp_path / "provider-secrets.dpapi")
    assert store.get("DEEPINFRA_API_KEY") is None


def test_bootstrap_without_key_entry_is_a_noop(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.local.json"
    secrets.write_text(json.dumps({"something_else": "x"}), encoding="utf-8")
    settings = _settings(tmp_path)

    assert bootstrap_from_local_secrets(settings, path=secrets) is False
    assert secrets.exists()


# --------------------------------------------------------------------------- #
# Упаковщик: secrets.local.json — обязательный источник для приватной сборки
# --------------------------------------------------------------------------- #


def _load_provision():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pkg_local_runtime", str(Path(__file__).resolve().parents[2] / "scripts" / "package_local_runtime.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._provision_packaged_credential


def test_provision_fails_loudly_without_any_source(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    provision = _load_provision()
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_BUILD_DEEPINFRA_API_KEY", raising=False)

    with pytest.raises(RuntimeError) as exc:
        provision(tmp_path / "out", secrets_file=tmp_path / "absent.json")
    assert "secrets.local.json missing" in str(exc.value)


def test_provision_reads_secrets_file_when_env_absent(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    provision = _load_provision()
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_BUILD_DEEPINFRA_API_KEY", raising=False)
    secrets = tmp_path / "secrets.local.json"
    secrets.write_text(json.dumps({"deepinfra_api_key": "pack-key-32-chars-x"}), encoding="utf-8")
    output = tmp_path / "out"

    blob = provision(output, secrets_file=secrets)

    store = DPAPISecretStore(blob)
    assert store.get("DEEPINFRA_API_KEY") == "pack-key-32-chars-x"


def test_provision_env_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provision = _load_provision()
    monkeypatch.setenv("DEEPINFRA_API_KEY", "env-pack-key-32-chars")
    secrets = tmp_path / "secrets.local.json"
    secrets.write_text(json.dumps({"deepinfra_api_key": "file-key"}), encoding="utf-8")

    blob = provision(tmp_path / "out", secrets_file=secrets)
    assert DPAPISecretStore(blob).get("DEEPINFRA_API_KEY") == "env-pack-key-32-chars"
