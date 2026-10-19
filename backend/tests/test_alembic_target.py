"""Tests for app.alembic_target — target resolution, redaction, and mismatch guard.

These are hermetic: every test injects its own environment mapping and settings
stub, so no local ``.env`` value can influence the result and no database
connection is opened.
"""

from __future__ import annotations

import io
import traceback

import pytest

from app.alembic_target import (
    AlembicTargetError,
    MigrationTarget,
    prepare_migration_target,
)

_LIVE_URL = (
    "postgresql://live_user:live_password@ep-live.neon.tech:5432/neondb"
    "?sslmode=require&channel_binding=require"
)
_TEST_URL = (
    "postgresql://test_user:test_password@ep-test.neon.tech:5432/testdb"
    "?sslmode=require"
)
_OTHER_URL = (
    "postgresql://other_user:other_password@ep-other.neon.tech:5432/otherdb"
)


class _FakeSettings:
    """Minimal stand-in for Settings.async_database_url_unpooled."""

    def __init__(self, unpooled_url: str) -> None:
        self.async_database_url_unpooled = unpooled_url


def _prepare(
    environ: dict[str, str],
    *,
    unpooled_url: str = _LIVE_URL,
) -> tuple[MigrationTarget, str]:
    stream = io.StringIO()
    target = prepare_migration_target(
        settings_obj=_FakeSettings(unpooled_url),
        environ=environ,
        stream=stream,
    )
    return target, stream.getvalue()


# ---------------------------------------------------------------------------
# Resolution precedence and source labels
# ---------------------------------------------------------------------------


def test_alembic_env_wins_over_target_and_fallback() -> None:
    target, stderr = _prepare(
        {
            "ALEMBIC_DATABASE_URL": _TEST_URL,
            "TARGET_DATABASE_URL": _OTHER_URL,
        },
        unpooled_url=_TEST_URL,
    )

    assert target.source == "ALEMBIC_DATABASE_URL"
    assert target.descriptor == "ep-test.neon.tech:5432/testdb"
    assert "ep-test.neon.tech:5432/testdb" in stderr
    assert "source: ALEMBIC_DATABASE_URL" in stderr


def test_target_env_wins_when_alembic_env_is_empty() -> None:
    target, stderr = _prepare(
        {
            "ALEMBIC_DATABASE_URL": "",
            "TARGET_DATABASE_URL": _TEST_URL,
        },
        unpooled_url=_TEST_URL,
    )

    assert target.source == "TARGET_DATABASE_URL"
    assert target.descriptor == "ep-test.neon.tech:5432/testdb"
    assert "source: TARGET_DATABASE_URL" in stderr


def test_empty_overrides_fall_back_to_settings_unpooled() -> None:
    target, stderr = _prepare(
        {
            "ALEMBIC_DATABASE_URL": "",
            "TARGET_DATABASE_URL": "",
        }
    )

    assert target.source == "DATABASE_URL_UNPOOLED"
    assert target.descriptor == "ep-live.neon.tech:5432/neondb"
    assert "source: DATABASE_URL_UNPOOLED" in stderr


def test_asyncpg_conversion_and_query_stripping() -> None:
    target, _ = _prepare(
        {"ALEMBIC_DATABASE_URL": _TEST_URL}, unpooled_url=_TEST_URL
    )

    assert target.url.startswith("postgresql+asyncpg://")
    assert "sslmode" not in target.url
    assert "channel_binding" not in target.url


def test_uppercase_scheme_is_normalized_to_asyncpg() -> None:
    target, _ = _prepare(
        {
            "ALEMBIC_DATABASE_URL": (
                "POSTGRESQL://live_user:live_password@ep-live.neon.tech:5432/neondb"
            )
        },
        unpooled_url=_LIVE_URL,
    )

    assert target.url.startswith("postgresql+asyncpg://")
    assert target.descriptor == "ep-live.neon.tech:5432/neondb"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_descriptor_and_stderr_never_include_credentials_or_query() -> None:
    target, stderr = _prepare(
        {"ALEMBIC_DATABASE_URL": _TEST_URL}, unpooled_url=_TEST_URL
    )

    for output in (target.descriptor, stderr):
        assert "test_user" not in output
        assert "test_password" not in output
        assert "sslmode" not in output
        assert "channel_binding" not in output

    assert target.descriptor == "ep-test.neon.tech:5432/testdb"


