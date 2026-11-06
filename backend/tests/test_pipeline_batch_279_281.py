"""Hermetic regression tests for the #279-281 fix batch (no DB, no network).

#279: truncated patches fail generation-time validation as retriable format
      failures and never persist (PatchFormatRetryExhausted after the regen).
#280: provider-reported token-budget truncation (finish_reason) raises
      FixOutputTruncatedError without retry and without persisting.
#281: the mirror push gate fail-closes (shared PR applier, exact paths) so a
      verify-pass guarantees a dry-run-pass; legacy fuzzy helpers unchanged.

#282 paths (Phase-8 regen loop, refinement pre-check, fresh base SHA) need a
DB-backed pipeline run; they are covered by the synced suites
(test_pr_dry_run_retry_276, test_orchestrator_pipeline, test_pr_feedback,
test_followup_reply), not here.
"""

from __future__ import annotations

import base64
import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.github.pr import GitHubPRValidationError, dry_run_patch_at_sha
from app.orchestrator import (
    _ensure_refinement_applies,
    _pr_dry_run_feedback,
    _resolve_phase_base_sha,
)
from app.subagents.fix_generator import (
    _FIX_MAX_TOKENS,
    FixGenerationError,
    FixOutputTruncatedError,
    PatchFormatRejected,
    PatchRejected,
    _call_and_parse,
    _call_with_format_retry,
    _detect_truncation,
    _provider_reported_truncation,
    _reported_finish_reason,
    _validate_patch,
)

pytestmark = pytest.mark.usefixtures("scoped_llm_client")

VALID_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-line2\n"
    "+LINE2\n"
    " line3\n"
)

TRUNCATED_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-line2\n"
)


