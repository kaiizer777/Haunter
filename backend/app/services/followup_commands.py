"""
Conversational follow-up command router.

Classifies the ``@haunter`` mention carried by an ``issue_comment`` or
``pull_request_review_comment`` webhook payload so the webhook router knows
whether the comment is asking Haunter for a fix, and which kind.

Fix commands, matched case-insensitively on the first word after the mention
(trailing prose, punctuation and extra whitespace are ignored):

- ``fix`` / ``address`` — full refinement. The existing pipeline generates a
  patch with ``fix_generator``, verifies it through the GitHub Actions
  sandbox runner, commits the verified patch onto the existing ``haunter/*``
  PR branch, and confirms in the thread that asked.
- ``test-fix`` (alias ``test_fix``) — verify-only. The patch is generated and
  sandbox-verified, the verdict is reported in the thread, and the PR branch
  is deliberately left untouched.

A mention with no recognisable command word (``"@haunter please handle the
None input"``) keeps the pre-router contract and is treated as ``fix``.

A mention whose command word belongs to another subsystem (``audit``, handled
by the read-only auditor) is *not* a fix request: the fix pipeline must stay a
strict no-op for it, because the auditor's zero-mutation invariant forbids
committing a patch nobody asked for.

The router is total and pure — no I/O, no DB, no exceptions — so it is
unit-testable hermetically and can never fail a webhook delivery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional, Union

FollowupCommandName = Literal["fix", "address", "test-fix"]

#: First word after an ``@haunter`` mention, mapped to its canonical command.
#: ``test_fix`` is accepted as an alias so both common spellings work.
_COMMAND_ALIASES: dict[str, FollowupCommandName] = {
    "fix": "fix",
    "address": "address",
    "test-fix": "test-fix",
    "test_fix": "test-fix",
}

#: Commands that must verify without committing to the PR branch.
TEST_ONLY_COMMANDS: frozenset[str] = frozenset({"test-fix"})

#: The public grammar: every command the router can emit. Derived from the
#: alias table so it can never drift from what the router actually accepts.
FOLLOWUP_COMMANDS: frozenset[str] = frozenset(_COMMAND_ALIASES.values())

#: Command words owned by a different Haunter subsystem. A mention starting
#: with one of these is never a fix request.
RESERVED_COMMANDS: frozenset[str] = frozenset({"audit"})

#: Conclusion stored on child ``Run`` rows for verify-only follow-ups.
#: ``fix``/``address`` runs keep the historical ``"feedback"`` value.
TEST_FIX_CONCLUSION: str = "test-fix"
FEEDBACK_CONCLUSION: str = "feedback"

#: The mention itself. ``\b`` keeps it from firing on ``@haunterbot``.
_MENTION_RE: re.Pattern[str] = re.compile(r"@haunter\b", re.IGNORECASE)

#: The first word following the mention. Separators (whitespace, ``:``,
#: ``,``) are tolerated so ``"@haunter, fix this"`` parses like
#: ``"@haunter fix this"``.
_FIRST_WORD_RE: re.Pattern[str] = re.compile(
    r"@haunter[\s:,]*([A-Za-z][A-Za-z_-]*)", re.IGNORECASE
)


@dataclass(frozen=True)
class FollowupCommand:
    """Typed classification of one ``@haunter`` fix request."""

    command: FollowupCommandName
    test_only: bool


def parse_followup_command(body: Union[str, None]) -> Optional[FollowupCommand]:
    """Classify a comment body as a Haunter fix request.

    Returns ``None`` — meaning "the fix pipeline must not act on this" — when
    the body carries no ``@haunter`` mention at all, or when the mention is
    addressed to a command owned by another subsystem (``@haunter audit``).
    Never raises, whatever the input.
    """
    if not isinstance(body, str) or _MENTION_RE.search(body) is None:
        return None

    match = _FIRST_WORD_RE.search(body)
    if match is not None:
        word = match.group(1).lower()
        if word in RESERVED_COMMANDS:
            return None
        command = _COMMAND_ALIASES.get(word)
        if command is not None:
            return FollowupCommand(
                command=command,
                test_only=command in TEST_ONLY_COMMANDS,
            )

    # No command word (or an unknown one that is not reserved): the mention
    # itself is the request. This is the pre-router behaviour and is what
    # keeps free-form asks like "@haunter please fix the None input" working.
    return FollowupCommand(command="fix", test_only=False)
