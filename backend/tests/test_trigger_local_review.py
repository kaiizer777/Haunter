"""
Tests for scripts/trigger_local_review.py.

Covers:
- _build_payload:
  - produces the fields the pull_request ingress reads (action, PR number/state,
    draft, head/base refs and SHAs, repository identity, sender)
  - respects --sha / --pr / --repo / --base-ref / --head-ref inputs
  - emits a distinct 40-hex base SHA
- _sign_payload: exact known-good HMAC-SHA256 hex for a fixed body + secret
- Signature compatibility with the real ingress verifier
  (app.services.audit_pipeline.verify_github_signature):
  - the generated header verifies True for the exact bytes
  - a tampered body verifies False
  - a wrong secret verifies False
- _REVIEWABLE_ACTIONS stays in sync with app.github.pr.REVIEWABLE_PR_ACTIONS
"""

from __future__ import annotations

import importlib
import re

import httpx
import pytest


def _load_trigger():
    """Import the script inside each test: it has module-level logging/sys.path side effects."""
    return importlib.import_module("scripts.trigger_local_review")


def test_build_payload_produces_required_fields() -> None:
    trigger = _load_trigger()
    sha = "a" * 40

    payload = trigger._build_payload(
        repo="saif-org/haunter-demo",
        pr_number=7,
        sha=sha,
        base_ref="main",
        head_ref="feature/local-dev",
        action="synchronize",
        sender="local-dev",
        sender_type="User",
    )

    assert payload["action"] == "synchronize"
    assert payload["number"] == 7

    pr = payload["pull_request"]
    assert pr["number"] == 7
    assert pr["state"] == "open"
    assert pr["draft"] is False
    assert pr["head"]["ref"] == "feature/local-dev"
    assert pr["head"]["sha"] == sha
    assert pr["base"]["ref"] == "main"
    assert re.fullmatch(r"[0-9a-f]{40}", pr["base"]["sha"])
    assert pr["base"]["sha"] != sha
    assert pr["user"] == {"login": "local-dev", "type": "User"}

    repo = payload["repository"]
    assert repo["name"] == "haunter-demo"
    assert repo["full_name"] == "saif-org/haunter-demo"
    assert repo["owner"]["login"] == "saif-org"
    assert repo["default_branch"] == "main"

    assert payload["sender"] == {"login": "local-dev", "type": "User"}


def test_build_payload_defaults_to_supplied_sha_and_pr() -> None:
    trigger = _load_trigger()
    sha = "b" * 40

    payload = trigger._build_payload(
        repo="acme/api",
        pr_number=42,
        sha=sha,
        base_ref="master",
        head_ref="feat/x",
        action="opened",
        sender="dev",
        sender_type="User",
    )

    assert payload["pull_request"]["number"] == 42
    assert payload["pull_request"]["head"]["sha"] == sha
    assert payload["repository"]["owner"]["login"] == "acme"
    assert payload["repository"]["name"] == "api"
    assert payload["pull_request"]["base"]["ref"] == "master"


def test_build_payload_uses_supplied_base_sha() -> None:
    trigger = _load_trigger()

    payload = trigger._build_payload(
        repo="saif-org/haunter-demo",
        pr_number=7,
        sha="a" * 40,
        base_ref="main",
        head_ref="feature/local-dev",
        action="opened",
        sender="local-dev",
        sender_type="User",
        base_sha="d" * 40,
    )

    assert payload["pull_request"]["base"]["sha"] == "d" * 40
    assert payload["pull_request"]["head"]["sha"] == "a" * 40


def test_build_payload_defaults_to_synthetic_base_sha() -> None:
    trigger = _load_trigger()
    sha = "a" * 40

    payload = trigger._build_payload(
        repo="saif-org/haunter-demo",
        pr_number=7,
        sha=sha,
        base_ref="main",
        head_ref="feature/local-dev",
        action="opened",
        sender="local-dev",
        sender_type="User",
    )

    base_sha = payload["pull_request"]["base"]["sha"]
    assert re.fullmatch(r"[0-9a-f]{40}", base_sha)
    assert base_sha != sha


def test_sign_payload_matches_known_good_hmac() -> None:
    trigger = _load_trigger()

    body = b'{"hello":"world"}'
    header = trigger._sign_payload(body, "devsecret")

    assert (
        header
        == "sha256=55e885b8b23e4e9c5c66e6bcdeedc51cdb9a5db9a6c7773e85a8c3ddd30d2ef4"
    )


def test_signature_verifies_with_real_ingress_verifier() -> None:
    from app.services.audit_pipeline import verify_github_signature

    trigger = _load_trigger()
    body = b'{"action":"opened","number":1}'
    secret = "devsecret"
    header = trigger._sign_payload(body, secret)

    assert verify_github_signature(body, header, secret) is True
    assert verify_github_signature(body + b" ", header, secret) is False
    assert verify_github_signature(body, header, "other-secret") is False


def test_reviewable_actions_mirror_ingress_contract() -> None:
    from app.github.pr import REVIEWABLE_PR_ACTIONS

    trigger = _load_trigger()

    assert set(trigger._REVIEWABLE_ACTIONS) == REVIEWABLE_PR_ACTIONS