def _llm_response(patch_text: str, **extra) -> dict:
    response = {
        "content": json.dumps(
            {"patch": patch_text, "confidence": 80, "strategy_notes": "ok"}
        ),
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    response.update(extra)
    return response


# ------------------------------------------------------------------ #279 ---


def test_truncated_hunk_rejected_as_retriable_format_failure() -> None:
    """A hunk whose body misses its declared counts is a format reject."""
    with pytest.raises(PatchFormatRejected):
        _validate_patch(TRUNCATED_PATCH)


def test_strict_gate_keeps_path_violations_hard() -> None:
    """Traversal still raises the base (non-retriable) PatchRejected."""
    traversal = (
        "--- a/../../etc/passwd\n"
        "+++ b/../../etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
    )
    with pytest.raises(PatchRejected) as exc_info:
        _validate_patch(traversal)
    assert type(exc_info.value) is PatchRejected


def test_valid_patch_passes_strict_gate() -> None:
    _validate_patch(VALID_PATCH)


@pytest.mark.anyio
async def test_truncated_patch_regenerates_then_exhausts_without_persisting() -> None:
    """Truncated LLM output enters the format-retry regen, then exhausts."""
    first = _llm_response(TRUNCATED_PATCH)
    second = _llm_response(TRUNCATED_PATCH)
    mock_complete = AsyncMock(side_effect=[first, second])
    with (
        patch("app.subagents.fix_generator.LLMClient.complete", mock_complete),
        pytest.raises(PatchFormatRejected),
    ):
        await _call_with_format_retry(
            messages=[{"role": "system", "content": "sys"}],
            diagnosis_summary="ImportError in app.py",
            prior_attempt=None,
            db=AsyncMock(),
            run_id=uuid.uuid4(),
        )
    assert mock_complete.call_count == 2


# ------------------------------------------------------------------ #280 ---


def test_truncation_reasons_are_positive_evidence_only() -> None:
    assert _provider_reported_truncation({"finish_reason": "length"})
    assert _provider_reported_truncation({"finish_reason": "max_tokens"})
    assert _provider_reported_truncation({"finish_reason": "max_output_tokens"})
    assert not _provider_reported_truncation({"finish_reason": "stop"})
    assert not _provider_reported_truncation({"finish_reason": "COMPLETED"})
    assert not _provider_reported_truncation({})
    assert not _provider_reported_truncation({"finish_reason": ""})


def test_reported_reason_reads_stop_reason_and_normalises() -> None:
    assert _reported_finish_reason({"stop_reason": "max_tokens"}) == "max_tokens"
    assert _reported_finish_reason({"finish_reason": "Length"}) == "length"
    assert _reported_finish_reason({}) is None


@pytest.mark.anyio
async def test_length_truncation_raises_without_retry_or_store() -> None:
    """finish_reason=length raises at once: one LLM call, nothing parsed."""
    mock_complete = AsyncMock(return_value=_llm_response(VALID_PATCH, finish_reason="length"))
    with (
        patch("app.subagents.fix_generator.LLMClient.complete", mock_complete),
        pytest.raises(FixOutputTruncatedError),
    ):
        await _call_and_parse(
            messages=[{"role": "system", "content": "sys"}],
            diagnosis_summary="ImportError in app.py",
            prior_attempt=None,
            db=AsyncMock(),
            run_id=uuid.uuid4(),
        )
    assert mock_complete.call_count == 1


def test_truncation_error_stays_a_generation_error() -> None:
    assert issubclass(FixOutputTruncatedError, FixGenerationError)


@pytest.mark.anyio
async def test_retry_response_truncation_raises() -> None:
    """A cut retry response raises instead of parsing a half object."""
    mock_complete = AsyncMock(
        side_effect=[
            {"content": "not json", "usage": {}},
            _llm_response(VALID_PATCH, finish_reason="max_tokens"),
        ]
    )
    with (
        patch("app.subagents.fix_generator.LLMClient.complete", mock_complete),
        pytest.raises(FixOutputTruncatedError),
    ):
        await _call_and_parse(
            messages=[{"role": "system", "content": "sys"}],
            diagnosis_summary="ImportError in app.py",
            prior_attempt=None,
            db=AsyncMock(),
            run_id=uuid.uuid4(),
        )
    assert mock_complete.call_count == 2


@pytest.mark.anyio
async def test_retry_inferred_truncation_raises() -> None:
    """Unparseable first response then reason-less full-budget retry is cut."""
    mock_complete = AsyncMock(
        side_effect=[
            {"content": "not json", "usage": {"input_tokens": 1, "output_tokens": 10}},
            {
                "content": "not json",
                "usage": {"input_tokens": 1, "output_tokens": _FIX_MAX_TOKENS},
            },
        ]
    )
    with (
        patch("app.subagents.fix_generator.LLMClient.complete", mock_complete),
        pytest.raises(FixOutputTruncatedError),
    ):
        await _call_and_parse(
            messages=[{"role": "system", "content": "sys"}],
            diagnosis_summary="ImportError in app.py",
            prior_attempt=None,
            db=AsyncMock(),
            run_id=uuid.uuid4(),
        )
    assert mock_complete.call_count == 2


@pytest.mark.anyio
async def test_clean_and_unknown_reasons_parse_normally() -> None:
    for response in (
        _llm_response(VALID_PATCH, finish_reason="stop"),
        _llm_response(VALID_PATCH, finish_reason="COMPLETED"),
        _llm_response(VALID_PATCH),
    ):
        with patch(
            "app.subagents.fix_generator.LLMClient.complete",
            AsyncMock(return_value=response),
        ):
            output, _ = await _call_and_parse(
                messages=[{"role": "system", "content": "sys"}],
                diagnosis_summary="ImportError in app.py",
                prior_attempt=None,
                db=AsyncMock(),
                run_id=uuid.uuid4(),
            )
        assert output.confidence == 80


# ------------------------------------------------------------------ #281 ---

SEEDS = {"app.py": b"line1\nline2\nline3\n"}

DEDENTED_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-  line2\n"
    "+LINE2\n"
    " line3\n"
)

REWORDED_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-LINE TWO\n"
    "+LINE2\n"
    " line3\n"
)


def test_gate_accepts_exact_patch() -> None:
    from app.sandbox.mirror import _fail_closed_push_gate

    _fail_closed_push_gate(VALID_PATCH, dict(SEEDS))


def test_gate_rejects_dedented_lines() -> None:
    """Probe 1: dedented lines verify-passed but dry-run-mismatched."""
    from app.sandbox.mirror import _fail_closed_push_gate

    with pytest.raises(ValueError, match="does not apply"):
        _fail_closed_push_gate(DEDENTED_PATCH, dict(SEEDS))


