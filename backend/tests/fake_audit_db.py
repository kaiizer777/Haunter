"""
In-process, hermetic stand-in for the audit pipeline's PostgreSQL session.

Why this exists
---------------
``tests/conftest.py::db`` opens a real (remote) Neon branch over ``NullPool`` and
runs ``truncate_all()`` — twelve ``DELETE`` statements plus a commit — before
*and* after every test. That costs 5-15s of round trips per test (measured:
7-30s per test on this branch), so the audit suites are unusable in a normal dev
loop and impossible to run without network access.

What this is
------------
A small executor for the exact SQLAlchemy statements ``app.services.audit_pipeline``
and ``app.webhooks`` emit, running against in-process rows that are *real*
``app.models`` ORM instances. Production code is not stubbed: the dispatcher
still builds the same ``INSERT ... ON CONFLICT DO NOTHING``, the same
``SELECT ... FOR UPDATE SKIP LOCKED``, the same conditional ``UPDATE`` fences,
the same ``CASE``-based lease recovery, the same delivery fingerprints and
HMAC dispatch fences. Only the transport is replaced.

What is deliberately NOT emulated
---------------------------------
Database-enforced behaviour, which is not reproducible in Python:

* ``UniqueConstraint`` enforcement for a plain ``INSERT`` (only
  ``ON CONFLICT DO NOTHING`` naming a constraint is honoured),
* ``CheckConstraint`` validation,
* foreign keys, ``server_default`` resolution, migrations.

Tests that depend on those are marked ``@pytest.mark.db`` and still run against
a real PostgreSQL database.

Fail-loud contract
------------------
:class:`FakeStore` models a closed set of statement shapes. Anything else raises
:class:`UnsupportedStatementError` naming the construct, so the fake can never
silently diverge from real SQL — a new query shape in production breaks the
suite loudly instead of quietly passing.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional, Sequence

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import ColumnProperty, Mapper
from sqlalchemy.orm.base import NO_VALUE
from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Case,
    ColumnElement,
    Grouping,
    Null,
    UnaryExpression,
)
from sqlalchemy.sql.functions import Function
from sqlalchemy.sql.selectable import Join, Select, TableClause

from app.models import Base

__all__ = [
    "FakeAsyncSession",
    "FakeResult",
    "FakeSessionMaker",
    "FakeStore",
    "UnsupportedStatementError",
    "audit_store",
    "fake_audit_db",
    "fake_audit_user_factory",
]


class UnsupportedStatementError(AssertionError):
    """The fake was handed a SQL construct it deliberately does not model."""


def _unsupported(node: Any) -> UnsupportedStatementError:
    return UnsupportedStatementError(
        f"fake audit store does not model this SQL construct: {type(node).__name__}"
    )


# ---------------------------------------------------------------------------
# Expression evaluation
# ---------------------------------------------------------------------------

#: Table name -> mapped class, resolved once from real model metadata so the
#: fake never has to guess what a ``TableClause``/``AnnotatedTable`` maps to.
_TABLE_ENTITY = {
    mapper.local_table.name: mapper.class_
    for mapper in Base.registry.mappers
    if mapper.local_table is not None
    and getattr(mapper.local_table, "name", None) is not None
}


def _entity_of(node: Any) -> Any:
    """Return the mapped class a column belongs to, or None for a literal."""
    table = getattr(node, "table", None)
    if table is None:
        return None
    return _TABLE_ENTITY.get(getattr(table, "name", None))


def _row_for(node: Any, bound: Mapping[Any, Any]) -> Optional[Any]:
    if isinstance(node, ColumnElement) and not isinstance(node, BindParameter):
        entity = _entity_of(node)
        if entity is None:
            return None
        if entity not in bound:
            raise UnsupportedStatementError(
                f"column {node.key!r} refers to {entity.__tablename__!r}, "
                "which is not part of this statement's FROM clause"
            )
        return bound[entity]
    return None


def _eval_value(node: Any, bound: Mapping[Any, Any]) -> Any:
    """Evaluate a scalar SQL expression: an UPDATE value, a literal, a column."""
    if isinstance(node, Grouping):
        return _eval_value(node.element, bound)
    if isinstance(node, BindParameter):
        return node.value
    if isinstance(node, Null):
        return None
    if isinstance(node, BinaryExpression):
        if node.operator is not operators.add:
            raise _unsupported(node)
        return _eval_value(node.left, bound) + _eval_value(node.right, bound)
    if isinstance(node, Case):
        for condition, value in node.whens:
            if _eval_predicate(condition, bound) is True:
                return _eval_value(value, bound)
        if node.else_ is None:
            return None
        return _eval_value(node.else_, bound)
    row = _row_for(node, bound)
    if row is not None:
        return getattr(row, node.key)
    raise _unsupported(node)


def _compare(left: Any, right: Any, check) -> bool:
    """PostgreSQL comparison: any NULL operand yields UNKNOWN, not a match."""
    if left is None or right is None:
        return False
    return bool(check(left, right))


_COMPARISONS = {
    operators.eq: lambda a, b: a == b,
    operators.ne: lambda a, b: a != b,
    operators.gt: lambda a, b: a > b,
    operators.ge: lambda a, b: a >= b,
    operators.lt: lambda a, b: a < b,
    operators.le: lambda a, b: a <= b,
}


def _eval_predicate(node: Any, bound: Mapping[Any, Any]) -> Optional[bool]:
    """Evaluate a WHERE/on-clause. Returns True, False, or None (UNKNOWN)."""
    if node is None:
        return True
    if isinstance(node, Grouping):
        return _eval_predicate(node.element, bound)
    if isinstance(node, BooleanClauseList):
        values = [_eval_predicate(clause, bound) for clause in node.clauses]
        if node.operator is operators.and_:
            if any(value is False for value in values):
                return False
            return None if any(value is None for value in values) else True
        if node.operator is operators.or_:
            if any(value is True for value in values):
                return True
            return None if any(value is None for value in values) else False
        raise _unsupported(node)
    if isinstance(node, BinaryExpression):
        if node.operator is operators.is_:
            return _eval_value(node.left, bound) is _eval_value(node.right, bound)
        if node.operator is operators.is_not:
            return _eval_value(node.left, bound) is not _eval_value(node.right, bound)
        if node.operator in _COMPARISONS:
            left = _eval_value(node.left, bound)
            right = _eval_value(node.right, bound)
            if left is None or right is None:
                return None
            return bool(_COMPARISONS[node.operator](left, right))
        if node.operator is operators.in_op:
            left = _eval_value(node.left, bound)
            if left is None:
                return None
            return any(
                _compare(left, item, _COMPARISONS[operators.eq])
                for item in _eval_value(node.right, bound)
            )
        raise _unsupported(node)
    raise _unsupported(node)


def _sort_key(value: Any) -> tuple[int, Any]:
    """PostgreSQL `ASC` puts NULLs first; keep mixed types comparable."""
    if value is None:
        return (0, "")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (1, str(value))
    return (1, float(value))


# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------


class FakeRow(tuple):
    """A result row addressable positionally and by column key."""

    def __new__(cls, keys: Sequence[str], values: Sequence[Any]) -> "FakeRow":
        row = super().__new__(cls, tuple(values))
        row.__dict__["_keys"] = tuple(keys)
        return row

    def __getattr__(self, name: str) -> Any:
        keys = self.__dict__["_keys"]
        if name not in keys:
            raise AttributeError(name)
        return self[keys.index(name)]


class _Rows:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> Any:
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise AssertionError("one_or_none() matched more than one row")
        return self._rows[0]

    def one(self) -> Any:
        value = self.one_or_none()
        if value is None:
            raise AssertionError("one() matched no rows")
        return value


class FakeResult:
    """Duck-types the part of `AsyncSession.execute()`'s result production uses."""

    def __init__(self, rows: Sequence[Any], rowcount: int = 0) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self) -> _Rows:
        return _Rows([row[0] if isinstance(row, tuple) else row for row in self._rows])

    def scalar(self) -> Any:
        return self.scalars().first()

    def scalar_one_or_none(self) -> Any:
        return self.scalars().one_or_none()

    def scalar_one(self) -> Any:
        return self.scalars().one()

    def one_or_none(self) -> Any:
        return _Rows(self._rows).one_or_none()

    def one(self) -> Any:
        return _Rows(self._rows).one()

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class FakeStore:
    """Shared in-memory row store standing in for one PostgreSQL database.

    Every :class:`FakeAsyncSession` handed out by :class:`FakeSessionMaker`
    shares one store, mirroring how separate pooled connections observe the same
    committed rows.
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[Any]] = {}

    def rows(self, table: TableClause) -> list[Any]:
        return self._tables.setdefault(table.name, [])

    def contains(self, instance: Any) -> bool:
        table = sa_inspect(type(instance)).local_table
        return any(row is instance for row in self.rows(table))

    # -- dispatch ----------------------------------------------------------

    def execute(self, statement: Any) -> FakeResult:
        if isinstance(statement, Insert):
            return self._insert(statement)
        if isinstance(statement, Update):
            return self._update(statement)
        if isinstance(statement, Select):
            rows = self._select(statement)
            # A single projected scalar is returned bare so `db.scalar(...)`,
            # `.scalar_one_or_none()` and `.scalars()` all behave like the ORM.
            if len(rows) == 1 and not isinstance(rows[0], FakeRow):
                return FakeResult([rows[0]])
            return FakeResult(rows)
        raise _unsupported(statement)

    # -- INSERT ... ON CONFLICT DO NOTHING ---------------------------------

    def _insert(self, statement: Insert) -> FakeResult:
        post = statement._post_values_clause
        if post is None or type(post).__name__ != "OnConflictDoNothing":
            raise UnsupportedStatementError(
                "the fake audit store only models INSERT ... ON CONFLICT DO NOTHING; "
                "a plain INSERT would require real unique/foreign-key enforcement"
            )
        constraint_name = getattr(post, "constraint_target", None)
        if constraint_name is None or getattr(post, "inferred_target_elements", None):
            raise UnsupportedStatementError(
                "ON CONFLICT DO NOTHING must name a unique constraint so the fake "
                "can resolve the conflict from real model metadata"
            )
        table = _require_table(statement.table)
        values = {
            getattr(column, "key", str(column)): getattr(value, "value", value)
            for column, value in (statement._values or {}).items()
        }
        keys = self._unique_constraint_keys(table, constraint_name)
        if any(
            all(getattr(row, key) == values[key] for key in keys)
            for row in self.rows(table)
        ):
            return FakeResult([], rowcount=0)
        entity = _require_entity(table)
        instance = entity()
        for key, value in values.items():
            setattr(instance, key, value)
        self.apply_defaults(sa_inspect(type(instance)), instance)
        self.rows(table).append(instance)
        returning = list(statement._returning or ())
        if not returning:
            return FakeResult([], rowcount=1)
        return FakeResult([_returning_row(instance, returning)], rowcount=1)

    @staticmethod
    def _unique_constraint_keys(table: TableClause, name: str) -> list[str]:
        constraints = getattr(table, "constraints", None)
        if constraints is not None:
            for constraint in constraints:
                if type(constraint).__name__ != "UniqueConstraint":
                    continue
                if getattr(constraint, "name", None) == name:
                    return [
                        getattr(column, "key", str(column))
                        for column in getattr(constraint, "columns", [])
                    ]
        table_name = getattr(table, "name", str(table))
        raise UnsupportedStatementError(
            f"unique constraint {name!r} does not exist on {table_name!r}"
        )

    # -- UPDATE -----------------------------------------------------------

    def _update(self, statement: Update) -> FakeResult:
        table = _require_table(statement.table)
        entity = _require_entity(table)
        matched: list[Any] = []
        for row in self.rows(table):
            bound = {entity: row}
            if _eval_predicate(statement.whereclause, bound) is not True:
                continue
            if statement._values is not None:
                for column, expression in statement._values.items():
                    col_key = getattr(column, "key", str(column))
                    setattr(row, col_key, _eval_value(expression, bound))
            matched.append(row)
        returning = list(statement._returning or ())
        if not returning:
            return FakeResult([], rowcount=len(matched))
        return FakeResult(
            [_returning_row(row, returning) for row in matched], rowcount=len(matched)
        )

    # -- SELECT -----------------------------------------------------------

    def _select(self, statement: Select) -> list[Any]:
        froms = statement.get_final_froms()
        if len(froms) != 1:
            raise UnsupportedStatementError(
                f"the fake audit store models exactly one FROM element, got {len(froms)}"
            )
        from_clause = froms[0]
        if isinstance(from_clause, Join):
            matched = self._join_rows(from_clause, statement)
        else:
            table = _require_table(from_clause)
            entity = _require_entity(table)
            matched = []
            for row in self.rows(table):
                bound = {entity: row}
                if _eval_predicate(statement.whereclause, bound) is not True:
                    continue
                matched.append(bound)

            if entity.__name__ == "Repo" and "repo_settings" in self._tables:
                settings_by_repo = {
                    s.repo_id: s for s in self._tables.get("repo_settings", [])
                }
                for bound in matched:
                    r = bound.get(entity)
                    if r is not None and getattr(r, "id", None) in settings_by_repo:
                        r.settings = settings_by_repo[r.id]

        self._order(matched, statement)
        limit = statement._limit_clause
        if limit is not None and isinstance(limit.value, int):
            matched = matched[: limit.value]
        return self._project(matched, statement)

    def _join_rows(self, join: Join, statement: Select) -> list[dict[Any, Any]]:
        """Inner-join two mapped tables on a single column equality."""
        left_table = _require_table(join.left)
        right_table = _require_table(join.right)
        matched = []
        for left_row in self.rows(left_table):
            for right_row in self.rows(right_table):
                bound = {
                    _require_entity(left_table): left_row,
                    _require_entity(right_table): right_row,
                }
                if _eval_predicate(join.onclause, bound) is not True:
                    continue
                if _eval_predicate(statement.whereclause, bound) is not True:
                    continue
                matched.append(bound)
        return matched

    @staticmethod
    def _order(matched: list[dict[Any, Any]], statement: Select) -> None:
        for clause in reversed(list(statement._order_by_clauses or ())):
            reverse = False
            if isinstance(clause, UnaryExpression):
                reverse = clause.modifier == "desc"
                clause = clause.element
            matched.sort(
                key=lambda bound, element=clause: _sort_key(
                    _eval_value(element, bound)
                ),
                reverse=reverse,
            )

    @staticmethod
    def _project(matched: list[dict[Any, Any]], statement: Select) -> list[Any]:
        """Build result rows, honouring entity vs column vs aggregate selects."""
        descriptions = statement.column_descriptions
        if not descriptions:
            raise UnsupportedStatementError("SELECT with an empty column list")
        selected = list(statement.selected_columns)
        # `column_descriptions[i]['type']` is the mapped class when the item
        # selects a whole entity, and a SQL type when it selects a column.
        whole_entities = [
            description["type"]
            for description in descriptions
            if isinstance(description["type"], type)
            and issubclass(description["type"], Base)
        ]
        if whole_entities:
            if len(whole_entities) != len(descriptions):
                raise UnsupportedStatementError(
                    "the fake audit store does not model a SELECT that mixes whole "
                    "entities with individual columns"
                )
            return [
                tuple(bound[entity] for entity in whole_entities) for bound in matched
            ]
        if len(selected) == 1 and isinstance(selected[0], Function):
            if selected[0].name == "count":
                return [len(matched)]
            if selected[0].name.lower() == "coalesce":
                clauses = list(selected[0].clauses.clauses)
                inner = clauses[0]
                default_arg = clauses[1]
                default_val = _eval_value(default_arg, {})
                if isinstance(inner, Function) and inner.name.lower() == "sum":
                    col = list(inner.clauses.clauses)[0]
                    vals = [_eval_value(col, bound) for bound in matched]
                    non_null = [v for v in vals if v is not None]
                    return [float(sum(non_null)) if non_null else float(default_val)]
            raise _unsupported(selected[0])
        if len(selected) == 1:
            return [_eval_value(selected[0], bound) for bound in matched]
        return [
            FakeRow(
                [
                    getattr(element, "key", str(index))
                    for index, element in enumerate(selected)
                ],
                [_eval_value(element, bound) for element in selected],
            )
            for bound in matched
        ]

    # -- ORM flush helpers -------------------------------------------------

    @staticmethod
    def apply_defaults(mapper: Mapper, instance: Any) -> None:
        """Apply client-side column defaults, as a real INSERT flush would."""
        state = sa_inspect(instance)
        for prop in mapper.column_attrs:
            if not isinstance(prop, ColumnProperty):
                continue
            column = prop.columns[0]
            if state.attrs[prop.key].loaded_value is not NO_VALUE:
                continue
            if column.default is None:
                continue
            default = column.default
            value = default.arg(None) if default.is_callable else default.arg
            setattr(instance, prop.key, value)


def _require_table(table: Any) -> TableClause:
    if not isinstance(table, TableClause):
        raise UnsupportedStatementError(
            f"the fake audit store requires a mapped table, got {type(table).__name__}"
        )
    return table


def _require_entity(table: TableClause) -> Any:
    entity = _TABLE_ENTITY.get(table.name)
    if entity is None:
        raise UnsupportedStatementError(f"no mapped entity for table {table.name!r}")
    return entity


def _returning_row(row: Any, returning: Sequence[Any]) -> Any:
    bound = {type(row): row}
    if len(returning) == 1:
        return _eval_value(returning[0], bound)
    return FakeRow(
        [column.key for column in returning],
        [_eval_value(column, bound) for column in returning],
    )


# ---------------------------------------------------------------------------
# Session + session-maker
# ---------------------------------------------------------------------------


class FakeAsyncSession:
    """Minimal `AsyncSession` stand-in over a :class:`FakeStore`.

    Statements apply immediately and are visible to every other session on the
    same store, which mirrors what these production helpers rely on: they commit
    before handing work to the next step and never read their own uncommitted
    writes. `rollback()` is therefore a no-op — nothing in the audit path issues
    a statement whose effect it then rolls back.
    """

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        return self._store.execute(statement)

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return self._store.execute(statement).scalar()

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> _Rows:
        return self._store.execute(statement).scalars()

    def add(self, instance: Any) -> None:
        mapper = sa_inspect(type(instance))
        self._store.apply_defaults(mapper, instance)
        rows = self._store.rows(mapper.local_table)
        if instance not in rows:
            rows.append(instance)

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        """No-op: `add` already applied column defaults and stored the row."""
        return None

    async def get(self, entity: Any, ident: Any) -> Any:
        """Primary-key lookup over the store, mirroring `AsyncSession.get`.

        Async because the real method is a coroutine: a sync double would let
        production code call it without `await` and still pass its tests.
        """
        mapper = sa_inspect(entity)
        pk_keys = [prop.key for prop in mapper.primary_key]
        keys = ident if isinstance(ident, tuple) else (ident,)
        if len(keys) != len(pk_keys):
            raise UnsupportedStatementError(
                f"get({entity.__name__}) expects {len(pk_keys)} identity key(s), "
                f"got {len(keys)}"
            )
        for row in self._store.rows(mapper.local_table):
            if all(
                getattr(row, key) == value
                for key, value in zip(pk_keys, keys, strict=True)
            ):
                return row
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def refresh(self, instance: Any, *args: Any, **kwargs: Any) -> None:
        if not self._store.contains(instance):
            raise UnsupportedStatementError(
                f"refresh() called for a {type(instance).__name__} that is not in the store"
            )

    async def close(self) -> None:
        return None

    def expire_all(self) -> None:
        """No-op: rows are live objects, so reads always observe current state."""

    async def __aenter__(self) -> "FakeAsyncSession":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        await self.close()
        return False


class FakeSessionMaker:
    """Callable mimicking `app.db.async_session_maker` over one shared store."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def __call__(self) -> FakeAsyncSession:
        return FakeAsyncSession(self.store)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_store() -> FakeStore:
    """A fresh, empty in-process database for one test."""
    return FakeStore()


