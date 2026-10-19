"""Safety regressions for thread resolution in the code-review pipeline.

Scope: ``app.services.review_orchestrator`` only.

Every test here encodes one invariant about the *superseded-thread* step and the
finding-suppression accounting that guards it. The defect class is "Haunter closes
a reviewer's open finding without evidence it read it":

* ``DiffGrounding.line_index`` is parsed from a prefix of the diff (bounded at
  ``AUDIT_MAX_DIFF_CHARS`` = 60_000 by ``build_diff_grounding``), while
  ``fetch_pull_request_diff`` returns up to ``MAX_TEXT_RESPONSE_BYTES`` (2 MB) and
  ``code_reviewer._build_review_messages`` puts the *whole* diff in the prompt. So
  the grounding can be an arbitrarily incomplete view of what the model actually
  read, and "this path is not in the index" is not evidence that a finding on it
  was fixed — it is evidence that we never saw the file.
* the commit-diff fallback and an empty diff body make the same class of mistake
  worse: the first is a strict subset of the PR diff, the second indexes nothing.
* a run that published zero inline comments has replaced nothing, so closing the
  previous run's threads cannot be justified by this run's output.

Harness: ``tests/fake_audit_db`` (the repo's in-process ``AsyncSession``
stand-in) plus the ``patch`` targets used by ``test_review_publish_grounding``.
No network, no real Postgres, no real LLM.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, AsyncIterator, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.llm.prompts.audit_prompts import MAX_GITHUB_COMMENT_CHARS
from app.models import CodeReview, Repo, User
from app.services import review_orchestrator as review_orch
from app.services.review_orchestrator import (
    _select_addressed_thread_ids,
    _thread_is_addressed,
    run_code_review_pipeline,
)
from app.subagents.auditor import AUDIT_MAX_DIFF_CHARS, build_diff_grounding

# `audit_store` and `fake_audit_db` are pytest fixtures; importing them here is
# what registers them for this module.
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    FakeSessionMaker,
    audit_store,
    fake_audit_db,
)

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

STALE_SHA = "1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
LIVE_HEAD_SHA = "2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
PR_NUMBER = 501

HAUNTER_BOT_LOGIN = "haunter-ci[bot]"
DEPENDABOT_LOGIN = "dependabot[bot]"


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this module may reach a real GitHub."""
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(self._transport, (httpx.ASGITransport, httpx.MockTransport)):
            return await original_send(self, request, **kwargs)
        raise AssertionError(
            f"external HTTP blocked in thread-safety tests: {request.url.host}"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)


@pytest.fixture(autouse=True)
def hermetic_review_db(
    audit_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeSessionMaker:
    """Route `run_code_review_pipeline`'s session to the in-process store.

    `review_orchestrator` imports `async_session_maker` at module scope, so the
    shared `fake_audit_db` fixture does not reach it.
    """
    maker = FakeSessionMaker(audit_store)
    monkeypatch.setattr(review_orch, "async_session_maker", maker, raising=False)
    return maker


@pytest.fixture
async def pending_review(fake_audit_db: FakeAsyncSession) -> CodeReview:
    user = User(
        github_id=555000999,
        github_username="thread-safety-tester",
        role="user",
    )
    fake_audit_db.add(user)
    await fake_audit_db.commit()

    repo = Repo(
        user_id=user.id,
        owner="safety-org",
        name="safety-repo",
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


def file_diff(name: str, *, added: int = 4) -> str:
    """One file, one satisfied hunk: 1 context line + ``added`` added lines.

    ``build_diff_grounding`` only folds a hunk in when the body exactly satisfies
    the header's declared counts, so a synthetic file has to declare what it
    delivers or it is reported as rejected rather than indexed.
    """
    return (
        f"diff --git a/{name} b/{name}\n"
        f"--- a/{name}\n"
        f"+++ b/{name}\n"
        f"@@ -1,1 +1,{added + 1} @@\n"
        " ctx\n"
        + "".join(f"+line{index}\n" for index in range(1, added + 1))
    )


#: A diff large enough that `build_diff_grounding` clips it at
#: `AUDIT_MAX_DIFF_CHARS` — i.e. everything in `OVERSIZED_TAIL_FILE` is invisible
#: to the grounding while still inside the body the model was shown.
OVERSIZED_HEAD_FILE = "a_head.py"
OVERSIZED_TAIL_FILE = "z_tail.py"
OVERSIZED_DIFF = file_diff(OVERSIZED_HEAD_FILE) + file_diff(
    OVERSIZED_TAIL_FILE, added=AUDIT_MAX_DIFF_CHARS
)


def _finding_payload(
    *,
    file_path: str = "app.py",
    line_start: int = 2,
    line_end: int = 2,
    critique: str = "Unchecked value reaches the render path.",
    suggested_patch: str | None = "value = int(value)",
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
    summary: str = "Something reaches the render path unchecked.",
    findings: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "risk_score": risk_score,
                "summary": summary,
                "findings": list(findings) if findings is not None else [],
            }
        ),
        "usage": {"input_tokens": 300, "output_tokens": 120},
    }


