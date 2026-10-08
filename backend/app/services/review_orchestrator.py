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
from dataclasses import dataclass
from enum import StrEnum
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
from app.llm.prompts.audit_prompts import MAX_GITHUB_COMMENT_CHARS
from app.log_hygiene import sanitize_log_value
from app.models import CodeReview
from app.services.repo_settings import get_repo_settings
from app.subagents.auditor import (
    DiffGrounding,
    _validate_repo_path,
    build_diff_grounding,
)
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


class GroundingSource(StrEnum):
    """Which diff body the :class:`DiffGrounding` used for this run was built from.

    Recorded rather than inferred, because the three sources have three
    different meanings and only one of them can support closing a reviewer's
    open thread (see :func:`_thread_resolution_block_reason`).
    """

    #: The PR's unified diff, i.e. every change the PR proposes. The only
    #: source whose line index covers the whole surface a prior thread can be
    #: anchored to.
    PR_DIFF = "pr_diff"
    #: A single commit's diff, used when the PR diff endpoint failed. A strict
    #: *subset* of the PR: every file the PR touches that this commit does not
    #: is missing from the index.
    COMMIT_DIFF = "commit_diff"
    #: The fetch succeeded and returned no body at all.
    EMPTY = "empty"


@dataclass(frozen=True)
class SuppressionReport:
    """Why findings were dropped instead of published as inline comments.

    A dropped finding is one GitHub will not let us anchor, and the count alone
    is not actionable: "the file was never in the diff" and "the line moved out
    of every hunk" call for different responses from whoever reads the review.
    Both reasons are kept so the published body can state which one applied.
    """

    #: The path is not among the files this diff touched at all — deleted code,
    #: a renamed-away path, or a path the model invented.
    path_unknown: int = 0
    #: The path is in the diff, but no line of the claimed span is in a hunk.
    line_ungrounded: int = 0

    @property
    def total(self) -> int:
        return self.path_unknown + self.line_ungrounded


#: Rendered into every review body that dropped something. Kept next to
#: :class:`SuppressionReport` so the wording cannot drift from the counts it
#: describes, and templated rather than f-stringed so the log line and the two
#: bodies cannot disagree about what a suppressed finding means.
_SUPPRESSION_TEMPLATE = (
    "> ℹ️ **Haunter**: {total} of {findings} findings could not be anchored as "
    "inline comments ({reasons}). {delivery}"
)

#: Rendered when the diff the model read was larger than the prefix the
#: anchoring view could parse. Distinct from suppression: nothing was dropped,
#: but the inline comments below are known to cover only part of the change.
_TRUNCATION_NOTICE = (
    "> ℹ️ **Haunter**: the diff read for this review was truncated before it "
    "could be anchored, so findings in the truncated portion may be missing "
    "from the inline comments."
)

_DROPPED_FINDINGS_TEMPLATE = (
    "> ℹ️ **Haunter**: {dropped} of {findings} findings did not fit in this "
    "comment and are not shown below; the full set is in the Haunter dashboard."
)

#: Characters held back from a rendered finding list so the disclosure block,
#: which :func:`_bound_review_body` prepends, still fits under the length bound.
#: A default only: suppression + truncation + dropped combined already exceed
#: it (~620 chars), so callers whose disclosure is known up front must pass
#: :func:`_fit_reserve_chars` explicitly instead of relying on this.
_DISCLOSURE_RESERVE_CHARS = 400


def _fit_reserve_chars(*, disclosure_len: int, footer_len: int) -> int:
    """Room :func:`_render_findings_section` must hold back for text it never sees.

    The fitted list later grows by the disclosure block (prepended with a blank
    line by :func:`_bound_review_body`) and the footer (appended with a blank
    line by the caller), so the reserve covers both lengths plus the two
    separators. Without it the final clamp trims the findings tail after
    ``dropped`` was counted, and the count understates what is missing.
    ``disclosure_len`` may be an upper bound when the dropped line is only
    known after the fit — over-holding drops at most a finding the disclosure
    then truthfully reports.
    """
    return disclosure_len + footer_len + 4

_PATH_UNKNOWN_REASON = (
    "the file was not part of the reviewed diff, so it was deleted, renamed "
    "away, or never touched by this change"
)
_LINE_UNGROUNDED_REASON = "the cited line falls outside every changed hunk"


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


