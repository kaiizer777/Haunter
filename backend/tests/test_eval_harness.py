"""
Tests for Phase 10 — Eval Harness.

Coverage:
  - Fixture loading and allowlist validation
  - Dry-run eval runner (all fixtures, subset, invalid IDs)
  - EvalResult DB persistence
  - Regression comparator (no regression, regression detected)
  - API endpoints: GET /eval-results, GET /eval-results/{id}, POST /eval/run
    - Admin gate: 403 for non-admin, 403 when ADMIN_USER_ID unset
    - Rate limit path (structural check only — no integration with slowapi in tests)
    - 422 for unknown fixture IDs
    - 404 for unknown eval_id
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _sign_user_id
from app.config import settings
from app.models import EvalResult, ModelConfig, User
from eval.compare import REGRESSION_THRESHOLD, compare_eval, compare_eval_objects
from eval.runner import (
    _load_fixtures,
    _score_context,
    _score_fix,
    _stub_context_output,
    _stub_fix_output,
    run_eval,
)


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def test_load_fixtures_returns_list() -> None:
    """golden_cases.json loads as a non-empty list of dicts."""
    fixtures = _load_fixtures()
    assert isinstance(fixtures, list)
    assert len(fixtures) >= 15
    for f in fixtures:
        assert "id" in f
        assert "repo_ref" in f
        assert "failure_type" in f
        assert "expected_root_cause_keywords" in f
        assert "expected_fix_characteristics" in f


def test_fixtures_no_tenant_repos() -> None:
    """
    Security: no fixture may reference a private/tenant repo pattern.
    All repo_refs must be in 'owner/repo' format and be well-known OSS repos.
    """
    fixtures = _load_fixtures()
    blocked_patterns = ["haunter", "kaiizer", "saif", "bari2", "private"]
    for f in fixtures:
        repo_ref = f["repo_ref"].lower()
        for blocked in blocked_patterns:
            assert blocked not in repo_ref, (
                f"Fixture {f['id']} repo_ref {f['repo_ref']!r} contains blocked pattern {blocked!r}"
            )
        # Must be owner/repo format
        parts = f["repo_ref"].split("/")
        assert len(parts) == 2, f"Fixture {f['id']} repo_ref must be 'owner/repo': {f['repo_ref']!r}"


def test_fixture_failure_types_valid() -> None:
    """All fixtures use one of the four allowed failure types."""
    fixtures = _load_fixtures()
    allowed = {"import_error", "type_error", "assertion", "dependency"}
    for f in fixtures:
        assert f["failure_type"] in allowed, (
            f"Fixture {f['id']} has invalid failure_type: {f['failure_type']!r}"
        )


def test_fixture_ids_unique() -> None:
    """All fixture IDs are unique."""
    fixtures = _load_fixtures()
    ids = [f["id"] for f in fixtures]
    assert len(ids) == len(set(ids)), "Duplicate fixture IDs detected"


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def test_stub_context_output_contains_keywords() -> None:
    """Stub context output must embed all expected keywords."""
    fixture = {
        "id": "test-001",
        "repo_ref": "testowner/testrepo",
        "commit_sha": "abc123",
        "failure_type": "import_error",
        "simulated_log_snippet": "ImportError: no module named foo",
        "expected_root_cause_keywords": ["ImportError", "foo", "missing"],
        "expected_fix_characteristics": {"confidence_min": 60},
    }
    result = _stub_context_output(fixture)
    assert "diagnosis_summary" in result
    for kw in ["ImportError", "foo", "missing"]:
        assert kw in result["diagnosis_summary"]


def test_stub_fix_output_meets_confidence() -> None:
    """Stub fix output confidence must equal the fixture's minimum."""
    fixture = {
        "id": "test-002",
        "repo_ref": "testowner/testrepo",
        "commit_sha": "abc123",
        "failure_type": "type_error",
        "simulated_log_snippet": "TypeError: NoneType",
        "expected_root_cause_keywords": ["TypeError"],
        "expected_fix_characteristics": {
            "touches_files": ["setup.cfg"],
            "confidence_min": 75,
        },
    }
    result = _stub_fix_output(fixture)
    assert result["confidence_score"] == 75
    assert "patch_text" in result
    assert "setup.cfg" in result["patch_text"]


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def test_score_context_full_match() -> None:
    """All keywords present → score 1.0."""
    fixture = {
        "expected_root_cause_keywords": ["ImportError", "module", "missing"]
    }
    ctx = {"diagnosis_summary": "ImportError: module missing from path"}
    result = _score_context(fixture, ctx)
    assert result["score"] == 1.0
    assert result["total_keywords"] == 3
    assert len(result["matched_keywords"]) == 3


