"""Model-hide (backend): non-admins get zero model vocabulary. DB-free."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.llm.exceptions import LLMExhaustedFreeTierError
from app.orchestrator import _format_failure_reason
from app.routers.model_config import (
    delete_repo_model_config,
    get_active_model_config_endpoint,
    get_available_models_endpoint,
    update_model_config_endpoint,
)
from app.schemas import ModelConfigUpdate
from main import sandbox_health

pytestmark = pytest.mark.fast


def _user(role: str) -> SimpleNamespace:
    return SimpleNamespace(
        role=role, id=f"{role}-id", is_admin=(role == "admin"), github_username=None
    )


NON_ADMIN = _user("user")
ADMIN = _user("admin")


async def _expect_403(coro) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await coro
    assert exc_info.value.status_code == 403


async def test_available_models_non_admin_403() -> None:
    await _expect_403(get_available_models_endpoint(current_user=NON_ADMIN))


async def test_repo_read_non_admin_403() -> None:
    await _expect_403(
        get_active_model_config_endpoint(
            current_user=NON_ADMIN, db=None, repo_id=uuid.uuid4()
        )
    )


async def test_repo_write_non_admin_403() -> None:
    body = ModelConfigUpdate(
        provider="openai", model_name="gpt-4o", repo_id=uuid.uuid4()
    )
    await _expect_403(
        update_model_config_endpoint(
            body=body, current_user=NON_ADMIN, db=None
        )
    )


async def test_repo_delete_non_admin_403() -> None:
    await _expect_403(
        delete_repo_model_config(
            repo_id=uuid.uuid4(), current_user=NON_ADMIN, db=None
        )
    )


async def test_sandbox_requires_auth_shape_and_redacts_detail() -> None:
    import inspect

    sig = inspect.signature(sandbox_health)
    assert "current_user" in sig.parameters  # auth required
    result = await sandbox_health(current_user=NON_ADMIN)
    assert result["ok"] is True
    assert result["detail"] == {}
    admin_result = await sandbox_health(current_user=ADMIN)
    assert isinstance(admin_result["detail"], dict)


def test_exhausted_message_has_no_model_tokens() -> None:
    attempts = [("gpt-4o", 0, "boom"), ("claude-sonnet-4-5", 1, "bust")]
    msg = LLMExhaustedFreeTierError(attempts).message
    assert "gpt-4o" not in msg
    assert "claude-sonnet-4-5" not in msg
    assert "retry" in msg.lower()


def test_failure_reason_redacts_model_tokens() -> None:
    reason = _format_failure_reason(
        "verification", ValueError("backend gpt-4o exploded")
    )
    assert "gpt-4o" not in reason
    assert "[model]" in reason
    exhausted = _format_failure_reason(
        "fix_generation",
        LLMExhaustedFreeTierError([("mimo-v2.6-flash-free", 0, "down")]),
    )
    assert "mimo-v2.6-flash-free" not in exhausted


def test_validator_errors_are_generic() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ModelConfigUpdate(provider="openai", model_name="not-a-real-model")
    msgs = " | ".join(e.get("msg", "") for e in exc_info.value.errors())
    assert "Invalid model selection." in msgs
    assert "Allowed models" not in msgs
