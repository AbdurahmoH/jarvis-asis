"""Regression S7 — Shadow sandbox: denylist не обходится алиасом.

Аудит 2026-09-05: ``safety_check`` проверял ``_FORBIDDEN_NAMES`` только когда
``ast.Call.func`` — прямое ``ast.Name``. Отсюда обходы:

* ``f = open`` и затем ``f("x", "w")`` — алиас;
* ``g = getattr`` и затем ``g(__builtins__, "open")`` — алиас рефлексии;
* ``open = ...`` — тень встроенного;
* ``a, f = 1, open`` — распаковка;
* ``__builtins__.open(...)`` — доступ мимо импорта.

Теперь запрещённое имя не может быть *ссылаемо* нигде (Load или Store),
``__builtins__``/``builtins`` запрещены как корень любой цепочки, а вызов
``x.open(...)`` через атрибут произвольного объекта блокируется так же, как
прямой. Живая проверка подтверждает, что оценочный subprocess по-прежнему
работает во временной папке без ``JARVIS_HOME`` и прочих переменных окружения
хоста.
"""
from __future__ import annotations

from core.shadow.sandbox import CodeEvaluator, SandboxTester


def _blocked(source: str) -> str:
    report = SandboxTester().safety_check(source)
    assert not report.passed, f"ожидался отказ, пропущено: {source!r}"
    return report.detail


def _allowed(source: str) -> None:
    report = SandboxTester().safety_check(source)
    assert report.passed, f"легитимный код заблокирован: {report.detail}"


# --------------------------------------------------------------------------- #
# Прямые случаи наряда S7
# --------------------------------------------------------------------------- #


def test_alias_of_open_is_blocked() -> None:
    detail = _blocked(
        "def execute_task(params):\n"
        "    f = open\n"
        "    return f('x', 'w')\n"
    )
    assert "open" in detail


def test_import_builtins_and_attribute_access_is_blocked() -> None:
    # import builtins отсекается allow-list'ом импортов...
    assert "allow-listed" in _blocked(
        "import builtins\ndef execute_task(params):\n    return builtins.open('x')\n"
    )
    # ...а ссылка на __builtins__ без импорта ловится правилом корня цепочки
    # или запрещённым атрибутом (что из двух — зависит от порядка обхода AST).
    detail = _blocked(
        "def execute_task(params):\n    return __builtins__.open('x', 'w')\n"
    )
    assert "open" in detail or "builtins" in detail


def test_getattr_reflection_is_blocked() -> None:
    assert "getattr" in _blocked(
        "def execute_task(params):\n"
        "    return getattr(__builtins__, 'open')\n"
    )
    # Алиас рефлексии: тот же обход через промежуточную переменную.
    assert "getattr" in _blocked(
        "def execute_task(params):\n"
        "    g = getattr\n"
        "    return g(__builtins__, 'open')\n"
    )


# --------------------------------------------------------------------------- #
# Остальные пути алиасинга и теней
# --------------------------------------------------------------------------- #


def test_shadowing_a_forbidden_name_is_blocked() -> None:
    assert "shadowing" in _blocked(
        "def execute_task(params):\n"
        "    open = __import__\n"
        "    return open('os').environ\n"
    )


def test_tuple_unpacking_alias_is_blocked() -> None:
    assert "open" in _blocked(
        "def execute_task(params):\n"
        "    a, f = 1, open\n"
        "    return f('x', 'w')\n"
    )


def test_walrus_alias_is_blocked() -> None:
    assert "open" in _blocked(
        "def execute_task(params):\n"
        "    return (f := open)('x', 'w')\n"
    )


def test_forbidden_name_as_call_argument_is_blocked() -> None:
    """functools.partial(open) — тоже ссылка на запрещённое имя."""
    assert "open" in _blocked(
        "import functools\n"
        "def execute_task(params):\n"
        "    return functools.partial(open, 'x', 'w')\n"
    )


def test_forbidden_name_via_attribute_of_any_object_is_blocked() -> None:
    assert "attribute" in _blocked(
        "def execute_task(params):\n"
        "    return params.open('x', 'w')\n"
    )


def test_subscripted_alias_is_blocked() -> None:
    assert "open" in _blocked(
        "def execute_task(params):\n"
        "    table = {'f': open}\n"
        "    return table['f']('x', 'w')\n"
    )


# --------------------------------------------------------------------------- #
# Регрессионный контроль: легитимный путь не заблокирован
# --------------------------------------------------------------------------- #


def test_legitimate_generated_tool_still_passes() -> None:
    _allowed(
        "import json\n"
        "import re\n"
        "def execute_task(params):\n"
        "    text = str(params.get('value', ''))\n"
        "    words = re.findall(r'\\w+', text)\n"
        "    return {'words': len(words), 'preview': json.dumps(words[:3])}\n"
    )
    report = SandboxTester().test_source(
        "import json\n"
        "def execute_task(params):\n"
        "    return 'ok'\n",
        {},
    )
    assert report.security_decision.value == "SAFE_TO_EVALUATE"


def test_direct_forbidden_call_is_still_blocked() -> None:
    assert "forbidden call" in _blocked(
        "def execute_task(params):\n    return open('outside.txt', 'w')\n"
    )


# --------------------------------------------------------------------------- #
# Живая проверка изоляции оценочного subprocess
# --------------------------------------------------------------------------- #


def test_evaluator_runs_cwd_isolated_without_host_environment() -> None:
    """Subprocess оценки: cwd — временная папка, JARVIS_HOME вычищен."""
    source = (
        "import os\n"
        "def run(params: dict) -> dict:\n"
        "    info = {'jarvis_home_set': 'JARVIS_HOME' in os.environ,\n"
        "            'cwd_is_temp': 'atlas-code-eval' in os.getcwd()}\n"
        "    return {'success': True, 'result': info, 'error': None}\n"
    )
    result = CodeEvaluator(timeout_sec=10.0).run_source(source, {})

    assert result.passed, f"оценка не выполнилась: {result.detail}"
    payload = result.value["result"]
    assert payload["jarvis_home_set"] is False
    assert payload["cwd_is_temp"] is True
