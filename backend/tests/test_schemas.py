"""
Tests for app.schemas — Pydantic request/response validation contracts.

Covers:
- RepoCreate: min/max lengths, optional fields, defaults, extra="ignore"
- RepoOut: from_attributes round-trip with Repo ORM instance
- AvailableRepoOut: default values (None for optional fields) and required fields
- ModelConfigUpdate: provider allowlist, suffix rules, whitespace stripping, unknown provider
- WorkflowRunWebhookPayload: valid payload, head_sha regex, extra field drop, missing required fields
- HostingConfigUpdate: AllowedHostingProvider and AllowedSandboxProvider strict allowlists
- HostingConfigOut: field assignment and output structure
"""

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import Repo
from app.schemas import (
    AvailableRepoOut,
    HostingConfigOut,
    HostingConfigUpdate,
    ModelConfigUpdate,
    RepoCreate,
    RepoOut,
    WorkflowRunObj,
    WorkflowRunRepo,
    WorkflowRunRepoOwner,
    WorkflowRunWebhookPayload,
)


# ---------------------------------------------------------------------------
# RepoCreate
# ---------------------------------------------------------------------------


def test_repo_create_valid() -> None:
    repo = RepoCreate(owner="kaiizer777", name="Haunter")
    assert repo.owner == "kaiizer777"
    assert repo.name == "Haunter"
    assert repo.default_branch is None
    assert repo.language_hint is None
    assert repo.active_model_config_id is None


def test_repo_create_all_fields() -> None:
    config_id = uuid.uuid4()
    repo = RepoCreate(
        owner="kaiizer777",
        name="Haunter",
        default_branch="main",
        language_hint="python",
        active_model_config_id=config_id,
    )
    assert repo.default_branch == "main"
    assert repo.language_hint == "python"
    assert repo.active_model_config_id == config_id


@pytest.mark.parametrize("field", ["owner", "name"])
def test_repo_create_min_length_rejected(field: str) -> None:
    data = {"owner": "kaiizer777", "name": "Haunter"}
    data[field] = ""
    with pytest.raises(ValidationError) as exc_info:
        RepoCreate(**data)
    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", ["owner", "name", "default_branch", "language_hint"])
def test_repo_create_max_length(field: str) -> None:
    # 255 chars is valid
    valid_data = {
        "owner": "o" * 255,
        "name": "n" * 255,
        "default_branch": "b" * 255,
        "language_hint": "l" * 255,
    }
    repo = RepoCreate(**valid_data)
    assert getattr(repo, field) == valid_data[field]

    # 256 chars raises ValidationError
    invalid_data = dict(valid_data)
    invalid_data[field] = "x" * 256
    with pytest.raises(ValidationError) as exc_info:
        RepoCreate(**invalid_data)
    assert field in str(exc_info.value)


def test_repo_create_extra_fields_ignored() -> None:
    repo = RepoCreate(
        owner="kaiizer777",
        name="Haunter",
        unrecognized_extra_key="some_injected_value",
    )
    dumped = repo.model_dump()
    assert "unrecognized_extra_key" not in dumped


# ---------------------------------------------------------------------------
# RepoOut
# ---------------------------------------------------------------------------


def test_repo_out_from_orm_round_trip() -> None:
    repo_id = uuid.uuid4()
    user_id = uuid.uuid4()
    config_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    repo_orm = Repo(
        id=repo_id,
        user_id=user_id,
        owner="kaiizer777",
        name="Haunter",
        default_branch="main",
        language_hint="python",
        github_install_id=12345,
        active_model_config_id=config_id,
        created_at=now,
    )

    out = RepoOut.model_validate(repo_orm)
    assert out.id == repo_id
    assert out.owner == "kaiizer777"
    assert out.name == "Haunter"
    assert out.default_branch == "main"
    assert out.language_hint == "python"
    assert out.active_model_config_id == config_id
    assert out.created_at == now


# ---------------------------------------------------------------------------
# AvailableRepoOut
# ---------------------------------------------------------------------------


