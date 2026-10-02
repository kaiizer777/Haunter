"""
Tests for the truncate_all() production guard in tests/conftest.py.

The guard exists because it failed once: backend/scripts/restore_repos.py records
that the suite wiped production data through truncate_all(). So these tests pin
both directions — a real prod host must still be blocked, and the CI throwaway
container must not be.

Hermetic: the guard is a pure function of a URL string plus settings, so nothing
here opens a socket.
"""

from unittest.mock import patch

import pytest

from tests.conftest import _is_loopback_url, _is_prod_url

PROD_URL = "postgresql+asyncpg://u:p@ep-square-sun-azk3knbd.c-3.ap-southeast-1.aws.neon.tech/neondb"
CI_URL = "postgresql+asyncpg://haunter_test:haunter_test@localhost:5432/haunter_test"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@localhost:5432/haunter_test",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/haunter_test",
        "postgresql+asyncpg://u:p@[::1]:5432/haunter_test",
        "postgresql://u:p@localhost/haunter_test",
    ],
)
def test_loopback_urls_are_recognised(url: str) -> None:
    assert _is_loopback_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        PROD_URL,
        # A prod host must not be loopback just because the credentials match.
        "postgresql+asyncpg://haunter_test:haunter_test@localhost-local:5432/db",
        "postgresql+asyncpg://u:p@localhost.evil.example.com:5432/db",
        "postgresql+asyncpg://u:p@notlocalhost:5432/db",
    ],
)
def test_remote_urls_are_not_loopback(url: str) -> None:
    assert _is_loopback_url(url) is False


def test_prod_host_still_matches_itself_under_host_equality() -> None:
    """The original protection: the configured prod host equals itself."""
    with patch("app.config.settings.database_url", PROD_URL):
        assert _is_prod_url(PROD_URL) is True


def test_a_different_remote_host_is_not_prod() -> None:
    with patch("app.config.settings.database_url", PROD_URL):
        assert _is_prod_url("postgresql+asyncpg://u:p@ep-other-branch.aws.neon.tech/neondb") is False


def test_ci_container_matching_settings_is_not_loopback_exempt_by_accident() -> None:
    """Documents the trap #35 hit: identical hosts, but a loopback one.

    With DATABASE_URL pointing at localhost (as CI now does), plain host
    equality returns True for the CI container — which is exactly why
    truncate_all() needs the explicit loopback exemption rather than relying
    on the hosts differing.
    """
    with patch("app.config.settings.database_url", CI_URL):
        assert _is_prod_url(CI_URL) is True
        assert _is_loopback_url(CI_URL) is True


def test_credentials_in_the_url_cannot_change_the_verdict() -> None:
    """Userinfo is stripped before comparison, so it cannot mask a prod host."""
    with patch("app.config.settings.database_url", PROD_URL):
        disguised = (
            "postgresql+asyncpg://localhost:pw@"
            "ep-square-sun-azk3knbd.c-3.ap-southeast-1.aws.neon.tech:5432/neondb"
        )
        assert _is_prod_url(disguised) is True


@pytest.mark.asyncio
async def test_truncate_all_blocks_a_real_prod_session() -> None:
    """The regression that matters: prod must still refuse to be truncated."""

    class _FakeUrl:
        database = "neondb"

        def __str__(self) -> str:
            return PROD_URL

    class _FakeBind:
        url = _FakeUrl()

    class _FakeSession:
        def get_bind(self) -> _FakeBind:
            return _FakeBind()

    from tests.conftest import truncate_all

    with patch("app.config.settings.database_url", PROD_URL):
        with pytest.raises(RuntimeError, match="truncate_all\\(\\) blocked"):
            await truncate_all(_FakeSession())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_truncate_all_permits_the_ci_loopback_container() -> None:
    class _FakeUrl:
        database = "haunter_test"

        def __str__(self) -> str:
            return CI_URL

    class _FakeBind:
        url = _FakeUrl()

    class _FakeSession:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def get_bind(self) -> _FakeBind:
            return _FakeBind()

        async def execute(self, stmt: object) -> None:
            self.executed.append(str(stmt))

        async def commit(self) -> None:
            return None

    from tests.conftest import truncate_all

    session = _FakeSession()
    with patch("app.config.settings.database_url", CI_URL):
        await truncate_all(session)  # type: ignore[arg-type]

    # Every table was actually cleared — the exemption permits the DELETEs
    # rather than silently skipping them.
    assert len(session.executed) == 13
    assert any("DELETE FROM users" in stmt for stmt in session.executed)
