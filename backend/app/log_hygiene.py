"""Canonical sanitization for untrusted values that reach a log line.

A log line is an output boundary like any other, and a stricter one in practice:
log aggregators are long-lived, widely readable, replicated to third parties, and
far harder to purge than a database row. Every attacker-influenced string that is
interpolated into a log record — an archive entry name, a repository name, a CI
job name, a file path — goes through :func:`sanitize_log_value` so no call site
can forget one of the four properties it enforces:

* **Control and invisible-character stripping.** A newline in an archive entry
  name or a branch name forges a second, attacker-authored log record; a
  zero-width or bidirectional-override character hides text from the operator
  reviewing the line while leaving it visible to anyone reading the raw bytes.
* **Canonical credential redaction.** A token pasted into a repository or job
  name is persisted by the log pipeline otherwise, and is then outside every
  rotation and revocation path the codebase has.
* **Length bounding.** Entry names and job names are chosen by the attacker and
  may be arbitrarily long; unbounded interpolation is a log-flooding primitive
  that also evades truncation further downstream.
* **Single-line normalization.** Whitespace runs collapse to one space so a
  value cannot impersonate the structured ``key=value`` fields around it.

Redaction runs *after* control stripping, so a multi-line secret (a PEM private
key) is collapsed to spaces first and is still recognised as one span by the
canonical pattern set.

The credential pattern set is imported from
:mod:`app.llm.prompts.audit_prompts` rather than restated here: the codebase has
exactly one definition of what "a secret" looks like, and a second copy in a
logging helper is how the two drift apart and a new provider key stops being
redacted.
"""

from __future__ import annotations

import re
from typing import Any

from app.llm.prompts.audit_prompts import redact_sensitive_text

MAX_LOG_VALUE_CHARS = 256
_TRUNCATION_SUFFIX = "..."

#: C0 and C1 controls, DEL, the soft hyphen, zero-width space/joiners, the
#: bidi embedding and override ranges and their isolates, the line/paragraph
#: separators, the word joiner and the byte-order mark. Newlines and tabs are
#: controls too: collapsing them is the entire point.
_CONTROL_AND_INVISIBLE_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e"
    "\u2060-\u2064\u2066-\u2069\ufeff]"
)


def sanitize_log_value(value: Any, maximum: int = MAX_LOG_VALUE_CHARS) -> str:
    """Return `value` made safe to interpolate into a single log line.

    An empty or absent value is returned as the empty string rather than a
    placeholder: a caller logging a genuinely empty field should see nothing
    there, and inventing a sentinel would make an absent value indistinguishable
    from a real one.
    """
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        maximum = MAX_LOG_VALUE_CHARS
    text = value if isinstance(value, str) else str(value or "")
    text = _CONTROL_AND_INVISIBLE_RE.sub(" ", text)
    text = redact_sensitive_text(text)
    text = " ".join(text.split())
    if len(text) > maximum:
        text = text[: max(0, maximum - len(_TRUNCATION_SUFFIX))] + _TRUNCATION_SUFFIX
    return text