def test_announcement_uses_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    prepare_migration_target(
        settings_obj=_FakeSettings(_TEST_URL),
        environ={"ALEMBIC_DATABASE_URL": _TEST_URL},
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[alembic] target: ep-test.neon.tech:5432/testdb" in captured.err
    assert "source: ALEMBIC_DATABASE_URL" in captured.err


def test_target_is_announced_before_a_mismatch_failure() -> None:
    stream = io.StringIO()
    with pytest.raises(AlembicTargetError, match="target mismatch"):
        prepare_migration_target(
            settings_obj=_FakeSettings(_LIVE_URL),
            environ={"ALEMBIC_DATABASE_URL": _TEST_URL},
            stream=stream,
        )

    assert "ep-test.neon.tech:5432/testdb" in stream.getvalue()
    assert "test_password" not in stream.getvalue()



def test_mismatch_error_redacts_both_targets() -> None:
    with pytest.raises(AlembicTargetError) as exc_info:
        _prepare({"ALEMBIC_DATABASE_URL": _TEST_URL})

    message = str(exc_info.value)
    assert "ep-test.neon.tech:5432/testdb" in message
    assert "ep-live.neon.tech:5432/neondb" in message
    assert "test_user" not in message
    assert "test_password" not in message
    assert "live_user" not in message
    assert "live_password" not in message
    assert "sslmode" not in message


# ---------------------------------------------------------------------------
# Mismatch guard
# ---------------------------------------------------------------------------


def test_mismatch_fails_closed_by_default() -> None:
    with pytest.raises(AlembicTargetError, match="target mismatch"):
        _prepare({"ALEMBIC_DATABASE_URL": _TEST_URL})


def test_mismatch_error_names_both_sources_and_the_fix() -> None:
    with pytest.raises(AlembicTargetError) as exc_info:
        _prepare({"TARGET_DATABASE_URL": _TEST_URL})

    message = str(exc_info.value)
    assert "TARGET_DATABASE_URL" in message
    assert "DATABASE_URL_UNPOOLED" in message
    assert "ALEMBIC_ALLOW_DB_MISMATCH=1" in message


def test_mismatch_bypass_requires_exactly_one() -> None:
    target, stderr = _prepare(
        {
            "ALEMBIC_DATABASE_URL": _TEST_URL,
            "ALEMBIC_ALLOW_DB_MISMATCH": "1",
        }
    )

    assert target.mismatch_bypassed is True
    assert "WARNING: ALEMBIC_ALLOW_DB_MISMATCH=1" in stderr
    assert "ep-test.neon.tech:5432/testdb" in stderr
    assert "ep-live.neon.tech:5432/neondb" in stderr


@pytest.mark.parametrize("bypass_value", ["true", "TRUE", "yes", "0", ""])
def test_mismatch_bypass_rejects_other_values(bypass_value: str) -> None:
    with pytest.raises(AlembicTargetError, match="target mismatch"):
        _prepare(
            {
                "ALEMBIC_DATABASE_URL": _TEST_URL,
                "ALEMBIC_ALLOW_DB_MISMATCH": bypass_value,
            }
        )


def test_same_host_and_default_port_is_not_a_mismatch() -> None:
    target, _ = _prepare(
        {"ALEMBIC_DATABASE_URL": "postgresql://u:p@EP-LIVE.NEON.TECH/otherdb"}
    )

    assert target.source == "ALEMBIC_DATABASE_URL"
    assert target.descriptor == "ep-live.neon.tech:5432/otherdb"
    assert target.mismatch_bypassed is False


def test_different_port_is_a_mismatch() -> None:
    with pytest.raises(AlembicTargetError, match="target mismatch"):
        _prepare(
            {
                "ALEMBIC_DATABASE_URL": (
                    "postgresql://u:p@ep-live.neon.tech:5433/neondb"
                )
            }
        )


def test_fallback_source_skips_the_mismatch_guard() -> None:
    target, _ = _prepare({})

    assert target.source == "DATABASE_URL_UNPOOLED"
    assert target.mismatch_bypassed is False


# ---------------------------------------------------------------------------
# Malformed URLs fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "postgresql://user:secret@ep-test.neon.tech:notaport/testdb",
        "postgresql:///testdb",
        "postgresql://user:secret@/testdb",
        "postgresql://user:secret@[::1/testdb",
        "sqlite+aiosqlite:///haunter.db",
        "postgresql://user:secret@ep-test.neon.tech:0/testdb",
    ],
)
def test_malformed_override_fails_closed_without_echoing_the_url(
    bad_url: str,
) -> None:
    with pytest.raises(AlembicTargetError) as exc_info:
        _prepare({"ALEMBIC_DATABASE_URL": bad_url})

    message = str(exc_info.value)
    assert "Refusing to run migrations" in message
    assert "ALEMBIC_DATABASE_URL" in message
    assert "secret" not in message
    assert "notaport" not in message