@pytest.fixture
def fake_audit_db(audit_store: FakeStore, monkeypatch: pytest.MonkeyPatch):
    """Install the fake audit database for the duration of one test.

    Yields the session the test itself drives, and wires that same store into
    the two places production code reaches for a database, so the durable
    outbox is exercised for real rather than mocked away:

    * ``app.db.async_session_maker`` — imported *inside* every
      ``audit_pipeline`` dispatcher/claim/retry helper, so patching the module
      attribute is enough to route them here;
    * ``app.dependency_overrides[get_db]`` — so the FastAPI webhook endpoint
      reads and writes these same rows.
    """
    from app import db as db_module

    session = FakeAsyncSession(audit_store)
    fake_session_maker = FakeSessionMaker(audit_store)
    monkeypatch.setattr(db_module, "async_session_maker", fake_session_maker)
    try:
        import app.orchestrator as orch_module

        monkeypatch.setattr(orch_module, "async_session_maker", fake_session_maker)
    except (ImportError, AttributeError):
        pass

    from main import app as fastapi_app

    previous = fastapi_app.dependency_overrides.get(db_module.get_db)

    async def _get_db():
        try:
            yield session
        finally:
            await session.close()

    fastapi_app.dependency_overrides[db_module.get_db] = _get_db
    try:
        yield session
    finally:
        if previous is None:
            fastapi_app.dependency_overrides.pop(db_module.get_db, None)
        else:
            fastapi_app.dependency_overrides[db_module.get_db] = previous


@pytest.fixture
def fake_audit_user_factory(fake_audit_db: FakeAsyncSession):
    """Factory that persists a `User` row in the fake audit store."""

    from app.auth import _encrypt_token
    from app.models import User

    async def _create(
        github_id: int | None = None,
        username: str = "test-user",
        access_token: str | None = "fake_access_token_123",
        avatar_url: str | None = "https://avatars.githubusercontent.com/u/123",
        role: str = "user",
    ) -> User:
        if github_id is None:
            github_id = int(uuid.uuid4().int % 1_000_000_000 + 100_000_000)
        user = User(
            github_id=github_id,
            github_username=username,
            access_token=_encrypt_token(access_token) if access_token else None,
            avatar_url=avatar_url,
            role=role,
        )
        fake_audit_db.add(user)
        await fake_audit_db.commit()
        return user

    return _create
