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
