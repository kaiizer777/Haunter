"""Regression reproductions for the GitHub publish layer of the code-review pipeline.

Scope: ``app.services.review_orchestrator`` -> ``app.github_client`` (PR review /
commit comment), plus the swallowed publish result in ``app.services.audit_pipeline``.

Each test encodes ONE intended invariant and currently FAILS against the unfixed
code. Those failures are the deliverable -- they are the reproduction. Do not
weaken them, skip them, or edit ``app/`` to turn them green.

Reference implementation (the auditor path, already correct):
``app.github.audit_publisher.validate_finding_coordinates`` /
``build_inline_review_comments`` validate every inline coordinate against
``app.subagents.auditor.build_diff_grounding`` before publishing, and
``app.llm.prompts.audit_prompts`` bounds every rendered field. The code-review
path shares none of that today.

Harness: ``tests/fake_audit_db`` (the repo's in-process ``AsyncSession``
stand-in), the ``deny_external_http`` guard from
``tests/test_github_audit_publisher.py``, and the ``patch`` targets used by
``tests/test_code_review_webhook.py``. No network, no real Postgres, no real LLM.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.github.audit_publisher import PublishResult
from app.github_client import GitHubAuthError
from app.llm.prompts.audit_prompts import (
    MAX_CATEGORY_CHARS,
    MAX_INLINE_FIELD_CHARS,
    MAX_REPORT_CHARS,
    MAX_SUGGESTED_FIX_CHARS,
    MAX_TITLE_CHARS,
)
from app.models import CodeReview, Repo, User
from app.services import audit_pipeline
from app.services import review_orchestrator as review_orch
from app.services.review_orchestrator import run_code_review_pipeline
from app.subagents.auditor import AuditResult, build_diff_grounding
from app.subagents.code_reviewer import ReviewFinding, format_github_suggestion

# `audit_store` and `fake_audit_db` are pytest fixtures; importing them here is
# what registers them for this module.
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    FakeSessionMaker,
    audit_store,
    fake_audit_db,
)

# ---------------------------------------------------------------------------
# Length budgets -- every bound below is read from the repo, never invented here
# ---------------------------------------------------------------------------
#   MAX_REPORT_CHARS          app/llm/prompts/audit_prompts.py:29 -- enforced on
#                             the auditor report by `_bound_report` (:560-565),
#                             which `audit_publisher` then POSTs (:323).
#   MAX_TITLE_CHARS           app/llm/prompts/audit_prompts.py:32
#   MAX_CATEGORY_CHARS        app/llm/prompts/audit_prompts.py:33
#   MAX_INLINE_FIELD_CHARS    app/llm/prompts/audit_prompts.py:31
#   MAX_SUGGESTED_FIX_CHARS   app/llm/prompts/audit_prompts.py:34
#                             -- all four are enforced on every rendered inline
#                             field by `format_inline_comment_body`
#                             (audit_publisher.py:122-148).
#
# They are repo-sourced, not community-sourced, so importing them is correct.
#
# What is NOT repo-sourced is the markup allowance: no repo constant covers the
# literal fences/headers/emoji a rendered comment wraps around those fields. It
# is pure formatting overhead, so it is declared as a named constant here rather
# than smuggled into an assertion as a magic number.

#: Markup overhead (```suggestion fences, "### heading", "**Category:** ...",
#: the finding-id line) allowed on top of the summed payload bounds for one
#: rendered inline comment body.
MARKUP_OVERHEAD_CHARS = 1_000

#: Total budget for one rendered inline review comment.
INLINE_BODY_BUDGET = (
    MAX_TITLE_CHARS
    + MAX_CATEGORY_CHARS
    + MAX_INLINE_FIELD_CHARS
    + MAX_SUGGESTED_FIX_CHARS
    + MARKUP_OVERHEAD_CHARS
)

#: Length of the verbatim run used to prove truncation actually happened. Chosen
#: larger than every single payload bound above, so "this run is absent from the
#: body" cannot be satisfied by a renderer that only partially bounds its fields.
UNBOUNDED_RUN = 10_000

STALE_SHA = "1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
LIVE_HEAD_SHA = "2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b"
BASE_SHA = "3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c"

PR_NUMBER = 77

#: Captured before any `patch("...httpx.AsyncClient", ...)` runs, so a factory
#: that has to build a real client around a MockTransport cannot call itself.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this module may reach a real GitHub."""
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(self._transport, (httpx.ASGITransport, httpx.MockTransport)):
            return await original_send(self, request, **kwargs)
        raise AssertionError(
            f"external HTTP blocked in publish-layer tests: {request.url.host}"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)