def _llm_patch_target(response: dict[str, Any]) -> MagicMock:
    mock_cls = MagicMock()
    mock_cls.return_value.complete = AsyncMock(return_value=response)
    return mock_cls


def _review_thread(
    *,
    thread_id: str,
    author_login: str = HAUNTER_BOT_LOGIN,
    is_resolved: bool = False,
    is_outdated: bool = False,
    path: str = "app.py",
    line: int | None = 2,
) -> dict[str, Any]:
    """One `PullRequestReviewThread` node in the shape `fetch_review_threads` reads."""
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "isOutdated": is_outdated,
        "isCollapsed": False,
        "path": path,
        "line": line,
        "originalLine": line,
        "comments": {"nodes": [{"databaseId": 90001, "author": {"login": author_login}}]},
    }


@contextlib.asynccontextmanager
async def _run_pr_review(
    review_id: Any,
    *,
    findings: Sequence[dict[str, Any]],
    threads: Sequence[dict[str, Any]] = (),
    pr_diff: str | None = None,
    commit_diff: str | None = None,
    summary: str | None = None,
    live_head_error: BaseException | None = None,
    publish_error: BaseException | None = None,
    resolution_error: BaseException | None = None,
) -> AsyncIterator[dict[str, AsyncMock]]:
    """Drive one PR review run; yield the mocks the assertions read.

    ``pr_diff=None`` means "the PR diff fetch raises and the pipeline falls back
    to the commit diff" — the only way to reach that branch.

    ``resolution_error`` replaces ``_resolve_superseded_threads`` wholesale, to
    prove the published state survives a failure of the courtesy step that runs
    after it.
    """
    publish = AsyncMock(return_value={"id": 4242})
    if publish_error is not None:
        publish.side_effect = publish_error
    identity = AsyncMock(return_value=HAUNTER_BOT_LOGIN)
    fetched = AsyncMock(return_value=list(threads))
    resolved = AsyncMock(return_value=0)

    if live_head_error is not None:
        fetch_pull_request: Any = AsyncMock(side_effect=live_head_error)
    else:
        fetch_pull_request = AsyncMock(
            return_value={"number": PR_NUMBER, "head": {"sha": LIVE_HEAD_SHA}}
        )

    if pr_diff is None:
        fetch_pull_request_diff: Any = AsyncMock(
            side_effect=RuntimeError("PR diff unavailable")
        )
    else:
        fetch_pull_request_diff = AsyncMock(return_value=pr_diff)

    patches: list[Any] = [
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request",
            fetch_pull_request,
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            fetch_pull_request_diff,
        ),
        patch(
            "app.services.review_orchestrator.fetch_diff",
            new_callable=AsyncMock,
            return_value=commit_diff if commit_diff is not None else "",
        ),
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            publish,
        ),
        patch("app.services.review_orchestrator.fetch_bot_identity", identity),
        patch("app.services.review_orchestrator.fetch_review_threads", fetched),
        patch("app.services.review_orchestrator.resolve_review_threads", resolved),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(
                _llm_response(
                    summary=summary
                    if summary is not None
                    else "Something reaches the render path unchecked.",
                    findings=list(findings),
                )
            ),
        ),
    ]
    if resolution_error is not None:
        patches.append(
            patch(
                "app.services.review_orchestrator._resolve_superseded_threads",
                AsyncMock(side_effect=resolution_error),
            )
        )

    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        await run_code_review_pipeline(review_id)
        yield {
            "publish": publish,
            "identity": identity,
            "fetched": fetched,
            "resolved": resolved,
        }


