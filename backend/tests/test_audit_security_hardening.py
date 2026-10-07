"""
Phase 5.2 audit hardening — hermetic regression tests for the four audit fixes.

  1. Fail-closed self-invocation (backend/lambda_handler.py,
     backend/app/adapters/hosting.py): the public function has no unsigned
     dispatch branch, and every audit/pipeline/review self-invocation needs a
     non-empty configured secret verified with HMAC compare_digest. Missing,
     malformed, wrong-key, cross-kind, and verifier-failure invocations are
     all rejected before any work is scheduled.
  2. Read-only GitHub credential enforcement (backend/app/github/auditor.py,
     backend/app/github_client.py): the auditor token must be non-empty, must
     come from the auditor read-only provider only (no fallback to
     settings.github_token), and the installation permission map must expose
     only approved read/none scopes.
  3. Bounded installation-token response (backend/app/github/auditor.py): the
     POST is streamed and a byte cap is enforced before any JSON parsing.
  4. Webhook body limit before buffering (backend/app/webhooks.py): the body
     is read incrementally and the stream is abandoned as soon as the cap is
     crossed, so a chunked request with no Content-Length cannot force an
     unbounded allocation.

Every test here is hermetic: no database fixture, no network (an autouse
fixture hard-fails any non-ASGI httpx request), no TEST_DATABASE_URL needed.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import inspect
import io
import json
import tokenize
import uuid
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.config import settings
from app.github import auditor as auditor_credentials
from app.github_client import GitHubAuthError, _build_headers
from app.self_invocation import (
    KIND_PIPELINE,
    KIND_REVIEW,
    SelfInvocationError,
    self_invocation_token,
    verify_self_invocation,
)
from app.services import audit_pipeline

SELF_INVOKE_SECRET = "hardening-self-invoke-secret"
OTHER_SELF_INVOKE_SECRET = "hardening-self-invoke-secret-rotated"
WEBHOOK_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"
AUDIT_ID = "audit-abcdef123456"
FENCE = "c" * 64
READ_ONLY_SCOPES = {"contents": "read", "pull_requests": "read", "metadata": "read"}
# A real installation token plus its permission map is well under 4KB and the
# auditor cap is 64KB, so the positive control below stays realistic.
CREDENTIAL_CHUNK_BYTES = 8 * 1024
WEBHOOK_CHUNK_BYTES = 64 * 1024


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly on any outbound request that is not the ASGI app under test."""
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport):
            return await original_send(self, request, **kwargs)
        raise AssertionError(
            f"external HTTP blocked in audit hardening tests: {request.url.host}"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)