def test_gate_rejects_reworded_deletion() -> None:
    """Probe 2: a reworded '-' line is a mismatch, never silently dropped."""
    from app.sandbox.mirror import _fail_closed_push_gate

    with pytest.raises(ValueError, match="does not apply"):
        _fail_closed_push_gate(REWORDED_PATCH, dict(SEEDS))


def test_gate_rejects_truncated_hunk() -> None:
    from app.sandbox.mirror import _fail_closed_push_gate

    with pytest.raises(ValueError, match="not a valid unified diff"):
        _fail_closed_push_gate(TRUNCATED_PATCH, dict(SEEDS))


def test_gate_rejects_modification_of_missing_file() -> None:
    from app.sandbox.mirror import _fail_closed_push_gate

    ghost = "--- a/ghost.py\n+++ b/ghost.py\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(ValueError, match="missing from the mirrored tree"):
        _fail_closed_push_gate(ghost, dict(SEEDS))


def test_gate_accepts_new_file() -> None:
    from app.sandbox.mirror import _fail_closed_push_gate

    new_file = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+hello\n"
    _fail_closed_push_gate(new_file, dict(SEEDS))


def test_gate_rejects_patch_with_no_file_changes() -> None:
    from app.sandbox.mirror import _fail_closed_push_gate

    delete_missing = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-whatever\n"
    with pytest.raises(ValueError, match="no file changes"):
        _fail_closed_push_gate(delete_missing, dict(SEEDS))


def test_gate_resolves_paths_exactly() -> None:
    from app.sandbox.mirror import _exact_seed_path

    assert _exact_seed_path("b/app.py", SEEDS.keys()) == "app.py"
    assert _exact_seed_path("app.py", {"src/app.py": b"x"}) is None


@pytest.mark.anyio
async def test_push_fails_before_any_write_on_fuzzy_only_patch() -> None:
    """A dedented patch raises before the first blob/tree/ref HTTP call."""
    import httpx
    import respx

    from app.sandbox.mirror import push_patch_to_mirror

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as rx:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="does not apply"):
                await push_patch_to_mirror(
                    client,
                    "org/haunter-sandbox-runner",
                    branch="sandbox/gate-probe",
                    patch_text=DEDENTED_PATCH,
                    commit_message="probe",
                    token="tok",
                    workflow_filename="wf.yml",
                    workflow_content="name: test\n",
                    seed_files=dict(SEEDS),
                )
    assert not rx.calls


# ------------------------------------------------- #281b: gate fail-close ---
# Deletion bodies run the fail-closed applier; missing paths pass only for
# true creations. All hermetic: pure functions, no DB, no network.

WRONG_CONTEXT_DELETION = (
    "--- a/app.py\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-line1\n"
    "-WRONG\n"
    "-line3\n"
)

FULL_DELETION = (
    "--- a/app.py\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-line1\n"
    "-line2\n"
    "-line3\n"
)

GHOST_ADDITION = (
    "--- a/ghost.py\n"
    "+++ b/ghost.py\n"
    "@@ -0,0 +1 @@\n"
    "+hello\n"
)


def test_gate_rejects_wrong_context_deletion() -> None:
    """A deletion whose '-' lines mismatch the tree is rejected, not counted."""
    from app.sandbox.mirror import _fail_closed_push_gate

    with pytest.raises(ValueError, match="does not apply"):
        _fail_closed_push_gate(WRONG_CONTEXT_DELETION, dict(SEEDS))


def test_gate_accepts_exact_deletion() -> None:
    """An exactly-quoted deletion applies (empty result is not a mismatch)."""
    from app.sandbox.mirror import _fail_closed_push_gate

    _fail_closed_push_gate(FULL_DELETION, dict(SEEDS))


def test_gate_rejects_non_creation_patch_for_missing_path() -> None:
    """A pure addition naming a missing path passes only as --- /dev/null."""
    from app.sandbox.mirror import _fail_closed_push_gate

    with pytest.raises(ValueError, match="missing from the mirrored tree"):
        _fail_closed_push_gate(GHOST_ADDITION, dict(SEEDS))