async def _commit_comment(
    review_id: Any,
    *,
    findings: Sequence[dict[str, Any]],
    commit_diff: str,
) -> dict[str, AsyncMock]:
    """Drive one push-level (commit comment) run and yield the publish mock."""
    publish = AsyncMock(return_value={"id": 1})
    with (
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
        patch(
            "app.services.review_orchestrator.fetch_diff",
            new_callable=AsyncMock,
            return_value=commit_diff,
        ),
        patch(
            "app.services.review_orchestrator.create_commit_comment",
            publish,
        ),
        patch(
            "app.subagents.code_reviewer.LLMClient",
            _llm_patch_target(_llm_response(findings=list(findings))),
        ),
    ):
        await run_code_review_pipeline(review_id)
    return {"publish": publish}


# ---------------------------------------------------------------------------
# Unit level: the selection predicate itself
# ---------------------------------------------------------------------------


def test_thread_on_path_absent_from_index_is_not_addressed() -> None:
    """An index that never saw the file is not evidence the finding was fixed.

    ``DiffGrounding.line_index`` is a view of a bounded prefix of the diff, so a
    path missing from it means "we did not read this file", not "the code the
    comment pointed at is gone". Treating absence as resolution is what let a
    single re-review close every open finding on a file past the 60_000-char
    clip.
    """
    grounding = build_diff_grounding(file_diff("app.py"))

    assert "never_read.py" not in grounding.line_index, (
        "precondition: the thread's path is not in the grounding index"
    )
    assert not _thread_is_addressed(
        _review_thread(thread_id="PRRT_never_read", path="never_read.py", line=2),
        grounding,
    ), (
        "a thread whose path is absent from the grounding index was reported as "
        "addressed; absence is absence of evidence, not evidence of a fix"
    )


def test_empty_grounding_addresses_nothing() -> None:
    """`build_diff_grounding("")` indexes nothing, so it must address nothing.

    An empty diff body is reachable: the PR diff endpoint can answer 200 with an
    empty body. Under the old rule every Haunter thread on the PR then resolved.
    """
    grounding = build_diff_grounding("")

    assert grounding.line_index == {}, "precondition: an empty diff indexes nothing"
    assert not _thread_is_addressed(
        _review_thread(thread_id="PRRT_empty", path="app.py", line=10),
        grounding,
    ), (
        "an empty grounding resolved a thread; with nothing indexed there is no "
        "evidence about any anchor"
    )
    assert (
        _select_addressed_thread_ids(
            [_review_thread(thread_id=f"PRRT_{i}", line=10 + i) for i in range(3)],
            bot_login=HAUNTER_BOT_LOGIN,
            grounding=grounding,
        )
        == []
    ), "an empty grounding selected threads for resolution"


def test_thread_whose_line_moved_off_an_indexed_path_is_addressed() -> None:
    """Positive evidence: the file is in the diff and the anchored line is not.

    This is the only shape that closes a thread on the grounding alone, and it
    must keep working — the safety rules above must not turn into "never
    resolve".
    """
    grounding = build_diff_grounding(file_diff("app.py", added=4))

    assert sorted(grounding.line_index["app.py"]) == [1, 2, 3, 4, 5]
    assert _thread_is_addressed(
        _review_thread(thread_id="PRRT_moved", path="app.py", line=99),
        grounding,
    ), (
        "the file is present in the diff and the thread's line is not among its "
        "groundable lines, yet the thread was not reported as addressed"
    )
    assert not _thread_is_addressed(
        _review_thread(thread_id="PRRT_still_here", path="app.py", line=2),
        grounding,
    ), "a thread still anchored to a line of the new diff must stay open"


def test_dependabot_thread_is_never_selected() -> None:
    """Author match is on the discovered bot login, never a ``[bot]`` suffix.

    Guard against the safety rules below ever being loosened into "resolve
    anything that looks stale": another account's thread is not ours to close.
    """
    grounding = build_diff_grounding(file_diff("app.py"))

    selected = _select_addressed_thread_ids(
        [
            _review_thread(
                thread_id="PRRT_dependabot",
                author_login=DEPENDABOT_LOGIN,
                is_outdated=True,
                line=None,
            ),
            _review_thread(thread_id="PRRT_codecov", author_login="codecov[bot]"),
        ],
        bot_login=HAUNTER_BOT_LOGIN,
        grounding=grounding,
    )

    assert selected == [], f"another account's threads were selected: {selected}"