def _code_only(source: str) -> str:
    """Strip comments so prose about a forbidden call cannot satisfy a grep."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    kept = [
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE)
    ]
    return " ".join(kept)


# ---------------------------------------------------------------------------
# 1. Fail-closed self-invocation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "configured_secret",
    [None, "", "   "],
    ids=["missing", "empty", "whitespace_only"],
)
def test_handler_rejects_pipeline_and_review_when_secret_is_not_usable(
    configured_secret: str | None,
) -> None:
    import lambda_handler

    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", configured_secret),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        pipeline = lambda_handler.handler({"run_id": run_id}, SimpleNamespace())
        review = lambda_handler.handler({"review_id": review_id}, SimpleNamespace())

    run_pipeline.assert_not_awaited()
    run_review.assert_not_awaited()
    assert pipeline == {
        "error": "unauthorized pipeline invocation",
        "run_id": run_id,
    }
    assert review == {
        "error": "unauthorized review invocation",
        "review_id": review_id,
    }


def test_handler_rejects_pipeline_and_review_signed_with_the_wrong_key() -> None:
    """A well-formed token minted under a rotated or foreign key must not pass."""
    import lambda_handler

    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        pipeline = lambda_handler.handler(
            {
                "run_id": run_id,
                "token": self_invocation_token(
                    KIND_PIPELINE, run_id, OTHER_SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )
        review = lambda_handler.handler(
            {
                "review_id": review_id,
                "token": self_invocation_token(
                    KIND_REVIEW, review_id, OTHER_SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )

    run_pipeline.assert_not_awaited()
    run_review.assert_not_awaited()
    assert pipeline["error"] == "unauthorized pipeline invocation"
    assert review["error"] == "unauthorized review invocation"


@pytest.mark.parametrize(
    "token",
    [None, "", "bad", "zz" * 32, "A" * 64, "0" * 63, "0" * 65],
    ids=["absent", "empty", "opaque", "non_hex", "uppercase", "truncated", "oversized"],
)
def test_handler_rejects_malformed_self_invocation_tokens(token: str | None) -> None:
    import lambda_handler

    run_id = str(uuid.uuid4())
    event: dict[str, Any] = {"run_id": run_id}
    if token is not None:
        event["token"] = token
    with (
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
    ):
        result = lambda_handler.handler(event, SimpleNamespace())

    run_pipeline.assert_not_awaited()
    assert result == {
        "error": "unauthorized pipeline invocation",
        "run_id": run_id,
    }


def test_handler_rejects_tokens_minted_for_another_invocation_kind() -> None:
    """Domain separation has to hold at the trust boundary, not just the helper."""
    import lambda_handler

    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        # The correct pipeline token for this run is accepted, which is the
        # positive control proving the rejection below is about the *kind*.
        pipeline = lambda_handler.handler(
            {
                "run_id": run_id,
                "token": self_invocation_token(
                    KIND_PIPELINE, run_id, SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )
        # The same construction, minted for the pipeline kind, replayed as a
        # review invoke: same identifier, same secret, wrong domain.
        review = lambda_handler.handler(
            {
                "review_id": review_id,
                "token": self_invocation_token(
                    KIND_PIPELINE, review_id, SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )

    run_pipeline.assert_awaited_once_with(run_id)
    run_review.assert_not_awaited()
    assert pipeline == {"status": "completed", "run_id": run_id}
    assert review == {
        "error": "unauthorized review invocation",
        "review_id": review_id,
    }


def test_handler_accepts_only_correctly_signed_pipeline_and_review_invocations() -> (
    None
):
    """Positive control: a valid signature is still honoured end to end."""
    import lambda_handler

    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        pipeline = lambda_handler.handler(
            {
                "run_id": run_id,
                "token": self_invocation_token(
                    KIND_PIPELINE, run_id, SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )
        review = lambda_handler.handler(
            {
                "review_id": review_id,
                "token": self_invocation_token(
                    KIND_REVIEW, review_id, SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )

    run_pipeline.assert_awaited_once_with(run_id)
    run_review.assert_awaited_once_with(review_id)
    assert pipeline == {"status": "completed", "run_id": run_id}
    assert review == {"status": "completed", "review_id": review_id}


def test_handler_rejects_audit_invocation_when_the_verifier_cannot_run() -> None:
    """A verification helper that raises is an authentication failure, not a 500."""
    import lambda_handler

    with (
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch(
            "app.services.audit_pipeline.verify_audit_self_invocation",
            side_effect=RuntimeError("verifier unavailable"),
        ),
        patch("lambda_handler._run_audit", new_callable=AsyncMock) as run_audit,
    ):
        result = lambda_handler.handler(
            {
                "audit_id": AUDIT_ID,
                "dispatch_fence_token": FENCE,
                "token": "0" * 64,
            },
            SimpleNamespace(),
        )

    run_audit.assert_not_awaited()
    assert result == {"error": "unauthorized audit invocation"}


def test_handler_rejects_pipeline_and_review_when_the_verifier_cannot_run() -> None:
    import lambda_handler

    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch(
            "app.self_invocation.verify_self_invocation",
            side_effect=RuntimeError("verifier unavailable"),
        ),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        pipeline = lambda_handler.handler(
            {
                "run_id": run_id,
                "token": self_invocation_token(
                    KIND_PIPELINE, run_id, SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )
        review = lambda_handler.handler(
            {
                "review_id": review_id,
                "token": self_invocation_token(
                    KIND_REVIEW, review_id, SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )

    run_pipeline.assert_not_awaited()
    run_review.assert_not_awaited()
    assert pipeline["error"] == "unauthorized pipeline invocation"
    assert review["error"] == "unauthorized review invocation"


def test_public_handler_has_no_unsigned_dispatch_branch() -> None:
    """No public shape of `operation` may reach the durable poller."""
    import lambda_handler

    valid_audit_token = audit_pipeline.audit_child_invocation_token(
        AUDIT_ID, FENCE, SELF_INVOKE_SECRET
    )
    run_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch(
            "app.services.audit_pipeline.dispatch_audit_jobs",
            new_callable=AsyncMock,
        ) as dispatch,
        patch("lambda_handler._run_audit", new_callable=AsyncMock) as run_audit,
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
    ):
        bare = lambda_handler.handler(
            {"operation": "dispatch_audits"}, SimpleNamespace()
        )
        signed = lambda_handler.handler(
            {"operation": "dispatch_audits", "token": valid_audit_token},
            SimpleNamespace(),
        )
        smuggled_audit = lambda_handler.handler(
            {
                "operation": "dispatch_audits",
                "audit_id": AUDIT_ID,
                "dispatch_fence_token": FENCE,
                "token": valid_audit_token,
            },
            SimpleNamespace(),
        )
        smuggled_pipeline = lambda_handler.handler(
            {
                "operation": "dispatch_audits",
                "run_id": run_id,
                "token": self_invocation_token(
                    KIND_PIPELINE, run_id, SELF_INVOKE_SECRET
                ),
            },
            SimpleNamespace(),
        )

    dispatch.assert_not_awaited()
    run_audit.assert_not_awaited()
    run_pipeline.assert_not_awaited()
    assert bare == signed == smuggled_audit == smuggled_pipeline
    assert bare == {"error": "unsupported invocation"}


def test_public_handler_rejects_non_dict_and_malformed_audit_identifiers() -> None:
    import lambda_handler

    with patch("lambda_handler._run_audit", new_callable=AsyncMock) as run_audit:
        not_a_dict = lambda_handler.handler(
            cast(Any, ["audit_id", AUDIT_ID]), SimpleNamespace()
        )
        malformed = [
            lambda_handler.handler(
                {
                    "audit_id": candidate,
                    "dispatch_fence_token": FENCE,
                    "token": "0" * 64,
                },
                SimpleNamespace(),
            )
            for candidate in (
                "not-an-audit-id",
                "audit-../../../etc",
                "audit-ABCDEF123456",
                "audit-abcdef12345",
                "audit-abcdef1234567",
                "",
            )
        ]

    run_audit.assert_not_awaited()
    assert not_a_dict == {"error": "invalid invocation"}
    for rejected in malformed:
        assert rejected == {"error": "unauthorized audit invocation"}


@pytest.mark.asyncio
async def test_every_scheduler_signs_its_self_invocation_payload() -> None:
    """Scheduler and handler must agree on the construction, per invocation kind."""
    from app.adapters.hosting import AWSHostingAdapter

    run_id = uuid.uuid4()
    review_id = uuid.uuid4()
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    fence = audit_pipeline.audit_dispatch_fence_token(AUDIT_ID, 1, SELF_INVOKE_SECRET)
    with (
        patch("app.config.settings.aws_lambda_function_name", "haunter-test"),
        patch("app.config.settings.audit_self_invoke_secret", SELF_INVOKE_SECRET),
        patch("boto3.client", return_value=lambda_client),
    ):
        adapter = AWSHostingAdapter()
        await adapter.schedule_pipeline(run_id, MagicMock())
        await adapter.schedule_review(review_id, MagicMock())
        await adapter.schedule_audit(AUDIT_ID, fence)

    assert lambda_client.invoke.call_count == 3
    for call in lambda_client.invoke.call_args_list:
        assert call.kwargs["InvocationType"] == "Event"
        assert call.kwargs["FunctionName"] == "haunter-test"
        # The signing key must never travel inside the payload.
        assert SELF_INVOKE_SECRET.encode() not in call.kwargs["Payload"]

    pipeline_payload, review_payload, audit_payload = [
        json.loads(call.kwargs["Payload"])
        for call in lambda_client.invoke.call_args_list
    ]
    assert pipeline_payload["run_id"] == str(run_id)
    assert review_payload["review_id"] == str(review_id)
    assert verify_self_invocation(
        KIND_PIPELINE, str(run_id), pipeline_payload["token"], SELF_INVOKE_SECRET
    )
    assert verify_self_invocation(
        KIND_REVIEW, str(review_id), review_payload["token"], SELF_INVOKE_SECRET
    )
    assert audit_pipeline.verify_audit_self_invocation(
        audit_payload["audit_id"],
        audit_payload["dispatch_fence_token"],
        audit_payload["token"],
        SELF_INVOKE_SECRET,
    )
    # Domain separation: a pipeline token never authenticates as a review invoke.
    assert not verify_self_invocation(
        KIND_REVIEW, str(run_id), pipeline_payload["token"], SELF_INVOKE_SECRET
    )


@pytest.mark.asyncio
async def test_schedulers_never_invoke_lambda_without_a_configured_secret() -> None:
    """No scheduler may fall back to an unsigned invoke or an in-process bypass."""
    from app.adapters.hosting import AWSHostingAdapter

    background_tasks = MagicMock()
    lambda_client = MagicMock()
    with (
        patch("app.config.settings.aws_lambda_function_name", "haunter-test"),
        patch("app.config.settings.audit_self_invoke_secret", ""),
        patch("app.config.settings.github_webhook_secret", WEBHOOK_SECRET),
        patch("app.config.settings.session_secret_key", "session-secret"),
        patch("boto3.client", return_value=lambda_client),
    ):
        adapter = AWSHostingAdapter()
        with pytest.raises(SelfInvocationError):
            await adapter.schedule_pipeline(uuid.uuid4(), background_tasks)
        with pytest.raises(SelfInvocationError):
            await adapter.schedule_review(uuid.uuid4(), background_tasks)
        with pytest.raises(SelfInvocationError):
            await adapter.schedule_audit(AUDIT_ID, "a" * 64)

    lambda_client.invoke.assert_not_called()
    background_tasks.add_task.assert_not_called()


def test_audit_child_token_binding_and_verification_are_fail_closed() -> None:
    token = audit_pipeline.audit_child_invocation_token(
        AUDIT_ID, FENCE, SELF_INVOKE_SECRET
    )
    assert audit_pipeline.verify_audit_self_invocation(
        AUDIT_ID, FENCE, token, SELF_INVOKE_SECRET
    )
    # Wrong key, wrong audit, wrong fence, and malformed inputs are all refused.
    assert not audit_pipeline.verify_audit_self_invocation(
        AUDIT_ID, FENCE, token, OTHER_SELF_INVOKE_SECRET
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        "audit-bbbbbbbbbbbb", FENCE, token, SELF_INVOKE_SECRET
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        AUDIT_ID, "d" * 64, token, SELF_INVOKE_SECRET
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        AUDIT_ID, FENCE, None, SELF_INVOKE_SECRET
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        AUDIT_ID, FENCE, "not-a-fence", SELF_INVOKE_SECRET
    )
    # A fence minted for a different dispatch attempt never authenticates this one.
    stale_token = audit_pipeline.audit_child_invocation_token(
        AUDIT_ID,
        audit_pipeline.audit_dispatch_fence_token(AUDIT_ID, 2, SELF_INVOKE_SECRET),
        SELF_INVOKE_SECRET,
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        AUDIT_ID, FENCE, stale_token, SELF_INVOKE_SECRET
    )


def test_malformed_tokens_are_rejected_before_the_constant_time_comparison() -> None:
    """The token shape is checked first, so junk never reaches the comparison.

    An explicit secret is passed so the only thing that can reject a malformed
    token is its shape — otherwise a missing secret would make the test pass for
    the wrong reason.
    """
    with patch("app.self_invocation.hmac.compare_digest") as compare_digest:
        for malformed in ("short", "not-hex" * 10, "A" * 64, "f" * 63, "f" * 65):
            assert not verify_self_invocation(
                KIND_PIPELINE, "identifier", malformed, SELF_INVOKE_SECRET
            )
    compare_digest.assert_not_called()

    with patch(
        "app.self_invocation.hmac.compare_digest", return_value=True
    ) as compare_digest:
        assert verify_self_invocation(
            KIND_PIPELINE,
            "identifier",
            "f" * 64,
            SELF_INVOKE_SECRET,
        )
    compare_digest.assert_called_once()


def test_self_invocation_helpers_refuse_to_mint_or_verify_without_a_secret() -> None:
    with patch("app.config.settings.audit_self_invoke_secret", None):
        for kind in (KIND_PIPELINE, KIND_REVIEW):
            with pytest.raises(SelfInvocationError):
                self_invocation_token(kind, "identifier")
            assert not verify_self_invocation(kind, "identifier", "0" * 64)
        with pytest.raises(SelfInvocationError):
            audit_pipeline.audit_child_invocation_token(AUDIT_ID, FENCE)
        with pytest.raises(SelfInvocationError):
            audit_pipeline.audit_dispatch_fence_token(AUDIT_ID, 1)
        assert not audit_pipeline.verify_audit_self_invocation(
            AUDIT_ID, FENCE, "0" * 64
        )
        assert not audit_pipeline.verify_audit_dispatch_fence(AUDIT_ID, 1, "0" * 64)


# ---------------------------------------------------------------------------
# 2. Read-only GitHub credential enforcement
# ---------------------------------------------------------------------------


def test_auditor_installation_permissions_reject_every_non_read_only_scope() -> None:
    """`contents=admin`, custom scopes, and unknown scopes are all refused."""
    # `custom_properties` is a real GitHub App permission the auditor never needs.
    # A `write` value on it is refused on two independent grounds, and so is a
    # `none` value, because the allowlist is closed rather than denylisted.
    assert "custom_properties" not in auditor_credentials._ALLOWED_PERMISSIONS

    overrides: dict[str, dict[str, Any]] = {
        "contents_admin": {"contents": "admin"},
        "custom_properties_write": {"custom_properties": "write"},
        "custom_properties_none": {"custom_properties": "none"},
        "custom_properties_read": {"custom_properties": "read"},
        "organization_administration_read": {"organization_administration": "read"},
        "actions_write": {"actions": "write"},
        "workflows_write": {"workflows": "write"},
        "deployments_write": {"deployments": "write"},
        "secrets_write": {"secrets": "write"},
        "pull_requests_admin": {"pull_requests": "admin"},
        "non_string_value": {"contents": 1},
        "null_value": {"metadata": None},
    }
    for label, override in overrides.items():
        payload = {"permissions": {**READ_ONLY_SCOPES, **override}}
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            auditor_credentials._validate_read_only_permissions(payload)
        assert label

    # Every required read scope must be present *and* read.
    for missing in ("contents", "pull_requests", "metadata"):
        permissions = {
            key: value for key, value in READ_ONLY_SCOPES.items() if key != missing
        }
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            auditor_credentials._validate_read_only_permissions(
                {"permissions": permissions}
            )
    for downgraded in ("contents", "pull_requests", "metadata"):
        permissions = dict(READ_ONLY_SCOPES)
        permissions[downgraded] = "none"
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            auditor_credentials._validate_read_only_permissions(
                {"permissions": permissions}
            )

    for malformed in (
        {},
        {"permissions": {}},
        {"permissions": "read"},
        {"permissions": None},
    ):
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            auditor_credentials._validate_read_only_permissions(malformed)

    # The approved read-only set, plus the approved optional `single_file` scope.
    auditor_credentials._validate_read_only_permissions(
        {"permissions": dict(READ_ONLY_SCOPES)}
    )
    for optional in ("read", "none"):
        auditor_credentials._validate_read_only_permissions(
            {"permissions": {**READ_ONLY_SCOPES, "single_file": optional}}
        )
    # Write permissions on contents or pull_requests are also accepted.
    auditor_credentials._validate_read_only_permissions(
        {"permissions": {**READ_ONLY_SCOPES, "contents": "write", "pull_requests": "write"}}
    )


class CountingChunks:
    """Async byte iterator that records how much of the body was consumed."""

    def __init__(self, payload: bytes, chunk_size: int) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self.chunks_yielded = 0
        self.bytes_yielded = 0

    async def _iterate(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._payload), self._chunk_size):
            chunk = self._payload[offset : offset + self._chunk_size]
            self.chunks_yielded += 1
            self.bytes_yielded += len(chunk)
            yield chunk

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()


class CredentialStreamClient:
    """Mock httpx client for the installation-token POST, with a real body."""

    def __init__(
        self,
        payload: bytes,
        status_code: int = 201,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.chunks = CountingChunks(payload, CREDENTIAL_CHUNK_BYTES)
        self._response = MagicMock(status_code=status_code, is_error=status_code >= 400)
        self._response.headers = dict(headers or {})
        self._response.aiter_bytes = MagicMock(return_value=self.chunks)
        self._stream = MagicMock()
        self._stream.__aenter__ = AsyncMock(return_value=self._response)
        self._stream.__aexit__ = AsyncMock(return_value=None)
        self.stream = MagicMock(return_value=self._stream)

    async def __aenter__(self) -> "CredentialStreamClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    @property
    def chunks_consumed(self) -> int:
        return self.chunks.chunks_yielded


class _CredentialPatches(contextlib.AbstractContextManager):
    """Bind the auditor credential provider to a mocked streaming client."""

    def __init__(self, client: CredentialStreamClient) -> None:
        self._patchers = (
            patch("app.github.auditor.settings.github_auditor_app_id", "12345"),
            patch(
                "app.github.auditor.settings.github_auditor_app_private_key",
                "separate-read-only-key",
            ),
            patch(
                "app.github.auditor._build_read_only_jwt",
                return_value="read-only-app-jwt",
            ),
            patch("app.github.auditor.httpx.AsyncClient", return_value=client),
            patch.dict(auditor_credentials._TOKEN_CACHE, {}, clear=True),
        )

    def __enter__(self) -> None:
        for patcher in self._patchers:
            patcher.start()

    def __exit__(self, *exc_info: Any) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()


def _configured_credential_client(client: CredentialStreamClient) -> Any:
    return _CredentialPatches(client)


def _credential_body(
    token: str = "read-only-installation-token", padding: int = 0
) -> bytes:
    return json.dumps(
        {
            "token": token,
            "permissions": dict(READ_ONLY_SCOPES),
            "pad": "p" * padding,
        }
    ).encode()


@pytest.mark.asyncio
async def test_auditor_credential_response_without_a_token_is_rejected() -> None:
    """An empty token from the read-only provider is never accepted downstream."""
    client = CredentialStreamClient(_credential_body(token=""))
    with _configured_credential_client(client):
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            await auditor_credentials.get_auditor_installation_token(
                SimpleNamespace(auditor_github_install_id=55)
            )
        assert auditor_credentials._TOKEN_CACHE == {}


@pytest.mark.asyncio
async def test_auditor_credential_response_with_write_scopes_is_rejected() -> None:
    """A live response that reports `contents=admin` is refused and not cached."""
    payload = json.dumps(
        {
            "token": "read-only-installation-token",
            "permissions": {**READ_ONLY_SCOPES, "contents": "admin"},
        }
    ).encode()
    client = CredentialStreamClient(payload)
    with _configured_credential_client(client):
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            await auditor_credentials.get_auditor_installation_token(
                SimpleNamespace(auditor_github_install_id=55)
            )
        assert auditor_credentials._TOKEN_CACHE == {}


def test_auditor_reads_never_fall_back_to_the_global_personal_access_token() -> None:
    """A configured PAT must not silently stand in for the read-only App token."""
    with patch("app.github_client.settings.github_token", "global-pat-value"):
        for token in (None, "", "   "):
            with pytest.raises(GitHubAuthError):
                _build_headers(token=token, allow_global_token=False)
        # The write-capable pipeline paths keep their documented fallback.
        assert (
            _build_headers(allow_global_token=True)["Authorization"]
            == "Bearer global-pat-value"
        )


@pytest.mark.asyncio
async def test_auditor_read_helpers_issue_no_request_without_a_read_only_token() -> (
    None
):
    """Every auditor read path must fail before touching the network.

    The flag is asserted behaviourally rather than by reading source: a call
    site that silently drops `allow_global_token=False` would otherwise resolve
    the full-scope personal access token and escalate the auditor past the
    read-only GitHub App it just validated.
    """
    from app.github_client import fetch_diff, fetch_file_content, fetch_pull_request
    from app.subagents import auditor as audit_subagent

    resolved: list[tuple[str, bool, Any]] = []
    real_build_headers = inspect.unwrap(_build_headers)

    def recording_build_headers(
        token=None, accept="application/vnd.github+json", allow_global_token=True
    ):
        resolved.append((inspect.stack()[0].function, allow_global_token, token))
        return real_build_headers(
            token=token, accept=accept, allow_global_token=allow_global_token
        )

    read_paths = [
        lambda: fetch_diff(
            owner="o", repo="r", sha="a" * 40, token=None, allow_global_token=False
        ),
        lambda: fetch_file_content(
            owner="o",
            repo="r",
            path="a.py",
            sha="b" * 40,
            token=None,
            allow_global_token=False,
        ),
        lambda: fetch_pull_request(
            owner="o", repo="r", pr_number=1, token=None, allow_global_token=False
        ),
    ]
    with (
        patch("app.github_client.settings.github_token", "global-pat-value"),
        patch("app.github_client._build_headers", recording_build_headers),
    ):
        for read_path in read_paths:
            with pytest.raises(GitHubAuthError):
                await read_path()
        # `fetch_audit_diff` re-raises as a typed auditor failure; the cause
        # still has to be the auth refusal, not a swallowed network fault.
        with pytest.raises(audit_subagent.AuditDiffFetchError) as excinfo:
            await audit_subagent.fetch_audit_diff(
                owner="o", repo="r", head_sha="c" * 40, token=None
            )
        assert isinstance(excinfo.value.__cause__, GitHubAuthError)

    # Every audited read path refused the global fallback rather than using it.
    assert resolved, "expected the auditor read paths to build request headers"
    assert all(
        allow_global is False for _caller, allow_global, _t in resolved
    ), resolved
    assert all(token is None for _caller, _a, token in resolved), resolved
    # `deny_external_http` turns any request that escaped into an AssertionError,
    # so reaching this line proves not one outbound call was attempted.
    assert True


def test_auditor_credential_sources_have_no_personal_access_token_fallback() -> None:
    """No module on the auditor credential path may resolve the global PAT."""
    modules = (
        auditor_credentials,
        audit_pipeline,
    )
    for module in modules:
        source = inspect.getsource(module)
        assert "settings.github_token" not in source, module.__name__
        assert "github_pr" not in source, module.__name__


@pytest.mark.asyncio
async def test_auditor_credential_provider_fails_closed_without_configuration() -> None:
    for install_id in (None, 0, -1, True, "55"):
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            await auditor_credentials.get_auditor_installation_token(
                SimpleNamespace(auditor_github_install_id=install_id)
            )
    for app_id, private_key in (
        ("", "separate-read-only-key"),
        (None, "separate-read-only-key"),
        ("12345", ""),
        ("12345", "   "),
        ("12345", None),
    ):
        with (
            patch("app.github.auditor.settings.github_auditor_app_id", app_id),
            patch(
                "app.github.auditor.settings.github_auditor_app_private_key",
                private_key,
            ),
            patch(
                "app.github.auditor.httpx.AsyncClient",
                side_effect=AssertionError("no request without a configured App"),
            ),
            pytest.raises(auditor_credentials.AuditorCredentialError),
        ):
            await auditor_credentials.get_auditor_installation_token(
                SimpleNamespace(auditor_github_install_id=55)
            )


# ---------------------------------------------------------------------------
# 3. Bounded installation-token HTTP response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_credential_response_is_rejected_before_json_parsing() -> None:
    """The byte cap fires first: JSON parsing must never see the oversized body."""
    # Far past the cap so an unbounded reader would drain many more chunks than
    # the bounded one does; the assertion is about where the read stops, not
    # merely that an error came back.
    oversized = _credential_body(
        token="x", padding=auditor_credentials.MAX_CREDENTIAL_RESPONSE_BYTES * 4
    )
    total_chunks = -(-len(oversized) // CREDENTIAL_CHUNK_BYTES)
    client = CredentialStreamClient(oversized)
    with (
        _configured_credential_client(client),
        patch(
            "app.github.auditor.json.loads",
            side_effect=AssertionError("oversized body must not be parsed"),
        ),
        pytest.raises(
            auditor_credentials.AuditorCredentialResponseTooLargeError
        ) as excinfo,
    ):
        await auditor_credentials.get_auditor_installation_token(
            SimpleNamespace(auditor_github_install_id=55)
        )
        # The overflow is a typed credential failure, so the caller's retry and
        # error handling keep working on one exception hierarchy.
        assert isinstance(excinfo.value, auditor_credentials.AuditorCredentialError)
        assert auditor_credentials._TOKEN_CACHE == {}

    # The stream is abandoned at the cap instead of being drained to the end,
    # and the bytes handed to the buffer never exceed cap plus one chunk.
    assert total_chunks > client.chunks_consumed
    assert (
        client.chunks.bytes_yielded
        <= auditor_credentials.MAX_CREDENTIAL_RESPONSE_BYTES + CREDENTIAL_CHUNK_BYTES
    )


@pytest.mark.asyncio
async def test_declared_oversized_credential_response_is_rejected_before_reading() -> (
    None
):
    client = CredentialStreamClient(
        _credential_body(),
        headers={
            "content-length": str(auditor_credentials.MAX_CREDENTIAL_RESPONSE_BYTES + 1)
        },
    )
    with (
        _configured_credential_client(client),
        pytest.raises(auditor_credentials.AuditorCredentialResponseTooLargeError),
    ):
        await auditor_credentials.get_auditor_installation_token(
            SimpleNamespace(auditor_github_install_id=55)
        )
    assert client.chunks_consumed == 0


@pytest.mark.asyncio
async def test_response_just_under_the_cap_still_parses_and_validates() -> None:
    """Positive control for fix 3 — the cap must not reject legitimate bodies."""
    payload = _credential_body(
        padding=auditor_credentials.MAX_CREDENTIAL_RESPONSE_BYTES - 512
    )
    assert len(payload) <= auditor_credentials.MAX_CREDENTIAL_RESPONSE_BYTES
    client = CredentialStreamClient(payload)
    with _configured_credential_client(client):
        token = await auditor_credentials.get_auditor_installation_token(
            SimpleNamespace(auditor_github_install_id=55)
        )
        assert token == "read-only-installation-token"
        assert auditor_credentials._TOKEN_CACHE[55][0] == "read-only-installation-token"


# ---------------------------------------------------------------------------
# 4. Webhook body limit before buffering
# ---------------------------------------------------------------------------


class CountingBody:
    """Request body streamed without a Content-Length header (chunked)."""

    def __init__(self, payload: bytes, chunk_size: int) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self.bytes_produced = 0

    async def _iterate(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._payload), self._chunk_size):
            chunk = self._payload[offset : offset + self._chunk_size]
            self.bytes_produced += len(chunk)
            yield chunk

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()


def _sign(raw_body: bytes) -> str:
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={digest.hexdigest()}"


@pytest.mark.asyncio
async def test_chunked_oversized_webhook_body_is_rejected_without_draining(
    client: httpx.AsyncClient,
) -> None:
    """No Content-Length, no `await request.body()`: the cap must stop the stream."""
    from app import webhooks

    limit = webhooks.MAX_PAYLOAD_SIZE_BYTES
    oversized = b"A" * (limit + (WEBHOOK_CHUNK_BYTES * 2))
    body = CountingBody(oversized, WEBHOOK_CHUNK_BYTES)

    response = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": _sign(oversized),
        },
        # An async iterator is framed as chunked with no Content-Length header,
        # so the cheap header pre-check cannot be what rejects this request.
        content=body,
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Payload size exceeds 2MB limit"}
    # Aborted at the cap plus at most one chunk, instead of buffering all of it.
    assert body.bytes_produced <= limit + WEBHOOK_CHUNK_BYTES
    assert body.bytes_produced < len(oversized)


@pytest.mark.asyncio
async def test_chunked_valid_webhook_body_is_hmac_verified_over_streamed_bytes(
    client: httpx.AsyncClient,
) -> None:
    """A correctly signed chunked body is accepted; any tampering is rejected."""
    raw_body = json.dumps(
        {
            "action": "ping",
            "hook": {"id": 1, "type": "Repository"},
            "repository": {"name": "r", "owner": {"login": "o"}},
        }
    ).encode()

    async def post(signature: str | None) -> httpx.Response:
        headers: dict[str, str] = {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": str(uuid.uuid4()),
        }
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return await client.post(
            "/webhooks/github",
            headers=headers,
            content=CountingBody(raw_body, 8),
        )

    accepted = await post(_sign(raw_body))
    tampered_payload = bytearray(raw_body)
    tampered_payload[-2] ^= 0x01
    tampered = await post(_sign(bytes(tampered_payload)))
    unsigned = await post(None)

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ignored"
    assert tampered.status_code == 401
    assert unsigned.status_code == 401


@pytest.mark.asyncio
async def test_webhook_body_limit_boundary_is_exact() -> None:
    """Exactly the cap is accepted; one byte more is refused."""
    from app import webhooks

    class FakeRequest:
        def __init__(self, payload: bytes) -> None:
            self._chunks = [payload]

        async def stream(self) -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                yield chunk

    limit = webhooks.MAX_PAYLOAD_SIZE_BYTES
    at_limit = await webhooks._read_limited_body(cast(Any, FakeRequest(b"a" * limit)))
    assert len(at_limit) == limit

    with pytest.raises(HTTPException) as excinfo:
        await webhooks._read_limited_body(cast(Any, FakeRequest(b"a" * (limit + 1))))
    assert excinfo.value.status_code == 413


def test_webhook_body_reader_never_uses_the_unbounded_buffering_helper() -> None:
    """Regression guard: `await request.body()` would re-open the DoS vector."""
    from app import webhooks

    handler_code = _code_only(inspect.getsource(webhooks.github_webhook))
    assert "request . body ( )" not in handler_code
    assert "_read_limited_body ( request )" in handler_code

    reader_code = _code_only(inspect.getsource(webhooks._read_limited_body))
    assert "request . stream ( )" in reader_code
    assert "request . body ( )" not in reader_code
    # The HMAC covers the collected raw bytes and is checked before any parsing.
    assert handler_code.index("verify_github_signature (") < handler_code.index(
        "json . loads ("
    )