# ------------------------------------------------- #280b: second layer ---
# finish_reason absent + whole output budget spent => inferred truncation.
# Reported reasons keep their verdict; a successful parse clears suspicion.


def test_detect_truncation_needs_positive_evidence() -> None:
    assert _detect_truncation({"finish_reason": "length"}, 100) == "length"
    assert _detect_truncation({"finish_reason": "stop"}, 100) is None
    full = {"usage": {"output_tokens": 100}}
    assert (
        _detect_truncation(full, 100) == "length (inferred from usage.output_tokens)"
    )
    assert _detect_truncation({"usage": {"output_tokens": 10}}, 100) is None
    assert _detect_truncation({}, 100) is None


@pytest.mark.anyio
async def test_none_reason_full_budget_cut_raises_without_retry() -> None:
    """Reason-less response that fills the budget and fails to parse is cut."""
    bad = {
        "content": "not json",
        "usage": {"input_tokens": 1, "output_tokens": _FIX_MAX_TOKENS},
    }
    mock_complete = AsyncMock(return_value=bad)
    with (
        patch("app.subagents.fix_generator.LLMClient.complete", mock_complete),
        pytest.raises(FixOutputTruncatedError),
    ):
        await _call_and_parse(
            messages=[{"role": "system", "content": "sys"}],
            diagnosis_summary="ImportError in app.py",
            prior_attempt=None,
            db=AsyncMock(),
            run_id=uuid.uuid4(),
        )
    assert mock_complete.call_count == 1


@pytest.mark.anyio
async def test_full_budget_valid_parse_still_returns() -> None:
    """A full-budget response that parses was complete; suspicion is dropped."""
    good = _llm_response(VALID_PATCH)
    good["usage"] = {"input_tokens": 10, "output_tokens": _FIX_MAX_TOKENS}
    mock_complete = AsyncMock(return_value=good)
    with patch("app.subagents.fix_generator.LLMClient.complete", mock_complete):
        output, _ = await _call_and_parse(
            messages=[{"role": "system", "content": "sys"}],
            diagnosis_summary="ImportError in app.py",
            prior_attempt=None,
            db=AsyncMock(),
            run_id=uuid.uuid4(),
        )
    assert output.confidence == 80
    assert mock_complete.call_count == 1


# ------------------------------------------------- #282: F4/F5/FB -------
# Hermetic orchestrator-guard tests: helpers + respx, no DB, no full run.


@pytest.mark.anyio
async def test_refinement_precheck_mismatch_raises_without_commit() -> None:
    """A stale refined patch raises before commit_patch is ever reached."""
    with (
        patch(
            "app.github_client.fetch_branch_sha", AsyncMock(return_value="f" * 40)
        ),
        patch(
            "app.github.pr.dry_run_patch_at_sha",
            AsyncMock(return_value="patch does not apply to app.py"),
        ),
        patch("app.github.pr.commit_patch", AsyncMock()) as mock_commit,
        pytest.raises(GitHubPRValidationError),
    ):
        await _ensure_refinement_applies(
            owner="o",
            repo_name="r",
            target_branch="main",
            patch_text=VALID_PATCH,
            token="tok",
        )
    mock_commit.assert_not_called()


@pytest.mark.anyio
async def test_refinement_precheck_clean_returns_sha() -> None:
    with (
        patch(
            "app.github_client.fetch_branch_sha", AsyncMock(return_value="f" * 40)
        ),
        patch("app.github.pr.dry_run_patch_at_sha", AsyncMock(return_value=None)),
    ):
        sha = await _ensure_refinement_applies(
            owner="o",
            repo_name="r",
            target_branch="main",
            patch_text=VALID_PATCH,
            token="tok",
        )
    assert sha == "f" * 40


@pytest.mark.anyio
async def test_phase_base_sha_refresh_changed() -> None:
    decisions: list[str] = []
    with patch(
        "app.github_client.fetch_branch_sha", AsyncMock(return_value="n" * 40)
    ):
        sha = await _resolve_phase_base_sha(
            owner="o",
            repo_name="r",
            base_branch="main",
            pinned_sha="p" * 40,
            run_id=uuid.uuid4(),
            token="tok",
            decisions=decisions,
        )
    assert sha == "n" * 40
    assert decisions == ["pr_base_sha_refreshed"]