def test_outdated_haunter_thread_is_selected() -> None:
    """`isOutdated` remains sufficient, and this test is why.

    GitHub computes ``isOutdated`` over the PR's *complete* diff: it reports that
    the code the comment was anchored to is no longer there in that position.
    That determination is independent of this run's 60_000-char grounding prefix,
    so — unlike "the path is not in my index" — it is never weakened by our own
    bounds. It is a positive statement about the anchor, and it is the only signal
    available for a thread whose ``line`` GitHub has already nulled.

    The residual risk is that a refactor can outdated an anchor without fixing
    the finding. It is accepted deliberately: the step only ever touches threads
    this pipeline authored itself, and requiring a corroborating local coordinate
    is impossible for exactly the threads ``isOutdated`` selects — their ``line``
    is null.
    """
    grounding = build_diff_grounding(file_diff("app.py"))

    assert _thread_is_addressed(
        _review_thread(thread_id="PRRT_outdated", is_outdated=True, line=None),
        grounding,
    ), (
        "an outdated Haunter thread must still be resolvable; GitHub's own "
        "outdated determination is evidence this run's clipped grounding cannot "
        "contradict"
    )
    assert _select_addressed_thread_ids(
        [_review_thread(thread_id="PRRT_outdated", is_outdated=True, line=None)],
        bot_login=HAUNTER_BOT_LOGIN,
        grounding=grounding,
    ) == ["PRRT_outdated"]


def test_unreadable_anchor_is_not_addressed() -> None:
    """A null/non-int ``line`` is an unknown anchor, not a resolved one.

    GitHub nulls ``line`` on an outdated comment — handled above — but a
    malformed node must not silently fall through to "addressed" either.
    """
    grounding = build_diff_grounding(file_diff("app.py"))

    assert not _thread_is_addressed(
        _review_thread(thread_id="PRRT_null_line", line=None),
        grounding,
    ), "a thread with no readable anchor line was reported as addressed"


# ---------------------------------------------------------------------------
# Pipeline level: the resolve mutation that leaves the process
# ---------------------------------------------------------------------------


async def test_clipped_diff_resolves_no_threads(pending_review: CodeReview) -> None:
    """A diff the grounding could not fully parse must close nothing.

    ``build_diff_grounding`` clips at ``AUDIT_MAX_DIFF_CHARS``, the PR diff fetch
    allows 2 MB, and the review prompt embeds the whole body — so on a large PR
    the model reads code the grounding never indexes, and every thread on that
    unseen portion looks stale.
    """
    grounding = build_diff_grounding(OVERSIZED_DIFF)
    assert grounding.clipped is True, (
        f"precondition: the fixture diff ({len(OVERSIZED_DIFF)} chars) must exceed "
        f"AUDIT_MAX_DIFF_CHARS ({AUDIT_MAX_DIFF_CHARS})"
    )
    assert OVERSIZED_TAIL_FILE not in grounding.line_index, (
        "precondition: the tail file is past the clip and therefore unindexed"
    )

    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path=OVERSIZED_HEAD_FILE)],
        pr_diff=OVERSIZED_DIFF,
        threads=[
            _review_thread(
                thread_id="PRRT_tail",
                path=OVERSIZED_TAIL_FILE,
                line=3,
            )
        ],
    ) as mocks:
        mocks["resolved"].assert_not_awaited()
        mocks["publish"].assert_awaited_once()


async def test_commit_diff_fallback_resolves_no_threads(
    pending_review: CodeReview,
) -> None:
    """The single-commit fallback is a subset of the PR, never a superset.

    When ``fetch_pull_request_diff`` fails the pipeline falls back to one
    commit's diff. Every file the PR touches but that commit does not disappears
    from the grounding, so "addressed" would mean "not in this commit".
    """
    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="app.py")],
        pr_diff=None,
        commit_diff=file_diff("app.py"),
        threads=[_review_thread(thread_id="PRRT_other_file", path="elsewhere.py")],
    ) as mocks:
        mocks["resolved"].assert_not_awaited()
        mocks["publish"].assert_awaited_once()


