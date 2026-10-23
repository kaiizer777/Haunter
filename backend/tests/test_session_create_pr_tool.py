"""
Hermetic tests for the ``create_pull_request`` session-agent tool (issue #70).

The tool accepts only a title/body: it cannot choose a branch, repository, or
token. Publishing is delegated to the shared session-commit service, which is
also what ``POST /sessions/{id}/commit`` calls.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import AgentSession, Repo, User
from app.schemas import SESSION_PR_BODY_MAX_LEN, SESSION_PR_TITLE_MAX_LEN, SessionCommitIn
from app.services.session_commit import SessionCommitError, SessionCommitResult
from app.services.session_orchestrator import (
    SessionOrchestrator,
    _TOOLS,
    _build_system_prompt,
)

_GH_TOKEN = "ghs_do_not_leak_this"
_PR_URL = "https://github.com/test-owner/test-repo/pull/42"


def _make_user() -> User:
    return User(
        id=uuid.uuid4(),
        github_id=98765,
        github_username="agent-user",
        role="user",
    )


def _make_repo(user: User) -> Repo:
    return Repo(
        id=uuid.uuid4(),
        user_id=user.id,
        owner="test-owner",
        name="test-repo",
        default_branch="main",
    )


def _make_session(
    user: User,
    repo: Repo,
    *,
    staged_patches: dict[str, str] | None = None,
) -> AgentSession:
    session = AgentSession(
        id=uuid.uuid4(),
        user_id=user.id,
        repo_id=repo.id,
        title="PR tool session",
        status="active",
        branch_name=f"haunter/session-{str(uuid.uuid4())[:8]}",
        base_sha="a" * 40,
        conversation_history=[],
        staged_patches=(
            {
                "app/utils.py": (
                    "--- a/app/utils.py\n+++ b/app/utils.py\n@@ -1 +1 @@\n-old\n+new\n"
                )
            }
            if staged_patches is None
            else staged_patches
        ),
    )
    session.repo = repo
    return session


def _make_result(branch_name: str = "haunter/session-abcd1234") -> SessionCommitResult:
    entry = {
        "role": "system",
        "content": f"Commit published. PR #42 opened: {_PR_URL}\nCommit SHA: {'c' * 40}",
        "pr_url": _PR_URL,
        "pr_number": 42,
        "commit_sha": "c" * 40,
        "timestamp": "2026-10-22T00:00:00+00:00",
    }
    return SessionCommitResult(
        pr_url=_PR_URL,
        pr_number=42,
        commit_sha="c" * 40,
        branch_name=branch_name,
        history_entry=entry,
    )


def _make_orchestrator(
    session: AgentSession,
    *,
    current_user: User | None,
    db: object | None = None,
) -> SessionOrchestrator:
    return SessionOrchestrator(
        session_id=session.id,
        db=db if db is not None else AsyncMock(),
        gh_token=_GH_TOKEN,
        current_user=current_user,
    )


async def _dispatch(
    orch: SessionOrchestrator,
    session: AgentSession | None,
    args: dict[str, object],
    *,
    history: list[dict[str, object]] | None = None,
    patches: dict[str, str] | None = None,
) -> str:
    return await orch._dispatch_tool(
        tool_name="create_pull_request",
        args=args,
        repo_owner="test-owner",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=(
            dict(session.staged_patches or {})
            if patches is None and session is not None
            else dict(patches or {})
        ),
        queue=AsyncMock(),
        session=session,
        conversation_history=history,
    )


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


def test_create_pull_request_schema_registered_exactly_once() -> None:
    defs = [
        tool
        for tool in _TOOLS
        if tool.get("function", {}).get("name") == "create_pull_request"
    ]
    assert len(defs) == 1

    names = [tool["function"]["name"] for tool in _TOOLS]
    assert len(names) == len(set(names)), "tool names must be unique"


def test_create_pull_request_schema_shape_is_minimal() -> None:
    fn = next(
        tool["function"]
        for tool in _TOOLS
        if tool["function"]["name"] == "create_pull_request"
    )
    params = fn["parameters"]

    assert params["type"] == "object"
    assert params["required"] == ["title"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"title", "body"}
    assert params["properties"]["title"]["type"] == "string"
    assert params["properties"]["body"]["type"] == "string"

    for forbidden in ("branch_name", "owner", "repo", "token", "head", "base"):
        assert forbidden not in params["properties"]

    description = fn["description"].lower()
    assert "write operation" in description
    assert "push" in description
    assert "completed" in description
    assert "explicitly" in description
    assert "verification" in description


def test_system_prompt_advertises_create_pull_request() -> None:
    prompt = _build_system_prompt(
        repo_owner="test-owner",
        repo_name="test-repo",
        branch_name="haunter/session-1",
        base_sha="a" * 40,
        staged_patches={},
    )
    assert "create_pull_request(title, body?)" in prompt
    assert "only when the user explicitly asks" in prompt.lower()
    assert "cannot target protected or default branches" in prompt.lower()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_calls_shared_publisher_and_returns_pr_details(caplog) -> None:
    """Catches a tool bypassing the shared publisher or leaking the token."""
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    orch = _make_orchestrator(session, current_user=user)
    history: list[dict[str, object]] = []
    result = _make_result()

    with (
        caplog.at_level("DEBUG"),
        patch(
            "app.services.session_commit.publish_session_patches",
            AsyncMock(return_value=result),
        ) as publisher,
    ):
        text = await _dispatch(
            orch,
            session,
            {"title": "Fix: publish", "body": "Details"},
            history=history,
        )

    publisher.assert_awaited_once()
    kwargs = publisher.await_args.kwargs
    assert kwargs["session"] is session
    assert kwargs["current_user"] is user
    assert kwargs["title"] == "Fix: publish"
    assert kwargs["body"] == "Details"
    assert kwargs["target_branch"] == f"haunter/session-{session.id.hex[:12]}"
    assert kwargs["gh_token"] == _GH_TOKEN

    assert _PR_URL in text
    assert result.branch_name in text
    assert result.commit_sha in text
    assert "Session marked completed." in text
    assert _GH_TOKEN not in text
    assert _GH_TOKEN not in caplog.text

    assert history == []
    assert orch._pr_history_entry is result.history_entry


@pytest.mark.asyncio
async def test_dispatch_syncs_turn_local_staged_patches_before_publishing() -> None:
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo, staged_patches={"app/old.py": "old diff"})
    orch = _make_orchestrator(session, current_user=user)
    turn_patches = {
        "app/old.py": "old diff",
        "app/new.py": "--- /dev/null\n+++ b/app/new.py\n@@ -0,0 +1 @@\n+new\n",
    }

    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(return_value=_make_result()),
    ) as publisher:
        await _dispatch(orch, session, {"title": "Fix"}, patches=turn_patches)

    assert session.staged_patches == turn_patches
    assert publisher.await_args.kwargs["session"] is session


@pytest.mark.asyncio
async def test_dispatch_maps_session_commit_error_to_tool_error() -> None:
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    orch = _make_orchestrator(session, current_user=user)
    history: list[dict[str, object]] = []

    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(side_effect=SessionCommitError("No staged patches to commit.", 422)),
    ):
        text = await _dispatch(orch, session, {"title": "Fix"}, history=history)

    assert text == "Error: No staged patches to commit."
    assert history == []


@pytest.mark.asyncio
async def test_dispatch_requires_session_context() -> None:
    user = _make_user()
    session = _make_session(user, _make_repo(user))
    orch = _make_orchestrator(session, current_user=user)

    with patch(
        "app.services.session_commit.publish_session_patches", AsyncMock()
    ) as publisher:
        text = await _dispatch(orch, None, {"title": "Fix"})

    assert text.startswith("Error:")
    assert "Session context" in text
    publisher.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"title": None},
        {"title": 123},
        {"title": "   "},
        {"title": "x" * (SESSION_PR_TITLE_MAX_LEN + 1)},
        {"title": "ok", "body": 42},
        {"title": "ok", "body": "y" * (SESSION_PR_BODY_MAX_LEN + 1)},
    ],
)
async def test_dispatch_rejects_invalid_arguments_without_publishing(
    args: dict[str, object],
) -> None:
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    orch = _make_orchestrator(session, current_user=user)

    with patch(
        "app.services.session_commit.publish_session_patches", AsyncMock()
    ) as publisher:
        text = await _dispatch(
            orch, session, args, history=[]
        )

    assert text.startswith("Error:")
    publisher.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [None, [], "title", 42])
async def test_dispatch_non_dict_arguments_returns_structured_error(args) -> None:
    """Catches a crash on malformed tool arguments (issue #70 F8)."""
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    orch = _make_orchestrator(session, current_user=user)

    with patch(
        "app.services.session_commit.publish_session_patches", AsyncMock()
    ) as publisher:
        text = await _dispatch(orch, session, args)  # type: ignore[arg-type]

    assert text == "Error: 'create_pull_request' arguments must be a JSON object."
    publisher.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_resolves_user_from_db_when_ctor_has_none() -> None:
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    db = AsyncMock()
    db.get = AsyncMock(return_value=user)
    orch = _make_orchestrator(session, current_user=None, db=db)

    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(return_value=_make_result()),
    ) as publisher:
        await _dispatch(orch, session, {"title": "Fix"})

    db.get.assert_awaited_once_with(User, session.user_id)
    assert publisher.await_args.kwargs["current_user"] is user