def _grounding_lookup_path(value: Any) -> str | None:
    """``value`` as a key of :attr:`DiffGrounding.line_index`, or ``None``.

    The index keys are produced by the diff parser itself
    (``auditor._diff_header_path`` -> ``auditor._validate_repo_path``), so the
    model's path is put through the *same* validation rather than through the
    structured-output sanitizer. That distinction is the whole point:
    ``sanitize_output_path`` HTML-escapes and redacts, so it turns ``src/a&b.py``
    into ``src/a&amp;b.py``, which is not a key the parser ever produces — every
    finding on such a file was silently dropped. Escaping belongs on markup this
    module renders into a body; ``path`` is a JSON field GitHub matches literally
    against the diff, so it is published exactly as validated.

    Reusing the parser's validator (rather than a second local copy) is also what
    keeps a path that the parser *rejected* from reaching the publish layer: the
    two can then never disagree about what a usable repo-relative path is.
    """
    try:
        normalized = _validate_repo_path(value)
    except ValueError:
        return None
    if not isinstance(normalized, str) or not normalized:
        return None
    return normalized


def _build_grounded_comments(
    findings: Sequence[ReviewFinding],
    grounding: DiffGrounding,
) -> tuple[list[dict[str, Any]], SuppressionReport]:
    """Turn findings into inline review comments anchored to real diff lines.

    ``ReviewFinding.file_path`` is only ``min_length=1`` and ``line_start`` /
    ``line_end`` are ``ge=1`` with no upper bound (code_reviewer.py:92-100), so
    every coordinate the model emits is attacker-controlled and unverified.
    GitHub rejects a review whose inline comment points at a file or line outside
    the diff hunks with HTTP 422, and ``create_pr_review`` posts the review body
    and *every* comment in one atomic request — so one bad coordinate discards
    every valid comment with it. Only a coordinate the diff actually supports is
    emitted; the rest are dropped and counted, so suppression is never silent.

    Only the *new* side of each hunk is indexed (the parser records no old-side
    line numbers), so every comment anchors with ``side: "RIGHT"`` and a finding
    about deleted code cannot be anchored at all — it lands in
    :attr:`SuppressionReport.path_unknown` and is disclosed in the review body
    rather than vanishing. Supporting ``side: "LEFT"`` would need an old-side
    index, which is the diff parser's to own.

    This mirrors ``audit_publisher.validate_finding_coordinates`` (the auditor
    path, which already does this) against ``DiffGrounding``, without importing
    it: that helper is typed to ``AuditFinding`` and would only duck-type here.

    Returns ``(comments, report)``.
    """
    comments: list[dict[str, Any]] = []
    path_unknown = 0
    line_ungrounded = 0
    for finding in findings:
        file_path = _grounding_lookup_path(finding.file_path)
        if file_path is None:
            path_unknown += 1
            continue
        lines_in_diff = grounding.line_index.get(file_path)
        if not lines_in_diff:
            # Two different failures, kept apart: a path the diff never touched
            # (deleted, renamed, or invented) says nothing about the line, while
            # a path whose hunks held none of the claimed lines is a coordinate
            # problem. The body states which, because the fix differs.
            if file_path in grounding.valid_paths:
                line_ungrounded += 1
            else:
                path_unknown += 1
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
                line_ungrounded += 1
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
    return comments, SuppressionReport(
        path_unknown=path_unknown,
        line_ungrounded=line_ungrounded,
    )


def _count_findings_by_severity(
    findings: Sequence[ReviewFinding],
) -> tuple[int, int, int]:
    """Returns (blockers, warnings, suggestions)."""
    blockers = sum(1 for f in findings if f.severity.lower() in ("critical", "high"))
    warnings = sum(1 for f in findings if f.severity.lower() == "medium")
    suggestions = sum(1 for f in findings if f.severity.lower() == "low")
    return blockers, warnings, suggestions