def test_score_context_partial_match() -> None:
    """Partial keyword match → score between 0 and 1."""
    fixture = {"expected_root_cause_keywords": ["ImportError", "module", "missing"]}
    ctx = {"diagnosis_summary": "ImportError found"}  # only 1/3
    result = _score_context(fixture, ctx)
    assert 0.0 < result["score"] < 1.0


def test_score_context_no_match() -> None:
    """No keywords present → score 0.0."""
    fixture = {"expected_root_cause_keywords": ["ImportError", "module", "missing"]}
    ctx = {"diagnosis_summary": "some unrelated text about nothing"}
    result = _score_context(fixture, ctx)
    assert result["score"] == 0.0


def test_score_fix_meets_threshold() -> None:
    """Confidence ≥ min → meets_threshold=True, score > 0."""
    fixture = {"expected_fix_characteristics": {"confidence_min": 60}}
    fix = {"confidence_score": 75, "strategy_notes": "looks good"}
    result = _score_fix(fixture, fix)
    assert result["meets_threshold"] is True
    assert result["score"] > 0.0


def test_score_fix_below_threshold() -> None:
    """Confidence < min → meets_threshold=False."""
    fixture = {"expected_fix_characteristics": {"confidence_min": 80}}
    fix = {"confidence_score": 50, "strategy_notes": "low confidence"}
    result = _score_fix(fixture, fix)
    assert result["meets_threshold"] is False


# ---------------------------------------------------------------------------
# run_eval — dry-run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_eval_dry_run_all_fixtures(db: AsyncSession) -> None:
    """Dry-run over all fixtures persists one EvalResult with valid scores."""
    result = await run_eval(golden_ids=None, model_config_id=None, dry_run=True)

    assert result.id is not None
    assert result.overall_accuracy is not None
    assert 0.0 <= result.overall_accuracy <= 1.0
    assert result.model_config_id is None

    scores = result.per_subagent_scores
    assert "context_gatherer" in scores
    assert "fix_generator" in scores
    assert "overall" in scores
    assert scores["mode"] == "DRY-RUN"

    # Dry-run stubs inject all keywords → context avg should be 1.0
    assert scores["context_gatherer"]["average_score"] == 1.0


@pytest.mark.asyncio
async def test_run_eval_dry_run_subset(db: AsyncSession) -> None:
    """Dry-run with subset of fixture IDs processes only those fixtures."""
    result = await run_eval(
        golden_ids=["fixture-001", "fixture-002"],
        model_config_id=None,
        dry_run=True,
    )
    assert result.overall_accuracy is not None
    scores = result.per_subagent_scores
    assert scores["overall"]["total_fixtures"] == 2


@pytest.mark.asyncio
async def test_run_eval_dry_run_persists_to_db(db: AsyncSession) -> None:
    """EvalResult row is actually written to the database."""
    result = await run_eval(
        golden_ids=["fixture-001"],
        model_config_id=None,
        dry_run=True,
    )
    # Reload from DB in this session
    db_result = await db.execute(select(EvalResult).where(EvalResult.id == result.id))
    row = db_result.scalar_one_or_none()
    assert row is not None
    assert row.overall_accuracy == result.overall_accuracy


@pytest.mark.asyncio
async def test_run_eval_invalid_fixture_id() -> None:
    """Unknown fixture ID raises ValueError — never silently proceeds."""
    with pytest.raises(ValueError, match="Unknown fixture IDs"):
        await run_eval(
            golden_ids=["fixture-DOES-NOT-EXIST"],
            model_config_id=None,
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_run_eval_with_valid_model_config_id(db: AsyncSession, user_factory) -> None:
    """Passing a valid model_config_id links it in EvalResult."""
    config = ModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)

    result = await run_eval(
        golden_ids=["fixture-001"],
        model_config_id=config.id,
        dry_run=True,
    )
    assert result.model_config_id == config.id