@pytest.mark.asyncio
async def test_dispatch_errors_when_user_cannot_be_resolved() -> None:
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    db = AsyncMock()
    db.get = AsyncMock(side_effect=RuntimeError("db down"))
    orch = _make_orchestrator(session, current_user=None, db=db)

    with patch(
        "app.services.session_commit.publish_session_patches", AsyncMock()
    ) as publisher:
        text = await _dispatch(orch, session, {"title": "Fix"})

    assert text.startswith("Error:")
    assert "owner" in text
    publisher.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_refuses_publish_when_user_mismatches_session_owner() -> None:
    """Catches an orchestrator reused across sessions publishing as the wrong identity."""
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    other_user = _make_user()
    orch = _make_orchestrator(session, current_user=other_user)

    with patch(
        "app.services.session_commit.publish_session_patches", AsyncMock()
    ) as publisher:
        text = await _dispatch(orch, session, {"title": "Fix"})

    assert text.startswith("Error:")
    assert "mismatch" in text
    publisher.assert_not_called()


# ---------------------------------------------------------------------------
# Agent and UI share one implementation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_and_tool_both_invoke_shared_publisher() -> None:
    # Router import lives here so only the parity test depends on the
    # endpoint's import graph; the rest of this module tests the tool path.
    from app.routers.sessions import commit_session

    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    result = _make_result()

    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = session
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()

    # NOTE: the endpoint never mints by construction (minting moved into the
    # shared publisher, mocked below), so no token patch belongs here.
    with (
        patch(
            "app.services.session_commit.publish_session_patches",
            AsyncMock(return_value=result),
        ) as route_publisher,
    ):
        out = await commit_session(
            session_id=session.id,
            body=SessionCommitIn(title="Fix via UI"),
            current_user=user,
            db=mock_db,
        )

    assert out.pr_url == _PR_URL
    route_publisher.assert_awaited_once()
    route_kwargs = route_publisher.await_args.kwargs
    assert route_kwargs["target_branch"] is None
    assert route_kwargs["session"] is session

    orch = _make_orchestrator(session, current_user=user)
    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(return_value=result),
    ) as tool_publisher:
        await _dispatch(orch, session, {"title": "Fix via agent"}, history=[])

    tool_publisher.assert_awaited_once()
    assert tool_publisher.await_args.kwargs["target_branch"] == (
        f"haunter/session-{session.id.hex[:12]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("session_branch", ["feat/x", "main", None, ""])
async def test_tool_always_mints_dedicated_session_branch(session_branch) -> None:
    """Catches the tool-adopts-session.branch_name regression (issue #70 F3).

    Even when the session branch is user-controlled (``feat/x``), protected
    (``main``), or missing, the tool must pass its own ``haunter/session-*``
    branch to the publisher rather than letting ``target_branch=None`` fall
    back to ``session.branch_name``.
    """
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    session.branch_name = session_branch
    orch = _make_orchestrator(session, current_user=user)
    expected = f"haunter/session-{session.id.hex[:12]}"

    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(return_value=_make_result(branch_name=expected)),
    ) as publisher:
        await _dispatch(orch, session, {"title": "Fix"}, history=[])

    assert publisher.await_args.kwargs["target_branch"] == expected
    assert publisher.await_args.kwargs["target_branch"] != session_branch


