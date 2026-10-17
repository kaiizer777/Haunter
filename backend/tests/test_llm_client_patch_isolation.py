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

# Captured at import, before any test has had a chance to touch it. The two
# backstop tests below compare against this to prove the repair happened.
_PRISTINE_COMPLETE = LLMClient.__dict__["complete"]


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


def test_package_reexport_patch_cannot_stub_the_shared_client() -> None:
    """``app.llm.LLMClient`` re-exports the shared class, so it is the worst vector.

    ``patch("app.llm.LLMClient.complete", ...)`` resolves the package attribute
    and therefore writes straight onto ``app.llm.client.LLMClient``. Unlike the
    subagent targets there is no intermediate module global to redirect, so this
    only holds together because ``scoped_llm_client`` rebinds ``app.llm`` itself.
    Three test files use this target (``test_ast_context_gatherer.py``,
    ``test_followup_reply.py``, ``test_pr_feedback.py``) and two of them sort
    before ``tests/test_llm.py``, so it is the variant that could still have
    reproduced the reported leak after the subagent-only guard landed.
    """
    import app.llm

    shared_complete = LLMClient.__dict__["complete"]
    assert app.llm.LLMClient is not LLMClient  # rebound to the scoped subclass

    patcher = patch(
        "app.llm.LLMClient.complete",
        new_callable=AsyncMock,
        return_value={"content": '{"patch": "leaked", "confidence": 78}'},
    )
    patcher.start()  # deliberately never stopped
    try:
        assert app.llm.LLMClient.complete is not shared_complete
        assert LLMClient.__dict__["complete"] is shared_complete
    finally:
        patcher.stop()

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


# ---------------------------------------------------------------------------
# Backstop: unaudited writers must still not be able to leak
# ---------------------------------------------------------------------------
# ``scoped_llm_client`` only covers the writers that opt into it. Roughly a dozen
# other modules patch ``LLMClient.complete`` through their own global, and any of
# them can be interrupted mid-``with`` by the 120s ``timeout`` in pytest.ini,
# leaving the stub installed for every later test. The autouse
# ``_restore_shared_llm_client_complete`` fixture in conftest.py repairs that
# between tests; these two tests pin the repair.
#
# They patch the shared class object directly rather than a module global,
# which is exactly what ``scoped_llm_client`` cannot intercept - so they still
# leak, and still have to be cleaned up.


def test_abandoned_stub_on_the_shared_class_is_installed() -> None:
    """Abandon a stub on the shared client exactly as an aborted test would."""
    patcher = patch.object(
        LLMClient,
        "complete",
        new_callable=AsyncMock,
        return_value={"content": '{"patch": "leaked", "confidence": 78}'},
    )
    patcher.start()  # deliberately never stopped

    assert LLMClient.__dict__["complete"] is not _PRISTINE_COMPLETE


def test_abandoned_stub_on_the_shared_class_is_repaired_afterwards() -> None:
    """Runs after the test above by definition order; fails if the backstop is gone."""
    assert LLMClient.__dict__["complete"] is _PRISTINE_COMPLETE
    assert LLMClient.complete is _PRISTINE_COMPLETE