@pytest.fixture(autouse=True)
def hermetic_review_db(
    audit_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeSessionMaker:
    """Route `run_code_review_pipeline`'s session to the in-process store.

    `review_orchestrator` imports `async_session_maker` at module scope, so the
    shared `fake_audit_db` fixture does not reach it; point that one attribute at
    the same store and no test in this module opens a Postgres connection.
    """
    maker = FakeSessionMaker(audit_store)
    monkeypatch.setattr(review_orch, "async_session_maker", maker, raising=False)
    return maker


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

#: A well-formed unified diff whose hunk headers exactly match their bodies, so
#: `build_diff_grounding` accepts both hunks. `app.py`'s hunk covers post-image
#: lines 8-13 and `other.py`'s covers 39-42; line 500 is outside every hunk of
#: either.
SAMPLE_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -8,3 +8,6 @@ def handler(request):
     ctx = build_context(request)
+    user_id = request.args.get("uid")
+    if not user_id:
+        raise ValueError("uid required")
     trace(ctx)
     return render(ctx)
diff --git a/other.py b/other.py
--- a/other.py
+++ b/other.py
@@ -39,2 +39,4 @@ def compute(values):
     total = 0
+    for v in values:
+        total += v
     return total
"""


def _finding_payload(
    *,
    file_path: str = "app.py",
    line_start: int = 10,
    line_end: int = 10,
    critique: str = "Unvalidated uid reaches the render path unchecked.",
    suggested_patch: str | None = "uid = str(request.args['uid'])",
    severity: str = "high",
    category: str = "security",
) -> dict[str, Any]:
    return {
        "file_path": file_path,
        "line_start": line_start,
        "line_end": line_end,
        "category": category,
        "severity": severity,
        "critique": critique,
        "suggested_patch": suggested_patch,
    }


def _llm_response(
    *,
    risk_score: int = 85,
    summary: str = "Unchecked user input reaches the render path.",
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "risk_score": risk_score,
                "summary": summary,
                "findings": findings if findings is not None else [],
            }
        ),
        "usage": {"input_tokens": 300, "output_tokens": 120},
    }


def _llm_patch_target(response: dict[str, Any]) -> MagicMock:
    """Patch target for `app.subagents.code_reviewer.LLMClient` (repo style)."""
    mock_cls = MagicMock()
    mock_cls.return_value.complete = AsyncMock(return_value=response)
    return mock_cls


def _install_token_patch() -> Any:
    return patch(
        "app.services.review_orchestrator.get_installation_token",
        new_callable=AsyncMock,
        return_value="mock-token",
    )


@pytest.fixture
async def pending_review(fake_audit_db: FakeAsyncSession) -> CodeReview:
    """A repo plus one pending PR review, ready for `run_code_review_pipeline`.

    The `repo` relationship is assigned explicitly because the pipeline reads it
    off the row after a `selectinload` the in-process store does not emulate.
    """
    user = User(
        github_id=555000111,
        github_username="publish-grounding-tester",
        role="user",
    )
    fake_audit_db.add(user)
    await fake_audit_db.commit()

    repo = Repo(
        user_id=user.id,
        owner="grounding-org",
        name="grounding-repo",
        default_branch="main",
    )
    fake_audit_db.add(repo)
    await fake_audit_db.commit()

    review = CodeReview(
        repo_id=repo.id,
        commit_sha=STALE_SHA,
        pr_number=PR_NUMBER,
        risk_score=0,
        summary="Pending review",
        findings=[],
        status="pending",
    )
    review.repo = repo
    fake_audit_db.add(review)
    await fake_audit_db.commit()
    return review


def _published(mock_pr_review: AsyncMock) -> list[dict[str, Any]]:
    return [call.kwargs for call in mock_pr_review.call_args_list]


# ---------------------------------------------------------------------------
# B1 - inline comments are never validated against the diff
# ---------------------------------------------------------------------------
# `ReviewFinding.file_path` is `min_length=1` and `line_start` / `line_end` are
# `ge=1` with no upper bound (app/subagents/code_reviewer.py:45-53); only
# `line_end < line_start` is normalised (:65-69). review_orchestrator.py:303-316
# then ships whatever the model produced, and github_client.py:1119-1131 posts
# the body plus every comment in ONE atomic request -- so a single invalid
# coordinate discards every valid comment with it.
#
# Both shapes below are accepted by `ReviewFinding` and would be rejected by
# GitHub with 422.

_BAD_COORDINATES = [
    pytest.param(
        _finding_payload(
            file_path="not_in_the_diff.py",
            line_start=4,
            line_end=4,
            critique="This file is not part of the analysed diff at all.",
            suggested_patch=None,
        ),
        id="file-not-in-diff",
    ),
    pytest.param(
        _finding_payload(
            file_path="app.py",
            line_start=500,
            line_end=500,
            critique="Line 500 sits outside every hunk of the analysed diff.",
            suggested_patch=None,
        ),
        id="line-outside-every-hunk",
    ),
]


@pytest.mark.parametrize("bad_finding", _BAD_COORDINATES)
async def test_b1_ungrounded_inline_finding_does_not_discard_valid_findings(
    pending_review: CodeReview,
    bad_finding: dict[str, Any],
) -> None:
    """B1: one bad coordinate must not take the whole review down with it.

    Intended: every inline comment handed to GitHub is anchored to a real
    post-image line of the diff that was actually analysed, the valid finding is
    still delivered, and the review is published in a single attempt rather than
    being discarded and rewritten summary-only.
    """
    grounding = build_diff_grounding(SAMPLE_DIFF)
    valid = _finding_payload(line_start=10, line_end=10)

    mock_pr_review = AsyncMock(return_value={"id": 9001})
    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(_llm_response(findings=[bad_finding, valid])),
        ),
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            mock_pr_review,
        ),
        _install_token_patch(),
    ):
        await run_code_review_pipeline(pending_review.id)

    publishes = _published(mock_pr_review)
    assert len(publishes) == 1, (
        "the review should be published once -- an ungrounded coordinate must be "
        f"dropped, not turned into a second summary-only attempt (saw "
        f"{len(publishes)} attempts)"
    )

    comments = publishes[0]["comments"]
    assert comments, "the valid finding must still be delivered inline"

    for comment in comments:
        path = comment["path"]
        line = comment["line"]
        assert path in grounding.line_index, (
            f"inline comment targets {path!r}, which has no line in the analysed "
            f"diff; groundable paths are {sorted(grounding.line_index)}"
        )
        assert line in grounding.line_index[path], (
            f"inline comment targets {path}:{line}, outside every hunk; groundable "
            f"lines are {sorted(grounding.line_index[path])}"
        )
        if "start_line" in comment:
            assert comment["start_line"] in grounding.line_index[path]

    assert any(c["path"] == "app.py" for c in comments)


# ---------------------------------------------------------------------------
# B2 - unbounded bodies, and a publish failure absorbed into "completed"
# ---------------------------------------------------------------------------


async def test_b2_commit_comment_body_sent_to_github_is_bounded(
    pending_review: CodeReview,
) -> None:
    """B2: the push-level commit comment body must be length-bounded.

    Intended: no model-authored text is shipped to GitHub verbatim when it is
    arbitrarily large -- the body is truncated to the repo's own report budget.
    `ReviewFinding.critique` / `suggested_patch` carry no `max_length`
    (code_reviewer.py:56-63) and review_orchestrator.py:414-429 concatenates
    every one of them with no bound at all.
    """
    review = pending_review
    review.pr_number = None

    mock_commit_comment = AsyncMock(return_value={"id": 4242})
    with (
        patch(
            "app.services.review_orchestrator.fetch_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(
                _llm_response(
                    risk_score=40,
                    findings=[
                        _finding_payload(
                            critique="c" * 400_000,
                            suggested_patch="p" * 400_000,
                        )
                    ],
                )
            ),
        ),
        patch(
            "app.services.review_orchestrator.create_commit_comment",
            mock_commit_comment,
        ),
        _install_token_patch(),
    ):
        await run_code_review_pipeline(review.id)

    mock_commit_comment.assert_awaited_once()
    body = mock_commit_comment.call_args.kwargs["body"]

    assert len(body) <= MAX_REPORT_CHARS, (
        f"commit comment body is {len(body)} chars, above the repo's own report "
        f"bound of {MAX_REPORT_CHARS} (audit_prompts.py:29)"
    )
    assert "c" * UNBOUNDED_RUN not in body, (
        "an unbounded critique was shipped to GitHub verbatim"
    )
    assert "p" * UNBOUNDED_RUN not in body, (
        "an unbounded suggested_patch was shipped to GitHub verbatim"
    )


def test_b2_rendered_inline_comment_body_is_bounded() -> None:
    """B2: `format_github_suggestion` must bound what it renders.

    Intended: the rendered inline comment body is bounded by the repo's own
    inline-field bounds. `format_github_suggestion` (code_reviewer.py:120-141)
    concatenates `critique` and `suggested_patch` with no cap, so it inherits
    whatever length the model emitted.
    """
    small = ReviewFinding(
        file_path="app.py",
        line_start=10,
        line_end=10,
        category="security",
        severity="high",
        critique="Unvalidated uid reaches the render path unchecked.",
        suggested_patch="uid = str(request.args['uid'])",
    )
    huge = ReviewFinding(
        file_path="app.py",
        line_start=10,
        line_end=10,
        category="security",
        severity="high",
        critique="c" * 400_000,
        suggested_patch="p" * 400_000,
    )

    body = format_github_suggestion(huge)

    assert len(body) <= INLINE_BODY_BUDGET, (
        f"inline comment body is {len(body)} chars, above the repo's own inline "
        f"bounds ({MAX_TITLE_CHARS}+{MAX_CATEGORY_CHARS}+{MAX_INLINE_FIELD_CHARS}"
        f"+{MAX_SUGGESTED_FIX_CHARS} payload chars, plus "
        f"{MARKUP_OVERHEAD_CHARS} markup overhead)"
    )
    assert "c" * UNBOUNDED_RUN not in body, "unbounded critique rendered verbatim"
    assert "p" * UNBOUNDED_RUN not in body, (
        "unbounded suggested_patch rendered verbatim"
    )
    # The bounded body must still be a usable comment, not an empty one.
    assert "```suggestion" in format_github_suggestion(small)


async def test_b2_publish_failure_is_never_recorded_as_completed() -> None:
    """B2: a failed publish must not be recorded as a completed audit job.

    `publish_audit_review` converts every GitHub failure into a returned
    `PublishResult(status="error")` (audit_publisher.py:391-406).
    `execute_audit_job` discards that return value (audit_pipeline.py:855), raises
    nothing, falls through to :870 and reports `status="completed"`.
    """
    repo = cast(Any, SimpleNamespace(id="repo-1", owner="octocat", name="hello-world"))
    target = audit_pipeline.AuditTarget(base_sha=BASE_SHA, head_sha=LIVE_HEAD_SHA)

    failed = PublishResult(
        published=False,
        status="error",
        target_type="pull_request_review",
        error="GitHubClientError: GitHub API returned error 422: body too long",
    )
    mock_publish = AsyncMock(return_value=failed)

    with (
        patch(
            "app.services.audit_pipeline._resolve_audit_target",
            new_callable=AsyncMock,
            return_value=target,
        ),
        patch(
            "app.services.audit_pipeline._fetch_pr_target",
            new_callable=AsyncMock,
            return_value=target,
        ),
        patch(
            "app.subagents.auditor.fetch_audit_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ),
        patch(
            "app.subagents.auditor.fetch_audit_source_contexts",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.subagents.auditor.build_ast_diff_summary_async",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            return_value=_minimal_audit_result(),
        ),
        patch("app.github.audit_publisher.publish_audit_review", mock_publish),
    ):
        outcome = await audit_pipeline.execute_audit_job(
            audit_id="audit-1234567890ab",
            audit_type="pr_audit",
            repo=repo,
            ref="main",
            pr_number=42,
            base_sha=BASE_SHA,
            head_sha=LIVE_HEAD_SHA,
            workflow_run_id=None,
            token="test-token",
        )

    mock_publish.assert_awaited_once()
    assert outcome.status != "completed", (
        "the GitHub publish failed (PublishResult.status='error') yet the audit "
        "job was recorded as 'completed' -- audit_pipeline.py:855 discards the "
        "publisher's return value, so the failure is invisible downstream"
    )


def _minimal_audit_result() -> AuditResult:
    """A real `AuditResult` with no findings -- only its identity matters here."""
    return AuditResult(
        audit_id="audit-1234567890ab",
        audit_type="pr_audit",
        repo_full_name="octocat/hello-world",
        target_label="Commit `a8f3b21` / PR `#42`",
        engine="nemotron-3.5-lightning",
        executive_summary="Nothing actionable found.",
        findings=[],
        confidence=90,
        status="OK",
        report_markdown="## report",
        publish_allowed=True,
    )


# ---------------------------------------------------------------------------
# B3 - the review is published against a commit whose diff was not analysed
# ---------------------------------------------------------------------------


async def test_b3_published_commit_matches_the_diff_that_was_analysed(
    pending_review: CodeReview,
) -> None:
    """B3: a push landing between enqueue and publish must not be published blind.

    `webhooks.py:1201` pins `commit_sha` at enqueue; review_orchestrator.py:191
    fetches the PR diff live at execution time and then publishes against the
    stored `commit_sha` (:331). When the head moves in between, the review is
    attached to a commit nobody analysed. The auditor path re-resolves the head
    before publishing (audit_pipeline.py:680-684, :801-808).

    Intended: the published `commit_id` is the head whose diff was analysed.
    Refusing to publish is also acceptable (it is the auditor's own behaviour),
    but only when the refusal is recorded as a failure rather than a success.
    """
    review = pending_review

    # The PR head moved after the webhook captured STALE_SHA. This is the only
    # API that reports the live head, so it is the one the pipeline must consult.
    live_pr_metadata = {
        "number": review.pr_number,
        "head": {"ref": "feature/hotfix", "sha": LIVE_HEAD_SHA},
        "base": {"ref": "main", "sha": BASE_SHA},
    }

    analysed: list[str] = []

    async def _diff_for_head(**kwargs: Any) -> str:
        analysed.append(LIVE_HEAD_SHA)
        return SAMPLE_DIFF

    mock_pr_review = AsyncMock(return_value={"id": 7007})
    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            side_effect=_diff_for_head,
        ),
        # Patched in both namespaces so the assertion holds whichever import
        # style the fix uses: `from app import github_client` + attribute
        # access, or a direct `from app.github_client import ...` binding.
        patch.object(
            review_orch,
            "fetch_pull_request",
            new_callable=AsyncMock,
            return_value=live_pr_metadata,
            create=True,
        ),
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            return_value=live_pr_metadata,
        ),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(_llm_response(risk_score=30)),
        ),
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            mock_pr_review,
        ),
        _install_token_patch(),
    ):
        await run_code_review_pipeline(review.id)

    assert analysed, "the PR diff was never fetched"

    publishes = _published(mock_pr_review)
    if publishes:
        published_sha = publishes[0]["commit_sha"]
        assert published_sha == LIVE_HEAD_SHA, (
            f"review was published against commit_id={published_sha!r}, but the "
            f"diff that was analysed belongs to the PR's live head "
            f"{LIVE_HEAD_SHA!r}; the enqueue-time SHA {STALE_SHA!r} is stale"
        )
    else:
        assert review.status != "completed", (
            "the pipeline neither published the review nor recorded a failure; "
            f"the run ended as 'completed' with status={review.status!r}"
        )
        assert review.failure_reason, (
            "the pipeline refused to publish but left no failure_reason to say why"
        )


# ---------------------------------------------------------------------------
# B4 - the 422 check is computed then ignored; the retry is unconditional
# ---------------------------------------------------------------------------


async def test_b4_non_422_failure_does_not_trigger_a_second_publish_attempt(
    pending_review: CodeReview,
) -> None:
    """B4: an auth failure must not be retried as if it were an invalid-coordinate 422.

    review_orchestrator.py:356 computes `is_422 = "422" in str(gh_err)` and uses
    it only in a log line; the summary-only retry at :376 fires for every
    exception. A substring match on an error string is unsound in both
    directions -- an auth, rate-limit or network failure burns a second API call
    that cannot succeed either.
    """
    review = pending_review

    attempts: list[dict[str, Any]] = []

    async def _always_unauthorized(**kwargs: Any) -> dict[str, Any]:
        attempts.append(kwargs)
        raise GitHubAuthError("GitHub authentication failure (401)")

    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(_llm_response(findings=[_finding_payload()])),
        ),
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            side_effect=_always_unauthorized,
        ),
        _install_token_patch(),
    ):
        await run_code_review_pipeline(review.id)

    assert len(attempts) == 1, (
        f"a non-422 GitHubAuthError triggered {len(attempts)} publish attempts; "
        "the summary-only fallback must fire only for an invalid-coordinate 422"
    )
    assert review.status == "error", (
        "an auth failure that could not publish must not be recorded as "
        f"'completed' (got {review.status!r})"
    )


async def test_b4_fallback_summary_body_is_bounded(
    pending_review: CodeReview,
) -> None:
    """B4: the summary-only fallback body must be length-bounded.

    The fallback (review_orchestrator.py:368-374) concatenates every finding's
    `critique` and `suggested_patch` with no cap. When the first attempt failed
    because the body was too large, the retry sends the same unbounded content
    with the inline comments removed -- so it can 422 a second time and the run
    ends as `status="error"` (:406-409).
    """
    review = pending_review

    attempts: list[dict[str, Any]] = []

    async def _reject_inline_then_accept(**kwargs: Any) -> dict[str, Any]:
        attempts.append(kwargs)
        if kwargs.get("comments"):
            raise Exception("GitHub API returned error 422: Review body is too long")
        return {"id": 8008}

    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(
                _llm_response(
                    findings=[
                        _finding_payload(
                            critique="c" * 400_000,
                            suggested_patch="p" * 400_000,
                        )
                    ]
                )
            ),
        ),
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            side_effect=_reject_inline_then_accept,
        ),
        _install_token_patch(),
    ):
        await run_code_review_pipeline(review.id)

    assert len(attempts) == 2, (
        "expected exactly the inline attempt plus one summary-only retry, got "
        f"{len(attempts)} attempts"
    )
    fallback = attempts[1]
    assert fallback["comments"] == [], "the retry must be summary-only"

    body = fallback["body"]
    assert len(body) <= MAX_REPORT_CHARS, (
        f"fallback body is {len(body)} chars, above the repo's own report bound "
        f"of {MAX_REPORT_CHARS} (audit_prompts.py:29)"
    )
    assert "c" * UNBOUNDED_RUN not in body, (
        "the summary-only fallback shipped an unbounded critique verbatim"
    )
    assert "p" * UNBOUNDED_RUN not in body, (
        "the summary-only fallback shipped an unbounded suggested_patch verbatim"
    )


# ---------------------------------------------------------------------------
# B6 - nothing resolves or supersedes prior findings when new commits land
# ---------------------------------------------------------------------------
# Verified absent from the codebase: GraphQL `resolveReviewThread`,
# `GET /pulls/{n}/files`, any `checks.*` or `repos.statuses.*` write. The only
# GraphQL call in the repo is `fetch_blame` (github_client.py:1390), and
# `app.services.review_orchestrator` never reads back
# `GET /pulls/{n}/comments` either. `post_commit_comment` still exposes the
# closing-down `position` parameter (github_client.py:547-553) that no caller
# passes -- a dormant `position`-vs-`line`/`side` surface, low severity, not
# exercised here.
#
# Thread ids below are real `PullRequestReviewThread` node ids (`PRRT_`).
# `PRRC_` is a `PullRequestReviewComment` node id -- a different GraphQL type,
# and one `resolveReviewThread` rejects -- so the previous revision of this
# fixture, which exposed only a `PRRC_` id from
# `GET /pulls/{n}/comments`, could not have produced a resolvable thread at all.

#: The account the App's installation token authenticates as, as `GET /user`
#: reports it. Discovered at runtime by `fetch_bot_identity`; nothing in the
#: pipeline configures a bot slug.
HAUNTER_BOT_LOGIN = "haunter-ci[bot]"

HAUNTER_OUTDATED_THREAD_ID = "PRRT_kwDOAHRlc3RhbGUtdGhyZWFkLW91dGRhdGVkLTE"
HAUNTER_CURRENT_THREAD_ID = "PRRT_kwDOAHRlc3RhbGUtdGhyZWFkLWN1cnJlbnQtdHdv"
HAUNTER_RESOLVED_THREAD_ID = "PRRT_kwDOAHRlc3RhbGUtdGhyZWFkLXJlc29sdmVkLTAx"
DEPENDABOT_OUTDATED_THREAD_ID = "PRRT_kwDOAHRlc3RhbGUtdGhyZWFkLW5vdC1vdXJzLTE"


def _review_thread(
    *,
    thread_id: str,
    author_login: str,
    is_resolved: bool = False,
    is_outdated: bool = False,
    path: str = "app.py",
    line: int | None = None,
    original_line: int | None = None,
) -> dict[str, Any]:
    """One `PullRequestReviewThread` node in the shape `fetch_review_threads` reads."""
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "isOutdated": is_outdated,
        "isCollapsed": False,
        "path": path,
        "line": line,
        "originalLine": original_line,
        "comments": {
            "nodes": [
                {
                    "databaseId": 90001,
                    "author": {"login": author_login},
                }
            ]
        },
    }


def _review_threads_payload() -> dict[str, Any]:
    """The `reviewThreads` connection `fetch_review_threads` walks.

    One node per branch of the selection predicate, so the assertion can be
    strict about all four outcomes rather than only the one it wants:

    1. ours + addressed (`isOutdated`) -> must be resolved;
    2. ours + still anchored to a line of the new diff -> must stay open;
    3. ours + already resolved -> must be left alone;
    4. another bot's + addressed -> must stay open, or the pipeline is closing
       other people's comments.
    """
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            _review_thread(
                                thread_id=HAUNTER_OUTDATED_THREAD_ID,
                                author_login=HAUNTER_BOT_LOGIN,
                                is_outdated=True,
                                original_line=10,
                            ),
                            _review_thread(
                                thread_id=HAUNTER_CURRENT_THREAD_ID,
                                author_login=HAUNTER_BOT_LOGIN,
                                line=10,
                            ),
                            _review_thread(
                                thread_id=HAUNTER_RESOLVED_THREAD_ID,
                                author_login=HAUNTER_BOT_LOGIN,
                                is_resolved=True,
                                is_outdated=True,
                            ),
                            _review_thread(
                                thread_id=DEPENDABOT_OUTDATED_THREAD_ID,
                                author_login="dependabot[bot]",
                                is_outdated=True,
                            ),
                        ],
                    }
                }
            }
        }
    }


async def test_b6_rereview_after_new_commits_supersedes_prior_findings(
    pending_review: CodeReview,
) -> None:
    """B6: a re-review must address the findings the previous review left open.

    GitHub's only machine-actionable way to close an existing review thread is
    the GraphQL `resolveReviewThread` mutation, so that is the mechanism this
    test observes. A re-review that publishes fresh findings while leaving the
    previous run's now-addressed threads open and indistinguishable from current
    ones is the defect.
    """
    review = pending_review
    # Re-review: the PR head has already moved on from the first review.
    review.commit_sha = LIVE_HEAD_SHA

    recorded: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        url = str(request.url)
        if url.endswith("/graphql"):
            # Discriminate on the request body: the thread *query* and the
            # resolve *mutation* share this one endpoint. Answering both with
            # the mutation's shape is what made the thread list unparseable --
            # `fetch_review_threads` read `data.repository.pullRequest
            # .reviewThreads` out of a `{"resolveReviewThread": ...}` body,
            # hit KeyError, and returned [].
            body = json.loads(request.content)
            query = str(body.get("query") or "")
            if "resolveReviewThread" in query:
                thread_id = (body.get("variables") or {}).get("threadId")
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "resolveReviewThread": {
                                "thread": {"id": thread_id, "isResolved": True}
                            }
                        }
                    },
                )
            return httpx.Response(200, json=_review_threads_payload())
        if url.endswith("/user"):
            # The credential's own identity, discovered rather than configured.
            return httpx.Response(
                200,
                json={"login": HAUNTER_BOT_LOGIN, "type": "Bot", "id": 42},
            )
        if f"/pulls/{PR_NUMBER}/reviews" in url:
            return httpx.Response(200, json={"id": 1, "state": "COMMENTED"})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(_handler)

    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(_llm_response(findings=[_finding_payload()])),
        ),
        _install_token_patch(),
        patch(
            "app.github_client.httpx.AsyncClient",
            side_effect=lambda *a, **k: _REAL_ASYNC_CLIENT(transport=transport),
        ),
    ):
        await run_code_review_pipeline(review.id)

    # Guard against vacuity: the wire really was exercised, and the review really
    # was published. Without this the assertion below would also hold if the
    # publish had silently never happened.
    publish_calls = [
        request
        for request in recorded
        if request.method == "POST"
        and f"/pulls/{PR_NUMBER}/reviews" in str(request.url)
    ]
    assert publish_calls, (
        "the re-review never reached GitHub, so this test would prove nothing"
    )

    identity_calls = [
        request
        for request in recorded
        if request.method == "GET" and str(request.url).endswith("/user")
    ]
    assert identity_calls, (
        "the bot identity was never discovered from GET /user; which threads are "
        "ours cannot be answered by a login suffix heuristic, because that would "
        "also match Dependabot and every other bot on the PR"
    )

    resolve_calls = [
        request
        for request in recorded
        if request.method == "POST"
        and str(request.url).endswith("/graphql")
        and b"resolveReviewThread" in request.content
    ]
    assert resolve_calls, (
        "the re-review after new commits published findings without touching the "
        "previously posted review threads: no GraphQL resolveReviewThread "
        "mutation was sent, so superseded findings stay open and "
        "indistinguishable from current ones"
    )

    resolved_ids = [
        json.loads(request.content)["variables"]["threadId"]
        for request in resolve_calls
    ]
    assert resolved_ids == [HAUNTER_OUTDATED_THREAD_ID], (
        "expected exactly the Haunter-authored thread the new diff has "
        f"addressed ({HAUNTER_OUTDATED_THREAD_ID}), got {resolved_ids}; a thread "
        f"still anchored to the new diff ({HAUNTER_CURRENT_THREAD_ID}), an "
        "already-resolved one, or another bot's "
        f"({DEPENDABOT_OUTDATED_THREAD_ID}) must all be left alone"
    )
    assert not [thread_id for thread_id in resolved_ids if "PRRC_" in thread_id], (
        "a PullRequestReviewComment node id (PRRC_) was passed to "
        "resolveReviewThread; only a PullRequestReviewThread id (PRRT_) "
        "resolves, so this would have been a silent no-op in production"
    )
