#!/usr/bin/env python
"""Stage the production runtime for the Tauri installer.

The conversational brain is DeepInfra/DeepSeek.  The owner credential is
provisioned into a Windows DPAPI payload during production packaging; it is
never written to tracked source/config files.  Only the backend, local Piper
TTS and local Whisper/STT assets are staged; no GGUF or llama-server is part
of the production bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _provision_packaged_credential(output: Path, *, dry_run: bool = False,
                                   secrets_file: Path | None = None) -> Path:
    """Create the owner credential payload without ever logging its value.

    C5: ключ берётся из env (``JARVIS_BUILD_DEEPINFRA_API_KEY`` /
    ``DEEPINFRA_API_KEY``) или из ``config/secrets.local.json`` разработчика.
    Нет ни того, ни другого — сборка ФЕЙЛИТСЯ с явным сообщением: приватная
    сборка без ключа недопустима.
    """
    target = output / "data" / "brain" / "provider-secrets.dpapi"
    if dry_run:
        return target
    raw = (
        os.environ.get("JARVIS_BUILD_DEEPINFRA_API_KEY", "").strip()
        or os.environ.get("DEEPINFRA_API_KEY", "").strip()
    )
    if not raw:
        local_secrets = secrets_file or (ROOT / "config" / "secrets.local.json")
        if not local_secrets.is_file():
            raise RuntimeError(
                "secrets.local.json missing — create it before packaging "
                '(format: {"deepinfra_api_key": "<key>"}) or set '
                "JARVIS_BUILD_DEEPINFRA_API_KEY / DEEPINFRA_API_KEY"
            )
        try:
            payload = json.loads(local_secrets.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError(f"secrets.local.json is not valid JSON: {exc}") from exc
        raw = str(payload.get("deepinfra_api_key") or "").strip()
        if not raw:
            raise RuntimeError("secrets.local.json has no 'deepinfra_api_key' entry")
    from core.brain.secrets import DPAPISecretStore
    target.parent.mkdir(parents=True, exist_ok=True)
    store = DPAPISecretStore(target)
    store.set("DEEPINFRA_API_KEY", raw)
    if store.get("DEEPINFRA_API_KEY") != raw:
        raise RuntimeError("Packaged DeepInfra credential verification failed")
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_backend_pythons(explicit: str | None = None) -> list[Path]:
    """Return Python runtimes in the same order as the desktop launcher."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_value = os.environ.get("JARVIS_PYTHON", "").strip()
    if env_value:
        candidates.append(Path(env_value).expanduser())
    candidates.extend([
        ROOT / "runtime" / "python.exe",
        ROOT / ".venv" / "Scripts" / "python.exe",
        Path.home() / "AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe",
        Path.home() / "venv/Scripts/python.exe",
        Path(sys.executable),
    ])
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key not in seen and resolved.is_file():
            seen.add(key)
            result.append(resolved)
    return result


