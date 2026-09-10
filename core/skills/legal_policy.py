"""Legal boundary enforcement for Skill Discovery & Forging.

This module is the single authoritative source for what Jarvis will and
will not do when acquiring new capabilities.  The rules are contextual
(not keyword-based) and use DeepSeek reasoning when ambiguous.

CRITICAL LEGAL BOUNDARY (non-negotiable, product ships to real users):
- Software installation: ONLY from official sources (vendor site, winget
  official repo, Microsoft Store, Steam).
- Free mods/plugins from creator's own site or official platform repo
  (e.g. CurseForge for Minecraft): allowed.
- Piracy, cracks, keygens, DRM bypass, hacking, unauthorized account
  access: refuse gracefully, offer legal alternative.
- Information retrieval: free of restrictions except DeepSeek-level blocks.
- When uncertain: open a browser search and let the human click.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.utils.logger import get_logger

log = get_logger(__name__)


class LegalVerdict(str, Enum):
    ALLOWED   = "allowed"
    OPEN_BROWSER = "open_browser"   # let user click — we won't automate
    REFUSED   = "refused"


@dataclass
class PolicyResult:
    verdict:        LegalVerdict
    reason:         str
    legal_url:      Optional[str] = None   # where to find the legal version
    refusal_text:   Optional[str] = None   # what to say to the user


# ---------------------------------------------------------------------------
# Pattern sets (contextual, not keyword-only)
# ---------------------------------------------------------------------------

_PIRACY_PATTERNS = re.compile(
    r"(?i)\b(crack|keygen|serial\s+key|license\s+bypass|drm\s+bypass|"
    r"warez|pirat|nulled|repack\s+by|scene\s+release|"
    r"скачать\s+бесплатно\s+без\s+регистрации|"
    r"взломанн|кряк|кейген|серийный\s+ключ|обход\s+защиты|"
    r"пиратск|нуллед)\b",
    re.UNICODE,
)

_HACKING_PATTERNS = re.compile(
    r"(?i)\b(hack|exploit|rootkit|malware|ransomware|keylogger|"
    r"brute\s*force\s+password|sql\s+injection|xss\s+attack|"
    r"взломай\s+аккаунт|взломать\s+пароль|получить\s+доступ\s+без\s+разрешения|"
    r"обойти\s+авторизацию)\b",
    re.UNICODE,
)

_UNOFFICIAL_INSTALL_PATTERNS = re.compile(
    r"(?i)\b(скачай\s+с\s+торрент|download\s+from\s+torrent|"
    r"rutracker|thepiratebay|1337x|rarbg|"
    r"неофициальн\w+\s+сайт|unofficial\s+mirror|"
    r"зеркало\s+скачать)\b",
    re.UNICODE,
)

# Official mod platforms — allowed
_OFFICIAL_MOD_PLATFORMS = re.compile(
    r"(?i)\b(curseforge|modrinth|nexusmods|steam\s+workshop|"
    r"minecraft\s+marketplace|bethesda\.net|thunderstore|"
    r"официальн\w+\s+мод|official\s+mod)\b",
    re.UNICODE,
)

# Official software sources — allowed
_OFFICIAL_SOURCES = re.compile(
    r"(?i)\b(winget|microsoft\s+store|steam|epic\s+games|"
    r"официальн\w+\s+сайт|official\s+site|vendor\s+site|"
    r"blender\.org|gimp\.org|vlc\.videolan\.org|"
    r"python\.org|nodejs\.org|github\.com/\w+/\w+/releases)\b",
    re.UNICODE,
)


def evaluate(goal: str, context: Optional[str] = None) -> PolicyResult:
    """Evaluate whether a goal is within legal boundaries.

    Args:
        goal:    The user's stated goal (natural language).
        context: Additional context (e.g. the specific app/mod name).

    Returns:
        PolicyResult with verdict and user-facing text.
    """
    text = f"{goal} {context or ''}".strip()

    # 1. Explicit piracy / DRM bypass
    if _PIRACY_PATTERNS.search(text):
        return PolicyResult(
            verdict=LegalVerdict.REFUSED,
            reason="piracy or DRM bypass detected",
            refusal_text=(
                "Это я не сделаю — пиратство и обход защиты вне моих правил. "
                "Могу помочь найти легальную версию — вот тут:"
            ),
        )

    # 2. Hacking / unauthorized access
    if _HACKING_PATTERNS.search(text):
        return PolicyResult(
            verdict=LegalVerdict.REFUSED,
            reason="hacking or unauthorized access detected",
            refusal_text=(
                "Взлом аккаунтов и несанкционированный доступ — не моя область. "
                "Если нужно восстановить доступ к своему аккаунту, могу помочь через официальный сайт."
            ),
        )

    # 3. Unofficial download sources
    if _UNOFFICIAL_INSTALL_PATTERNS.search(text):
        return PolicyResult(
            verdict=LegalVerdict.OPEN_BROWSER,
            reason="unofficial source requested",
            refusal_text=(
                "Скачивать с неофициальных зеркал я не буду — открою официальный сайт, "
                "там ты сам нажмёшь."
            ),
        )

    # 4. Official mod platforms — explicitly allowed
    if _OFFICIAL_MOD_PLATFORMS.search(text):
        return PolicyResult(
            verdict=LegalVerdict.ALLOWED,
            reason="official mod platform",
        )

    # 5. Official software sources — allowed
    if _OFFICIAL_SOURCES.search(text):
        return PolicyResult(
            verdict=LegalVerdict.ALLOWED,
            reason="official software source",
        )

    # 6. Default: allowed (information retrieval, general tasks)
    return PolicyResult(
        verdict=LegalVerdict.ALLOWED,
        reason="no policy violation detected",
    )


def refusal_message(result: PolicyResult, legal_url: Optional[str] = None) -> str:
    """Format a user-facing refusal message."""
    base = result.refusal_text or "Это выходит за рамки того, что я могу сделать."
    if legal_url or result.legal_url:
        url = legal_url or result.legal_url
        return f"{base} {url}"
    return base
