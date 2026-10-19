"""Resolve and guard the database target used by Alembic migrations.

Alembic runs from ``backend/``, where ``alembic/env.py`` calls bare
``load_dotenv()`` before reading the target. Because a stale
``ALEMBIC_DATABASE_URL`` in ``.env`` silently outranks the configured
``DATABASE_URL_UNPOOLED``, a green migration run is not proof that the intended
database was migrated. This module makes the resolved target visible, refuses
malformed URLs, and blocks host/port mismatches unless the operator explicitly
opts into them.

No database connection is opened here, and nothing is written to stdout: Alembic
uses stdout for ``--sql`` output, so all announcements go to stderr.

The URL is parsed defensively: the authority is split by hand so the userinfo
component (``user:password@``) can never reach a descriptor, an announcement,
or an error message, and a URL whose authority was truncated (a credential
delimiter outside the authority) is rejected instead of being mistaken for a
host and a database name.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TextIO
from urllib.parse import urlparse

from app.config import Settings, _to_asyncpg_url, settings

#: Checked in order. The first non-empty value wins.
_OVERRIDE_ENV_VARS: tuple[str, str] = (
    "ALEMBIC_DATABASE_URL",
    "TARGET_DATABASE_URL",
)

_FALLBACK_SOURCE = "DATABASE_URL_UNPOOLED"
_MISMATCH_BYPASS_ENV = "ALEMBIC_ALLOW_DB_MISMATCH"
_ALLOWED_SCHEMES = frozenset({"postgresql", "postgresql+asyncpg"})
_DEFAULT_POSTGRES_PORT = 5432
_MAX_PORT = 65535


class AlembicTargetError(RuntimeError):
    """The migration target is malformed or differs from the configured database."""


class _MalformedUrl(Exception):
    """Internal sentinel for a URL that cannot be parsed safely.

    The message never contains any part of the URL, so the sentinel can never
    leak a credential even if it is surfaced by accident.
    """


@dataclass(frozen=True)
class MigrationTarget:
    """A validated Alembic target, safe to hand to ``context.configure``."""

    #: asyncpg-compatible URL with Alembic-incompatible query parameters removed.
    url: str
    #: Env var that supplied the URL (``DATABASE_URL_UNPOOLED`` for the fallback).
    source: str
    #: ``host:port/db`` only — never userinfo, password, or query string.
    descriptor: str
    #: True when an intentional host/port mismatch was allowed via the bypass env var.
    mismatch_bypassed: bool = False


@dataclass(frozen=True)
class _ParsedTarget:
    url: str
    hostname: str
    port: int
    descriptor: str


def _redacted_descriptor(hostname: str, port: int, database: str | None) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    descriptor = f"{host}:{port}"
    if database:
        descriptor = f"{descriptor}/{database}"
    return descriptor


def _split_authority(netloc: str) -> tuple[str, int | None]:
    """Split a URL authority into ``(host, port)``.

    The optional userinfo component (``user:password@``) is validated and then
    dropped: it must never reach a descriptor, an announcement, or a message.
    The port is parsed here rather than through ``urlsplit`` so that a bad port
    can never end up inside an exception message.
    """
    hostinfo = netloc
    if "@" in netloc:
        userinfo, _, hostinfo = netloc.rpartition("@")
        if "@" in userinfo:
            raise _MalformedUrl("multiple delimiters in the authority")
        if not userinfo:
            raise _MalformedUrl("empty userinfo")
        if any(not char.isprintable() or char.isspace() for char in userinfo):
            raise _MalformedUrl("unusable userinfo")

    if hostinfo.startswith("["):
        end = hostinfo.find("]")
        if end == -1:
            raise _MalformedUrl("unterminated IPv6 literal")
        host = hostinfo[1:end]
        tail = hostinfo[end + 1 :]
        if not tail:
            return host, None
        if not tail.startswith(":"):
            raise _MalformedUrl("unexpected characters after the host")
        port_text = tail[1:]
    else:
        host, separator, port_text = hostinfo.rpartition(":")
        if not separator:
            return hostinfo, None
        if ":" in host:
            raise _MalformedUrl("unbracketed IPv6 literal")

    if not port_text:
        return host, None
    if not (port_text.isascii() and port_text.isdigit()):
        raise _MalformedUrl("port is not numeric")
    port = int(port_text)
    if port > _MAX_PORT:
        raise _MalformedUrl("port is out of range")
    return host, port


def _database_name(path: str) -> str | None:
    """Extract the database name from the URL path.

    ``None`` means the URL carries no database name. Anything ambiguous — a
    second path segment, a credential delimiter, whitespace — is rejected: the
    path is only ever a database name, never the tail of a truncated authority.
    """
    name = path.strip("/")
    if not name:
        return None
    if "/" in name:
        raise _MalformedUrl("unexpected path segment")
    if "@" in name:
        raise _MalformedUrl("credential delimiter in the path")
    if any(not char.isprintable() or char.isspace() for char in name):
        raise _MalformedUrl("unusable database name")
    return name


def _parse_target(raw_url: str, *, source: str) -> _ParsedTarget:
    """Parse a PostgreSQL URL without ever echoing its credentials in an error."""
    cleaned = raw_url.strip()
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        # urlparse raises for invalid IPv6 literals. The original error is
        # discarded instead of chained: it can embed a fragment of the URL.
        raise AlembicTargetError(
            f"Alembic target from {source} is malformed (invalid host or port). "
            "Refusing to run migrations."
        ) from None

    scheme = parsed.scheme.lower()
    if not scheme:
        raise AlembicTargetError(
            f"Alembic target from {source} has no URL scheme "
            "(expected postgresql:// or postgresql+asyncpg://). "
            "Refusing to run migrations."
        )
    if scheme not in _ALLOWED_SCHEMES:
        raise AlembicTargetError(
            f"Alembic target from {source} is not a PostgreSQL URL "
            f"(scheme {scheme!r}). Refusing to run migrations."
        )

    # Hard guard: the credential delimiter belongs to the authority. A
    # delimiter in the path means the authority was truncated (for example a
    # '/' inside a password), and the rest of the URL would otherwise be
    # mistaken for a host and a database name.
    if "@" in parsed.path:
        raise AlembicTargetError(
            f"Alembic target from {source} is malformed (misplaced credential "
            "delimiter outside the authority). Refusing to run migrations."
        )

    try:
        hostname, port = _split_authority(parsed.netloc)
    except _MalformedUrl:
        raise AlembicTargetError(
            f"Alembic target from {source} is malformed (unusable credentials, "
            "host, or port). Refusing to run migrations."
        ) from None

    if not hostname or any(char.isspace() for char in hostname):
        raise AlembicTargetError(
            f"Alembic target from {source} has no usable hostname. "
            "Refusing to run migrations."
        )
    if port == 0:
        raise AlembicTargetError(
            f"Alembic target from {source} uses port 0, which is not a valid "
            "PostgreSQL port. Refusing to run migrations."
        )

    try:
        database = _database_name(parsed.path)
    except _MalformedUrl:
        raise AlembicTargetError(
            f"Alembic target from {source} is malformed (unusable database "
            "name). Refusing to run migrations."
        ) from None

    normalized_host = hostname.lower()
    normalized_port = port if port is not None else _DEFAULT_POSTGRES_PORT
    normalized_url = parsed._replace(scheme=scheme).geturl()

    return _ParsedTarget(
        url=_to_asyncpg_url(normalized_url),
        hostname=normalized_host,
        port=normalized_port,
        descriptor=_redacted_descriptor(normalized_host, normalized_port, database),
    )


def _select_target(
    settings_obj: Settings, environ: Mapping[str, str]
) -> tuple[str, str]:
    for source in _OVERRIDE_ENV_VARS:
        value = environ.get(source)
        if value:
            return source, value
    return _FALLBACK_SOURCE, settings_obj.async_database_url_unpooled


def prepare_migration_target(
    *,
    settings_obj: Settings = settings,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> MigrationTarget:
    """Resolve, announce, and guard the target before anything connects.

    Precedence is ``ALEMBIC_DATABASE_URL`` → ``TARGET_DATABASE_URL`` →
    ``DATABASE_URL_UNPOOLED`` (empty values are skipped). An override that wins
    must match the fallback's ``(hostname, port)`` unless
    ``ALEMBIC_ALLOW_DB_MISMATCH`` is exactly ``"1"``; a bypass is announced
    loudly. Both the resolved target and the source are always printed to
    stderr before the mismatch guard can fail, so a failed invocation still
    shows which database it was about to touch.
    """
    active_environ = os.environ if environ is None else environ
    stderr = sys.stderr if stream is None else stream

    source, raw_url = _select_target(settings_obj, active_environ)
    resolved = _parse_target(raw_url, source=source)

    print(
        f"[alembic] target: {resolved.descriptor} (source: {source})",
        file=stderr,
        flush=True,
    )

    mismatch_bypassed = False
    if source != _FALLBACK_SOURCE:
        fallback = _parse_target(
            settings_obj.async_database_url_unpooled,
            source=_FALLBACK_SOURCE,
        )
        if (resolved.hostname, resolved.port) != (fallback.hostname, fallback.port):
            if active_environ.get(_MISMATCH_BYPASS_ENV) != "1":
                raise AlembicTargetError(
                    "Alembic target mismatch: "
                    f"{source} resolves to {resolved.descriptor}, but "
                    f"{_FALLBACK_SOURCE} resolves to {fallback.descriptor}. "
                    "Refusing to migrate a database that differs from the "
                    "configured unpooled target. If this is intentional (for "
                    "example a scratch or test database on another host), set "
                    f"{_MISMATCH_BYPASS_ENV}=1 for that single command and "
                    "re-run. Both live and test migration targets must use a "
                    "direct (non-pooler) endpoint."
                )
            mismatch_bypassed = True
            print(
                f"[alembic] WARNING: {_MISMATCH_BYPASS_ENV}=1 - migrating "
                f"{resolved.descriptor} ({source}) while {_FALLBACK_SOURCE} "
                f"points at {fallback.descriptor}. Target mismatch explicitly "
                f"allowed. Scope {_MISMATCH_BYPASS_ENV} to this single command "
                "and unset it afterwards.",
                file=stderr,
                flush=True,
            )

    return MigrationTarget(
        url=resolved.url,
        source=source,
        descriptor=resolved.descriptor,
        mismatch_bypassed=mismatch_bypassed,
    )
