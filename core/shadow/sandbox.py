"""Static quality checks plus disposable evaluation for generated tools.

Generated code is allow-listed, evaluated in a temporary subprocess with a
reduced environment and killed as a process tree on timeout. Quality never
overrides the explicit security decision.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class SecurityDecision(str, Enum):
    SAFE_TO_EVALUATE = "SAFE_TO_EVALUATE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    BLOCKED = "BLOCKED"


_SAFE_IMPORTS = {
    "json", "re", "math", "datetime", "statistics", "collections", "itertools",
    "functools", "decimal", "typing", "string", "csv", "base64", "hashlib",
}
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "__build_class__",
}
# S7: корни цепочек атрибутов, через которые достают встроенные имена.
# ``import builtins`` отсекается allow-list'ом импортов, поэтому живой путь —
# ссылка на ``__builtins__`` внутри исполняемого кода.
_BUILTINS_ROOTS = {"builtins", "__builtins__", "__builtin__"}
_FORBIDDEN_TEXT = (
    "os.system", "subprocess", "multiprocessing", "threading", "_winapi", "ctypes",
    "requests", "socket", "urllib", "http.client", "pathlib", "shutil", "webbrowser",
    ".unlink(", ".rmdir(", ".remove(", "c:\\windows", "/etc/", "rm -rf", "del /",
    "powershell", "cmd.exe", "createprocess", "fork", "spawn",
)


def _alias_target_names(node: ast.AST):
    """Все имена-цели присваивания, включая распаковку кортежей и starred."""
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for item in node.elts:
            yield from _alias_target_names(item)
    elif isinstance(node, ast.Starred):
        yield from _alias_target_names(node.value)


def _safety_violation(node: ast.AST) -> str | None:
    """S7: причина отказа для узла AST; None — узел чист.

    Запрещённое имя не должно быть ссылаемо нигде. Проверка только
    ``ast.Call.func`` пропускала алиасинг: ``f = open``, затем ``f("x", "w")``;
    ``g = getattr``; тень ``open = ...``; ``functools.partial(open)``;
    распаковку ``a, f = 1, open``. Все эти пути — ссылки на одно и то же имя,
    и все они запрещены одинаково.
    """
    if isinstance(node, ast.Name):
        if node.id in _FORBIDDEN_NAMES:
            kind = "shadowing" if isinstance(node.ctx, (ast.Store, ast.Del)) else "reference"
            return f"forbidden name {kind}: {node.id}"
        if node.id in _BUILTINS_ROOTS:
            return f"forbidden builtins access: {node.id}"
        return None
    if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
        # ``x.open(...)`` — то же имя через атрибут произвольного объекта.
        return f"forbidden attribute access: {node.attr}"
    return None


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    detail: str = ""
    value: Any = None


@dataclass(frozen=True)
class SandboxReport:
    syntax: CheckResult
    safety: CheckResult
    functional: CheckResult
    style: CheckResult
    quality_score: int
    security_decision: SecurityDecision | None = None
    execution: CheckResult | None = None

    def __post_init__(self) -> None:
        # Compatibility reports created by older tester plug-ins are inferred
        # only from their explicit checks; a report from the real tester always
        # supplies an explicit decision.
        if self.security_decision is None:
            decision = SecurityDecision.SAFE_TO_EVALUATE if self.syntax.passed and self.safety.passed and self.functional.passed else SecurityDecision.REQUIRES_REVIEW
            object.__setattr__(self, "security_decision", decision)

    @property
    def confidence(self) -> int:
        """Compatibility alias; this is quality, never permission."""
        return self.quality_score

    @property
    def registration_allowed(self) -> bool:
        return self.security_decision is SecurityDecision.SAFE_TO_EVALUATE and self.quality_score >= 90


def _kill_process_tree(process: subprocess.Popen[Any], grace: float = 0.2) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=max(0.05, grace))
    except (subprocess.TimeoutExpired, OSError):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            try:
                os.killpg(process.pid, 9)
            except (AttributeError, OSError):
                process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


class CodeEvaluator:
    """Disposable evaluator for already allow-listed generated Python."""

    def __init__(self, *, timeout_sec: float = 3.0, output_limit: int = 64 * 1024) -> None:
        self.timeout_sec = max(0.1, float(timeout_sec))
        self.output_limit = max(1024, int(output_limit))

    def run_source(self, source: str, test_params: dict[str, Any]) -> CheckResult:
        body = source
        if "def run(" not in body:
            body += "\n\ndef run(params: dict) -> dict:\n    try:\n        return {'success': True, 'result': str(execute_task(params)), 'error': None}\n    except Exception as exc:\n        return {'success': False, 'result': None, 'error': str(exc)}\n"
        harness = body + "\nimport json\nprint(json.dumps(run(" + repr(dict(test_params)) + "), ensure_ascii=False))\n"
        with tempfile.TemporaryDirectory(prefix="atlas-code-eval-") as raw_dir:
            path = Path(raw_dir) / "tool.py"
            path.write_text(harness, encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            kwargs: dict[str, Any] = {"cwd": raw_dir, "env": env, "stdout": subprocess.PIPE,
                                      "stderr": subprocess.PIPE,
                                      "text": True, "creationflags": creationflags}
            if os.name != "nt":
                kwargs["start_new_session"] = True
            process = subprocess.Popen([sys.executable, "-I", str(path)], **kwargs)
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_sec)
            except subprocess.TimeoutExpired:
                terminated = _kill_process_tree(process)
                return CheckResult(False, f"functional timeout; terminated={terminated}")
            if len(stdout.encode("utf-8", "replace")) > self.output_limit:
                return CheckResult(False, "functional output exceeded limit")
            if process.returncode != 0:
                return CheckResult(False, f"functional exit {process.returncode}: {stderr[-300:]}")
            try:
                result = json.loads(stdout.strip().splitlines()[-1])
            except (IndexError, ValueError) as exc:
                return CheckResult(False, f"functional JSON missing: {exc}")
            expected = {"success", "result", "error"}
            if not isinstance(result, dict) or set(result) != expected or not isinstance(result["success"], bool):
                return CheckResult(False, "run() did not return success/result/error contract")
        return CheckResult(True, value=result)


class SandboxTester:
    """Compatibility facade around static checks and :class:`CodeEvaluator`."""

    def __init__(self, *, evaluator: CodeEvaluator | None = None) -> None:
        self.evaluator = evaluator or CodeEvaluator()

    def syntax_check(self, source: str) -> CheckResult:
        try:
            compile(source, "<shadow-tool>", "exec")
        except (SyntaxError, TypeError, ValueError) as exc:
            return CheckResult(False, f"syntax: {exc}")
        return CheckResult(True)

    def safety_check(self, source: str) -> CheckResult:
        lowered = (source or "").lower()
        found = next((item for item in _FORBIDDEN_TEXT if item in lowered), None)
        if found:
            return CheckResult(False, f"forbidden construct: {found}")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return CheckResult(False, f"cannot inspect syntax: {exc}")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "", asname=None)]
                for alias in names:
                    root = alias.name.split(".")[0]
                    if root not in _SAFE_IMPORTS:
                        return CheckResult(False, f"import is not allow-listed: {root}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_NAMES:
                return CheckResult(False, f"forbidden call: {node.func.id}")
            elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                return CheckResult(False, f"forbidden dunder attribute: {node.attr}")
            else:
                reason = _safety_violation(node)
                if reason:
                    return CheckResult(False, reason)
        return CheckResult(True)

    def functional_check(self, source: str, test_params: dict[str, Any]) -> CheckResult:
        return self.evaluator.run_source(source, test_params)

    @staticmethod
    def style_check(source: str) -> CheckResult:
        required = ("def run(params: dict)", "def execute_task(params)", "Generated by: Shadow Engine")
        missing = [item for item in required if item not in source]
        return CheckResult(not missing, "missing template pieces: " + ", ".join(missing))

    def test_source(self, source: str, test_params: dict[str, Any]) -> SandboxReport:
        syntax = self.syntax_check(source)
        if not syntax.passed:
            failed = CheckResult(False, "not run after syntax failure")
            return SandboxReport(syntax, failed, failed, failed, 0, SecurityDecision.BLOCKED)
        safety = self.safety_check(source)
        if not safety.passed:
            failed = CheckResult(False, "not run after safety failure")
            return SandboxReport(syntax, safety, failed, failed, 0, SecurityDecision.BLOCKED)
        functional = self.functional_check(source, test_params)
        style = self.style_check(source)
        quality = 20 + 30 + (40 if functional.passed else 0) + (10 if style.passed else 0)
        decision = SecurityDecision.SAFE_TO_EVALUATE if functional.passed else SecurityDecision.REQUIRES_REVIEW
        return SandboxReport(syntax, safety, functional, style, quality, decision, functional)


__all__ = ["CheckResult", "CodeEvaluator", "SandboxReport", "SandboxTester", "SecurityDecision"]
