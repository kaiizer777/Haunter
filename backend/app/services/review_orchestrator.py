"""
Autonomous Code Review Orchestrator — Feature 1.

Orchestrates the asynchronous code review pipeline:
1. Loads pending CodeReview from DB.
2. Resolves GitHub App installation token.
3. Fetches pull request unified diff or commit diff via GitHub API.
4. Invokes the Review Subagent (code_reviewer.analyze_diff).
5. Persists findings, risk_score, summary, and token telemetry to code_reviews table.
6. Submits formal GitHub Pull Request Review (REQUEST_CHANGES or COMMENT) with inline
   ```suggestion blocks, or posts a commit comment on push events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import async_session_maker
from app.config import settings
from app.github.pr import (
    _is_configured_str,
    _resolve_app_credentials,
    get_installation_token,
    resolve_installation_id,
)
from app.github_client import (
    GitHubClientError,
    GitHubUnprocessableEntityError,
    create_commit_comment,
    create_pull_request_review,
    fetch_bot_identity,
    fetch_diff,
    fetch_pull_request,
    fetch_pull_request_diff,
    fetch_review_threads,
    resolve_review_threads,
)
from app.llm.prompts.audit_prompts import sanitize_output_path
from app.log_hygiene import sanitize_log_value
from app.models import CodeReview
from app.services.repo_settings import get_repo_settings
from app.subagents.auditor import DiffGrounding, build_diff_grounding
from app.subagents.code_reviewer import (
    ReviewFinding,
    analyze_diff,
    bound_github_body,
    format_github_suggestion,
)

logger = logging.getLogger(__name__)

# Maximum chars stored in code_reviews.failure_reason (human-readable, no secrets).
_FAILURE_REASON_MAX_CHARS = 500

# Hard wall-clock bound for one review pipeline run. The review body is a single
# LLM call plus a handful of GitHub calls, each with its own 30s client timeout,
# so anything past this is a hang, not slow work. Leaves wide headroom under the
# Lambda 900s limit so the timeout handler still has budget to record a terminal
# state. Without a bound, a hung fetch or a hung LLM leaves the row
# "in_progress" forever: the dedup guard counts that as handled and no reaper
# exists. Mirrors `app.orchestrator.ORCHESTRATOR_TIMEOUT_S`.
REVIEW_TIMEOUT_S: float = 300.0

# Terminal statuses for a CodeReview row: nothing transitions out of them, so
# every exit path of this module must land on one of them. "completed" means
# GitHub received the review; the other two say precisely what did not happen.
REVIEW_STATUS_COMPLETED = "completed"
REVIEW_STATUS_ERROR = "error"
#: Nothing was published because `RepoSettings.enable_pr_comments` is false.
#: Deliberately not "completed": that status is the dashboard's only signal that
#: a review reached GitHub, and a suppressed run produced zero GitHub output.
REVIEW_STATUS_SUPPRESSED = "suppressed"

_TERMINAL_REVIEW_STATUSES: frozenset[str] = frozenset(
    {
        REVIEW_STATUS_COMPLETED,
        REVIEW_STATUS_ERROR,
        REVIEW_STATUS_SUPPRESSED,
    }
)


def _truncate_failure_reason(reason: object) -> str:
    """Truncate a publish/fetch failure to 500 chars for DB storage."""
    text = str(reason) if reason is not None else "unknown error"
    return text[:_FAILURE_REASON_MAX_CHARS]


def _is_unprocessable_payload(error: BaseException) -> bool:
    """True only for the one GitHub status the caller can fix by changing the request.

    ``create_pr_review`` raises :class:`GitHubUnprocessableEntityError` on HTTP
    422 (github_client.py:1165), which is the actionable status: dropping the
    inline comments, or clamping the body, is what makes the retry succeed. Every
    other typed client error — auth, rate limit, not-found, network, response
    limit — is a failure the identical second request cannot fix, so it must not
    burn a second API call on a review it will never be able to post.

    An exception that crosses this boundary carrying no client type at all is
    still classified by its message, which is how a 422 was recognised before the
    type existed. A *typed* non-422 client error is never reclassified that way:
    its message is a faithful report of its own type, so a substring match on it
    is exactly the unsoundness this replaces.
    """
    if isinstance(error, GitHubUnprocessableEntityError):
        return True
    if isinstance(error, GitHubClientError):
        return False
    return "422" in str(error)


def _build_grounded_comments(
    findings: Sequence[ReviewFinding],
    grounding: DiffGrounding,
) -> tuple[list[dict[str, Any]], int]:
    """Turn findings into inline review comments anchored to real diff lines.

    ``ReviewFinding.file_path`` is only ``min_length=1`` and ``line_start`` /
    ``line_end`` are ``ge=1`` with no upper bound (code_reviewer.py:86-94), so
    every coordinate the model emits is attacker-controlled and unverified.
    GitHub rejects a review whose inline comment points at a file or line outside
    the diff hunks with HTTP 422, and ``create_pr_review`` posts the review body
    and *every* comment in one atomic request — so one bad coordinate discards
    every valid comment with it. Only a coordinate the diff actually supports is
    emitted; the rest are dropped and counted, so suppression is never silent.

    This mirrors ``audit_publisher.validate_finding_coordinates`` (the auditor
    path, which already does this) against ``DiffGrounding``, without importing
    it: that helper is typed to ``AuditFinding`` and would only duck-type here.

    Returns ``(comments, suppressed_count)``.
    """
    comments: list[dict[str, Any]] = []
    suppressed = 0
    for finding in findings:
        file_path = sanitize_output_path(finding.file_path)
        lines_in_diff = grounding.line_index.get(file_path)
        if not lines_in_diff:
            suppressed += 1
            continue
        if finding.line_end in lines_in_diff:
            target_line = finding.line_end
        elif finding.line_start in lines_in_diff:
            target_line = finding.line_start
        else:
            # Lowest groundable line inside the claimed span. Selected from the
            # grounding's own line set rather than by walking
            # ``range(line_start, line_end)``: that range has no upper bound, so
            # walking it would let one model-chosen integer stall the pipeline.
            in_span = sorted(
                line for line in lines_in_diff if finding.line_start <= line <= finding.line_end
            )
            if not in_span:
                suppressed += 1
                continue
            target_line = in_span[0]

        comment: dict[str, Any] = {
            "path": file_path,
            "line": target_line,
            "side": "RIGHT",
            "body": format_github_suggestion(finding),
        }
        # A multi-line anchor is only sent when its start is groundable too;
        # GitHub 422s a `start_line` outside the hunks just as it does a `line`.
        if finding.line_start < target_line and finding.line_start in lines_in_diff:
            comment["start_line"] = finding.line_start
            comment["start_side"] = "RIGHT"
        comments.append(comment)
    return comments, suppressed


async def _resolve_publish_sha(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    enqueue_sha: str,
    token: Any,
) -> str:
    """Resolve the PR head the review should be published against.

    ``review.commit_sha`` is pinned when the webhook is delivered
    (webhooks.py:1201), but the PR diff is fetched live at analysis time. A push
    landing in between makes the two disagree, and posting the review against
    the enqueue-time SHA anchors every inline comment to a commit the reviewer
    can no longer see. ``GET /pulls/{n}`` is the only endpoint that reports the
    live head, so it is consulted here and the resulting SHA is used for both
    the fallback commit-diff fetch and the publish.

    Falling back to the enqueue SHA when the head cannot be resolved is
    deliberate: the review would otherwise be discarded over a transient
    metadata failure, and the caller logs the unverified pin either way.
    """
    try:
        pr = await fetch_pull_request(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            token=token,
        )
    except Exception as head_err:
        logger.warning(
            "review_orchestrator: publish_sha_unresolved repo=%s/%s pr=%s "
            "error_type=%s error=%s using_enqueue_sha=%s",
            sanitize_log_value(owner, 128),
            sanitize_log_value(repo, 128),
            pr_number,
            type(head_err).__name__,
            sanitize_log_value(head_err),
            sanitize_log_value(enqueue_sha, 64),
        )
        return enqueue_sha

    head = pr.get("head") if isinstance(pr, dict) else None
    live_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(live_sha, str) or not live_sha.strip():
        logger.warning(
            "review_orchestrator: publish_sha_unresolved repo=%s/%s pr=%s "
            "reason=missing_head_sha using_enqueue_sha=%s",
            sanitize_log_value(owner, 128),
            sanitize_log_value(repo, 128),
            pr_number,
            sanitize_log_value(enqueue_sha, 64),
        )
        return enqueue_sha

    live_sha = live_sha.strip()
    if live_sha != enqueue_sha:
        logger.info(
            "review_orchestrator: publish_sha_diverged repo=%s/%s pr=%s "
            "enqueue_sha=%s live_sha=%s",
            sanitize_log_value(owner, 128),
            sanitize_log_value(repo, 128),
            pr_number,
            sanitize_log_value(enqueue_sha, 64),
            sanitize_log_value(live_sha, 64),
        )
    return live_sha


#: Wall-clock ceiling for the whole post-publish thread-resolution step.
#: `resolve_review_threads` is one mutation per thread (capped at
#: ``MAX_REVIEW_THREAD_RESOLVES`` = 25), so on a pathological PR it could spend
#: 25 x the 30s client timeout — enough to overrun ``REVIEW_TIMEOUT_S`` and have
#: the timeout handler overwrite a review that already published successfully.
#: The step is a courtesy to the reviewer, so it yields rather than the row.
REVIEW_THREAD_RESOLUTION_TIMEOUT_S: float = 30.0


def _thread_author_login(thread: dict[str, Any]) -> str:
    """Case-folded login of the comment that opened ``thread``, or ``""``.

    ``comments(first: 1)`` is the *oldest* comment of the thread, i.e. the one
    that started it, so this is the thread's author. A human replying inside one
    of our threads does not change that, and must not: an authorless thread
    would drop the thread out of our own selection.
    """
    comments = thread.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return ""
    first = nodes[0]
    if not isinstance(first, dict):
        return ""
    author = first.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(login, str):
        return ""
    return login.strip().casefold()[:128]


def _thread_is_addressed(thread: dict[str, Any], grounding: DiffGrounding) -> bool:
    """True when the diff just analysed no longer supports this thread's anchor.

    Two independent signals, both read against the *new* grounding rather than
    against the diff the thread was opened on:

    * ``isOutdated`` — GitHub itself marks the anchor's context as changed.
    * the anchored ``path``/``line`` falling outside
      :attr:`DiffGrounding.line_index` — the same coordinate check
      :func:`_build_grounded_comments` applies to every finding it publishes, so
      a thread whose line this run cannot anchor is by definition no longer a
      live finding.

    An unreadable anchor (``path`` absent from the index, ``line`` null or not an
    int) counts as addressed: the reviewer cannot act on a comment that points
    nowhere, and leaving it open costs them a manual resolve.
    """
    if thread.get("isOutdated") is True:
        return True
    path = thread.get("path")
    lines = grounding.line_index.get(path) if isinstance(path, str) else None
    if not lines:
        return True
    line = thread.get("line")
    if isinstance(line, bool) or not isinstance(line, int):
        return True
    return line not in lines


def _select_addressed_thread_ids(
    threads: Sequence[Any],
    *,
    bot_login: str,
    grounding: DiffGrounding,
) -> list[str]:
    """Ids of this PR's own threads that the new diff has addressed.

    Both predicates are required and neither is a heuristic:

    * **ours** — the thread's author is exactly ``bot_login``, the account the
      credential proved it authenticates as. Matching on a ``[bot]`` suffix
      instead would sweep in Dependabot, Codecov and any other bot on the PR and
      silently resolve other people's comments.
    * **addressed** — :func:`_thread_is_addressed`.

    Already-resolved threads are dropped (nothing to gain), as are malformed
    entries and duplicates.
    """
    selected: list[str] = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        if thread.get("isResolved") is True:
            continue
        raw_id = thread.get("id")
        if not isinstance(raw_id, str):
            continue
        thread_id = raw_id.strip()
        if not thread_id or thread_id in selected:
            continue
        if _thread_author_login(thread) != bot_login:
            continue
        if _thread_is_addressed(thread, grounding):
            selected.append(thread_id)
    return selected


async def _resolve_superseded_threads(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    grounding: DiffGrounding,
    token: Any,
) -> None:
    """Close the prior Haunter threads that this run's diff has answered.

    Called only after a review with inline findings has been *accepted* by
    GitHub — never on a suppressed run, a failed publish, or the summary-only
    422 fallback, because a thread closed for a finding that was never
    republished is worse than an open one. Resolving after the publish (rather
    than before it) is the same reason: the reverse order would close threads and
    then discover the replacement review was rejected.

    A "re-review" is not a stored flag — it is simply the presence of prior
    threads this pipeline authored. On a PR's first review there are none, so the
    selection is empty and nothing is sent; there is no separate code path, and
    therefore no way for the two to disagree.

    The prior threads come from ``fetch_review_threads`` (GraphQL), because only
    GraphQL yields a real ``PullRequestReviewThread`` id — a
    ``PullRequestReviewComment`` node id is a different type and the mutation
    rejects it. Both the page walk and the mutation count stay inside
    ``github_client``'s existing caps.

    Never raises. A resolution failure leaves threads open, which is the state
    the reviewer can still act on, so it can never fail an already-published
    review.
    """
    try:
        bot_login = await fetch_bot_identity(token)
        if not bot_login:
            logger.info(
                "review_orchestrator: thread_resolution_skipped repo=%s/%s pr=%s "
                "reason=bot_identity_unresolved",
                sanitize_log_value(owner, 128),
                sanitize_log_value(repo, 128),
                pr_number,
            )
            return

        threads = await fetch_review_threads(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            token=token,
        )
        candidates = _select_addressed_thread_ids(
            threads,
            bot_login=bot_login.strip().casefold(),
            grounding=grounding,
        )
        if not candidates:
            return

        resolved = await asyncio.wait_for(
            resolve_review_threads(
                owner=owner,
                repo=repo,
                thread_ids=candidates,
                token=token,
            ),
            timeout=REVIEW_THREAD_RESOLUTION_TIMEOUT_S,
        )
        logger.info(
            "review_orchestrator: thread_resolution repo=%s/%s pr=%s bot=%s "
            "prior_threads=%d candidates=%d resolved=%d",
            sanitize_log_value(owner, 128),
            sanitize_log_value(repo, 128),
            pr_number,
            sanitize_log_value(bot_login, 128),
            len(threads),
            len(candidates),
            resolved,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "review_orchestrator: thread_resolution_timeout repo=%s/%s pr=%s "
            "timeout_s=%.0f",
            sanitize_log_value(owner, 128),
            sanitize_log_value(repo, 128),
            pr_number,
            REVIEW_THREAD_RESOLUTION_TIMEOUT_S,
        )
    except Exception as resolve_err:
        logger.warning(
            "review_orchestrator: thread_resolution_failed repo=%s/%s pr=%s "
            "error_type=%s error=%s",
            sanitize_log_value(owner, 128),
            sanitize_log_value(repo, 128),
            pr_number,
            type(resolve_err).__name__,
            sanitize_log_value(resolve_err),
        )


async def _record_review_timeout(review_id: uuid.UUID) -> None:
    """Force a terminal state on a review that outran :data:`REVIEW_TIMEOUT_S`.

    A fresh session is opened for the same reason the orchestrator's timeout
    handler opens one: the cancelled run's own session is in whatever state the
    cancellation left it. Never raises — a timeout that could not be recorded
    must not become a second, unrelated failure on top of the timeout.
    """
    try:
        async with async_session_maker() as error_session:
            stale = await error_session.get(CodeReview, review_id)
            if stale is None or stale.status in _TERMINAL_REVIEW_STATUSES:
                return
            stale.status = REVIEW_STATUS_ERROR
            stale.failure_reason = "code review wall-clock timeout"
            await error_session.commit()
    except Exception as persist_err:
        logger.error(
            "review_orchestrator: timeout_state_persist_failed review_id=%s "
            "error_type=%s error=%s",
            review_id,
            type(persist_err).__name__,
            sanitize_log_value(persist_err),
        )


async def _ensure_install_id(session: Any, repo: Any) -> None:
    """
    Backfill ``repo.github_install_id`` when missing but the GitHub App is configured.

    Resolves via ``GET /repos/{owner}/{repo}/installation`` (App JWT) and
    persists the result on the repo row. No-op when the id is already set or
    when App credentials are absent (dev fallback uses ``settings.github_token``).
    Never raises — failures are logged and the caller proceeds to token
    resolution, which will surface a clear error downstream.
    """
    existing = getattr(repo, "github_install_id", None)
    if isinstance(existing, int) and not isinstance(existing, bool) and existing > 0:
        return
    # Explicit pair checked against this module's settings first (unit tests
    # patch review_orchestrator.settings directly). The sync resolver is
    # env-only on purpose: the read-only auditor App is NEVER valid for
    # writes, so only the write-capable pair justifies a backfill attempt
    # here (the async SSM path is resolved later by get_installation_token()).
    explicit_configured = _is_configured_str(
        settings.github_app_id
    ) and _is_configured_str(settings.github_app_private_key)
    if not explicit_configured:
        # SSM-backed Lambda path: App ID + SSM PEM path (no PEM in env)
        # counts as configured — resolve_installation_id() below loads the
        # PEM from SSM via _resolve_write_credentials(), so backfill must
        # not be skipped or get_installation_token() would fail on the
        # missing install id.
        ssm_path = getattr(settings, "github_app_private_key_ssm_path", "")
        ssm_configured = _is_configured_str(
            settings.github_app_id
        ) and _is_configured_str(ssm_path)
        if not ssm_configured:
            _, _, app_source = _resolve_app_credentials()
            if app_source == "none":
                return
    try:
        install_id = await resolve_installation_id(repo.owner, repo.name)
    except Exception as exc:
        logger.warning(
            "review_orchestrator: could not auto-resolve install id for %s/%s: %s",
            repo.owner,
            repo.name,
            exc,
        )
        return
    repo.github_install_id = install_id
    try:
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning(
            "review_orchestrator: failed to backfill install id for %s/%s: %s",
            repo.owner,
            repo.name,
            exc,
        )
        return
    logger.info(
        "review_orchestrator: backfilled github_install_id for %s/%s",
        repo.owner,
        repo.name,
    )


def _build_risk_badge(risk_score: int) -> str:
    if risk_score <= 30:
        return "🟢 **Low Risk**"
    elif risk_score <= 70:
        return "🟡 **Moderate Risk**"
    return "🔴 **Critical / High Risk**"


async def run_code_review_pipeline(review_id: uuid.UUID) -> None:
    """
    Execute end-to-end code review pipeline for a CodeReview row, under a hard
    wall-clock bound. Runs asynchronously in BackgroundTasks or via AWS Lambda
    self-invocation.

    The body never raises and never leaves the row in a non-terminal state: a
    pipeline that hangs is cancelled here and its terminal state recorded from a
    fresh session, because "in_progress" is never revisited — the webhook dedup
    guard counts it as already handled and no reaper exists.
    """
    logger.info("review_orchestrator: starting code review for review_id=%s", review_id)
    try:
        await asyncio.wait_for(
            _run_review_pipeline_body(review_id),
            timeout=REVIEW_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error(
            "review_orchestrator: review_id=%s wall-clock timeout after %ss "
            "— forcing error state",
            review_id,
            REVIEW_TIMEOUT_S,
        )
        await _record_review_timeout(review_id)


async def _run_review_pipeline_body(review_id: uuid.UUID) -> None:
    """The review pipeline itself, bounded by :func:`run_code_review_pipeline`.

    Every exit path records a terminal ``code_reviews.status``: "completed" only
    once GitHub has the review, "suppressed" when the repo forbids PR comments,
    "error" for every failure. Raises nothing.
    """
    async with async_session_maker() as session:
        stmt = (
            select(CodeReview)
            .options(selectinload(CodeReview.repo))
            .where(CodeReview.id == review_id)
        )
        res = await session.execute(stmt)
        review = res.scalars().first()

        if not review:
            logger.error("review_orchestrator: review_id=%s not found", review_id)
            return

        if review.status not in ("pending", "in_progress"):
            logger.info(
                "review_orchestrator: review_id=%s already in terminal status=%s",
                review_id,
                review.status,
            )
            return

        review.status = "in_progress"
        await session.commit()
        await session.refresh(review)

        repo = review.repo
        if not repo:
            logger.error(
                "review_orchestrator: repo not found for review_id=%s", review_id
            )
            review.status = "error"
            review.summary = "Repository record not found."
            await session.commit()
            return

        # 1. Resolve installation token (backfill install id first for legacy repos)
        await _ensure_install_id(session, repo)
        try:
            token = await get_installation_token(repo)
        except Exception as exc:
            logger.warning(
                "review_orchestrator: failed to resolve token for repo %s/%s "
                "(has_install_id=%s app_configured=%s): %s",
                repo.owner,
                repo.name,
                bool(getattr(repo, "github_install_id", None)),
                _resolve_app_credentials()[2],
                exc,
            )
            token = None

        # 2. Fetch diff, and pin the commit it actually belongs to.
        #
        # `review.commit_sha` is the enqueue-time SHA (webhooks.py pins it when
        # the delivery arrives) while the PR diff below is fetched live, so a
        # push landing in between makes the two disagree and the review would be
        # anchored to a commit nobody analysed. The live head is resolved first
        # and every fetch below — including the commit-diff fallback — uses it,
        # so the diff that is analysed and the commit the review is posted
        # against are the same one.
        diff_text = ""
        target_sha = review.commit_sha
        try:
            if review.pr_number:
                target_sha = await _resolve_publish_sha(
                    owner=repo.owner,
                    repo=repo.name,
                    pr_number=review.pr_number,
                    enqueue_sha=review.commit_sha,
                    token=token,
                )
                try:
                    diff_text = await fetch_pull_request_diff(
                        owner=repo.owner,
                        repo=repo.name,
                        pr_number=review.pr_number,
                        token=token,
                    )
                except Exception as pr_diff_err:
                    logger.warning(
                        "review_orchestrator: fetch_pull_request_diff failed for %s/%s PR #%d (%s), falling back to commit diff",
                        repo.owner,
                        repo.name,
                        review.pr_number,
                        pr_diff_err,
                    )
                    diff_text = await fetch_diff(
                        owner=repo.owner,
                        repo=repo.name,
                        sha=target_sha,
                        token=token,
                    )
            else:
                diff_text = await fetch_diff(
                    owner=repo.owner,
                    repo=repo.name,
                    sha=target_sha,
                    token=token,
                )
        except Exception as diff_err:
            logger.error(
                "review_orchestrator: failed to fetch diff for %s/%s @ %s: %s",
                repo.owner,
                repo.name,
                review.commit_sha,
                diff_err,
            )
            review.status = "error"
            review.summary = f"Failed to fetch git diff: {diff_err}"
            review.failure_reason = _truncate_failure_reason(
                f"Failed to fetch git diff: {diff_err}"
            )
            await session.commit()
            return

        # 3. Analyze diff via subagent
        repo_context = f"{repo.owner}/{repo.name}"
        if repo.language_hint:
            repo_context += f" (primary language: {repo.language_hint})"

        try:
            result = await analyze_diff(
                diff_text=diff_text,
                repo_context=repo_context,
                db=session,
                repo_id=repo.id,
            )
        except Exception as llm_err:
            logger.exception(
                "review_orchestrator: LLM review analysis failed for review_id=%s: %s",
                review_id,
                llm_err,
            )
            review.status = "error"
            review.summary = f"Code review analysis failed: {llm_err}"
            review.failure_reason = _truncate_failure_reason(
                f"Code review analysis failed: {llm_err}"
            )
            await session.commit()
            return

        # 4. Persist analysis results — status stays in_progress until the
        # GitHub publish below succeeds. Marking completed here would hide
        # POST failures behind a success status (R2).
        review.risk_score = result.output.risk_score
        review.summary = result.output.summary
        review.findings = [f.model_dump() for f in result.output.findings]
        review.input_tokens = result.input_tokens
        review.output_tokens = result.output_tokens
        review.failure_reason = None
        await session.commit()
        await session.refresh(review)

        logger.info(
            "review_orchestrator: review analyzed for %s/%s @ %s (risk_score=%d, findings=%d, latency=%dms)",
            repo.owner,
            repo.name,
            target_sha,
            review.risk_score,
            len(result.output.findings),
            result.latency_ms,
        )

        # 5. Submit review to GitHub — only now may the row become completed.
        # Publish failures set status=error + failure_reason (truncated 500)
        # while keeping findings/summary so the dashboard stays truthful.
        try:
            repo_settings = await get_repo_settings(session, repo.id)
        except Exception as settings_err:
            logger.error(
                "review_orchestrator: repo_settings_unavailable repo=%s/%s "
                "review_id=%s error_type=%s error=%s",
                sanitize_log_value(repo.owner, 128),
                sanitize_log_value(repo.name, 128),
                review_id,
                type(settings_err).__name__,
                sanitize_log_value(settings_err),
            )
            review.status = REVIEW_STATUS_ERROR
            review.failure_reason = _truncate_failure_reason(
                f"Failed to read repository review settings: {settings_err}"
            )
            await session.commit()
            await session.refresh(review)
            return
        risk_badge = _build_risk_badge(review.risk_score)
        if not repo_settings.enable_pr_comments:
            logger.info(
                "review_orchestrator: pr_comments_disabled repo=%s/%s review_id=%s "
                "publish=suppressed",
                sanitize_log_value(repo.owner, 128),
                sanitize_log_value(repo.name, 128),
                review_id,
            )
            # "completed" is the dashboard's only signal that a review reached
            # GitHub. Nothing did, so the run records its own terminal status and
            # says why — see app/orchestrator.py, which treats enable_pr_comments
            # purely as a publish guard and never fabricates a success status.
            review.status = REVIEW_STATUS_SUPPRESSED
            review.failure_reason = _truncate_failure_reason(
                "PR comments disabled by repo settings; review published nowhere"
            )
            await session.commit()
            await session.refresh(review)
            return

        # Coordinate validation against the diff that was actually analysed —
        # `diff_text` is the PR diff, or the commit-diff fallback when the PR
        # diff could not be fetched. Only the PR branch has inline coordinates
        # to ground, so the parse is paid for there and nowhere else.
        if review.pr_number:
            # PR Review: comments array + overall event
            grounding = build_diff_grounding(diff_text)
            comments, suppressed_findings = _build_grounded_comments(
                result.output.findings, grounding
            )
            if suppressed_findings:
                # Never silent: a suppressed finding is one GitHub will not let
                # us anchor, and the count is what tells an operator the model
                # produced coordinates outside the diff it was shown.
                logger.info(
                    "review_orchestrator: inline_comment_suppression repo=%s/%s "
                    "pr=%s suppressed=%d published=%d total=%d",
                    sanitize_log_value(repo.owner, 128),
                    sanitize_log_value(repo.name, 128),
                    review.pr_number,
                    suppressed_findings,
                    len(comments),
                    len(result.output.findings),
                )

            review_event = "REQUEST_CHANGES" if review.risk_score >= 80 else "COMMENT"
            # Per-field bounds hold (every finding goes through
            # `CodeReviewOutput.model_validate`), but the concatenation of N
            # findings is a separate quantity with its own ceiling — bound the
            # whole body, or one oversized body 422s the request and takes every
            # inline comment down with it.
            body = bound_github_body(
                f"### 🛡️ Haunter Autonomous Code Review\n\n"
                f"**Risk Score**: {review.risk_score}/100 — {risk_badge}\n\n"
                f"{review.summary}\n\n"
                f"*Actionable findings: {len(comments)} flagged across 4 engineering dimensions.*"
            )

            try:
                await create_pull_request_review(
                    owner=repo.owner,
                    repo=repo.name,
                    pr_number=review.pr_number,
                    commit_sha=target_sha,
                    body=body,
                    comments=comments,
                    event=review_event,
                    token=token,
                )
                logger.info(
                    "review_orchestrator: posted PR review on %s/%s PR #%d (event=%s)",
                    repo.owner,
                    repo.name,
                    review.pr_number,
                    review_event,
                )
                review.status = REVIEW_STATUS_COMPLETED
                await session.commit()
                await session.refresh(review)
                logger.info(
                    "review_orchestrator: review completed for %s/%s @ %s (risk_score=%d, findings=%d)",
                    repo.owner,
                    repo.name,
                    target_sha,
                    review.risk_score,
                    len(result.output.findings),
                )
                # Only now, with the replacement review accepted by GitHub and
                # the row already terminal, may the findings this one supersedes
                # be closed. Never reached on a failed publish, on the
                # summary-only 422 fallback, or on a suppressed run.
                await _resolve_superseded_threads(
                    owner=repo.owner,
                    repo=repo.name,
                    pr_number=review.pr_number,
                    grounding=grounding,
                    token=token,
                )
            except Exception as gh_err:
                if not _is_unprocessable_payload(gh_err):
                    # Not actionable by changing the request: an auth, rate
                    # limit, not-found or network failure posts nothing on a
                    # second identical attempt, it only spends one.
                    logger.error(
                        "review_orchestrator: publish_failed repo=%s/%s pr=%s "
                        "error_type=%s error=%s",
                        sanitize_log_value(repo.owner, 128),
                        sanitize_log_value(repo.name, 128),
                        review.pr_number,
                        type(gh_err).__name__,
                        sanitize_log_value(gh_err),
                    )
                    # Findings/summary are kept; only the status signals the
                    # publish failure so the dashboard stays truthful.
                    review.status = REVIEW_STATUS_ERROR
                    review.failure_reason = _truncate_failure_reason(
                        f"Failed to publish PR review: {gh_err}"
                    )
                    await session.commit()
                    await session.refresh(review)
                    return
                logger.warning(
                    "review_orchestrator: invalid_inline_422 repo=%s/%s pr=%s "
                    "error_type=%s error=%s — dropping inline comments and "
                    "retrying summary-only",
                    sanitize_log_value(repo.owner, 128),
                    sanitize_log_value(repo.name, 128),
                    review.pr_number,
                    type(gh_err).__name__,
                    sanitize_log_value(gh_err),
                )
                try:
                    # The retry exists because the first request was rejected
                    # for its inline comments, and it carries every finding
                    # inline in the body instead — so it needs the same total
                    # bound the first one had, or it 422s for the same reason.
                    findings_detail = "\n\n### 🔍 Detailed Findings\n"
                    for i, f in enumerate(result.output.findings, 1):
                        findings_detail += f"\n{i}. **{f.file_path}:{f.line_start}-{f.line_end}** ({f.category.upper()} / {f.severity.upper()}):\n   {f.critique}\n"
                        if f.suggested_patch:
                            findings_detail += (
                                f"\n   ```\n   {f.suggested_patch.strip()}\n   ```\n"
                            )
                    fallback_body = bound_github_body(body + findings_detail)

                    await create_pull_request_review(
                        owner=repo.owner,
                        repo=repo.name,
                        pr_number=review.pr_number,
                        commit_sha=target_sha,
                        body=fallback_body,
                        comments=[],
                        event=review_event,
                        token=token,
                    )
                    logger.info(
                        "review_orchestrator: posted fallback summary PR review on %s/%s PR #%d (event=%s)",
                        repo.owner,
                        repo.name,
                        review.pr_number,
                        review_event,
                    )
                    review.status = REVIEW_STATUS_COMPLETED
                    await session.commit()
                    await session.refresh(review)
                except Exception as fallback_err:
                    logger.error(
                        "review_orchestrator: fallback PR review also failed on %s/%s PR #%d: %s",
                        repo.owner,
                        repo.name,
                        review.pr_number,
                        fallback_err,
                    )
                    # Findings/summary are kept; only the status signals the
                    # publish failure so the dashboard stays truthful.
                    review.status = REVIEW_STATUS_ERROR
                    review.failure_reason = _truncate_failure_reason(
                        f"Failed to publish PR review: {fallback_err}"
                    )
                    await session.commit()
                    await session.refresh(review)
        else:
            # Commit comment for push without PR
            findings_md = ""
            if result.output.findings:
                findings_md = "\n\n### 🔍 Actionable Findings\n"
                for i, f in enumerate(result.output.findings, 1):
                    findings_md += f"\n{i}. **{f.file_path}:{f.line_start}-{f.line_end}** ({f.category.upper()} / {f.severity.upper()}):\n   {f.critique}\n"
                    if f.suggested_patch:
                        findings_md += (
                            f"\n   ```\n   {f.suggested_patch.strip()}\n   ```\n"
                        )

            # Same total bound as the PR review body: N findings concatenated
            # exceed GitHub's comment ceiling long before any single field does.
            body = bound_github_body(
                f"### 🛡️ Haunter Push-Level Code Review\n\n"
                f"**Risk Score**: {review.risk_score}/100 — {risk_badge}\n\n"
                f"{review.summary}"
                f"{findings_md}"
            )

            try:
                await create_commit_comment(
                    owner=repo.owner,
                    repo=repo.name,
                    commit_sha=review.commit_sha,
                    body=body,
                    token=token,
                )
                logger.info(
                    "review_orchestrator: posted commit comment on %s/%s @ %s",
                    repo.owner,
                    repo.name,
                    review.commit_sha,
                )
                review.status = "completed"
                await session.commit()
                await session.refresh(review)
            except Exception as gh_err:
                logger.error(
                    "review_orchestrator: failed to post commit comment on %s/%s @ %s: %s",
                    repo.owner,
                    repo.name,
                    review.commit_sha,
                    gh_err,
                )
                review.status = "error"
                review.failure_reason = _truncate_failure_reason(
                    f"Failed to publish commit comment: {gh_err}"
                )
                await session.commit()
                await session.refresh(review)