async def _send(
    trigger,
    status_code: int,
    body: dict,
    headers: dict[str, str] | None = None,
):
    """POST through the script's helper against a MockTransport (no network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body, headers=headers or {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await trigger._post_webhook(
            client,
            url="http://testserver/webhooks/github",
            body=b"{}",
            headers={},
            timeout=5.0,
        )
    return trigger._outcome_from_response(response)


@pytest.mark.parametrize(
    ("status_code", "body", "expected_exit"),
    [
        (
            200,
            {
                "status": "queued",
                "review_id": "11111111-1111-1111-1111-111111111111",
            },
            0,
        ),
        (
            200,
            {
                "status": "queued",
                "reason": "auditor pr audit owns this delivery",
                "audit_id": "audit-abc123def456",
            },
            0,
        ),
        (
            200,
            {"status": "duplicate", "review_id": "22222222-2222-2222-2222-222222222222"},
            3,
        ),
        (200, {"status": "ignored", "reason": "unregistered repository"}, 4),
        (200, {"status": "skipped", "reason": "base branch not allowed"}, 4),
        (500, {"detail": "boom"}, 1),
        (401, {"detail": "Invalid or missing signature"}, 1),
        (429, {"detail": "rate limit"}, 1),
        (503, {"detail": "dispatch failed"}, 1),
    ],
    ids=[
        "queued-review",
        "queued-auditor",
        "duplicate",
        "ignored",
        "skipped",
        "server-error",
        "unauthorized",
        "rate-limited",
        "unavailable",
    ],
)
async def test_response_maps_to_exit_code(status_code, body, expected_exit) -> None:
    trigger = _load_trigger()

    outcome = await _send(trigger, status_code, body)

    assert outcome.exit_code == expected_exit


async def test_queued_review_reports_review_id_and_background_note() -> None:
    trigger = _load_trigger()

    outcome = await _send(
        trigger,
        200,
        {"status": "queued", "review_id": "review-123"},
    )

    assert outcome.exit_code == 0
    assert any("review-123" in line for line in outcome.lines)
    assert any("BackgroundTask" in line for line in outcome.lines)


async def test_auditor_queued_reports_audit_id_and_no_code_review_row() -> None:
    trigger = _load_trigger()

    outcome = await _send(
        trigger,
        200,
        {
            "status": "queued",
            "reason": "auditor pr audit owns this delivery",
            "audit_id": "audit-abc123def456",
        },
    )

    assert outcome.exit_code == 0
    assert any("audit-abc123def456" in line for line in outcome.lines)
    assert any("No CodeReview row" in line for line in outcome.lines)


async def test_duplicate_advises_fresh_sha() -> None:
    trigger = _load_trigger()

    outcome = await _send(
        trigger,
        200,
        {"status": "duplicate", "review_id": "review-123"},
    )

    assert outcome.exit_code == 3
    assert any("--sha" in line for line in outcome.lines)


async def test_ignored_reason_is_printed_verbatim_with_hint() -> None:
    trigger = _load_trigger()

    outcome = await _send(
        trigger,
        200,
        {"status": "ignored", "reason": "unregistered repository"},
    )

    assert outcome.exit_code == 4
    assert any("unregistered repository" in line for line in outcome.lines)
    assert any("--seed-repo" in line for line in outcome.lines)


async def test_429_reports_retry_after() -> None:
    trigger = _load_trigger()

    outcome = await _send(
        trigger,
        429,
        {"detail": "Webhook rate limit exceeded"},
        headers={"Retry-After": "42"},
    )

    assert outcome.exit_code == 1
    assert any("42" in line for line in outcome.lines)


@pytest.mark.parametrize(
    "argv",
    [
        ["--repo", "no-slash", "--pr", "1"],
        ["--repo", "owner/", "--pr", "1"],
        ["--repo", "/name", "--pr", "1"],
        ["--repo", "a/b/c", "--pr", "1"],
    ],
)
def test_parse_args_rejects_malformed_repo(argv) -> None:
    trigger = _load_trigger()

    with pytest.raises(SystemExit) as excinfo:
        trigger._parse_args(argv)

    assert excinfo.value.code == 2


@pytest.mark.parametrize("pr", ["0", "-3"])
def test_parse_args_rejects_non_positive_pr(pr) -> None:
    trigger = _load_trigger()

    with pytest.raises(SystemExit) as excinfo:
        trigger._parse_args(["--repo", "saif-org/haunter-demo", "--pr", pr])

    assert excinfo.value.code == 2


def test_parse_args_rejects_malformed_sha() -> None:
    trigger = _load_trigger()

    with pytest.raises(SystemExit) as excinfo:
        trigger._parse_args(
            ["--repo", "saif-org/haunter-demo", "--pr", "1", "--sha", "deadbeef"]
        )

    assert excinfo.value.code == 2


def test_parse_args_rejects_malformed_base_sha() -> None:
    trigger = _load_trigger()

    with pytest.raises(SystemExit) as excinfo:
        trigger._parse_args(
            ["--repo", "saif-org/haunter-demo", "--pr", "1", "--base-sha", "deadbeef"]
        )

    assert excinfo.value.code == 2


def test_parse_args_accepts_base_sha() -> None:
    trigger = _load_trigger()

    args = trigger._parse_args(
        [
            "--repo",
            "saif-org/haunter-demo",
            "--pr",
            "7",
            "--sha",
            "A" * 40,
            "--base-sha",
            "D" * 40,
        ]
    )

    assert args.sha == "a" * 40
    assert args.base_sha == "d" * 40


def test_parse_args_base_sha_defaults_to_none() -> None:
    trigger = _load_trigger()

    args = trigger._parse_args(["--repo", "saif-org/haunter-demo", "--pr", "7"])

    assert args.base_sha is None


def test_parse_args_accepts_valid_inputs() -> None:
    trigger = _load_trigger()

    args = trigger._parse_args(
        ["--repo", "saif-org/haunter-demo", "--pr", "7", "--sha", "A" * 40]
    )

    assert args.repo == "saif-org/haunter-demo"
    assert args.pr == 7
    assert args.sha == "a" * 40
    assert args.action == "opened"
    assert args.base_url == "http://127.0.0.1:7555"
    assert args.seed_repo is False
    assert args.dry_run is False