def _render_findings_table(findings: Sequence[ReviewFinding]) -> str:
    """Render a clean, scannable GFM table of findings."""
    if not findings:
        return ""
    severity_icons = {
        "critical": "🛑 Critical",
        "high": "🚨 High",
        "medium": "⚠️ Warning",
        "low": "💡 Note",
    }
    rows = [
        "| Severity | Category | File & Line | Summary |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for f in findings:
        icon_sev = severity_icons.get(f.severity.lower(), f.severity.upper())
        # Every cell is GFM table content: an unescaped `|` in a category,
        # path, or summary splits the row. Escape all three, not just the
        # summary — a filename can carry one as legitimately as prose can.
        cat = f.category.replace("_", " ").title().replace("|", "\\|")
        safe_path = f.file_path.replace("|", "\\|")
        loc = (
            f"`{safe_path}:{f.line_start}`"
            if f.line_start == f.line_end
            else f"`{safe_path}:{f.line_start}-{f.line_end}`"
        )
        summary_first_line = f.critique.splitlines()[0].strip()
        if len(summary_first_line) > 85:
            summary_first_line = summary_first_line[:82] + "..."
        summary_clean = summary_first_line.replace("|", "\\|")
        rows.append(f"| {icon_sev} | {cat} | {loc} | {summary_clean} |")
    return "\n".join(rows)


def _render_review_footer(review_id: uuid.UUID, target_sha: str) -> str:
    commit_short = target_sha[:7] if target_sha else "head"
    dashboard_base = (
        settings.frontend_url.rstrip("/")
        if settings.frontend_url
        else "https://haunter.dev"
    )
    dashboard_link = f"{dashboard_base}/reviews/{review_id}"
    return (
        "---\n"
        f"<sub>⚡ Powered by **Haunter** • Commit: `{commit_short}` • "
        f"[View Run Trace in Dashboard]({dashboard_link})</sub>"
    )


def _render_findings_section(
    header: str,
    findings: Sequence[ReviewFinding],
    *,
    heading: str = "",
    budget: int = MAX_GITHUB_COMMENT_CHARS,
    reserve: int = _DISCLOSURE_RESERVE_CHARS,
) -> tuple[str, int]:
    """``header`` plus as many rendered findings as fit in ``budget`` characters.

    :func:`bound_github_body` clamps the finished body, so a long finding list is
    silently cut with nothing but a ``[TRUNCATED]`` marker at the end — a reader
    of the comment has no way to tell that most of the review is missing. The
    list is therefore fitted here, with room held back for the line that says how
    many did not fit, and the count is returned so the caller can say so.

    ``heading`` is appended only when there is at least one finding to put under
    it. ``reserve`` is the room held back for the disclosure block and footer
    the caller adds after the fit; pass :func:`_fit_reserve_chars` with the
    actual lengths whenever they exceed the default. Returns ``(body, dropped)``.
    """
    prefix = f"{header}{heading}" if findings else header
    rendered: list[str] = []
    dropped = 0
    # The reserve is charged even when nothing is dropped: the disclosure block
    # is prepended to the fitted result afterwards, so the finding list has to
    # leave room for it either way.
    remaining = max(0, budget - len(prefix) - reserve)

    severity_icons = {
        "critical": "🛑",
        "high": "🚨",
        "medium": "⚠️",
        "low": "💡",
    }

    for index, finding in enumerate(findings, 1):
        icon = severity_icons.get(finding.severity.lower(), "🔍")
        cat = finding.category.replace("_", " ").title()
        sev = finding.severity.upper()
        loc = (
            f"{finding.file_path}:{finding.line_start}"
            if finding.line_start == finding.line_end
            else f"{finding.file_path}:{finding.line_start}-{finding.line_end}"
        )

        block_parts = [
            f"\n<details open>\n<summary>{icon} <b>[{sev} · {cat}]</b> <code>{loc}</code></summary>\n",
            f"\n**Problem:**\n{finding.critique}\n",
        ]
        if finding.suggested_patch and finding.suggested_patch.strip():
            # Route through the canonical renderer so a patch that triggered
            # secret redaction is shown as a plain (non-applyable) block with
            # its warning, never as a one-click ```suggestion. Only the
            # remediation tail is embedded — this block already renders its
            # own header and Problem section.
            remediation_md = format_github_suggestion(finding)
            marker = "**Remediation:**"
            if marker in remediation_md:
                remediation_tail = remediation_md.split(marker, 1)[1].strip()
                block_parts.append(
                    f"\n**Suggested Remediation:**\n{remediation_tail}\n"
                )
        block_parts.append("\n</details>\n")

        block = "".join(block_parts)
        if len(block) > remaining:
            dropped = len(findings) - index + 1
            break
        remaining -= len(block)
        rendered.append(block)
    return prefix + "".join(rendered), dropped


def _bound_review_body(header: str, disclosure: str) -> str:
    """``bound_github_body`` with ``disclosure`` pinned to the top of the body.

    The bound trims the *end* of a body, and the field most able to fill one is
    ``review.summary`` — itself clamped to GitHub's whole comment ceiling. A
    disclosure appended after the summary is therefore exactly what a verbose
    model would push off the end, silently reinstating the omission it exists to
    report. Prepending makes that unfalsifiable: nothing after the disclosure can
    remove it, and a caveat about a review reads better above its headline than
    underneath it.
    """
    if not disclosure:
        return bound_github_body(header)
    return bound_github_body(f"{disclosure}\n\n{header}")


def _suppression_disclosure(
    *,
    report: SuppressionReport,
    findings_total: int,
    grounding_clipped: bool,
    delivery: str,
) -> str:
    """Markdown telling the reader what this review could not show them.

    Two independent omissions, kept as two sentences: findings that were dropped
    (with the reason, since "the file was not in the diff" and "the line is
    outside every hunk" are different problems), and a diff that was clipped
    before it could be anchored — where nothing was dropped, but every inline
    comment in the review is known to cover only part of the change.

    Returns ``""`` when there is nothing to disclose.
    """
    parts: list[str] = []
    if report.total:
        reasons = "; ".join(
            reason
            for reason, count in (
                (_PATH_UNKNOWN_REASON, report.path_unknown),
                (_LINE_UNGROUNDED_REASON, report.line_ungrounded),
            )
            if count
        )
        parts.append(
            _SUPPRESSION_TEMPLATE.format(
                total=report.total,
                findings=findings_total,
                reasons=reasons,
                delivery=delivery,
            )
        )
    if grounding_clipped:
        parts.append(_TRUNCATION_NOTICE)
    return "\n\n".join(parts)


async def _resolve_publish_sha(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    enqueue_sha: str,
    token: Any,
) -> tuple[str, bool]:
    """Resolve the PR head the review should be published against.

    ``review.commit_sha`` is pinned when the webhook is delivered
    (webhooks.py:1287), but the PR diff is fetched live at analysis time. A push
    landing in between makes the two disagree, and posting the review against
    the enqueue-time SHA anchors every inline comment to a commit the reviewer
    can no longer see. ``GET /pulls/{n}`` is the only endpoint that reports the
    live head, so it is consulted here and the resulting SHA is used for both
    the fallback commit-diff fetch and the publish.

    What this does **not** establish, and what the returned flag reports: the
    diff body is fetched *after* this call and carries no commit identity of its
    own, so a push landing between the two is indistinguishable from one that
    did not. The live head is therefore a lower bound on the diff that was
    read — never a proof that the two are the same commit. The flag says whether
    even that lower bound was confirmed, so the caller can log the weaker case
    instead of implying a guarantee it does not have.

    Falling back to the enqueue SHA when the head cannot be resolved is
    deliberate: the review would otherwise be discarded over a transient
    metadata failure, and the caller logs the unverified pin either way.

    Returns ``(commit_sha, live_head_verified)``.
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
        return enqueue_sha, False

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
        return enqueue_sha, False

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
    return live_sha, True


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
    """True only on positive evidence that this run's diff answered the thread.

    Closing a reviewer's open finding is destructive: the code is still there,
    the finding is still true, and nothing on GitHub tells the reviewer it was
    dismissed by an agent. So every branch below must be a statement *about the
    anchor*, and none of them may be a statement about what this run could see.

    * ``isOutdated`` — GitHub's own determination that the code the comment was
      anchored to is no longer at that position. It is computed over the PR's
      complete diff, so unlike the grounding it cannot be weakened by this
      module's 60_000-char parse bound, and it is the only usable signal for a
      thread whose ``line`` GitHub has already nulled. It is *not* proof the
      finding was fixed — a refactor outdates an anchor too — and that residual
      is accepted because the selection is restricted to this pipeline's own
      threads and corroboration is impossible for exactly these nodes.
    * the anchored ``path`` being present in :attr:`DiffGrounding.line_index`
      and the anchored ``line`` absent from that path's set. Both halves are
      required: presence of the path is what makes the negative line result
      meaningful.

    Everything else — an unindexed path, an empty line set, a null or non-int
    ``line`` — is *unknown*, and unknown is not addressed. ``line_index`` is a
    view of a bounded prefix of a diff that the fetch layer allows up to
    ``MAX_TEXT_RESPONSE_BYTES`` and the review prompt embeds in full, so "the
    path is not in the index" is far more often "this run never read the file"
    than "the finding was fixed".
    """
    if thread.get("isOutdated") is True:
        return True
    path = thread.get("path")
    if not isinstance(path, str):
        return False
    lines = grounding.line_index.get(path)
    if not lines:
        return False
    line = thread.get("line")
    if isinstance(line, bool) or not isinstance(line, int):
        return False
    return line not in lines


def _thread_resolution_block_reason(
    *,
    grounding: DiffGrounding,
    grounding_source: GroundingSource,
    published_comments: int,
) -> str | None:
    """Why this run must not close any prior thread, or ``None`` when it may.

    :func:`_thread_is_addressed` decides whether an individual anchor is
    addressed. This decides whether the grounding is *fit to answer the question
    at all* — and it is checked separately, before any API call, because an
    incomplete grounding makes the answer wrong in one direction only: every
    thread this run never read reads as stale.

    * **source** — only the PR diff covers the whole surface a prior thread can
      be anchored to. The single-commit fallback is a strict subset, and an empty
      body indexes nothing at all.
    * **clipping** — :attr:`DiffGrounding.clipped` means the parse saw only the
      first ``AUDIT_MAX_DIFF_CHARS`` of a larger body. Every path past the cut is
      indistinguishable from a path that was fixed.
    * **published comments** — a run that anchored nothing replaced nothing. Its
      review says nothing about any earlier finding, and closing the earlier
      findings would erase the only record of them. This is also the cheapest
      signal that the model and the grounding have drifted apart, which is what
      the two checks above are about.
    """
    if published_comments < 1:
        return "no_inline_comments_published"
    if grounding_source is not GroundingSource.PR_DIFF:
        return f"grounding_source={grounding_source.value}"
    if grounding.clipped:
        return "diff_truncated"
    if not grounding.line_index:
        return "empty_grounding"
    return None


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

    This answers "is *this* anchor answered"; whether the grounding is complete
    enough to answer that at all is :func:`_thread_resolution_block_reason`'s
    question, and it is asked before this function runs.
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
    grounding_source: GroundingSource,
    published_comments: int,
    token: Any,
) -> None:
    """Close the prior Haunter threads that this run's diff has answered.

    Called only after a review with inline findings has been *accepted* by
    GitHub — never on a suppressed run, a failed publish, or the summary-only
    422 fallback, because a thread closed for a finding that was never
    republished is worse than an open one. Resolving after the publish (rather
    than before it) is the same reason: the reverse order would close threads and
    then discover the replacement review was rejected.

    The run must also have a grounding fit to answer the question at all —
    :func:`_thread_resolution_block_reason` decides that, and it is asked here,
    before any API call, so a run that cannot justify closing anything spends no
    request discovering it. This is the difference between a re-review that tidies
    up after itself and one that deletes findings it never read.

    A "re-review" is not a stored flag — it is simply the presence of prior
    threads this pipeline authored. On a PR's first review there are none, so the
    selection is empty and nothing is sent; there is no separate code path, and
    therefore no way for the two to disagree.

    The prior threads come from ``fetch_review_threads`` (GraphQL), because only
    GraphQL yields a real ``PullRequestReviewThread`` id — a
    ``PullRequestReviewComment`` node id is a different type and the mutation
    rejects it. Both the page walk and the mutation count stay inside the caps
    ``github_client`` applies to them (``MAX_REVIEW_THREAD_PAGES`` = 5 and
    ``MAX_REVIEW_THREAD_RESOLVES`` = 25), which is where those bounds live rather
    than here.

    Never raises. A resolution failure leaves threads open, which is the state
    the reviewer can still act on, so it can never fail an already-published
    review.
    """
    block_reason = _thread_resolution_block_reason(
        grounding=grounding,
        grounding_source=grounding_source,
        published_comments=published_comments,
    )
    if block_reason is not None:
        logger.info(
            "review_orchestrator: thread_resolution_skipped repo=%s/%s pr=%s "
            "reason=%s grounding_source=%s published_comments=%d",
            sanitize_log_value(owner, 128),
            sanitize_log_value(repo, 128),
            pr_number,
            block_reason,
            grounding_source.value,
            published_comments,
        )
        return

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


async def _record_terminal_state(review_id: uuid.UUID, *, failure_reason: str) -> None:
    """Force ``error`` onto a review that left the pipeline non-terminal.

    A row stuck at ``in_progress`` is never revisited: the webhook dedup guard
    counts that status as already handled and no reaper exists, so every
    redelivery of the delivery that started it answers ``duplicate`` and the
    review is lost. That is why every exit path of this module — including the
    ones reached when the database itself is what failed — has to land on a
    terminal status.

    Two properties matter:

    * a **fresh session** is used, for the same reason the orchestrator's timeout
      handler opens one: the failed run's own session is in whatever state the
      failure left it, and reusing it is how a DB error turns into a second DB
      error;
    * an **already-terminal row is left alone**. ``completed`` in particular
      means GitHub has the review, and a failure in a step that runs *after* the
      publish must not rewrite that into ``error``.

    Never raises — a state that could not be recorded must not become a second,
    unrelated failure on top of the one being recorded.
    """
    try:
        async with async_session_maker() as error_session:
            stale = await error_session.get(CodeReview, review_id)
            if stale is None or stale.status in _TERMINAL_REVIEW_STATUSES:
                return
            stale.status = REVIEW_STATUS_ERROR
            stale.failure_reason = failure_reason[:_FAILURE_REASON_MAX_CHARS]
            await error_session.commit()
    except Exception as persist_err:
        logger.error(
            "review_orchestrator: terminal_state_persist_failed review_id=%s "
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


def _build_status_badge(risk_score: int, blockers: int = 0) -> str:
    # The blocker count overrides a low score: a model-authored risk_score in
    # the Approved band alongside critical/high findings is a false clean bill.
    # Any blocker floors the badge at Changes Recommended, never Approved.
    if risk_score >= 80:
        return "🛑 `Changes Required`"
    elif risk_score > 30 or blockers > 0:
        return "⚠️ `Changes Recommended`"
    return "✅ `Approved`"


def _build_risk_badge(risk_score: int) -> str:
    if risk_score <= 30:
        return "🟢 `Low Risk`"
    elif risk_score <= 70:
        return "🟡 `Moderate Risk`"
    return "🔴 `Critical / High Risk`"


async def run_code_review_pipeline(review_id: uuid.UUID) -> None:
    """
    Execute end-to-end code review pipeline for a CodeReview row, under a hard
    wall-clock bound. Runs asynchronously in BackgroundTasks or via AWS Lambda
    self-invocation.

    Every exit lands on a terminal ``code_reviews.status``. That includes the
    exits nobody plans for: a hang is cancelled here, an unexpected exception
    anywhere in the body is terminalised, and both record the state from a fresh
    session — because "in_progress" is never revisited, the webhook dedup guard
    counts it as already handled and no reaper exists.

    An already-terminal row is never downgraded, so a failure *after* a
    successful publish leaves ``completed`` intact.

    Raises nothing except :class:`asyncio.CancelledError`, which is recorded and
    then re-raised: cancellation is the caller's decision rather than this
    module's error, and swallowing it breaks ``asyncio.run`` / BackgroundTasks
    teardown. The Lambda handler reports it as a failed run, which is what a
    cancelled run is.
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
        await _record_terminal_state(
            review_id,
            failure_reason="code review wall-clock timeout",
        )
    except asyncio.CancelledError:
        logger.error(
            "review_orchestrator: review_id=%s cancelled — forcing error state",
            review_id,
        )
        await _record_terminal_state(
            review_id,
            failure_reason="code review cancelled before reaching a terminal state",
        )
        raise
    except Exception as unexpected:
        # Anything the body did not anticipate — a `session.commit()` that fails,
        # a session teardown that raises. Without this the row stays
        # "in_progress" forever and every redelivery of the delivery that
        # started it answers "duplicate".
        logger.exception(
            "review_orchestrator: review_id=%s unhandled pipeline failure: %s",
            review_id,
            unexpected,
        )
        await _record_terminal_state(
            review_id,
            failure_reason=f"Code review pipeline crashed: {unexpected}",
        )


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
            review.status = REVIEW_STATUS_ERROR
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

        # 2. Fetch diff, and pin the commit it should be published against.
        #
        # `review.commit_sha` is the enqueue-time SHA (webhooks.py:1287 pins it
        # when the delivery arrives) while the PR diff below is fetched live, so
        # a push landing in between makes the two disagree. The live head is
        # therefore resolved first, and every fetch below — including the
        # commit-diff fallback — uses it.
        #
        # What that buys is *ordering*, not identity: the diff body arrives after
        # this call and carries no commit id, so a push landing between the
        # resolution and the fetch is indistinguishable from one that did not.
        # The analysed diff is at least as new as the resolved head; it is not
        # proven to be that head's diff. Every path where even that ordering
        # guarantee is unavailable is logged, because the review body is a
        # statement about specific lines and a reader deserves to know how firm
        # the commit underneath it is.
        diff_text = ""
        target_sha = review.commit_sha
        sha_pin_verified = True
        grounding_source = GroundingSource.COMMIT_DIFF
        try:
            if review.pr_number:
                target_sha, sha_pin_verified = await _resolve_publish_sha(
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
                    grounding_source = GroundingSource.PR_DIFF
                except Exception as pr_diff_err:
                    # The consequence is stated, not just the cause: the
                    # grounding built from a single commit is a strict subset of
                    # the PR, which is why nothing downstream may treat "not in
                    # the index" as "fixed" from here on.
                    logger.warning(
                        "review_orchestrator: fetch_pull_request_diff failed for %s/%s PR #%d (%s), falling back to commit diff — "
                        "the anchoring view now covers only that one commit, so prior threads will not be resolved this run",
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
            review.status = REVIEW_STATUS_ERROR
            review.summary = f"Failed to fetch git diff: {diff_err}"
            review.failure_reason = _truncate_failure_reason(
                f"Failed to fetch git diff: {diff_err}"
            )
            await session.commit()
            return

        if not diff_text.strip():
            grounding_source = GroundingSource.EMPTY

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
            review.status = REVIEW_STATUS_ERROR
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
            comments, suppression = _build_grounded_comments(
                result.output.findings, grounding
            )
            if suppression.total:
                # Never silent, and never only in a log: the count and its reason
                # also go into the body below, because the reader of the review
                # is the only person who can act on a finding that is missing
                # from it.
                logger.info(
                    "review_orchestrator: inline_comment_suppression repo=%s/%s "
                    "pr=%s suppressed=%d path_unknown=%d line_ungrounded=%d "
                    "published=%d total=%d grounding_clipped=%s",
                    sanitize_log_value(repo.owner, 128),
                    sanitize_log_value(repo.name, 128),
                    review.pr_number,
                    suppression.total,
                    suppression.path_unknown,
                    suppression.line_ungrounded,
                    len(comments),
                    len(result.output.findings),
                    grounding.clipped,
                )

            review_event = "REQUEST_CHANGES" if review.risk_score >= 80 else "COMMENT"

            blockers, warnings, suggestions = _count_findings_by_severity(
                result.output.findings
            )
            findings_breakdown = (
                f"{blockers} Blockers • {warnings} Warnings • {suggestions} Suggestions"
            )
            status_badge = _build_status_badge(review.risk_score, blockers)

            if review.risk_score >= 80 or blockers > 0:
                alert_callout = (
                    "> [!CAUTION]\n"
                    f"> **Action Required**: {blockers} blocker finding(s) detected. Please resolve critical issues before merge.\n"
                )
            elif review.risk_score > 30:
                alert_callout = (
                    "> [!WARNING]\n"
                    f"> **Action Recommended**: {warnings} warning(s) detected. Review the proposed remediations below.\n"
                )
            else:
                alert_callout = (
                    "> [!TIP]\n"
                    "> **Clean Review**: No blocking issues detected. Changes look safe and well-structured.\n"
                )

            table_md = _render_findings_table(result.output.findings)
            table_section = (
                f"\n### 📋 Findings Summary\n\n{table_md}\n" if table_md else ""
            )
            footer_md = _render_review_footer(review.id, target_sha)

            body_content = (
                f"## ⚡ Haunter Code Review\n\n"
                f"| Status | Risk Score | Findings Breakdown |\n"
                f"| :--- | :--- | :--- |\n"
                f"| {status_badge} | `{review.risk_score}/100` ({risk_badge}) | {findings_breakdown} |\n\n"
                f"{alert_callout}\n"
                f"### 📝 Executive Summary\n\n"
                f"{review.summary}\n"
                f"{table_section}\n"
                f"{footer_md}"
            )

            body = _bound_review_body(
                body_content,
                _suppression_disclosure(
                    report=suppression,
                    findings_total=len(result.output.findings),
                    grounding_clipped=grounding.clipped,
                    delivery=(
                        "They are recorded in the Haunter dashboard and are "
                        "not shown in this review."
                    ),
                ),
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
                # The retry exists because the first request was rejected
                # for its inline comments, and it carries every finding
                # inline in the body instead — so it needs the same total
                # bound the first one had, or it 422s for the same reason.
                fallback_header = (
                    f"## ⚡ Haunter Code Review (Summary)\n\n"
                    f"| Status | Risk Score | Findings Breakdown |\n"
                    f"| :--- | :--- | :--- |\n"
                    f"| {status_badge} | `{review.risk_score}/100` ({risk_badge}) | {findings_breakdown} |\n\n"
                    f"{alert_callout}\n"
                    f"### 📝 Executive Summary\n\n"
                    f"{review.summary}\n"
                    f"{table_section}"
                )
                # The suppression half of the disclosure needs no fit count, so it
                # is built before the fit and its length — plus the worst-case
                # dropped line and the footer appended below — is held back
                # from the finding list. The default 400-char reserve is
                # smaller than suppression + truncation + dropped combined, so
                # the final bound used to clip findings the `dropped` count
                # said were shown.
                suppression_note = _suppression_disclosure(
                    report=suppression,
                    findings_total=len(result.output.findings),
                    grounding_clipped=grounding.clipped,
                    delivery=(
                        "They are recorded in the Haunter dashboard and are "
                        "not shown in this review."
                    ),
                )
                max_dropped_line = len(
                    _DROPPED_FINDINGS_TEMPLATE.format(
                        dropped=len(result.output.findings),
                        findings=len(result.output.findings),
                    )
                )
                fallback_body, dropped = _render_findings_section(
                    fallback_header,
                    result.output.findings,
                    heading="\n### 🔍 Detailed Findings & Remediations\n",
                    reserve=_fit_reserve_chars(
                        disclosure_len=len(suppression_note) + 2 + max_dropped_line,
                        footer_len=len(footer_md),
                    ),
                )
                fallback_body = f"{fallback_body}\n\n{footer_md}"
                # The suppression block already rides at the top of `body`, so
                # prepending the length-bound disclosure here keeps both
                # statements above anything the bound can trim. The retry
                # carries every finding inline in place of the dropped inline
                # comments, so it must repeat the suppression disclosure too —
                # otherwise findings unanchorable to any hunk vanish silently.
                dropped_note = (
                    _DROPPED_FINDINGS_TEMPLATE.format(
                        dropped=dropped,
                        findings=len(result.output.findings),
                    )
                    if dropped
                    else ""
                )
                fallback_disclosure = "\n\n".join(
                    part for part in (suppression_note, dropped_note) if part
                )
                fallback_body = _bound_review_body(
                    fallback_body,
                    fallback_disclosure,
                )

                try:
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
                    return
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
                # No resolution on this path: the summary-only retry carries
                # every finding in its body, but it was posted because GitHub
                # would not accept the anchored one, so the replacement for a
                # prior inline thread is not demonstrably in place.
                return

            # Past this point the review is on GitHub. Everything below records
            # that fact or tidies up after it, so it sits deliberately outside
            # the publish `try` above: an untyped failure in a commit or in the
            # courtesy step must not be classified as an invalid-coordinate 422
            # and trigger a second review for one that already succeeded.
            logger.info(
                "review_orchestrator: posted PR review on %s/%s PR #%d (event=%s)",
                repo.owner,
                repo.name,
                review.pr_number,
                review_event,
            )
            if not sha_pin_verified:
                logger.warning(
                    "review_orchestrator: sha_pin_unverified repo=%s/%s pr=%s "
                    "commit_sha=%s — the live PR head could not be read, so the "
                    "diff analysed for this review is not confirmed to belong to "
                    "the commit it is published against",
                    sanitize_log_value(repo.owner, 128),
                    sanitize_log_value(repo.name, 128),
                    review.pr_number,
                    sanitize_log_value(target_sha, 64),
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
            # Only now, with the replacement review accepted by GitHub and the
            # row already terminal, may the findings this one supersedes be
            # closed. Never reached on a failed publish, on the summary-only
            # 422 fallback, or on a suppressed run.
            await _resolve_superseded_threads(
                owner=repo.owner,
                repo=repo.name,
                pr_number=review.pr_number,
                grounding=grounding,
                grounding_source=grounding_source,
                published_comments=len(comments),
                token=token,
            )
        else:
            # Commit comment for push without PR. Nothing is anchored inline on
            # this path — a push has no review surface to anchor to — so the
            # finding list is the whole review and its length bound is the only
            # thing that can drop one. Same total bound as the PR review body:
            # N findings concatenated exceed GitHub's comment ceiling long
            # before any single field does.
            blockers, warnings, suggestions = _count_findings_by_severity(
                result.output.findings
            )
            findings_breakdown = (
                f"{blockers} Blockers • {warnings} Warnings • {suggestions} Suggestions"
            )
            status_badge = _build_status_badge(review.risk_score, blockers)

            if review.risk_score >= 80 or blockers > 0:
                alert_callout = (
                    "> [!CAUTION]\n"
                    f"> **Action Required**: {blockers} blocker finding(s) detected.\n"
                )
            elif review.risk_score > 30:
                alert_callout = (
                    "> [!WARNING]\n"
                    f"> **Action Recommended**: {warnings} warning finding(s) detected.\n"
                )
            else:
                alert_callout = (
                    "> [!TIP]\n"
                    "> **Clean Review**: No blocking issues detected.\n"
                )

            table_md = _render_findings_table(result.output.findings)
            table_section = (
                f"\n### 📋 Findings Summary\n\n{table_md}\n" if table_md else ""
            )
            footer_md = _render_review_footer(review.id, review.commit_sha)

            push_header = (
                f"## ⚡ Haunter Push Review\n\n"
                f"| Status | Risk Score | Findings Breakdown |\n"
                f"| :--- | :--- | :--- |\n"
                f"| {status_badge} | `{review.risk_score}/100` ({risk_badge}) | {findings_breakdown} |\n\n"
                f"{alert_callout}\n"
                f"### 📝 Executive Summary\n\n"
                f"{review.summary}\n"
                f"{table_section}"
            )

            # The dropped line is only known after the fit, so the worst case
            # (every finding dropped) is held back along with the footer
            # appended below — otherwise the final bound clips findings the
            # `dropped` count said were shown, as on the fallback path above.
            max_dropped_line = len(
                _DROPPED_FINDINGS_TEMPLATE.format(
                    dropped=len(result.output.findings),
                    findings=len(result.output.findings),
                )
            )
            body, dropped = _render_findings_section(
                push_header,
                result.output.findings,
                heading="\n### 🔍 Actionable Findings & Remediations\n",
                reserve=_fit_reserve_chars(
                    disclosure_len=max_dropped_line + 2,
                    footer_len=len(footer_md),
                ),
            )
            body = f"{body}\n\n{footer_md}"
            body = _bound_review_body(
                body,
                _DROPPED_FINDINGS_TEMPLATE.format(
                    dropped=dropped,
                    findings=len(result.output.findings),
                )
                if dropped
                else "",
            )

            try:
                await create_commit_comment(
                    owner=repo.owner,
                    repo=repo.name,
                    commit_sha=review.commit_sha,
                    body=body,
                    token=token,
                )
            except Exception as gh_err:
                logger.error(
                    "review_orchestrator: failed to post commit comment on %s/%s @ %s: %s",
                    repo.owner,
                    repo.name,
                    review.commit_sha,
                    gh_err,
                )
                review.status = REVIEW_STATUS_ERROR
                review.failure_reason = _truncate_failure_reason(
                    f"Failed to publish commit comment: {gh_err}"
                )
                await session.commit()
                await session.refresh(review)
                return
            logger.info(
                "review_orchestrator: posted commit comment on %s/%s @ %s",
                repo.owner,
                repo.name,
                review.commit_sha,
            )
            review.status = REVIEW_STATUS_COMPLETED
            await session.commit()
            await session.refresh(review)