@pytest.mark.asyncio
async def test_run_eval_invalid_model_config_id() -> None:
    """Non-existent model_config_id raises ValueError."""
    with pytest.raises(ValueError, match="model_config_id"):
        await run_eval(
            golden_ids=["fixture-001"],
            model_config_id=uuid.uuid4(),  # random UUID not in DB
            dry_run=True,
        )


# ---------------------------------------------------------------------------
# compare_eval
# ---------------------------------------------------------------------------


def _make_eval_result(
    overall_accuracy: float,
    ctx_avg: float,
    fix_avg: float,
    pass_rate: float,
) -> EvalResult:
    """Construct an in-memory EvalResult for comparison tests."""
    from datetime import datetime, timezone

    er = EvalResult(
        overall_accuracy=overall_accuracy,
        per_subagent_scores={
            "context_gatherer": {"average_score": ctx_avg},
            "fix_generator": {"average_score": fix_avg},
            "overall": {"pass_rate": pass_rate, "total_fixtures": 5, "passed": int(pass_rate * 5), "failed": 0},
            "mode": "DRY-RUN",
        },
    )
    er.id = uuid.uuid4()
    er.created_at = datetime.now(timezone.utc)
    return er


def test_compare_no_regression() -> None:
    """Same or improved scores → verdict OK."""
    a = _make_eval_result(0.80, 0.90, 0.75, 0.80)
    b = _make_eval_result(0.82, 0.92, 0.76, 0.82)
    report = compare_eval_objects(a, b)
    assert report["verdict"] == "OK"
    assert report["any_regression"] is False
    assert len(report["regressions"]) == 0


def test_compare_regression_detected() -> None:
    """Drop > 5% in any metric → REGRESSION."""
    a = _make_eval_result(0.90, 0.95, 0.88, 0.90)
    b = _make_eval_result(0.80, 0.85, 0.75, 0.80)  # drops of 10%
    report = compare_eval_objects(a, b)
    assert report["verdict"] == "REGRESSION"
    assert report["any_regression"] is True
    assert len(report["regressions"]) > 0


def test_compare_borderline_no_regression() -> None:
    """Drop exactly at threshold (5%) does NOT trigger regression (threshold is >5%)."""
    a = _make_eval_result(0.80, 0.80, 0.80, 0.80)
    b = _make_eval_result(0.75, 0.75, 0.75, 0.75)  # exactly 5% drop
    report = compare_eval_objects(a, b)
    # delta = -0.05, regressed = delta < -0.05 → False
    assert report["verdict"] == "OK"


def test_compare_borderline_triggers_regression() -> None:
    """Drop of more than 5.01% → REGRESSION."""
    a = _make_eval_result(0.80, 0.80, 0.80, 0.80)
    b = _make_eval_result(0.749, 0.749, 0.749, 0.749)  # 5.1% drop
    report = compare_eval_objects(a, b)
    assert report["verdict"] == "REGRESSION"


@pytest.mark.asyncio
async def test_compare_eval_db_lookup(db: AsyncSession) -> None:
    """compare_eval loads from DB and produces correct diff."""
    # Create two real EvalResults in DB
    a = await run_eval(golden_ids=["fixture-001"], model_config_id=None, dry_run=True)
    b = await run_eval(golden_ids=["fixture-001"], model_config_id=None, dry_run=True)

    report = await compare_eval(a.id, b.id)
    # Two identical dry-run results → no regression
    assert report["verdict"] == "OK"
    assert report["eval_a"]["id"] == str(a.id)
    assert report["eval_b"]["id"] == str(b.id)


@pytest.mark.asyncio
async def test_compare_eval_not_found() -> None:
    """ValueError on unknown eval_id."""
    with pytest.raises(ValueError, match="not found"):
        await compare_eval(uuid.uuid4(), uuid.uuid4())


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


def _make_admin_client(make_auth_client, admin_id: uuid.UUID) -> AsyncClient:
    return make_auth_client(admin_id)


