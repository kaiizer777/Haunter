"""Failure-signature clustering for CI runs.

Normalizes ``runs.failure_reason`` into a stable grouping key by stripping
volatile tokens (commit SHAs, file paths, timestamps, numeric IDs) so that
repeated failures with the same root cause collapse to one signature.

Pure stdlib (``re``) — no new dependencies. Deterministic and safe to call
in the request path.
"""

from __future__ import annotations

import re

MAX_SIGNATURE_CHARS = 200

_SHA40_RE = re.compile(r"\b[0-9a-f]{40}\b")
_SHA_SHORT_RE = re.compile(r"\b[0-9a-f]{7,39}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\b")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*")
_UNIX_ABS_PATH_RE = re.compile(r"(?<![\w.])/(?:[\w.\-]+/)+[\w.\-]+")
_FILE_LINE_RE = re.compile(r"(?<![\w/])([\w.\-]+/)*[\w.\-]+\.\w+:\d+(?::\d+)?")
_NUMBER_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")


def normalize_failure_signature(reason: str | None) -> str:
    """Normalize a raw failure_reason into a stable clustering signature.

    Strips SHAs, UUIDs, absolute/relative file paths, timestamps, IPs, and
    bare numbers, then lowercases, collapses whitespace, and truncates to
    ``MAX_SIGNATURE_CHARS``. Returns ``"unknown"`` for empty input.
    """
    if not reason or not reason.strip():
        return "unknown"
    # Stable grouping key: first non-empty line carries the error type/message.
    first_line = ""
    for line in reason.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if not first_line:
        return "unknown"
    text = first_line.lower()
    text = _UUID_RE.sub("<id>", text)
    text = _SHA40_RE.sub("<sha>", text)
    text = _SHA_SHORT_RE.sub("<sha>", text)
    text = _ISO_TS_RE.sub("<ts>", text)
    text = _DATE_RE.sub("<ts>", text)
    text = _TIME_RE.sub("<ts>", text)
    text = _IPV4_RE.sub("<ip>", text)
    text = _WIN_PATH_RE.sub("<path>", text)
    text = _UNIX_ABS_PATH_RE.sub("<path>", text)
    text = _FILE_LINE_RE.sub("<path>", text)
    text = _NUMBER_RE.sub("<n>", text)
    text = _WS_RE.sub(" ", text).strip(" -–—:;,.|")
    if not text:
        return "unknown"
    if len(text) > MAX_SIGNATURE_CHARS:
        text = text[:MAX_SIGNATURE_CHARS].rstrip()
    return text
