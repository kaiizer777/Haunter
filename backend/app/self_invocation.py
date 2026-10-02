"""Uniform HMAC-SHA256 authentication for internal machine-triggered calls.

Every internal machine-triggered call (Lambda self-invocation for the CI
pipeline, the code reviewer and the audit worker; the retention cron against
the sweeper endpoint) is authenticated with the same dedicated secret and the
same construction:

    token = HMAC-SHA256(secret, "<kind>:<identifier>")

`kind` domain-separates the invocation classes so a token minted for one class
can never authenticate another. The secret is the dedicated
`audit_self_invoke_secret` setting, which must be non-empty; the previous
webhook/session secret fallbacks were removed because a leaked webhook signing
key must not grant the ability to trigger paid LLM pipeline work.

`resolve_self_invocation_secret` is the single source of truth for that
requirement — the audit fence and child tokens in `app.services.audit_pipeline`
resolve their key through it rather than through a second copy of the rule.

Reusing one secret for the retention kind is deliberate. The alternative — a
dedicated secret per kind — would add an operator-provisioned credential whose
blank default would silently disable the sweeper, while the retention token can
do strictly less than any of the other three: it triggers one idempotent
retention pass and cannot mint LLM work, delete a run, or touch another
tenant's rows.

Verification never raises: it returns False for any malformed input, missing
secret, or mismatched token so callers fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Optional

KIND_PIPELINE = "pipeline"
KIND_REVIEW = "review"
KIND_AUDIT = "audit"
KIND_RETENTION = "retention"

SELF_INVOCATION_KINDS = frozenset(
    {KIND_PIPELINE, KIND_REVIEW, KIND_AUDIT, KIND_RETENTION}
)

_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_MAX_IDENTIFIER_LEN = 256


class SelfInvocationError(RuntimeError):
    """Raised when a self-invocation token cannot be minted."""


def resolve_self_invocation_secret(secret: Optional[str] = None) -> str:
    """Return a usable self-invocation secret or raise SelfInvocationError.

    `None` means "use the configured `audit_self_invoke_secret`". An explicitly
    supplied value is used verbatim and never falls back to the setting: a
    caller that asked for one specific key and was handed a blank one must fail
    rather than be silently authenticated with a different key.

    A whitespace-only value is an unset secret, not a usable one — it passes a
    bare truthiness check and then authenticates against an effectively empty
    HMAC key.
    """
    if secret is None:
        from app.config import settings

        secret = settings.audit_self_invoke_secret
    if not isinstance(secret, str) or not secret.strip():
        raise SelfInvocationError("self-invocation secret is not configured")
    return secret


def self_invocation_message(kind: str, identifier: str) -> str:
    if kind not in SELF_INVOCATION_KINDS:
        raise ValueError("self-invocation kind is invalid")
    if (
        not isinstance(identifier, str)
        or not identifier
        or len(identifier) > _MAX_IDENTIFIER_LEN
        or any(character.isspace() for character in identifier)
    ):
        raise ValueError("self-invocation identifier is invalid")
    return f"{kind}:{identifier}"


def self_invocation_token(
    kind: str,
    identifier: str,
    secret: Optional[str] = None,
) -> str:
    message = self_invocation_message(kind, identifier)
    return hmac.new(
        resolve_self_invocation_secret(secret).encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_self_invocation(
    kind: str,
    identifier: str,
    token: Optional[str],
    secret: Optional[str] = None,
) -> bool:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return False
    try:
        expected = self_invocation_token(kind, identifier, secret)
    except Exception:  # noqa: BLE001 — a verifier that cannot verify must return False
        # Minting the expected token can fail for reasons well beyond a bad
        # argument: an unconfigured secret, an unimportable app.config, a
        # non-str identifier from a malformed event. Every one of those means
        # "this invocation is unauthenticated", so the answer is False and never
        # an exception the caller might not be built to fail closed on.
        return False
    return hmac.compare_digest(expected, token)