# ---------------------------------------------------------------------------
# Persistence: order + exactly-once, including publish -> restore
# ---------------------------------------------------------------------------


def _realistic_base_history() -> list[dict[str, object]]:
    """Prior turns a real session would already have in conversation_history."""
    return [
        {"role": "user", "content": "Investigate the checkout timeout."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "grep_search", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "app/checkout.py:42: while True:",
        },
        {"role": "assistant", "content": "The retry loop never updates the deadline."},
    ]


def _current_turn_entries() -> list[dict[str, object]]:
    """The user message + assistant/tool entries produced by the publish turn."""
    return [
        {"role": "user", "content": "Open the PR with that fix."},
        {
            "role": "assistant",
            "content": "Publishing.",
            "tool_calls": [
                {
                    "id": "call_pr",
                    "type": "function",
                    "function": {
                        "name": "create_pull_request",
                        "arguments": '{"title": "Fix checkout timeout"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_pr",
            "content": f"Pull request opened: {_PR_URL}",
        },
        {"role": "assistant", "content": "The PR is open."},
    ]


@pytest.mark.asyncio
async def test_pr_entry_is_appended_after_turn_entries_exactly_once() -> None:
    """Catches the PR-entry-before-the-user-message regression (issue #70 F5).

    The tool only buffers ``result.history_entry``; ``_persist`` appends it
    after every entry produced by the publish turn. A tool that appends to the
    pre-turn history copy, or a second writer, fails the exact-order assertion.
    """
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    base_history = _realistic_base_history()
    current_turn = _current_turn_entries()
    session.conversation_history = list(base_history)
    orch = _make_orchestrator(session, current_user=user)
    result = _make_result()

    async def _fake_publish(**_kwargs: object) -> SessionCommitResult:
        # Mirrors the tool path: status changes, history is left to _persist.
        # Counterpart: test_tool_path_does_not_write_history_inside_publisher pins the real half.
        session.status = "completed"
        return result

    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(side_effect=_fake_publish),
    ) as publisher:
        text = await _dispatch(
            orch,
            session,
            {"title": "Fix checkout timeout"},
            history=base_history,
        )

    assert "Session marked completed." in text
    assert publisher.await_args.kwargs["persist_history"] is False
    # The pre-turn copy is not touched; the entry waits in the orchestrator.
    assert base_history == _realistic_base_history()
    assert orch._pr_history_entry is result.history_entry

    await orch._persist(
        session,
        list(base_history) + current_turn,
        dict(session.staged_patches or {}),
        base_history=base_history,
        new_entries=current_turn,
    )

    expected = [*base_history, *current_turn, result.history_entry]
    assert session.conversation_history == expected
    assert session.conversation_history[-1]["content"] == result.history_entry["content"]
    assert [
        entry
        for entry in session.conversation_history
        if isinstance(entry, dict) and entry.get("pr_url") == _PR_URL
    ] == [result.history_entry]
    assert session.status == "completed"

    # A second persist call must not duplicate the buffered entry.
    await orch._persist(
        session,
        list(session.conversation_history),
        dict(session.staged_patches or {}),
    )
    assert session.conversation_history == expected


@pytest.mark.asyncio
async def test_checkpoint_restore_after_publish_is_refused_and_pr_entry_survives() -> None:
    """Catches the broken variant where publish -> checkpoint_restore -> persist
    drops the PR history entry while leaving status=completed (issue #70 F1).

    Publishing is irreversible, so restore must refuse before pruning
    checkpoints, reverting patches, or truncating history. The end-of-turn
    persist then writes the PR entry after the trigger message, exactly once.
    """
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    base_history = _realistic_base_history()
    current_turn = _current_turn_entries()
    session.conversation_history = list(base_history)
    checkpoint = {
        "checkpoint_id": "cp_before_publish",
        "turn": 1,
        "timestamp": "2026-10-22T00:00:00+00:00",
        "description": "before publish",
        "staged_patches": {},
        "history_length": 0,
    }
    session.checkpoints = [checkpoint]

    result = _make_result()
    orch = _make_orchestrator(session, current_user=user)
    queue = AsyncMock()
    staged_patches = dict(session.staged_patches or {})
    turn_history = list(base_history)

    async def _fake_publish(**_kwargs: object) -> SessionCommitResult:
        # The real publisher marks the session completed; history persistence
        # belongs to the caller (the handler buffers it, _persist writes it).
        session.status = "completed"
        return result

    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(side_effect=_fake_publish),
    ):
        await _dispatch(
            orch,
            session,
            {"title": "Publish"},
            history=turn_history,
            patches=staged_patches,
        )
        assert session.status == "completed"

        text = await orch._dispatch_tool(
            tool_name="checkpoint_restore",
            args={"checkpoint_id": "cp_before_publish"},
            repo_owner="test-owner",
            repo_name="test-repo",
            base_sha="a" * 40,
            staged_patches=staged_patches,
            queue=queue,
            session=session,
            conversation_history=turn_history,
        )

    assert (
        text
        == "Error: Cannot restore a checkpoint after the session has been published."
    )
    # Restore mutated nothing: not the ORM row, the turn-local copies, or the DB.
    assert session.conversation_history == list(base_history)
    assert session.staged_patches == staged_patches
    assert session.checkpoints == [checkpoint]
    assert turn_history == list(base_history)
    queue.put_checkpoint_restored.assert_not_awaited()
    orch.db.flush.assert_not_awaited()
    orch.db.commit.assert_not_awaited()

    # The end-of-turn persist writes the PR entry after the trigger message.
    await orch._persist(
        session,
        list(turn_history) + current_turn,
        dict(staged_patches),
        base_history=base_history,
        new_entries=current_turn,
    )
    expected = [*base_history, *current_turn, result.history_entry]
    assert session.conversation_history == expected
    assert session.status == "completed"
    assert [
        entry
        for entry in session.conversation_history
        if isinstance(entry, dict) and entry.get("pr_url") == _PR_URL
    ] == [result.history_entry]
