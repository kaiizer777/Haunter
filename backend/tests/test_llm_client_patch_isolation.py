"""Isolation pin for #146: subagent LLM stubs must not mutate the shared client.

``app.subagents.fix_generator`` and ``app.subagents.context_gatherer`` both do
``from app.llm import LLMClient``, so ``LLMClient`` is a single process-wide
class object that every test module holds. ``patch("app.subagents.fix_generator
.LLMClient.complete", ...)`` resolves that module global and writes the
attribute onto the *shared* class.

When such a patch is abandoned before its ``with`` block unwinds, the stub stays
installed for the rest of the session and every later test that drives the real
client gets the leftover canned payload instead of doing HTTP. That is how the
fix generator's diff blob reached ``tests/test_llm.py`` and turned its
exhaustion assertions into "DID NOT RAISE LLMExhaustedFreeTierError".

``scoped_llm_client`` (tests/conftest.py) rebinds the subagent module globals to
a private subclass first, so those patches land on the subclass and
``app.llm.client.LLMClient`` is never written to. These tests pin that
invariant, including the abandoned-patch state that actually broke.

Hermetic: no DB, no network.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.llm.client import LLMClient
from app.subagents import fix_generator

pytestmark = pytest.mark.usefixtures("scoped_llm_client")


def test_subagent_patch_installs_on_the_scoped_subclass_only() -> None:
    """The stub is visible to the module under test and nowhere else."""
    shared_complete = LLMClient.__dict__["complete"]

    with patch(
        "app.subagents.fix_generator.LLMClient.complete",
        new_callable=AsyncMock,
        return_value={"content": "{}"},
    ):
        assert fix_generator.LLMClient.complete is not shared_complete
        # The process-wide class other modules drive is untouched while the
        # stub is live, so no concurrent reader can observe it either.
        assert LLMClient.__dict__["complete"] is shared_complete
        assert LLMClient.complete is shared_complete

    assert fix_generator.LLMClient.complete is shared_complete
    assert LLMClient.__dict__["complete"] is shared_complete


def test_abandoned_subagent_patch_cannot_stub_the_shared_client() -> None:
    """A patch that is never unwound must still leave the shared client clean.

    This is the exact failure shape behind #146: the patch outlives the test
    that installed it, so the leak must be impossible rather than merely
    unlikely.
    """
    shared_complete = LLMClient.__dict__["complete"]

    patcher = patch(
        "app.subagents.fix_generator.LLMClient.complete",
        new_callable=AsyncMock,
        return_value={"content": '{"patch": "leaked", "confidence": 78}'},
    )
    patcher.start()
    try:
        assert fix_generator.LLMClient.complete is not shared_complete
        assert LLMClient.__dict__["complete"] is shared_complete
    finally:
        patcher.stop()

    assert LLMClient.__dict__["complete"] is shared_complete


def test_context_gatherer_patch_is_scoped_too() -> None:
    """context_gatherer shares the same import shape and needs the same guard."""
    from app.subagents import context_gatherer

    shared_complete = LLMClient.__dict__["complete"]

    with patch(
        "app.subagents.context_gatherer.LLMClient.complete",
        new_callable=AsyncMock,
        return_value={"content": "{}"},
    ):
        assert context_gatherer.LLMClient.complete is not shared_complete
        assert LLMClient.__dict__["complete"] is shared_complete

    assert LLMClient.__dict__["complete"] is shared_complete
