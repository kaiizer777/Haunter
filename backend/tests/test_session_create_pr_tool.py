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
from app.routers.sessions import SessionCommitIn, commit_session
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
    assert kwargs["target_branch"] is None
    assert kwargs["gh_token"] == _GH_TOKEN

    assert _PR_URL in text
    assert result.branch_name in text
    assert result.commit_sha in text
    assert "Session marked completed." in text
    assert _GH_TOKEN not in text
    assert _GH_TOKEN not in caplog.text

    assert history == [result.history_entry]
    assert history[0] is result.history_entry


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
        {"title": "x" * 256},
        {"title": "ok", "body": 42},
        {"title": "ok", "body": "y" * 65536},
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


# ---------------------------------------------------------------------------
# Agent and UI share one implementation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_and_tool_both_invoke_shared_publisher() -> None:
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    result = _make_result()

    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = session
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()

    with (
        patch(
            "app.routers.sessions.get_installation_token",
            AsyncMock(return_value="tok"),
        ),
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
    assert tool_publisher.await_args.kwargs["target_branch"] is None


# ---------------------------------------------------------------------------
# Persistence trap: _persist must not erase the PR history entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_keeps_pr_history_entry_exactly_once() -> None:
    user = _make_user()
    repo = _make_repo(user)
    session = _make_session(user, repo)
    orch = _make_orchestrator(session, current_user=user)
    history: list[dict[str, object]] = [{"role": "user", "content": "publish it"}]
    result = _make_result()

    async def _fake_publish(**kwargs: object) -> SessionCommitResult:
        # Mirror the real publisher: persist the entry on the ORM session.
        kwargs["session"].status = "completed"  # type: ignore[union-attr]
        kwargs["session"].conversation_history = [  # type: ignore[union-attr]
            *list(kwargs["session"].conversation_history or []),  # type: ignore[union-attr]
            result.history_entry,
        ]
        return result

    with patch(
        "app.services.session_commit.publish_session_patches",
        AsyncMock(side_effect=_fake_publish),
    ):
        text = await _dispatch(orch, session, {"title": "Publish"}, history=history)

    assert "Session marked completed." in text
    # The handler appended the identical entry to the in-turn history copy.
    assert history[-1] is result.history_entry

    # End-of-turn persist uses the in-turn copy; it must not clobber the entry.
    await orch._persist(session, list(history), dict(session.staged_patches or {}))

    pr_entries = [
        entry
        for entry in session.conversation_history
        if isinstance(entry, dict) and entry.get("pr_url") == _PR_URL
    ]
    assert len(pr_entries) == 1
    assert session.status == "completed"
    assert session.conversation_history[0]["content"] == "publish it"