@pytest.mark.asyncio
async def test_eval_list_requires_auth(client: AsyncClient) -> None:
    """GET /eval-results without auth → 401 or 403."""
    response = await client.get("/eval-results")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_eval_list_non_admin_403(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """GET /eval-results with non-admin user → 403."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", str(uuid.uuid4())):  # different ID
        async with make_auth_client(user.id) as ac:
            response = await ac.get("/eval-results")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_eval_list_no_admin_configured_403(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """GET /eval-results when ADMIN_USER_ID not set → 403 (fail-closed)."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", None):
        async with make_auth_client(user.id) as ac:
            response = await ac.get("/eval-results")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_eval_list_admin_200(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """GET /eval-results with admin user → 200 with list."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as ac:
            response = await ac.get("/eval-results")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_eval_get_by_id_admin(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """GET /eval-results/{id} with admin → 200 with correct data."""
    user = await user_factory(github_id=None)

    # Create a real EvalResult first
    er = await run_eval(golden_ids=["fixture-001"], model_config_id=None, dry_run=True)

    with patch.object(settings, "admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as ac:
            response = await ac.get(f"/eval-results/{er.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(er.id)
    assert "overall_accuracy" in data
    assert "context_gatherer_avg" in data
    assert "fix_generator_avg" in data


@pytest.mark.asyncio
async def test_eval_get_by_id_not_found(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """GET /eval-results/{id} with unknown ID → 404."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as ac:
            response = await ac.get(f"/eval-results/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_eval_get_by_id_non_admin_403(make_auth_client) -> None:
    """GET /eval-results/{id} for non-admin → 403."""
    # Create user in its own isolated session to avoid connection lifecycle
    # interference from run_eval in the previous test closing the shared connection.
    from app.db import async_session_maker as _sm
    from app.models import User as _User

    user_id = uuid.uuid4()
    async with _sm() as _db:
        u = _User(
            id=user_id,
            github_id=int(user_id.int % 1_000_000_000 + 200_000_000),
            github_username="non-admin-tester",
            access_token="tok_nonadmin",
        )
        _db.add(u)
        await _db.commit()

    with patch.object(settings, "admin_user_id", str(uuid.uuid4())):
        async with make_auth_client(user_id) as ac:
            response = await ac.get(f"/eval-results/{uuid.uuid4()}")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_eval_run_admin_dry_run(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """POST /eval/run with admin, dry_run=True → 201 with EvalResult."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as ac:
            response = await ac.post(
                "/eval/run",
                json={"fixture_ids": ["fixture-001", "fixture-002"], "dry_run": True},
            )
    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert "overall_accuracy" in data
    assert data["mode"] == "DRY-RUN"


@pytest.mark.asyncio
async def test_eval_run_non_admin_403(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """POST /eval/run for non-admin → 403."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", str(uuid.uuid4())):
        async with make_auth_client(user.id) as ac:
            response = await ac.post(
                "/eval/run",
                json={"fixture_ids": ["fixture-001"], "dry_run": True},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_eval_run_unknown_fixture_422(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """POST /eval/run with unknown fixture ID → 422."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as ac:
            response = await ac.post(
                "/eval/run",
                json={"fixture_ids": ["fixture-TOTALLY-FAKE"], "dry_run": True},
            )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_eval_run_no_auth_401_or_403(client: AsyncClient) -> None:
    """POST /eval/run without session cookie → 401 or 403."""
    response = await client.post(
        "/eval/run",
        json={"fixture_ids": ["fixture-001"], "dry_run": True},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_eval_run_response_no_patch_text(
    make_auth_client, user_factory, db: AsyncSession
) -> None:
    """POST /eval/run response must not expose raw patch text or prompts."""
    user = await user_factory(github_id=None)

    with patch.object(settings, "admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as ac:
            response = await ac.post(
                "/eval/run",
                json={"fixture_ids": ["fixture-001"], "dry_run": True},
            )
    assert response.status_code == 201
    data = response.json()
    response_text = json.dumps(data)
    # Patch text marker from stub should not leak into API response
    assert "--- a/" not in response_text
    assert "+++ b/" not in response_text
