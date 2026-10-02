"""
Tests for .github/workflows/*.yml — no workflow may hardcode a remote database URL.

The bug this guards (#35): the backend job's test step exported DATABASE_URL and
DATABASE_URL_UNPOOLED pointing at a production-shaped Neon host
(`haunter_prod:haunter_prod@ep-prod.neon.tech/haunter_prod`) alongside
TEST_DATABASE_URL. The credentials were a placeholder, so nothing leaked, but
`Settings.database_url` has no default and `app/db.py` builds a live engine from
it at import time — so the test job carried a prod-bound engine whose only
protection was the conftest fixture guard. Deleting the vars is not an option
(they are required for Settings() to construct); leaving a prod-shaped slot in
the file is worse.

These tests are hermetic: they read the workflow files as text and never open a
socket, so they stay in the `fast` suite.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_CI_WORKFLOW = _WORKFLOW_DIR / "ci.yml"

# Matches the scheme only, so filesystem paths like /var/lib/postgresql/certs
# are not mistaken for a connection string.
_URL_RE = re.compile(r"postgresql(?:\+asyncpg)?://[^\s\"']+")

# A connection string whose authority came from a GitHub expression
# (${{ secrets.* }}) is resolved at run time and cannot be checked here — and is
# the sanctioned way to reference a real database from a workflow.
_SECRET_EXPANSION = "${{"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

_TEST_DB_URL = (
    "postgresql+asyncpg://haunter_test:haunter_test@"
    "localhost:5432/haunter_test?sslmode=require"
)


def _iter_workflow_urls() -> list[tuple[Path, int, str]]:
    """Return (file, line number, url) for every postgres URL in every workflow."""
    found: list[tuple[Path, int, str]] = []
    for workflow in sorted(_WORKFLOW_DIR.glob("*.yml")):
        for lineno, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for url in _URL_RE.findall(line):
                found.append((workflow, lineno, url))
    return found


def _host(url: str) -> str | None:
    """Hostname with any userinfo and port stripped, lowercased.

    Uses urlparse().hostname rather than splitting netloc on "@" so the port is
    excluded too — a loopback container publishes on 127.0.0.1:5432, and
    comparing netloc against a bare hostname would flag it.
    """
    return urlparse(url).hostname


def test_no_workflow_hardcodes_a_remote_database_url() -> None:
    """Every postgres URL in a workflow must point at loopback.

    A hardcoded production host in CI is the failure #35 reported: it is inert
    only for as long as nobody replaces the placeholder credentials with real
    ones, and app/db.py then hands the whole test run an engine bound to prod.
    """
    offenders = [
        f"{workflow.relative_to(_REPO_ROOT)}:{lineno}: {url}"
        for workflow, lineno, url in _iter_workflow_urls()
        if _SECRET_EXPANSION not in url and _host(url) not in _LOOPBACK_HOSTS
    ]
    assert not offenders, (
        "Workflow files must not hardcode a non-loopback postgres URL — the test "
        "job would then build a live engine against it at import time "
        "(backend/app/db.py). Point it at the local Postgres service, or reference "
        "a real database through a ${{ secrets.* }} expression in the job that "
        "genuinely needs it. Offending lines:\n" + "\n".join(offenders)
    )


def test_ci_test_step_database_urls_match_the_migration_step() -> None:
    """The test step's DATABASE_URL must equal the URL its migrations ran against.

    The backend job starts a Postgres container, migrates it, then runs the suite
    against it. If TEST_DATABASE_URL and DATABASE_URL disagree, every session
    taken from app.db.async_session_maker outside the `db` fixture talks to a
    different database than the one the schema was created in — which is exactly
    the split #35 introduced.
    """
    source = _CI_WORKFLOW.read_text(encoding="utf-8")

    test_step = source.split("- name: Run backend tests", 1)[1]
    test_urls = dict(
        re.findall(
            r"^\s+(TEST_DATABASE_URL|DATABASE_URL):\s*(\S+)\s*$",
            test_step,
            re.MULTILINE,
        )
    )
    assert test_urls, (
        "The 'Run backend tests' step no longer declares TEST_DATABASE_URL / "
        "DATABASE_URL — Settings() requires both, so their absence means this "
        "test no longer knows what the suite is pointed at. Update this test."
    )
    assert set(test_urls) == {"TEST_DATABASE_URL", "DATABASE_URL"}, (
        f"Unexpected env keys in the test step: {sorted(test_urls)}"
    )
    wrong = {key: url for key, url in test_urls.items() if url != _TEST_DB_URL}
    assert not wrong, (
        f"The test step must point every database URL at the same local container "
        f"the migration steps use ({_TEST_DB_URL!r}). Divergent values: {wrong}"
    )

    migration_step = source.split("- name: Apply Alembic migrations", 1)[1]
    migration_url = re.search(
        r"^\s+DATABASE_URL:\s*(\S+)\s*$", migration_step, re.MULTILINE
    )
    assert migration_url is not None, (
        "The 'Apply Alembic migrations' step no longer declares DATABASE_URL — "
        "update this test."
    )
    assert migration_url.group(1) == _TEST_DB_URL, (
        f"The migration step now targets {migration_url.group(1)!r} while the test "
        f"step targets {_TEST_DB_URL!r}. The suite must run against the database it "
        "was migrated on."
    )
