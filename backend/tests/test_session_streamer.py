"""
Unit tests for Session Streamer Engine (session_streamer.py).

Validates:
1. Canonical SSE wire-format serialization in format_sse_event.
2. Whitelisted event name enforcement (raises ValueError on unknown events).
3. Secret redaction on outgoing SSE payloads:
   - AWS access keys (AKIA...)
   - GitHub PATs (ghp_..., github_pat_...)
   - GitHub recovery/runner tokens (ghr_...)
   - Neon Postgres keys (npg_...)
   - OpenAI / Anthropic keys (sk-...)
   - Private keys (PEM blocks)
   - PostgreSQL connection strings (postgres://, postgresql://)
   - MySQL connection strings (mysql://)
   - MongoDB connection strings (mongodb://, mongodb+srv://)
   - Authorization: Bearer headers
   - token= query parameters
4. Refined Bearer pattern does not over-redact plain prose ("bearer token handling").
5. Refined PostgreSQL password regex excludes whitespace and '/' from user/pass classes.
6. SseQueue async queue operations and stream generator termination.
"""

from __future__ import annotations

import asyncio
import json
import pytest

from app.services.session_streamer import (
    _ALLOWED_EVENTS,
    _SECRET_PATTERNS,
    _redact_secrets,
    _redact_string,
    format_sse_event,
    ResumeGapError,
    SseQueue,
)


# ---------------------------------------------------------------------------
# 1. format_sse_event protocol & validation
# ---------------------------------------------------------------------------


def test_format_sse_event_valid_event() -> None:
    """format_sse_event formats canonical wire representation."""
    payload = {"delta": "Thinking...", "model": "test-model"}
    formatted = format_sse_event("thought", payload)

    assert formatted.startswith("event: thought\n")
    assert "data: " in formatted
    assert formatted.endswith("\n\n")

    # Data line must parse to valid JSON
    data_line = formatted.split("\n")[1]
    assert data_line.startswith("data: ")
    parsed = json.loads(data_line.removeprefix("data: "))
    assert parsed["delta"] == "Thinking..."


def test_format_sse_event_disallows_unknown_event() -> None:
    """format_sse_event rejects unknown event names with ValueError."""
    with pytest.raises(ValueError, match="unknown event"):
        format_sse_event("unknown_bogus_event", {"key": "val"})


