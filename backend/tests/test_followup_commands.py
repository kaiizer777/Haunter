"""
Hermetic unit tests for the conversational follow-up command router
(`app.services.followup_commands.parse_followup_command`).

The router is a pure, total function of the comment body — no fixtures, no
engine, no network, no DB — so the whole classification matrix is asserted
here directly.
"""

from __future__ import annotations

import pytest

from app.services.followup_commands import (
    FEEDBACK_CONCLUSION,
    FOLLOWUP_COMMANDS,
    TEST_FIX_CONCLUSION,
    parse_followup_command,
)

#: (comment body, expected command, expected test_only)
COMMAND_CASES: list[tuple[str, str, bool]] = [
    # Canonical spellings.
    ("@haunter fix", "fix", False),
    ("@haunter address", "address", False),
    ("@haunter test-fix", "test-fix", True),
    # Underscore alias normalises onto the canonical command.
    ("@haunter test_fix", "test-fix", True),
    # Mixed / upper case.
    ("@Haunter FIX", "fix", False),
    ("@HAUNTER Address", "address", False),
    ("@haunter TEST-FIX", "test-fix", True),
    ("@HaUnTeR TeSt-FiX", "test-fix", True),
    # Extra whitespace and separators around the command word.
    ("@haunter    fix", "fix", False),
    ("@haunter\t test-fix", "test-fix", True),
    ("@haunter, address", "address", False),
    ("@haunter: fix", "fix", False),
    # Trailing prose after the command is tolerated and ignored.
    ("@haunter fix handle the None input", "fix", False),
    ("@haunter address the timeout flake please", "address", False),
    ("@haunter test-fix just verify, don't commit", "test-fix", True),
    ("@haunter fix: retry with backoff", "fix", False),
    # Prose in front of the mention is tolerated.
    ("hey @haunter please fix the null deref", "fix", False),
    # A mention with no command word keeps the pre-router contract.
    ("@haunter", "fix", False),
    ("@haunter please handle the None input", "fix", False),
    # A command word that is merely a prefix of another word is not a command.
    ("@haunter fixing the docs", "fix", False),
    ("@haunter addressed in the changelog", "fix", False),
]


@pytest.mark.parametrize("body,command,test_only", COMMAND_CASES)
def test_command_matrix(body: str, command: str, test_only: bool) -> None:
    parsed = parse_followup_command(body)
    assert parsed is not None, f"expected a command for {body!r}"
    assert parsed.command == command
    assert parsed.test_only is test_only is (command == "test-fix")


#: Bodies that must never reach the fix pipeline.
NO_OP_BODIES: list[str] = [
    # No mention at all.
    "",
    "LGTM! Merging soon.",
    "@haunterbot fix the build",
    # Explicitly addressed to the read-only auditor, whose zero-mutation
    # invariant forbids committing a patch nobody asked for.
    "@haunter audit",
    "@haunter audit this PR for security issues",
    "@haunter Audit please",
    "@haunter, audit",
    # Non-string / None defensiveness.
    "None",
]


@pytest.mark.parametrize("body", NO_OP_BODIES)
def test_no_op_bodies(body: str) -> None:
    assert parse_followup_command(body) is None


def test_none_body_is_no_op() -> None:
    assert parse_followup_command(None) is None


def test_non_string_body_is_no_op() -> None:
    assert parse_followup_command(1234) is None  # type: ignore[arg-type]
    assert parse_followup_command(["@haunter fix"]) is None  # type: ignore[arg-type]


def test_audit_intent_never_falls_through_to_fix() -> None:
    """`audit_pipeline` reads prose audit requests as manual audits.

    If the fix router disagreed, one comment would queue a read-only audit
    *and* a committing refinement. An explicit fix command still wins, so a
    reviewer asking to fix audit-log parsing must still get a fix run.
    """
    fixed = parse_followup_command("@haunter fix the audit log rotation")
    assert fixed is not None
    assert fixed.command == "fix"

    for body in (
        "@haunter please audit why the job hangs",
        "@haunter could you take an audit of this PR",
        "@haunter AUDIT the retry logic",
        "@haunter audit",
    ):
        assert parse_followup_command(body) is None, body


def test_parsed_commands_are_declared_in_the_public_sets() -> None:
    for _, command, _ in COMMAND_CASES:
        assert command in FOLLOWUP_COMMANDS


def test_conclusion_constants_match_pipeline_contract() -> None:
    # `feedback` is the historical conclusion for fix/address runs and must
    # not drift; `test-fix` is the orchestrator's verify-only sentinel.
    assert FEEDBACK_CONCLUSION == "feedback"
    assert TEST_FIX_CONCLUSION == "test-fix"
    assert FEEDBACK_CONCLUSION != TEST_FIX_CONCLUSION
