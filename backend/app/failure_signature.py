"""Failure-signature clustering for CI runs.

Normalizes ``runs.failure_reason`` into a stable grouping key by replacing
volatile tokens (UUIDs, timestamps, SHAs, paths, IPs, bare numbers) with
placeholders, so repeated failures with the same root cause collapse onto one
signature.

Normalization is a pure function of its input: same input always yields the
same signature, and ``normalize(normalize(x)) == normalize(x)``.

The rules are ordered most-anchored first so no rule can ever consume a
substring that a later, broader rule needed:

===  ======================  =========================================================
 #   Pattern                 Placeholder / why it is placed here
===  ======================  =========================================================
 1   UUID                    ``<id>``  dashed, so the hex run rule below would
                                   shred it into five bogus "short SHAs".
 2   ISO-8601 date-time      ``<ts>``  longest datetime form (optional seconds,
                                   fractional seconds, ``Z`` / numeric offset).
 3   IPv4 (optional port)    ``<ip>``  must precede the ``file:line`` rule, which
                                   would otherwise claim ``10.0.0.1:5432``.
 4   Windows absolute path   ``<path>`` drive-letter rooted.
 5   POSIX absolute path     ``<path>`` must precede rule 10, otherwise a hex
                                   directory component shreds the path, and must
                                   follow rule 2/3 so a timestamp or IP inside a
                                   path is consumed as one token.
 6   ``file.ext:line[:col]`` ``<path>`` relative or nested; covers what the
                                   absolute-path rules cannot see.
 7   ISO date (no time)      ``<ts>``  hyphens keep it out of rules 9/10, which
                                   would otherwise shred it into ``<n>-<n>-<n>``.
 8   clock time              ``<ts>``  ``HH:MM[:SS[.fff]]``, same reason.
 9   bare integers           ``<n>``  a digit run fenced by non-word characters
                                   is a number: CI run id, port, count, epoch.
                                   ``\b`` can never fall inside a word, so hex
                                   identifiers are never touched here.
10   7-40 hex-char run       ``<sha>``  git short SHA (7) through full SHA (40).
                                   The floor is 7 because shorter hex runs are
                                   ordinary words and numbers. The one overlap
                                   with rule 9 is an all-digit short SHA, which
                                   rule 9 already collapsed to ``<n>`` — stable,
                                   so clustering is unaffected either way.
11   lowercase, collapse     trailing/leading punctuation and internal whitespace
     whitespace, truncate
===  ======================  =========================================================

This module is the single source of truth for failure signatures. The runs
dashboard groups on the ``signature`` the API returns and never recomputes it
client-side: a second implementation of this normalization is exactly how the
original ISO-8601 defect ended up existing in two languages at once. The
golden corpus in ``backend/tests/failure_signature_vectors.json`` pins the
exact output for 40 representative inputs.
"""

from __future__ import annotations

import re

MAX_SIGNATURE_CHARS = 200
UNKNOWN_SIGNATURE = "unknown"

# `runs.failure_reason` is a `Text` column written by the orchestrator from
# sanitized CI output and can hold millions of characters, but the signature
# only ever uses the first non-empty line truncated to MAX_SIGNATURE_CHARS.
# Clipping the input first keeps the cost of normalizing one run constant
# instead of proportional to the stored log, which matters because
# list_runs normalizes up to SIGNATURE_CLUSTER_MAX_RUNS runs per request.
MAX_SIGNATURE_INPUT_CHARS = 1024

# All patterns are case-insensitive so they stay correct regardless of where
# lowercasing happens; the normalized output is lowercased regardless so that
# ``ValueError`` and ``valueerror`` cluster together.
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*")
# The lookbehind keeps the opening slash from being matched mid-token, which
# stops `a//b/c` and `./b/c` from being shredded into a bogus <path>.
_UNIX_ABS_PATH_RE = re.compile(r"(?<![\w./])/(?:[\w.\-]+/)+[\w.\-]+")
_FILE_LINE_RE = re.compile(r"(?<![\w/])(?:[\w.\-]+/)*[\w.\-]+\.\w+:\d+(?::\d+)?")
_HEX_RUN_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\b")
_NUMBER_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")

_EDGE_PUNCTUATION = " -–—:;,.|"


def normalize_failure_signature(reason: str | None) -> str:
    """Normalize a raw ``failure_reason`` into a stable clustering signature.

    At most ``MAX_SIGNATURE_INPUT_CHARS`` characters of input are examined, and
    of those only the first non-empty line: it carries the error
    type/message, while later lines are stack frames and log noise. Returns
    ``UNKNOWN_SIGNATURE`` when nothing survives normalization.
    """
    if not reason:
        return UNKNOWN_SIGNATURE
    reason = reason[:MAX_SIGNATURE_INPUT_CHARS]
    if not reason.strip():
        return UNKNOWN_SIGNATURE

    first_line = ""
    for line in reason.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if not first_line:
        return UNKNOWN_SIGNATURE

    # Applied in precedence order (see module docstring); each rule consumes the
    # most anchored token form available so later, broader rules cannot chew
    # through a substring of an already-matched token.
    text = _UUID_RE.sub("<id>", first_line)
    text = _ISO_TS_RE.sub("<ts>", text)
    text = _IPV4_RE.sub("<ip>", text)
    text = _WIN_PATH_RE.sub("<path>", text)
    text = _UNIX_ABS_PATH_RE.sub("<path>", text)
    text = _FILE_LINE_RE.sub("<path>", text)
    text = _DATE_RE.sub("<ts>", text)
    text = _TIME_RE.sub("<ts>", text)
    text = _NUMBER_RE.sub("<n>", text)
    text = _HEX_RUN_RE.sub("<sha>", text)
    text = _WS_RE.sub(" ", text.lower()).strip(_EDGE_PUNCTUATION)
    if not text:
        return UNKNOWN_SIGNATURE
    if len(text) > MAX_SIGNATURE_CHARS:
        text = text[:MAX_SIGNATURE_CHARS].rstrip()
    return text