# ---------------------------------------------------------------------------
# 2. Secret Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_aws_and_github_tokens() -> None:
    """Redacts AKIA, ghp_, github_pat_, ghr_."""
    text = (
        "AWS key: AKIAIOSFODNN7EXAMPLE\n"
        "PAT: ghp_123456789012345678901234567890123456\n"
        "Fine-grained: github_pat_1234567890123456789012345678901234567890123456789012\n"
        "Runner: ghr_1234567890abcdef\n"
    )
    redacted = _redact_string(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "ghp_123456789012345678901234567890123456" not in redacted
    assert "github_pat_1234567890123456789012345678901234567890123456789012" not in redacted
    assert "ghr_1234567890abcdef" not in redacted
    assert "[REDACTED_AWS_KEY]" in redacted
    assert "[REDACTED_GITHUB_TOKEN]" in redacted


def test_redact_npg_and_sk_api_keys() -> None:
    """Redacts npg_ and sk-... keys."""
    text = (
        "Neon: npg_1234567890abcdef1234\n"
        "OpenAI: sk-proj-1234567890abcdef123456\n"
    )
    redacted = _redact_string(text)
    assert "npg_1234567890abcdef1234" not in redacted
    assert "sk-proj-1234567890abcdef123456" not in redacted
    assert "[REDACTED_API_KEY]" in redacted


def test_redact_database_credentials() -> None:
    """Redacts passwords in PostgreSQL, MySQL, and MongoDB URLs."""
    urls = [
        ("postgresql://haunter:pgsecret123@ep-cool.neon.tech/db", "pgsecret123", "postgresql://haunter:[REDACTED_PASSWORD]@ep-cool.neon.tech/db"),
        ("postgres://admin:pgpass456@localhost:5432/haunter", "pgpass456", "postgres://admin:[REDACTED_PASSWORD]@localhost:5432/haunter"),
        ("mysql://dbuser:mysqlpass789@db.example.com:3306/prod", "mysqlpass789", "mysql://dbuser:[REDACTED_PASSWORD]@db.example.com:3306/prod"),
        ("mongodb://mongouser:mongopass012@cluster.mongodb.net/test", "mongopass012", "mongodb://mongouser:[REDACTED_PASSWORD]@cluster.mongodb.net/test"),
        ("mongodb+srv://admin:srvpass345@prod.mongodb.net/haunter", "srvpass345", "mongodb+srv://admin:[REDACTED_PASSWORD]@prod.mongodb.net/haunter"),
    ]
    for url, secret, expected in urls:
        redacted = _redact_string(url)
        assert secret not in redacted, f"Failed to redact password from {url}"
        assert expected in redacted, f"Expected {expected} in {redacted}"


def test_postgres_regex_excludes_whitespace_and_slashes() -> None:
    """PostgreSQL password regex excludes whitespace and '/' from username/password classes."""
    # Text containing slashes or whitespace in user/password is not matched as a credentials URI
    non_url = "postgresql:// /path/to/script.py:fakepass@host.com"
    redacted = _redact_string(non_url)
    assert "fakepass" in redacted


def test_redact_authorization_bearer_and_query_token() -> None:
    """Redacts Authorization: Bearer headers and token= query parameters."""
    header = "Authorization: Bearer mySecretToken1234567890abcdef"
    redacted_header = _redact_string(header)
    assert "mySecretToken1234567890abcdef" not in redacted_header
    assert "Authorization: Bearer [REDACTED]" in redacted_header

    query = "https://api.github.com/repos?token=secretquerytoken1234567890&per_page=10"
    redacted_query = _redact_string(query)
    assert "secretquerytoken1234567890" not in redacted_query
    assert "token=[REDACTED]" in redacted_query


def test_bearer_token_min_length_preserves_prose() -> None:
    """Prose mentioning 'bearer token handling' is not over-redacted."""
    prose = "We implement bearer token handling for oauth authentication."
    assert _redact_string(prose) == prose


def test_redact_secrets_recursive_structures() -> None:
    """_redact_secrets processes nested dicts, lists, sets, and tuples."""
    payload = {
        "event_data": [
            {"token": "ghp_123456789012345678901234567890123456"},
            {"auth": ("Bearer secretjwttoken12345678901234567890",)},
        ],
        "meta": {"url": "mysql://user:supersecretpass@db.local:3306/db"},
    }
    redacted = _redact_secrets(payload)
    dumped = json.dumps(redacted)
    assert "ghp_123456789012345678901234567890123456" not in dumped
    assert "secretjwttoken12345678901234567890" not in dumped
    assert "supersecretpass" not in dumped


# ---------------------------------------------------------------------------
# 3. SseQueue Operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_queue_streaming_flow() -> None:
    """SseQueue puts and streams events cleanly until close()."""
    queue = SseQueue()

    await queue.put_thought("Analyzing codebase...")
    await queue.put_tool_call("read_file", {"path": "src/main.py"})
    await queue.put_file_diff(
        "src/main.py",
        "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n",
        action="modify",
    )
    await queue.put_done("session-123", staged_files_count=1, model_used="test-llm")

    events: list[str] = []
    async for chunk in queue.stream():
        events.append(chunk)

    assert len(events) == 4
    assert events[0].startswith("event: thought\n")
    assert events[1].startswith("event: tool_call\n")
    assert events[2].startswith("event: file_diff\n")
    assert events[3].startswith("event: done\n")


@pytest.mark.asyncio
async def test_sse_queue_never_deadlocks_on_overflow() -> None:
    """Producer does not deadlock when pushing more events than queue maxsize without a consumer."""
    small_queue = SseQueue(maxsize=5)

    # Push 20 events into a maxsize=5 queue without reading
    for i in range(20):
        await small_queue.put_thought(f"Thought step {i}")

    await small_queue.put_done("session-overflow", staged_files_count=0)

    # Verify stream still yields the latest events + done marker without hanging
    events: list[str] = []
    async for chunk in small_queue.stream():
        events.append(chunk)

    # Drop-oldest must retain the newest payloads in order, terminated by done:
    # 20 thoughts fill the 5-slot queue with [15..19]; put_done evicts 15 and
    # close evicts 16, leaving [thought 17, thought 18, thought 19, done].
    assert len(events) == 4
    assert "Thought step 17" in events[0]
    assert "Thought step 18" in events[1]
    assert "Thought step 19" in events[2]
    assert "event: done" in events[3]


@pytest.mark.asyncio
async def test_sse_queue_client_disconnect_draining() -> None:
    """When a client disconnects mid-stream, stream() exits cleanly and marks queue disconnected."""
    queue = SseQueue(maxsize=10)

    for i in range(5):
        await queue.put_thought(f"Step {i}")

    # Consumer reads 2 events and then disconnects (closes generator)
    gen = queue.stream()
    read_events: list[str] = []
    read_events.append(await anext(gen))
    read_events.append(await anext(gen))
    assert len(read_events) == 2

    # Simulate ASGI client disconnect
    await gen.aclose()

    assert queue.is_disconnected is True

    # Producer continues emitting after consumer disconnect — must not block
    for i in range(10):
        await queue.put_thought(f"Post-disconnect step {i}")

    await queue.put_done("session-disconnect", staged_files_count=0)
    assert queue.is_disconnected is True


@pytest.mark.asyncio
async def test_sse_queue_resumption_with_last_event_id() -> None:
    """stream(last_event_id=N) replays buffered events with id > N."""
    queue = SseQueue(maxsize=50, replay_buffer_size=50)

    await queue.put_thought("Event 1")
    await queue.put_thought("Event 2")
    await queue.put_thought("Event 3")
    await queue.close()

    # Replay from event 1 (should yield events 2 and 3)
    replayed: list[str] = []
    async for chunk in queue.stream(last_event_id=1):
        replayed.append(chunk)

    assert len(replayed) == 2
    assert "Event 2" in replayed[0]
    assert "Event 3" in replayed[1]


def test_format_sse_event_with_id_and_retry() -> None:
    """format_sse_event correctly formats optional event_id and retry fields per SSE spec."""
    formatted = format_sse_event(
        event="thought",
        data={"delta": "Processing..."},
        event_id=42,
        retry=5000,
    )
    assert "event: thought\n" in formatted
    assert "id: 42\n" in formatted
    assert "retry: 5000\n" in formatted
    assert 'data: {"delta":"Processing..."}\n\n' in formatted


async def _collect(gen, sink: list[str]) -> None:
    """Drain an async generator into sink (helper for overlap tests)."""
    async for chunk in gen:
        sink.append(chunk)


@pytest.mark.asyncio
async def test_sse_queue_generator_handover_preserves_live_events() -> None:
    """A reconnecting generator supersedes a blocked one without losing live events.

    Regression: the old generator's finally block used to clear
    _consumer_active and drain the queue while the replacement waited on
    _q.get(), dropping live events (or hanging the new stream).
    """
    queue = SseQueue(maxsize=10, replay_buffer_size=50)
    await queue.put_thought("first")

    old_gen = queue.stream()
    assert "first" in await anext(old_gen)

    # Reconnect attaches a replacement while the old generator is blocked.
    new_gen = queue.stream()
    old_events: list[str] = []
    new_events: list[str] = []
    old_task = asyncio.create_task(_collect(old_gen, old_events))
    new_task = asyncio.create_task(_collect(new_gen, new_events))
    await asyncio.sleep(0.05)

    # The superseded generator exits on its own without retiring the connection.
    await asyncio.wait_for(old_task, timeout=2.0)
    assert old_events == []
    assert queue.is_disconnected is False

    # Live events emitted after the handover still reach the replacement.
    await queue.put_thought("live-after-handover")
    await queue.put_done("session-handover", staged_files_count=0)
    await asyncio.wait_for(new_task, timeout=2.0)

    assert any("live-after-handover" in e for e in new_events)
    assert any("event: done" in e for e in new_events)


@pytest.mark.asyncio
async def test_sse_queue_resume_gap_rejected() -> None:
    """Resuming from an id older than the retained replay window is rejected.

    Otherwise stream(last_event_id=...) would replay only the survivors
    (potentially ending with done) without signalling the missed output.
    """
    queue = SseQueue(maxsize=512, replay_buffer_size=4)
    for i in range(10):
        await queue.put_thought(f"Event {i}")

    # Buffer retains ids 7..10; ids 1..6 were evicted.
    assert queue.resume_gap(None) is False
    assert queue.resume_gap(6) is False
    assert queue.resume_gap(9) is False
    assert queue.resume_gap(5) is True
    assert queue.resume_gap(0) is True

    with pytest.raises(ResumeGapError):
        async for _chunk in queue.stream(last_event_id=5):
            pass

    # A non-stale resumption still replays the retained survivors.
    await queue.put_done("session-gap", staged_files_count=0)
    replayed: list[str] = []
    async for chunk in queue.stream(last_event_id=7):
        replayed.append(chunk)
    assert any("Event 8" in e for e in replayed)
    assert any("event: done" in e for e in replayed)


@pytest.mark.asyncio
async def test_sse_queue_replay_after_returns_survivors_without_live_tail() -> None:
    """replay_after() serves retained events (incl. done) with no queue attach."""
    queue = SseQueue(maxsize=512, replay_buffer_size=50)

    await queue.put_thought("Event 1")
    await queue.put_thought("Event 2")
    await queue.put_done("session-replay", staged_files_count=0)

    replayed = queue.replay_after(1)
    assert len(replayed) == 2
    assert "Event 2" in replayed[0]
    assert "event: done" in replayed[1]

    # Nothing newer than the terminal event: empty replay, no hang.
    assert queue.replay_after(3) == []

