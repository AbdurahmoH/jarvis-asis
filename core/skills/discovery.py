"""Skill Discovery — finds how to handle intents without matching tools.

Pipeline (per brief):
1. Combinatorial search: can existing tools + browser_bridge compose the goal?
2. External method search: winget, PowerShell, official APIs.
3. Generated skill: delegate to SkillForger.
4. Honest failure: tell user what's specifically missing.

Legal policy is enforced at step 0 before any search begins.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.utils.logger import get_logger
from .legal_policy import LegalVerdict, PolicyResult, evaluate as policy_evaluate

log = get_logger(__name__)


class DiscoveryResult:
    """Result of a skill discovery attempt."""

    def __init__(
        self,
        found: bool,
        method: str,                    # "composition" | "winget" | "powershell" | "forged" | "browser" | "refused" | "failed"
        description: str,
        tool_sequence: Optional[List[Dict[str, Any]]] = None,
        winget_id: Optional[str] = None,
        forged_skill_id: Optional[str] = None,
        policy: Optional[PolicyResult] = None,
        failure_reason: Optional[str] = None,
    ) -> None:
        self.found           = found
        self.method          = method
        self.description     = description
        self.tool_sequence   = tool_sequence or []
        self.winget_id       = winget_id
        self.forged_skill_id = forged_skill_id
        self.policy          = policy
        self.failure_reason  = failure_reason


# ---------------------------------------------------------------------------
# Winget helpers
# ---------------------------------------------------------------------------

_WINGET_KNOWN: Dict[str, str] = {
    "blender":      "Blender.Blender",
    "vlc":          "VideoLAN.VLC",
    "7-zip":        "7zip.7zip",
    "notepad++":    "Notepad++.Notepad++",
    "vscode":       "Microsoft.VisualStudioCode",
    "python":       "Python.Python.3",
    "git":          "Git.Git",
    "obs":          "OBSProject.OBSStudio",
    "discord":      "Discord.Discord",
    "steam":        "Valve.Steam",
    "firefox":      "Mozilla.Firefox",
    "chrome":       "Google.Chrome",
}


def _winget_id_for(app_name: str) -> Optional[str]:
    """Return winget package ID for a known app, or search winget."""
    key = app_name.lower().strip()
    if key in _WINGET_KNOWN:
        return _WINGET_KNOWN[key]
    # Try winget search (best-effort, may not be available)
    try:
        result = subprocess.run(
            ["winget", "search", "--name", app_name, "--accept-source-agreements"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.splitlines()
        for line in lines[2:]:  # skip header
            parts = line.split()
            if len(parts) >= 2 and app_name.lower() in line.lower():
                return parts[1]  # ID column
    except Exception:
        pass
    return None


def _winget_available() -> bool:
    try:
        r = subprocess.run(["winget", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Composition helpers
# ---------------------------------------------------------------------------

_TWITTER_COMPOSE_SEQUENCE = [
    {"tool": "browser_bridge", "action": "navigate", "args": {"url": "https://twitter.com/compose/tweet"}},
    {"tool": "computer_keyboard", "action": "type", "args": {"text": "{tweet_text}"}},
    {"tool": "browser_bridge", "action": "click", "args": {"selector": "[data-testid='tweetButtonInline']"}},
]

_COMPOSITION_PATTERNS: List[Tuple[re.Pattern, List[Dict[str, Any]], str]] = [
    (
        re.compile(r"(?i)(опубликуй|запости|напиши|post|tweet).*(twitter|твиттер)", re.UNICODE),
        _TWITTER_COMPOSE_SEQUENCE,
        "browser_bridge + computer_keyboard composition for Twitter post",
    ),
]


def _try_composition(goal: str) -> Optional[DiscoveryResult]:
    for pattern, sequence, desc in _COMPOSITION_PATTERNS:
        if pattern.search(goal):
            return DiscoveryResult(
                found=True,
                method="composition",
                description=desc,
                tool_sequence=sequence,
            )
    return None


# ---------------------------------------------------------------------------
# SkillDiscovery
# ---------------------------------------------------------------------------

class SkillDiscovery:
    """Finds how to handle an intent that has no matching tool.

    Args:
        forger:     SkillForger instance (optional; used for step 3).
        llm_fn:     LLM callable for composition proposals (optional).
        open_browser_fn: callable(url) to open a URL in the browser.
    """

    def __init__(
        self,
        forger: Optional[Any] = None,
        llm_fn: Optional[Callable[[str, str], str]] = None,
        open_browser_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._forger         = forger
        self._llm            = llm_fn
        self._open_browser   = open_browser_fn

    def discover(
        self,
        goal: str,
        intent: str,
        profile_id: str = "default",
        context: Optional[Dict[str, Any]] = None,
    ) -> DiscoveryResult:
        """Run the full discovery pipeline for an unmatched intent."""

        # 0. Legal policy check
        policy = policy_evaluate(goal, intent)
        if policy.verdict == LegalVerdict.REFUSED:
            return DiscoveryResult(
                found=False,
                method="refused",
                description=policy.reason,
                policy=policy,
                failure_reason=policy.refusal_text,
            )
        if policy.verdict == LegalVerdict.OPEN_BROWSER:
            search_url = f"https://www.google.com/search?q={goal.replace(' ', '+')}"
            if self._open_browser:
                self._open_browser(search_url)
            return DiscoveryResult(
                found=True,
                method="browser",
                description="opened browser search for user to click",
                policy=policy,
            )

        # 1. Combinatorial composition
        comp = _try_composition(goal)
        if comp:
            return comp

        # 2. Winget / external method
        winget_result = self._try_winget(goal, intent)
        if winget_result:
            return winget_result

        # 3. Generated skill
        if self._forger is not None:
            try:
                skill_id = self._forger.forge(goal, intent, profile_id=profile_id)
                if skill_id:
                    return DiscoveryResult(
                        found=True,
                        method="forged",
                        description=f"generated skill {skill_id}",
                        forged_skill_id=skill_id,
                    )
            except Exception as exc:
                log.warning("SkillDiscovery: forger failed: %s", exc)

        # 4. Honest failure
        reason = self._diagnose_failure(goal, intent)
        return DiscoveryResult(
            found=False,
            method="failed",
            description="no method found",
            failure_reason=reason,
        )

    def _try_winget(self, goal: str, intent: str) -> Optional[DiscoveryResult]:
        """Check if goal is an install request satisfiable via winget."""
        install_re = re.compile(
            r"(?i)(установи|поставь|install)\s+(.+?)(?:\s+и\s+|\s+and\s+|$)", re.UNICODE
        )
        m = install_re.search(goal)
        if not m:
            return None

        app_name = m.group(2).strip().split()[0]

        if not _winget_available():
            # Winget not available — open official download page
            search_url = f"https://www.google.com/search?q={app_name}+official+download"
            if self._open_browser:
                self._open_browser(search_url)
            return DiscoveryResult(
                found=True,
                method="browser",
                description=f"winget not available; opened download page for {app_name}",
                failure_reason=(
                    f"winget не установлен. Открыл страницу загрузки {app_name} — "
                    "нажми Download там."
                ),
            )

        pkg_id = _winget_id_for(app_name)
        if pkg_id:
            return DiscoveryResult(
                found=True,
                method="winget",
                description=f"install {app_name} via winget ({pkg_id})",
                winget_id=pkg_id,
            )

        # App not in winget — open official site
        search_url = f"https://www.google.com/search?q={app_name}+official+download+site"
        if self._open_browser:
            self._open_browser(search_url)
        return DiscoveryResult(
            found=True,
            method="browser",
            description=f"{app_name} not found in winget; opened official download page",
        )

    @staticmethod
    def _diagnose_failure(goal: str, intent: str) -> str:
        """Produce a specific, honest failure message."""
        if re.search(r"(?i)(установи|install)", goal):
            return (
                "Для установки нужен winget (не найден) или официальный сайт. "
                "Скажи, и я открою страницу загрузки."
            )
        if re.search(r"(?i)(войди|авторизуйся|login|sign in)", goal):
            return "Для этого нужен вход в аккаунт — я не храню пароли. Открою страницу входа?"
        if re.search(r"(?i)(купи|оплати|pay|purchase)", goal):
            return "Платёжные операции я не выполняю автоматически. Открою страницу оплаты?"
        return (
            f"Не нашёл способа выполнить «{intent[:80]}». "
            "Уточни, что именно нужно сделать, или я открою поиск."
        )