async def test_empty_pr_diff_resolves_no_threads(pending_review: CodeReview) -> None:
    """An empty PR diff body resolves nothing.

    Reachable: the endpoint answers 200 with no content. The grounding then
    indexes no path at all, and every Haunter thread on the PR reads as stale.
    """
    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="app.py")],
        pr_diff="",
        threads=[_review_thread(thread_id="PRRT_any", path="app.py", line=2)],
    ) as mocks:
        mocks["resolved"].assert_not_awaited()


async def test_run_without_inline_comments_resolves_no_threads(
    pending_review: CodeReview,
) -> None:
    """A run that anchored nothing has replaced nothing.

    This is the smell guard: if every finding this run produced was dropped as
    ungroundable, the review that was just published contains no inline finding
    that could supersede any earlier thread, and closing them would destroy the
    only record of findings this pipeline ever made.
    """
    async with _run_pr_review(
        pending_review.id,
        # A file that is not in the diff: nothing can be anchored inline.
        findings=[_finding_payload(file_path="not_in_the_diff.py")],
        pr_diff=file_diff("app.py"),
        # The anchor is *demonstrably* gone (line 99 is outside every hunk of
        # app.py), so the coordinate rule alone would close it: only the
        # zero-published-comment guard stands between this run and a resolved
        # thread it has not replaced.
        threads=[_review_thread(thread_id="PRRT_prior", path="app.py", line=99)],
    ) as mocks:
        publish = mocks["publish"]
        publish.assert_awaited_once()
        assert publish.call_args.kwargs["comments"] == [], (
            "precondition: the run must publish zero inline comments"
        )
        mocks["resolved"].assert_not_awaited()


async def test_moved_line_on_indexed_path_is_resolved(
    pending_review: CodeReview,
) -> None:
    """The positive case survives end to end: the mutation is still sent.

    Without this, "resolve nothing unless the grounding is perfect" would pass
    every safety test above while quietly never closing anything at all.
    """
    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="app.py")],
        pr_diff=file_diff("app.py"),
        threads=[_review_thread(thread_id="PRRT_moved", path="app.py", line=99)],
    ) as mocks:
        mocks["resolved"].assert_awaited_once()
        assert mocks["resolved"].call_args.kwargs["thread_ids"] == ["PRRT_moved"]


async def test_outdated_haunter_thread_is_resolved_end_to_end(
    pending_review: CodeReview,
) -> None:
    """The `isOutdated` decision, observed as the mutation that leaves."""
    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="app.py")],
        pr_diff=file_diff("app.py"),
        threads=[
            _review_thread(thread_id="PRRT_outdated", is_outdated=True, line=None),
            _review_thread(
                thread_id="PRRT_dependabot",
                author_login=DEPENDABOT_LOGIN,
                is_outdated=True,
                line=None,
            ),
        ],
    ) as mocks:
        mocks["resolved"].assert_awaited_once()
        assert mocks["resolved"].call_args.kwargs["thread_ids"] == ["PRRT_outdated"]


async def test_failed_publish_resolves_no_threads(
    pending_review: CodeReview,
) -> None:
    """A failed publish resolves nothing — there is no replacement to justify it."""
    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="app.py")],
        pr_diff=file_diff("app.py"),
        threads=[_review_thread(thread_id="PRRT_prior", path="app.py", line=99)],
        publish_error=RuntimeError("GitHub API returned error 500"),
    ) as mocks:
        mocks["publish"].assert_awaited_once()
        mocks["resolved"].assert_not_awaited()
        assert pending_review.status == review_orch.REVIEW_STATUS_ERROR


async def test_failed_resolution_step_does_not_repost_the_review(
    pending_review: CodeReview,
) -> None:
    """A courtesy step that fails after the publish must not trigger a retry.

    ``_resolve_superseded_threads`` runs after the review is accepted and the row
    is terminal. Inside the publish ``try``, an untyped failure carrying "422" is
    classified as an invalid-coordinate 422 and a **second** review is posted for
    a review that already succeeded.
    """
    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="app.py")],
        pr_diff=file_diff("app.py"),
        threads=[_review_thread(thread_id="PRRT_prior", path="app.py", line=99)],
        resolution_error=RuntimeError("upstream said 422 while resolving"),
    ) as mocks:
        assert mocks["publish"].await_count == 1, (
            f"the review was posted {mocks['publish'].await_count} times; a "
            "failure in the post-publish thread-resolution step must not "
            "re-enter the 422 summary-only fallback"
        )
        assert pending_review.status == review_orch.REVIEW_STATUS_COMPLETED, (
            "the review reached GitHub, so the row must stay 'completed'; a later "
            f"failure changed it to {pending_review.status!r}"
        )