@pytest.mark.anyio
async def test_phase_base_sha_refresh_failed_falls_back() -> None:
    decisions: list[str] = []
    with patch(
        "app.github_client.fetch_branch_sha",
        AsyncMock(side_effect=RuntimeError("dns")),
    ):
        sha = await _resolve_phase_base_sha(
            owner="o",
            repo_name="r",
            base_branch="main",
            pinned_sha="p" * 40,
            run_id=uuid.uuid4(),
            token="tok",
            decisions=decisions,
        )
    assert sha == "p" * 40
    assert decisions == ["pr_base_sha_refresh_failed"]


@pytest.mark.anyio
async def test_stale_mismatch_passes_at_refreshed_sha() -> None:
    """The same patch mismatches the stale pin but applies at the fresh HEAD."""
    stale_sha, fresh_sha = "0" * 40, "1" * 40
    stale_body = base64.b64encode(b"line1\nSTALE\nline3\n").decode("ascii")
    fresh_body = base64.b64encode(b"line1\nline2\nline3\n").decode("ascii")
    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as rx:
        rx.get(f"/repos/o/r/contents/app.py?ref={stale_sha}").mock(
            return_value=httpx.Response(200, json={"content": stale_body})
        )
        rx.get(f"/repos/o/r/contents/app.py?ref={fresh_sha}").mock(
            return_value=httpx.Response(200, json={"content": fresh_body})
        )
        stale_mismatch = await dry_run_patch_at_sha(
            owner="o", repo="r", patch_text=VALID_PATCH, base_sha=stale_sha,
            token="tok",
        )
        decisions: list[str] = []
        with patch(
            "app.github_client.fetch_branch_sha", AsyncMock(return_value=fresh_sha)
        ):
            resolved = await _resolve_phase_base_sha(
                owner="o",
                repo_name="r",
                base_branch="main",
                pinned_sha=stale_sha,
                run_id=uuid.uuid4(),
                token="tok",
                decisions=decisions,
            )
        fresh_mismatch = await dry_run_patch_at_sha(
            owner="o", repo="r", patch_text=VALID_PATCH, base_sha=resolved,
            token="tok",
        )
    assert stale_mismatch is not None
    assert decisions == ["pr_base_sha_refreshed"]
    assert fresh_mismatch is None


def test_structurally_invalid_patch_gets_format_feedback() -> None:
    """Validation failures regen with header-format feedback, not byte-exact."""
    feedback = _pr_dry_run_feedback(
        mismatch="patch is not a valid unified diff: short hunk",
        validation_error="short hunk",
    )
    assert "NOT a valid unified diff" in feedback
    assert "'---'/'+++'" in feedback
    assert "@@" in feedback
    assert "byte-exact" not in feedback


def test_apply_mismatch_keeps_byte_exact_feedback() -> None:
    feedback = _pr_dry_run_feedback(
        mismatch="patch does not apply to app.py", validation_error=None
    )
    assert "does NOT apply" in feedback
    assert "byte-exact" in feedback


@pytest.mark.anyio
async def test_push_succeeds_for_exact_patch() -> None:
    import httpx
    import respx

    from app.sandbox.mirror import push_patch_to_mirror

    repo_full = "org/haunter-sandbox-runner"
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as rx:
        rx.post(f"/repos/{repo_full}/git/trees").respond(201, json={"sha": "t"})
        rx.post(f"/repos/{repo_full}/git/commits").respond(201, json={"sha": "c"})
        rx.post(f"/repos/{repo_full}/git/refs").respond(
            201, json={"ref": "refs/heads/sandbox/gate-ok", "sha": "c"}
        )
        async with httpx.AsyncClient() as client:
            sha = await push_patch_to_mirror(
                client,
                repo_full,
                branch="sandbox/gate-ok",
                patch_text=VALID_PATCH,
                commit_message="ok",
                token="tok",
                workflow_filename="wf.yml",
                workflow_content="name: test\n",
                seed_files=dict(SEEDS),
            )
    assert sha == "c"
