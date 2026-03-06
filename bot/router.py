"""Model router — selects effort level based on message complexity.

With adaptive thinking, the SDK decides when/how much to think.
The router only controls the effort level: low, medium, high, max.
"""

import logging
import re
from dataclasses import dataclass

from config import EFFORT_SIMPLE, EFFORT_MEDIUM, EFFORT_COMPLEX, EFFORT_MAX

log = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    effort: str  # "low", "medium", "high", "max"


# Keywords that suggest code/upgrade tasks → max effort
_CODE_KEYWORDS = (
    "upgrade", "deploy", "edit code", "change code", "modify code",
    "add feature", "fix bug", "self-upgrade", "/health", "endpoint",
    "source code", "bot code", "implement", "refactor",
)

# Complex task patterns → high effort
_COMPLEX_PATTERNS = [
    r"(?:plan|design|architect|implement|build|create|develop)\b.*\b(?:system|app|feature|project)",
    r"(?:analyze|compare|evaluate|review|assess)\b",
    r"(?:research|investigate|find out|look into)\b",
    r"(?:write|draft|compose)\b.*\b(?:email|letter|message|report|plan)",
    r"(?:why|how does|explain|what if|should we)\b.*\b(?:because|since|therefore|considering)",
    r"(?:\d+\)|[\d]+\.)\s*\w+",
    r"(?:pros and cons|trade-?offs?|advantages|disadvantages|best option)\b",
    r"(?:itinerary|booking|reservation|flight|hotel|visa)\b",
    r"(?:application|register|deadline|admissions)\b",
]

# Simple patterns → low effort
_SIMPLE_PATTERNS = [
    r"^(?:yes|no|ok|sure|thanks|thank you)\b",
    r"^(?:send it|go ahead|confirm|cancel)\b",
    r"^(?:what time|when|where|who|how much)\b.{0,50}$",
    r"^(?:remind)\b.{0,80}$",
]

_complex_re = [re.compile(p, re.IGNORECASE) for p in _COMPLEX_PATTERNS]
_simple_re = [re.compile(p, re.IGNORECASE) for p in _SIMPLE_PATTERNS]


def route_message(message_text: str, message_count: int = 1) -> RoutingDecision:
    """Select effort level based on message complexity."""
    text = message_text.strip()

    # Code/upgrade → max effort
    if any(kw in text.lower() for kw in _CODE_KEYWORDS):
        log.info(f"Router → max (code keyword: '{text[:50]}')")
        return RoutingDecision(effort=EFFORT_MAX)

    # Short/simple → low effort
    if len(text) < 30 or any(r.search(text) for r in _simple_re):
        log.info(f"Router → low ('{text[:50]}')")
        return RoutingDecision(effort=EFFORT_SIMPLE)

    # Large batch → high effort
    if message_count > 3:
        log.info(f"Router → high (batch of {message_count} messages)")
        return RoutingDecision(effort=EFFORT_COMPLEX)

    # Long + complex patterns → high effort
    if len(text) > 200 and any(r.search(text) for r in _complex_re):
        log.info(f"Router → high (long + pattern: '{text[:50]}')")
        return RoutingDecision(effort=EFFORT_COMPLEX)

    # Multiple complex pattern matches → high effort
    complex_matches = sum(1 for r in _complex_re if r.search(text))
    if complex_matches >= 2:
        log.info(f"Router → high ({complex_matches} patterns: '{text[:50]}')")
        return RoutingDecision(effort=EFFORT_COMPLEX)

    # Default → medium effort
    log.info(f"Router → medium ('{text[:50]}')")
    return RoutingDecision(effort=EFFORT_MEDIUM)