def test_malformed_fallback_fails_closed() -> None:
    with pytest.raises(AlembicTargetError) as exc_info:
        _prepare({}, unpooled_url="not-a-url")

    message = str(exc_info.value)
    assert "DATABASE_URL_UNPOOLED" in message
    assert "Refusing to run migrations" in message


def test_override_does_not_bypass_fallback_parsing_for_the_guard() -> None:
    with pytest.raises(AlembicTargetError) as exc_info:
        _prepare(
            {"ALEMBIC_DATABASE_URL": _TEST_URL},
            unpooled_url="not-a-url",
        )

    assert "DATABASE_URL_UNPOOLED" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Credentials never leak — message, stderr, or chained exception
# ---------------------------------------------------------------------------

# Fragments of the credentials embedded in the malformed URLs below. A URL
# whose authority was truncated (``postgresql://user:pa/ss@host:5432/db``)
# used to leak the password prefix through the chained ``ValueError`` and the
# password suffix through the printed descriptor.
_PASSWORD_FRAGMENTS = ("pa", "ss", "user")


@pytest.mark.parametrize(
    "malformed_url",
    [
        # Password prefix: the '/' inside the password truncates the authority,
        # so the password prefix was parsed as the port and echoed by the
        # chained ValueError.
        "postgresql://user:pa/ss@host:5432/db",
        # Password suffix: the '@' lands in the path, so the userinfo was
        # mistaken for the host and the password for the database name.
        "postgresql://user:12/ss@host/db",
        # Two delimiters in the authority — the userinfo is ambiguous.
        "postgresql://user:pa@ss@host:5432/db",
        # A truncated authority followed by a second path segment.
        "postgresql://user:12/ss/db",
    ],
)
def test_malformed_url_never_leaks_credentials(
    malformed_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(AlembicTargetError) as exc_info:
        prepare_migration_target(
            settings_obj=_FakeSettings(_LIVE_URL),
            environ={"ALEMBIC_DATABASE_URL": malformed_url},
        )

    message = str(exc_info.value)
    stderr = capsys.readouterr().err

    for output in (message, stderr):
        for fragment in _PASSWORD_FRAGMENTS:
            assert fragment not in output
        assert "@" not in output
        assert "://" not in output
        assert malformed_url not in output

    # The original parse error must never be chained: its text can embed a
    # fragment of the URL (for example the offending port).
    assert exc_info.value.__cause__ is None
    assert (
        exc_info.value.__context__ is None
        or exc_info.value.__suppress_context__ is True
    )
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "The above exception" not in rendered
    assert "direct cause" not in rendered
    assert malformed_url not in rendered


def test_password_is_never_part_of_the_descriptor_or_announcement() -> None:
    url = "postgresql://app_user:app_password@db.internal:5432/appdb"
    target, stderr = _prepare({"ALEMBIC_DATABASE_URL": url}, unpooled_url=url)

    assert target.descriptor == "db.internal:5432/appdb"
    for output in (target.descriptor, stderr):
        assert "app_user" not in output
        assert "app_password" not in output
        assert "password" not in output
        assert "@" not in output
        assert "://" not in output