# ---------------------------------------------------------------------------
# REJECT #2 — the grounding lookup key must be the validated path
# ---------------------------------------------------------------------------


async def test_finding_on_path_with_ampersand_is_published_inline(
    pending_review: CodeReview,
) -> None:
    """The index key is the validated path, not an HTML-escaped copy of it.

    ``DiffGrounding.line_index`` keys come from the diff parser
    (``_diff_header_path`` -> ``_validate_repo_path``), which normalises but does
    not escape. Looking the finding up under ``sanitize_output_path`` — which
    HTML-escapes — can never match ``src/a&b.py``, so every finding on such a
    file is silently dropped.
    """
    diff = file_diff("src/a&b.py")
    grounding = build_diff_grounding(diff)
    assert sorted(grounding.line_index) == ["src/a&b.py"], (
        "precondition: the parser must index the raw path"
    )

    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="src/a&b.py")],
        pr_diff=diff,
    ) as mocks:
        publish = mocks["publish"]
        publish.assert_awaited_once()
        comments = publish.call_args.kwargs["comments"]
        assert comments, (
            "the finding on 'src/a&b.py' was dropped: the grounding lookup used "
            "a different string than the index key"
        )
        assert comments[0]["path"] == "src/a&b.py"
        assert comments[0]["path"] in grounding.line_index, (
            "the published path must be a key of the grounding index; GitHub "
            "matches it literally against the diff, so an escaped form cannot "
            "resolve"
        )


# ---------------------------------------------------------------------------
# REJECT #3 — suppression is stated in the body, not only in a log line
# ---------------------------------------------------------------------------


async def test_review_body_discloses_suppressed_findings(
    pending_review: CodeReview,
) -> None:
    """The published body says how many findings were dropped and why.

    A confident high-risk review that silently omits findings it could not anchor
    reads as a clean bill of health for the omitted code.
    """
    async with _run_pr_review(
        pending_review.id,
        findings=[
            _finding_payload(file_path="app.py"),
            _finding_payload(file_path="deleted_module.py"),
        ],
        pr_diff=file_diff("app.py"),
    ) as mocks:
        body = mocks["publish"].call_args.kwargs["body"]
        assert "1 of 2 findings could not be anchored" in body, (
            "the review body does not report the suppressed finding:\n" + body
        )
        assert "not part of the reviewed diff" in body, (
            "the review body does not say why the finding was dropped:\n" + body
        )


async def test_review_body_discloses_a_truncated_diff(
    pending_review: CodeReview,
) -> None:
    """Clipping is disclosed even when nothing was suppressed.

    Clipping means the anchoring view is smaller than the body the model read,
    which is a caveat on every inline comment in the review.
    """
    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path=OVERSIZED_HEAD_FILE)],
        pr_diff=OVERSIZED_DIFF,
    ) as mocks:
        body = mocks["publish"].call_args.kwargs["body"]
        assert "truncated" in body, (
            "the diff was clipped at AUDIT_MAX_DIFF_CHARS but the body does not "
            f"say so:\n{body[-600:]}"
        )


async def test_disclosure_survives_a_summary_long_enough_to_fill_the_body(
    pending_review: CodeReview,
) -> None:
    """The disclosure is pinned above the summary, not appended below it.

    ``review.summary`` is itself clamped to GitHub's whole comment ceiling, so a
    verbose model produces a body that is already at the bound. A disclosure
    placed at the end is then trimmed away by the very bound that is supposed to
    guarantee it — reinstating the silence this line exists to prevent.
    """
    async with _run_pr_review(
        pending_review.id,
        findings=[
            _finding_payload(file_path="app.py"),
            _finding_payload(file_path="deleted_module.py"),
        ],
        pr_diff=file_diff("app.py"),
        summary="verbose " * 20_000,
    ) as mocks:
        body = mocks["publish"].call_args.kwargs["body"]
        assert len(body) <= MAX_GITHUB_COMMENT_CHARS, (
            f"the body is {len(body)} chars, above GitHub's own ceiling of "
            f"{MAX_GITHUB_COMMENT_CHARS}"
        )
        assert "1 of 2 findings could not be anchored" in body, (
            "the summary filled the body and pushed the disclosure off the end; "
            f"tail was:\n{body[-400:]}"
        )


