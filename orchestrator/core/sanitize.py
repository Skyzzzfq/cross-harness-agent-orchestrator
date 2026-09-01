"""Credential and sensitive-data redaction for persisted failure text (P1-05).

SDK exception messages, provider raw responses, URLs and logs can embed API
keys, session tokens, cookies or query-string credentials. Everything that is
persisted (``backend_calls.failure_json``, ``result_json``, audit events) must
be redacted before storage so the orchestration database never becomes a
credential leak.
"""

from __future__ import annotations

import re

# 常见 token 形态（OpenAI/Anthropic/CodeBuddy/通用）
_API_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"\b(oauth2:[A-Za-z0-9._-]{8,})\b"),
    re.compile(r"\b(bearer [A-Za-z0-9._~+/-]{8,}=*)\b", re.IGNORECASE),
    re.compile(r"\b(session[-_]?token[=:\s]+[A-Za-z0-9._~+/-]{8,})\b", re.IGNORECASE),
    re.compile(r"\b(cookie[=:][^;\s]{8,})\b", re.IGNORECASE),
)

# 敏感键值对（key=value / key: value）
_SENSITIVE_KEYS = (
    "api[_-]?key",
    "apikey",
    "access[_-]?token",
    "auth[_-]?token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "client[_-]?secret",
    "authorization",
    "set-cookie",
    "session[_-]?id",
    "signature",
    "private[_-]?key",
)
_KEY_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(
        rf"(\b{key}\b\s*[=:]\s*)([^\s&,;'\"]+)(?=\s|$|[&,;])",
        re.IGNORECASE,
    )
    for key in _SENSITIVE_KEYS
)

# URL query string 中的敏感参数
_URL_QUERY_PATTERN = re.compile(
    r"(\?[^\"'\s]*)([?&](?:api_key|apikey|key|token|secret|sig|signature|"
    r"access_token|auth)[^=]*=)[^&\"'\s]*",
    re.IGNORECASE,
)


def redact_sensitive(text: str | None) -> str:
    """Return ``text`` with credentials and sensitive key=value pairs redacted."""
    if not text:
        return text or ""
    redacted = text
    for pattern in _API_KEY_PATTERNS:
        redacted = pattern.sub(_mask, redacted)
    for pattern in _KEY_VALUE_PATTERNS:
        redacted = pattern.sub(_mask_kv, redacted)
    redacted = _URL_QUERY_PATTERN.sub(_mask_query, redacted)
    return redacted


def _mask(match: re.Match[str]) -> str:
    return match.group(1)[:3] + "***" if match.group(1) else "***"


def _mask_kv(match: re.Match[str]) -> str:
    key_part = match.group(1)
    value = match.group(2)
    if len(value) <= 3:
        return f"{key_part}***"
    return f"{key_part}{value[:2]}***"


def _mask_query(match: re.Match[str]) -> str:
    prefix = match.group(1)
    param = match.group(2)
    return f"{prefix}{param}***"