def test_available_repo_out_defaults() -> None:
    repo = AvailableRepoOut(
        owner="kaiizer777",
        name="Haunter",
        full_name="kaiizer777/Haunter",
        private=True,
        already_connected=False,
        permissions_push=False,
    )
    assert repo.default_branch is None
    assert repo.language is None
    assert repo.updated_at is None
    assert repo.already_connected is False
    assert repo.permissions_push is False


def test_available_repo_out_required_fields() -> None:
    # Missing already_connected / permissions_push raises ValidationError
    with pytest.raises(ValidationError) as exc_info:
        AvailableRepoOut(
            owner="kaiizer777",
            name="Haunter",
            full_name="kaiizer777/Haunter",
            private=True,
        )
    err_str = str(exc_info.value)
    assert "already_connected" in err_str
    assert "permissions_push" in err_str


# ---------------------------------------------------------------------------
# ModelConfigUpdate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_name",
    ["nemotron-3.5-lightning-free", "qwen-2.5-coder-32b-free", "foo-free"],
)
def test_model_config_update_opencode_zen_valid(model_name: str) -> None:
    cfg = ModelConfigUpdate(provider="opencode_zen", model_name=model_name)
    assert cfg.provider == "opencode_zen"
    assert cfg.model_name == model_name


@pytest.mark.parametrize(
    "model_name",
    ["nemotron-3.5-lightning", "qwen-2.5-coder-32b", "foo-bar", "gpt-4o"],
)
def test_model_config_update_opencode_zen_invalid_suffix(model_name: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ModelConfigUpdate(provider="opencode_zen", model_name=model_name)
    assert "OpenCode Zen models must end with '-free'" in str(exc_info.value)


@pytest.mark.parametrize("model_name", ["gpt-4o", "gpt-4o-mini"])
def test_model_config_update_openai_valid(model_name: str) -> None:
    cfg = ModelConfigUpdate(provider="openai", model_name=model_name)
    assert cfg.provider == "openai"
    assert cfg.model_name == model_name


@pytest.mark.parametrize("model_name", ["gpt-5", "gpt-3.5-turbo", "claude-sonnet-4-5"])
def test_model_config_update_openai_invalid(model_name: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ModelConfigUpdate(provider="openai", model_name=model_name)
    assert "Invalid model" in str(exc_info.value)


@pytest.mark.parametrize("model_name", ["claude-sonnet-4-5", "claude-haiku-3-5"])
def test_model_config_update_anthropic_valid(model_name: str) -> None:
    cfg = ModelConfigUpdate(provider="anthropic", model_name=model_name)
    assert cfg.provider == "anthropic"
    assert cfg.model_name == model_name


@pytest.mark.parametrize("model_name", ["claude-opus", "claude-3-haiku", "gpt-4o"])
def test_model_config_update_anthropic_invalid(model_name: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ModelConfigUpdate(provider="anthropic", model_name=model_name)
    assert "Invalid model" in str(exc_info.value)


def test_model_config_update_unknown_provider() -> None:
    with pytest.raises(ValidationError):
        ModelConfigUpdate(provider="gcp", model_name="gemini-1.5-pro")  # type: ignore[arg-type]


def test_model_config_update_whitespace_stripped() -> None:
    cfg_zen = ModelConfigUpdate(provider="opencode_zen", model_name="  foo-free  \n")
    assert cfg_zen.model_name.strip() == "foo-free"

    cfg_openai = ModelConfigUpdate(provider="openai", model_name="  gpt-4o  ")
    assert cfg_openai.model_name.strip() == "gpt-4o"


# ---------------------------------------------------------------------------
# WorkflowRunWebhookPayload
# ---------------------------------------------------------------------------


def test_workflow_run_webhook_payload_valid() -> None:
    raw = {
        "action": "completed",
        "workflow_run": {
            "id": 998877,
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
            "head_branch": "feature/test",
            "conclusion": "failure",
            "html_url": "https://github.com/kaiizer777/Haunter/actions/runs/998877",
            "extra_run_field": "ignored",
        },
        "repository": {
            "name": "Haunter",
            "full_name": "kaiizer777/Haunter",
            "owner": {"login": "kaiizer777", "extra_owner_field": "ignored"},
            "extra_repo_field": "ignored",
        },
        "extra_root_field": "ignored",
    }
    payload = WorkflowRunWebhookPayload(**raw)
    assert payload.action == "completed"
    assert payload.workflow_run.id == 998877
    assert payload.workflow_run.head_sha == "0123456789abcdef0123456789abcdef01234567"
    assert payload.workflow_run.head_branch == "feature/test"
    assert payload.workflow_run.conclusion == "failure"
    assert payload.repository.name == "Haunter"
    assert payload.repository.owner.login == "kaiizer777"


@pytest.mark.parametrize(
    "bad_sha",
    [
        "0123456789abcdef0123456789abcdef0123456",    # 39 chars (too short)
        "0123456789abcdef0123456789abcdef012345678",   # 41 chars (too long)
        "0123456789abcdef0123456789abcdef0123456g",   # non-hex 'g'
        "0123456789abcdef; rm -rf /; 0123456789ab",    # injection attempt
        "",
    ],
)
def test_workflow_run_head_sha_regex(bad_sha: str) -> None:
    with pytest.raises(ValidationError):
        WorkflowRunObj(id=1, head_sha=bad_sha)


def test_workflow_run_head_sha_valid_uppercase() -> None:
    sha = "A" * 40
    obj = WorkflowRunObj(id=1, head_sha=sha)
    assert obj.head_sha == sha


def test_workflow_run_webhook_payload_extra_ignored() -> None:
    payload = WorkflowRunWebhookPayload(
        action="completed",
        workflow_run=WorkflowRunObj(id=1, head_sha="a" * 40),
        repository=WorkflowRunRepo(
            name="Haunter",
            full_name="kaiizer777/Haunter",
            owner=WorkflowRunRepoOwner(login="kaiizer777"),
        ),
        unknown_metadata={"test": 123},
    )
    dumped = payload.model_dump()
    assert "unknown_metadata" not in dumped


@pytest.mark.parametrize(
    "missing_key",
    ["action", "workflow_run", "repository"],
)
def test_workflow_run_webhook_payload_missing_required(missing_key: str) -> None:
    full = {
        "action": "completed",
        "workflow_run": {"id": 1, "head_sha": "a" * 40},
        "repository": {
            "name": "Haunter",
            "full_name": "kaiizer777/Haunter",
            "owner": {"login": "kaiizer777"},
        },
    }
    del full[missing_key]
    with pytest.raises(ValidationError) as exc_info:
        WorkflowRunWebhookPayload(**full)
    assert missing_key in str(exc_info.value)


# ---------------------------------------------------------------------------
# HostingConfigUpdate and HostingConfigOut
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hosting", "sandbox"),
    [
        ("aws", "github_actions"),
        ("aws", "aws"),
    ],
)
def test_hosting_config_update_valid(hosting: str, sandbox: str) -> None:
    cfg = HostingConfigUpdate(hosting_provider=hosting, sandbox_provider=sandbox)  # type: ignore[arg-type]
    assert cfg.hosting_provider == hosting
    assert cfg.sandbox_provider == sandbox


@pytest.mark.parametrize(
    ("hosting", "sandbox"),
    [
        ("gcp", "github_actions"),
        ("azure", "aws"),
        ("aws", "docker"),
        ("aws", "gcp"),
    ],
)
def test_hosting_config_update_rejected(hosting: str, sandbox: str) -> None:
    with pytest.raises(ValidationError):
        HostingConfigUpdate(hosting_provider=hosting, sandbox_provider=sandbox)  # type: ignore[arg-type]


@pytest.mark.parametrize("source", ["db", "env"])
def test_hosting_config_out(source: str) -> None:
    out = HostingConfigOut(
        hosting_provider="aws",
        sandbox_provider="github_actions",
        source=source,
    )
    assert out.hosting_provider == "aws"
    assert out.sandbox_provider == "github_actions"
    assert out.source == source
