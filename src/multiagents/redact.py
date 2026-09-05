"""Credential scrubbing.

Every byte this system writes to disk or returns through an MCP tool passes
through :func:`scrub`. That placement is the whole point: redaction lives in the
log writer and the result serialiser, not at the call sites, so a new code path
cannot forget to apply it.

Two mechanisms:

* **Shape matching** — regexes for token formats we can recognise on sight
  (OAuth bearers, JWTs, ``sk-``/``ghp_``/``AIza`` keys, long high-entropy hex).
* **Registered literals** — exact strings we *know* are secret because we read
  them ourselves (an env value we deliberately withheld from a child, a token
  from opencode's ``account`` table). Registered once at startup, masked
  verbatim wherever they later appear, however they were mangled into a string.

Keys named like secrets are dropped wholesale rather than pattern-matched, since
a short or unusual token would slip past every regex.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[redacted]"

# Dict keys whose values never survive, whatever they contain.
_SECRET_KEYS = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|apikey"
    r"|auth[_-]?token|authorization|password|passwd|secret|client[_-]?secret"
    r"|credential|cookie|session[_-]?token|private[_-]?key)"
)

# Token shapes recognisable without context.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    # key=value / "key": "value" where the key looks secret
    re.compile(
        r"(?i)\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|auth[_-]?token"
        r"|secret|password)\b\s*[:=]\s*[\"']?([A-Za-z0-9._~+/=-]{12,})[\"']?"
    ),
]

_literals: set[str] = set()


def register_literal(value: str | None, *, min_length: int = 8) -> None:
    """Mark an exact string as secret so it is masked wherever it appears.

    Short values are ignored: masking a 4-character string would corrupt
    unrelated output far more often than it would protect anything.
    """
    if value and len(value) >= min_length:
        _literals.add(value)


def register_environment(names: list[str]) -> None:
    """Register the *values* of named env vars as secret literals.

    Called for every variable we withhold from children, so that if one leaks
    into output by another route it is still masked on the way to disk.
    """
    import os

    for name in names:
        register_literal(os.environ.get(name))


def _scrub_text(text: str) -> str:
    for literal in _literals:
        if literal in text:
            text = text.replace(literal, MASK)
    for pattern in _PATTERNS:
        if pattern.groups:
            # Replace only the captured secret, keeping the key for readability.
            text = pattern.sub(lambda m: m.group(0).replace(m.group(1), MASK), text)
        else:
            text = pattern.sub(MASK, text)
    return text


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a JSON-shaped value. Structure is preserved."""
    if _depth > 24:
        return value
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEYS.search(key):
                out[key] = MASK
            else:
                out[key] = scrub(item, _depth + 1)
        return out
    if isinstance(value, list):
        return [scrub(item, _depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub(item, _depth + 1) for item in value)
    return value