async def test_commit_comment_body_discloses_findings_that_did_not_fit(
    pending_review: CodeReview,
) -> None:
    """The push-level comment says how many findings its length bound dropped.

    Nothing is anchored inline on this path, but the concatenated finding list is
    still clamped at GitHub's 65_536-char ceiling, so the comment can silently
    omit most of the review.
    """
    pending_review.pr_number = None
    findings = [
        _finding_payload(
            file_path="app.py",
            line_start=2 + (index % 4),
            critique="c" * 20_000,
            suggested_patch="p" * 20_000,
        )
        for index in range(40)
    ]

    mocks = await _commit_comment(
        pending_review.id,
        findings=findings,
        commit_diff=file_diff("app.py"),
    )

    publish = mocks["publish"]
    publish.assert_awaited_once()
    body = publish.call_args.kwargs["body"]
    assert "40" in body, body[-400:]
    assert "did not fit in this comment" in body, (
        "the commit comment silently dropped findings past GitHub's length "
        f"bound:\n{body[-600:]}"
    )


# ---------------------------------------------------------------------------
# S4 — the SHA pin is only as good as the log that says so
# ---------------------------------------------------------------------------


async def test_unverified_head_pin_is_logged(
    pending_review: CodeReview,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live-head lookup that fails is a divergence, and it is logged as one.

    The pipeline then publishes a review whose inline comments were anchored
    against a diff fetched live, against a SHA it could not confirm. That is
    strictly better than the enqueue SHA, but it is not the guarantee the old
    comment claimed, so it has to be visible.
    """
    with caplog.at_level(logging.INFO, logger=review_orch.logger.name):
        async with _run_pr_review(
            pending_review.id,
            findings=[_finding_payload(file_path="app.py")],
            pr_diff=file_diff("app.py"),
            live_head_error=RuntimeError("pull request metadata unavailable"),
        ) as mocks:
            mocks["publish"].assert_awaited_once()

    records = " ".join(record.getMessage() for record in caplog.records)
    assert "publish_sha_unresolved" in records, (
        f"an unresolvable live head was not logged; records: {records!r}"
    )
    assert "sha_pin_unverified" in records, (
        "the review was published against an unverified commit pin and no log "
        f"record says so; records: {records!r}"
    )


# ---------------------------------------------------------------------------
# S1 — an unexpected failure must not strand the row at "in_progress"
# ---------------------------------------------------------------------------


async def test_unexpected_failure_does_not_strand_the_row(
    pending_review: CodeReview,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure the body did not anticipate still lands on a terminal status.

    ``in_progress`` is never revisited: the webhook dedup guard counts it as
    already handled and no reaper exists, so a row left there answers
    ``duplicate`` to every redelivery of the delivery that started it and the
    review is lost silently. This drives that through a session call that is not
    wrapped in a ``try`` — the shape the timeout handler alone used to cover.

    The call also asserts the module's own contract: the exception does not
    escape into the caller.
    """
    async def _refresh_raises(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("session refresh failed")

    monkeypatch.setattr(FakeAsyncSession, "refresh", _refresh_raises)

    async with _run_pr_review(
        pending_review.id,
        findings=[_finding_payload(file_path="app.py")],
        pr_diff=file_diff("app.py"),
    ) as mocks:
        mocks["publish"].assert_not_awaited()

    assert pending_review.status == review_orch.REVIEW_STATUS_ERROR, (
        "an unhandled failure left the row at "
        f"{pending_review.status!r}; every redelivery will be answered "
        "'duplicate' and the review is lost"
    )
    assert "crashed" in (pending_review.failure_reason or ""), (
        "the terminal state was recorded without saying why: "
        f"{pending_review.failure_reason!r}"
    )