def _select_backend_python(explicit: str | None = None) -> Path:
    """Find a Python that has the frozen-backend build dependency installed."""
    for candidate in _candidate_backend_pythons(explicit):
        probe = subprocess.run(
            [str(candidate), "-c", "import pydantic, websockets, PyInstaller"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError(
        "Не найден Python с pydantic, websockets и PyInstaller. "
        "Укажите --backend-python на окружение сборки."
    )


def _build_backend_executable(*, output: Path, python: Path, dry_run: bool = False) -> Path:
    """Build a windowless, self-contained WS backend for the installer."""
    if dry_run:
        return output / "runtime" / "jarvis-backend.exe"
    build_root = ROOT / "runs" / "packaging" / "backend"
    if build_root.exists():
        shutil.rmtree(build_root)
    dist_root = build_root / "dist"
    work_root = build_root / "work"
    spec_root = build_root / "spec"
    for path in (dist_root, work_root, spec_root):
        path.mkdir(parents=True, exist_ok=True)
    command = [
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--noconsole", "--name", "jarvis-backend",
        "--distpath", str(dist_root), "--workpath", str(work_root),
        "--specpath", str(spec_root), "--paths", str(ROOT),
        "--collect-submodules", "core", "--collect-submodules", "config",
        "--collect-submodules", "chromadb",
        "--collect-data", "chromadb",
        "--hidden-import", "chromadb.execution",
        "--hidden-import", "chromadb.execution.executor",
        "--hidden-import", "chromadb.execution.executor.local",
        "--collect-submodules", "chromadb.segment.impl.metadata",
        str(ROOT / "scripts" / "packaged_backend.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    built = dist_root / "jarvis-backend.exe"
    if not built.is_file() or built.stat().st_size < 1024 * 1024:
        raise RuntimeError(f"PyInstaller не создал backend: {built}")
    target = output / "runtime" / "jarvis-backend.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, target)
    return target


def stage(*, output: Path, include_fallback: bool = False,
          backend_python: str | None = None, backend_executable: str | None = None,
          dry_run: bool = False) -> dict:
    from config import load_config
    settings = load_config()
    files: list[Path] = []
    configured_voice = getattr(settings.voice, "resolved_primary_piper_model", None)
    if configured_voice is None:
        configured_voice = getattr(settings.voice, "resolved_piper_model", None)
    voice_model = Path(configured_voice) if configured_voice is not None else (
        ROOT / "data" / "models" / "piper" / "ru_RU-denis-medium.onnx"
    )
    voice_config = voice_model.with_name(voice_model.name + ".json")
    stt_model_dir = ROOT / "data" / "models" / "stt" / "faster-whisper-small"
    stt_model_files = sorted(
        item for item in stt_model_dir.iterdir() if item.is_file()
    ) if stt_model_dir.is_dir() else []
    if not stt_model_files:
        raise FileNotFoundError(
            f"Локальная STT-модель не найдена: {stt_model_dir}"
        )
    embedding_source = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
    if not (embedding_source / "onnx" / "model.onnx").is_file():
        raise FileNotFoundError(f"Локальная embedding-модель ChromaDB не найдена: {embedding_source}")
    voice_runtime_candidates = [
        ROOT / "data" / "runtime" / "piper",
        ROOT / "runtime" / "piper",
    ]
    voice_runtime = next(
        (candidate for candidate in voice_runtime_candidates if (candidate / "piper.exe").is_file()),
        voice_runtime_candidates[0],
    )
    if not voice_model.is_file() or not voice_config.is_file():
        raise FileNotFoundError(
            f"Русский Piper voice не найден: {voice_model}(.json)"
        )
    if not (voice_runtime / "piper.exe").is_file():
        raise FileNotFoundError("Проверенный Piper runtime не найден: data/runtime/piper/piper.exe")

    if not dry_run and output.exists():
        shutil.rmtree(output)

    if backend_executable:
        backend = Path(backend_executable).expanduser().resolve()
        if not dry_run and not backend.is_file():
            raise FileNotFoundError(f"backend executable not found: {backend}")
    else:
        backend = _build_backend_executable(
            output=output, python=_select_backend_python(backend_python), dry_run=dry_run,
        )
    manifest = {
        "format": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "offline": False,
        "cloud_api": True,
        # The manifest is shipped to another machine.  Keep it reproducible
        # without leaking the builder's drive, user profile, or workspace.
        "server": None,
        "backend": {"source": "bundled PyInstaller backend", "filename": "runtime/jarvis-backend.exe", "windowless": True},
        "voice": {"provider": "piper", "language": "ru", "model": f"data/models/piper/{voice_model.name}", "runtime": "runtime/piper/piper.exe"},
        "brain": {
            "provider": "deepinfra",
            "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
            "credential": "data/brain/provider-secrets.dpapi",
            "credential_backend": "windows_dpapi",
        },
        "model_family": "deepseek",
        "models": [],
        "files": [],
    }
    manifest["files"].append({
        "filename": "runtime/jarvis-backend.exe",
        "size_bytes": backend.stat().st_size if backend.is_file() else 0,
        "sha256": _sha256(backend) if backend.is_file() else "",
    })
    for item in (voice_model, voice_config):
        manifest["files"].append({
            "filename": f"data/models/piper/{item.name}",
            "size_bytes": item.stat().st_size,
            "sha256": _sha256(item),
        })
    for item in stt_model_files:
        manifest["files"].append({
        "filename": f"data/models/stt/faster-whisper-small/{item.name}",
            "size_bytes": item.stat().st_size,
            "sha256": _sha256(item),
        })
    for item in sorted(embedding_source.rglob("*")):
        if item.is_file():
            manifest["files"].append({
                "filename": f"data/models/embeddings/all-MiniLM-L6-v2/{item.relative_to(embedding_source).as_posix()}",
                "size_bytes": item.stat().st_size,
                "sha256": _sha256(item),
            })
    for item in voice_runtime.rglob("*"):
        if item.is_file():
            manifest["files"].append({
                "filename": f"runtime/piper/{item.relative_to(voice_runtime).as_posix()}",
                "size_bytes": item.stat().st_size,
                "sha256": _sha256(item),
            })
    total_bytes = sum(int(item["size_bytes"]) for item in manifest["models"] + manifest["files"])
    manifest["total_bytes"] = total_bytes
    if dry_run:
        return manifest

    (output / "data" / "models" / "piper").mkdir(parents=True, exist_ok=True)
    (output / "runtime").mkdir(parents=True, exist_ok=True)
    credential = _provision_packaged_credential(output)
    # C5: копия secrets.local.json в пакет — запасной одноразовый бутстрап,
    # если DPAPI-блоб не расшифруется на машине пользователя; первый запуск
    # переносит ключ в локальный DPAPI и удаляет файл.
    local_secrets = ROOT / "config" / "secrets.local.json"
    if local_secrets.is_file():
        secrets_target = output / "config" / "secrets.local.json"
        secrets_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_secrets, secrets_target)
        manifest["files"].append({
            "filename": "config/secrets.local.json",
            "size_bytes": secrets_target.stat().st_size,
            "sha256": _sha256(secrets_target),
        })
    manifest["files"].append({
        "filename": "data/brain/provider-secrets.dpapi",
        "size_bytes": credential.stat().st_size,
        "sha256": _sha256(credential),
    })
    manifest["total_bytes"] = sum(
        int(item["size_bytes"]) for item in manifest["models"] + manifest["files"]
    )
    backend_target = output / "runtime" / "jarvis-backend.exe"
    if backend.resolve() != backend_target.resolve():
        shutil.copy2(backend, backend_target)
    shutil.copy2(voice_model, output / "data" / "models" / "piper" / voice_model.name)
    shutil.copy2(voice_config, output / "data" / "models" / "piper" / voice_config.name)
    stt_target = output / "data" / "models" / "stt" / "faster-whisper-small"
    stt_target.mkdir(parents=True, exist_ok=True)
    for item in stt_model_files:
        shutil.copy2(item, stt_target / item.name)
    shutil.copytree(
        embedding_source,
        output / "data" / "models" / "embeddings" / "all-MiniLM-L6-v2",
        dirs_exist_ok=True,
    )
    shutil.copytree(voice_runtime, output / "runtime" / "piper", dirs_exist_ok=True)

    # The PyInstaller archive owns executable Python modules. Shipping a
    # loose core tree beside the executable puts it ahead of the archive on
    # sys.path and silently runs stale source after an incremental package.
    # Only non-executable resources are staged here.
    for source_name in ("config", "persona"):
        source = ROOT / source_name
        if source.is_dir():
            shutil.copytree(source, output / source_name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "settings.json"))
    external_core = output / "core"
    if external_core.exists():
        shutil.rmtree(external_core)
    notices = ROOT / "THIRD_PARTY_NOTICES.txt"
    if notices.is_file():
        shutil.copy2(notices, output / notices.name)

    config = json.loads((ROOT / "config" / "settings.example.json").read_text(encoding="utf-8"))
    config["offline_mode"] = False
    config["auto_download_models"] = False
    config["deepseek_brain_mode"] = True
    config["deepseek_provider"] = "deepinfra"
    config["deepseek_model"] = "deepseek-ai/DeepSeek-V4-Flash-0731"
    config["model_tiers"] = {key: "deepseek-ai/DeepSeek-V4-Flash-0731" for key in ("fast", "analyst", "coder", "architect", "research")}
    config["tier_providers"] = {key: "deepinfra" for key in ("fast", "analyst", "coder", "architect", "research")}
    config.setdefault("api_endpoints", {})["deepinfra"] = "https://api.deepinfra.com/v1/openai"
    config.setdefault("api_keys", {})["deepinfra"] = ""
    config["credential_store"] = {
        "provider": "deepinfra",
        "reference": "DEEPINFRA_API_KEY",
        "backend": "dpapi",
        "path": "data/brain/provider-secrets.dpapi",
        "required_in_production": True,
    }
    config["warmup_local_on_start"] = False
    config.setdefault("brain_policy", {}).update({
        "mode": "QUALITY",
        "prefer_local": False,
        "allow_cloud": True,
        "allow_sensitive_cloud": True,
        "background_allow_cloud": True,
        "max_fallbacks": 0,
    })
    config["local_model"].update({
        "gguf_path": "",
        "runtime_backend": "disabled",
        "server_binary_path": "",
        "server_start_timeout_sec": 1.0,
    })
    config["local_coder_model"]["gguf_path"] = ""
    config.setdefault("launcher", {}).update({
        "backend_command": ["runtime/jarvis-backend.exe"],
        "backend_workdir": "",
        "autostart": True,
        # Startup stays silent.  The first spoken response belongs to the
        # user's first command; an unsolicited greeting used to look like a
        # phantom analysis run in the UI and made the voice path feel broken.
        "greeting_enabled": False,
    })
    config.setdefault("logging", {}).update({
        "console": False,
    })
    config.setdefault("voice", {}).update({
        "tts_enabled": True,
        "tts_always_on": True,
        "provider": "piper",
        "language": "ru",
        "voice": voice_model.stem,
        "fallback": "none",
        # Keep the legacy field for additive compatibility; PiperTTS uses the
        # explicit primary path for the shipped voice.
        "piper_model_path": "data/models/piper/ru_RU-dmitri-medium.onnx",
        "primary_piper_model_path": f"data/models/piper/{voice_model.name}",
        "piper_binary_path": "runtime/piper/piper.exe",
        "stt_enabled": True,
    })
    config.setdefault("stt", {}).update({
        "enabled": True,
        "model": "data/models/stt/faster-whisper-small",
        "language": "ru",
        "hotkey_mode": "toggle_ctrl_space",
    })
    (output / "config").mkdir(parents=True, exist_ok=True)
    (output / "config" / "settings.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "runtime-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "jarvis" / "src-tauri" / "resources" / "jarvis-runtime")
    parser.add_argument("--include-fallback", action="store_true", help="добавить имеющийся 1.7B fallback для слабых машин")
    parser.add_argument("--backend-python", default=None, help="Python с зависимостями PyInstaller/pydantic/websockets")
    parser.add_argument("--backend-exe", default=None, help="готовый windowless jarvis-backend.exe; пропускает PyInstaller")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = stage(
        output=args.output,
        include_fallback=args.include_fallback,
        backend_python=args.backend_python,
        backend_executable=args.backend_exe,
        dry_run=args.dry_run,
    )
    print(json.dumps({
        "output": str(args.output),
        "dry_run": args.dry_run,
        "total_bytes": manifest["total_bytes"],
        "models": [],
        "server": manifest["server"],
        "backend": manifest["backend"],
        "voice": manifest["voice"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
