"""
Database schema, model invariants, migrations, and connection pool tests (test_db_models.py).
"""

import hashlib
import io

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError, MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.config import _to_asyncpg_url
from app.db import engine, engine_unpooled
from app.models import (
    Attempt,
    AuditJob,
    CodeReview,
    ModelConfig,
    Repo,
    RepoSettings,
    Run,
    RunStep,
    User,
    WebhookDelivery,
    WebhookRateLimitHit,
)
from app.routers.traces import build_retry_child
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_alembic_head_and_migration_check():
    """Verify Alembic migration script directory has a valid head revision."""
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected exactly 1 migration head, found {heads}"
    assert heads[0] == "a9b8c7d6e5f4"
    assert script.get_revision("a4b7c9d2e6f1").down_revision == "f2a9c4e7b1d3"
    assert script.get_revision("b6d8f0a2c4e6").down_revision == "a4b7c9d2e6f1"
    assert script.get_revision("c7a1b2c3d4e5").down_revision == "b6d8f0a2c4e6"
    assert script.get_revision("d8e9f0a1b2c3").down_revision == "c7a1b2c3d4e5"
    assert script.get_revision("e9d1c2b3a405").down_revision == "d8e9f0a1b2c3"
    assert script.get_revision("f3a4b5c6d7e8").down_revision == "e9d1c2b3a405"
    assert script.get_revision("a1b2c3d4e5f7").down_revision == "f3a4b5c6d7e8"
    assert script.get_revision("f4a5b6c7d8e9").down_revision == "a1b2c3d4e5f7"
    assert script.get_revision("e8f9a0b1c2d3").down_revision == "f4a5b6c7d8e9"
    assert script.get_revision("b7c8d9e0f1a2").down_revision == "e8f9a0b1c2d3"
    assert script.get_revision("a5c6d7e8f9b0").down_revision == "b7c8d9e0f1a2"
    assert script.get_revision("b8c9d0e1f2a3").down_revision == "a5c6d7e8f9b0"
    assert script.get_revision("e1f2a3b4c5d6").down_revision == "b8c9d0e1f2a3"
    assert script.get_revision("d1e2f3a4b5c6").down_revision == "e1f2a3b4c5d6"
    assert script.get_revision("c2d3e4f5a6b7").down_revision == "d1e2f3a4b5c6"
    assert script.get_revision("d5e6f7a8b9c0").down_revision == "c2d3e4f5a6b7"
    assert script.get_revision("a2b3c4d5e6f7").down_revision == "d5e6f7a8b9c0"
    assert script.get_revision("d205c3e4f5a6").down_revision == "a2b3c4d5e6f7"
    assert script.get_revision("a9b8c7d6e5f4").down_revision == "d205c3e4f5a6"


def test_issue_110_composite_indexes_match_models():
    """Issue #110: composite indexes exist on Run / CodeReview / WebhookDelivery
    with equality-first column order, AND the migration that creates them
    declares each index operation with the same (name, table, columns) plus
    CONCURRENTLY so webhook writes are not blocked while the build runs.

    This inspects both the SQLAlchemy models (single source of truth at runtime)
    and the actual migration source (single source of truth at upgrade time) so
    that drift between the two surfaces immediately. A migration with the right
    string content but the wrong op.create_index arguments cannot satisfy it.
    """
    import ast
    from pathlib import Path

    from sqlalchemy import Index as SAIndex

    expected = {
        "ix_runs_repo_status_created": (
            "runs",
            ["repo_id", "status", "created_at"],
        ),
        "ix_code_reviews_repo_commit_pr": (
            "code_reviews",
            ["repo_id", "commit_sha", "pr_number"],
        ),
        "ix_webhook_deliveries_repo_event_created": (
            "webhook_deliveries",
            ["repo_id", "event", "created_at"],
        ),
    }

    # ----- Model side: each expected composite Index on Run / CodeReview /
    # WebhookDelivery must exist, be non-unique, and carry the equality-first
    # columns the migration declares. We iterate only the expected names so
    # the deliberate UNIQUE idempotency guards on runs (trigger_comment_id /
    # github_run_id / github_delivery_id) are not false-positives. -----
    by_name: dict[str, SAIndex] = {}
    for model in (Run, CodeReview, WebhookDelivery):
        for idx in model.__table__.indexes:
            if idx.name in expected:
                by_name[idx.name] = idx

    actual: dict[str, tuple[str, list[str]]] = {}
    for name, (exp_table, exp_cols) in expected.items():
        idx = by_name.get(name)
        assert idx is not None, (
            f"model is missing Index {name!r}; the issue-110 composite must "
            f"exist on {exp_table}"
        )
        assert isinstance(idx, SAIndex), (
            f"{name!r} must be an Index, got {type(idx).__name__}"
        )
        assert not idx.unique, (
            f"{name!r} must be non-unique; the deliberate UNIQUE idempotency "
            "guards on runs are github_run_id / trigger_comment_id / "
            "github_delivery_id and live on separate indexes."
        )
        cols = list(idx.columns.keys())
        assert cols == exp_cols, (
            f"{name!r} columns drift: model has {cols}, "
            f"migration declares {exp_cols}"
        )
        assert idx.table.name == exp_table, (
            f"{name!r} lives on {idx.table.name}, expected {exp_table}"
        )
        actual[name] = (idx.table.name, cols)

    assert actual == expected, (
        f"model/migration drift: expected {expected}, got {actual}"
    )

    # ----- Migration side: AST-walk the migration file and assert every
    # op.create_index / op.drop_index operation matches the expected set.
    # Because the migration iterates a module-level _INDEXES tuple through a
    # for-loop, the call-site Name refs cannot be resolved statically — but
    # the *source data* in _INDEXES is a literal tuple that ast.literal_eval
    # can pull apart. We therefore assert two things separately: (a) the
    # source data matches the models, and (b) the call sites reference that
    # data correctly with CONCURRENT + IF EXISTS kwargs. -----
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "d1e2f3a4b5c6_add_issue_110_composite_indexes.py"
    )
    src = migration.read_text(encoding="utf-8")
    assert "autocommit_block" in src, (
        "CONCURRENTLY builds must run outside the env.py transaction; "
        "missing op.get_context().autocommit_block()"
    )

    tree = ast.parse(src)

    def _str(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _const(node: ast.AST) -> object | None:
        if isinstance(node, ast.Constant):
            return node.value
        return None

    def _kw(call: ast.Call, name: str) -> ast.AST | None:
        for kw in call.keywords:
            if kw.arg == name:
                return kw.value
        return None

    # Extract the module-level _INDEXES tuple literal — that is the source of
    # truth for what the migration actually creates at upgrade time.
    index_table: dict[str, tuple[str, tuple[str, ...]]] = {}
    found_indexes_const = False
    for stmt in tree.body:
        value_node: ast.AST | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            tgt = stmt.targets[0]
            if isinstance(tgt, ast.Name):
                value_node = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            value_node = stmt.value
        if value_node is None:
            continue
        # Find the name we assigned to (handles both plain Assign and AnnAssign).
        if isinstance(stmt, ast.Assign):
            name_id = stmt.targets[0].id  # type: ignore[union-attr]
        else:
            name_id = stmt.target.id  # type: ignore[union-attr]
        if name_id != "_INDEXES":
            continue
        try:
            data = ast.literal_eval(value_node)
        except (ValueError, SyntaxError) as e:
            raise AssertionError(
                f"_INDEXES must be a literal tuple-of-tuples so the migration "
                f"reads as data, not magic: {e}"
            )
        assert isinstance(data, (list, tuple)), (
            f"_INDEXES must be a tuple/list of (name, table, columns) triples; "
            f"got {type(data).__name__}"
        )
        found_indexes_const = True
        for triple in data:
            assert (
                isinstance(triple, (list, tuple)) and len(triple) == 3
            ), f"_INDEXES entry must be a 3-tuple; got {triple!r}"
            name, table, cols = triple
            assert isinstance(name, str) and isinstance(table, str) and isinstance(
                cols, (list, tuple)
            ), f"_INDEXES entry must be (str, str, list[str]); got {triple!r}"
            index_table[name] = (table, tuple(cols))

    assert found_indexes_const, (
        "migration must declare a module-level _INDEXES constant with the "
        "(name, table, columns) triples it builds"
    )

    # Each expected index triple must be present in _INDEXES exactly as declared.
    expected_index_table: dict[str, tuple[str, tuple[str, ...]]] = {
        name: (table, tuple(cols)) for name, (table, cols) in expected.items()
    }
    assert index_table == expected_index_table, (
        f"_INDEXES drift: expected {expected_index_table}, got {index_table}"
    )

    # Collect every op.create_index / op.drop_index call scoped to the upgrade()
    # and downgrade() function definitions only. The recovery path inside
    # upgrade() also drops invalid indexes via op.drop_index, so we must
    # scope by enclosing function to avoid double-counting.
    def _calls_in(func_name: str, attr: str) -> list[ast.Call]:
        for stmt in tree.body:
            if not isinstance(stmt, ast.FunctionDef) or stmt.name != func_name:
                continue
            out: list[ast.Call] = []
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (
                    isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "op"
                    and f.attr == attr
                ):
                    out.append(node)
            return out
        return []

    upgrade_creates = _calls_in("upgrade", "create_index")
    upgrade_drops = _calls_in("upgrade", "drop_index")
    downgrade_drops = _calls_in("downgrade", "drop_index")

    # All op.create_index calls in the module are inside upgrade(); assert
    # we found at least one and that there are no stray op.create_index calls
    # outside upgrade/downgrade.
    all_create_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "op"
            and func.attr == "create_index"
        ):
            all_create_calls.append(node)
    assert all_create_calls == upgrade_creates, (
        "op.create_index calls outside upgrade(); migration should only "
        "create indexes from upgrade()."
    )
    create_calls = upgrade_creates
    drop_calls = downgrade_drops
    upgrade_drop_calls = upgrade_drops

    assert create_calls, "migration must call op.create_index at least once"
    assert drop_calls, "migration must call op.drop_index at least once"

    # Every op.create_index call must be wired to the _INDEXES tuple via Name
    # references AND carry concurrent+non-unique kwargs. This closes the gap
    # the original test had, where a docstring + import block would pass the
    # assertion while the actual call wired the wrong table or columns.
    for call in create_calls:
        # Positional args must be Name nodes (i.e. for-loop variables). Any
        # literal Name that isn't a known module constant is a for-loop local.
        assert len(call.args) >= 3, (
            f"op.create_index must pass (name, table, columns); got {len(call.args)} args"
        )
        for idx, arg in enumerate(call.args[:3]):
            assert isinstance(arg, ast.Name), (
                f"op.create_index positional[{idx}] must be a Name reference to "
                f"a for-loop variable bound from _INDEXES; got {ast.dump(arg)}"
            )
        unique_kw = _kw(call, "unique")
        assert unique_kw is not None and _const(unique_kw) is False, (
            f"op.create_index must set unique=False; got {ast.dump(unique_kw)}"
        )
        concurrent_kw = _kw(call, "postgresql_concurrently")
        assert (
            concurrent_kw is not None and _const(concurrent_kw) is True
        ), (
            f"op.create_index must set postgresql_concurrently=True; "
            f"got {ast.dump(concurrent_kw)}"
        )

    # Every op.drop_index call must declare CONCURRENTLY + IF EXISTS so a
    # partial retry does not error out, and table_name= must reference the
    # table the migration drops from. The migration iterates _INDEXES in a
    # for-loop, so the AST sees one op.drop_index call inside downgrade() that
    # is invoked once per triple in _INDEXES (already asserted above) and
    # again in reverse for downgrade.
    assert drop_calls, (
        "downgrade must declare at least one op.drop_index call iterating _INDEXES"
    )
    for i, call in enumerate(drop_calls):
        assert call.args, (
            f"op.drop_index call #{i} requires a positional[0] (name); got {ast.dump(call)}"
        )
        name_arg = call.args[0]
        assert isinstance(name_arg, (ast.Constant, ast.Name)), (
            f"op.drop_index call #{i} positional[0] must be a literal or Name; "
            f"got {ast.dump(name_arg)}"
        )
        table_kw = _kw(call, "table_name")
        assert table_kw is not None, (
            f"op.drop_index call #{i} must declare table_name="
        )
        assert isinstance(table_kw, (ast.Constant, ast.Name)), (
            f"op.drop_index call #{i} table_name must be a literal string or Name; "
            f"got {ast.dump(table_kw)}"
        )
        if_exists = _kw(call, "if_exists")
        assert (
            if_exists is not None and _const(if_exists) is True
        ), (
            f"op.drop_index call #{i} must set if_exists=True so a partial "
            "retry does not error out"
        )
        concurrent_kw = _kw(call, "postgresql_concurrently")
        assert (
            concurrent_kw is not None and _const(concurrent_kw) is True
        ), (
            f"op.drop_index call #{i} must set postgresql_concurrently=True"
        )

    # Recovery drop(s) inside upgrade() (for INVALID leftover indexes) must
    # also be CONCURRENT + Name-ref the index — non-blocking for live writes.
    for i, call in enumerate(upgrade_drop_calls):
        assert call.args, (
            f"upgrade-recovery op.drop_index call #{i} requires positional[0]"
        )
        assert isinstance(call.args[0], ast.Name), (
            f"upgrade-recovery op.drop_index call #{i} positional[0] must be a "
            f"Name bound from _INDEXES; got {ast.dump(call.args[0])}"
        )
        concurrent_kw = _kw(call, "postgresql_concurrently")
        assert (
            concurrent_kw is not None and _const(concurrent_kw) is True
        ), (
            f"upgrade-recovery op.drop_index call #{i} must set "
            "postgresql_concurrently=True"
        )


def test_shared_webhook_rate_limit_migration_ddl_and_execution():
    """Verify revision a2b3c4d5e6f7 upgrade() and downgrade() DDL and execution.

    Finding 8 (PR 176): Revision DDL for
    `a2b3c4d5e6f7_add_shared_webhook_rate_limit_store.py` must be compiled
    and executed against SQLAlchemy engines/dialects to verify:
    1. PostgreSQL dialect compilation produces valid DDL for table creation,
       composite index (bucket_key, hit_at), standalone index (hit_at), and
       clean downgrade drop_index / drop_table statements.
    2. Alembic Operations execution against an active engine creates table
       `webhook_rate_limit_hits` with expected columns, primary key, and indexes.
    3. Downgrade execution cleanly drops both indexes and the table with zero dialect errors.
    4. Table schema aligns with the SQLAlchemy WebhookRateLimitHit model.
    """
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("a2b3c4d5e6f7")
    assert rev is not None, "Revision a2b3c4d5e6f7 not found in alembic script directory"
    migration = rev.module

    assert migration.revision == "a2b3c4d5e6f7"
    assert migration.TABLE == "webhook_rate_limit_hits"
    assert migration.TABLE == WebhookRateLimitHit.__tablename__

    # ----- 1. PostgreSQL dialect offline DDL compilation -----
    buf_upgrade = io.StringIO()
    ctx_pg_up = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf_upgrade},
    )
    with Operations.context(ctx_pg_up):
        migration.upgrade()

    pg_upgrade_sql = buf_upgrade.getvalue()
    assert "CREATE TABLE webhook_rate_limit_hits" in pg_upgrade_sql
    assert "bucket_key VARCHAR(200) NOT NULL" in pg_upgrade_sql
    assert "hit_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in pg_upgrade_sql
    assert "PRIMARY KEY (id)" in pg_upgrade_sql
    assert (
        "CREATE INDEX ix_webhook_rate_limit_hits_key_hit_at ON webhook_rate_limit_hits (bucket_key, hit_at)"
        in pg_upgrade_sql
    )
    assert (
        "CREATE INDEX ix_webhook_rate_limit_hits_hit_at ON webhook_rate_limit_hits (hit_at)"
        in pg_upgrade_sql
    )

    buf_downgrade = io.StringIO()
    ctx_pg_down = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf_downgrade},
    )
    with Operations.context(ctx_pg_down):
        migration.downgrade()

    pg_downgrade_sql = buf_downgrade.getvalue()
    assert "DROP INDEX ix_webhook_rate_limit_hits_hit_at" in pg_downgrade_sql
    assert "DROP INDEX ix_webhook_rate_limit_hits_key_hit_at" in pg_downgrade_sql
    assert "DROP TABLE webhook_rate_limit_hits" in pg_downgrade_sql

    # ----- 2. Live execution against engine & schema validation -----
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx_sqlite = MigrationContext.configure(conn)
        with Operations.context(ctx_sqlite):
            # Baseline: table does not exist yet
            insp = inspect(conn)
            assert "webhook_rate_limit_hits" not in insp.get_table_names()

            # Execute upgrade()
            migration.upgrade()

            # Verify table exists
            insp = inspect(conn)
            assert "webhook_rate_limit_hits" in insp.get_table_names()

            # Verify columns (names, primary key, nullability)
            cols = {c["name"]: c for c in insp.get_columns("webhook_rate_limit_hits")}
            assert set(cols.keys()) == {"id", "bucket_key", "hit_at"}
            assert cols["id"]["primary_key"] == 1
            assert cols["id"]["nullable"] is False
            assert cols["bucket_key"]["nullable"] is False
            assert cols["hit_at"]["nullable"] is False

            # Verify indexes (names, column lists, uniqueness)
            indexes = {
                idx["name"]: idx
                for idx in insp.get_indexes("webhook_rate_limit_hits")
            }
            assert "ix_webhook_rate_limit_hits_key_hit_at" in indexes
            assert indexes["ix_webhook_rate_limit_hits_key_hit_at"]["column_names"] == [
                "bucket_key",
                "hit_at",
            ]
            assert not indexes["ix_webhook_rate_limit_hits_key_hit_at"]["unique"]

            assert "ix_webhook_rate_limit_hits_hit_at" in indexes
            assert indexes["ix_webhook_rate_limit_hits_hit_at"]["column_names"] == ["hit_at"]
            assert not indexes["ix_webhook_rate_limit_hits_hit_at"]["unique"]

            # Verify schema alignment against SQLAlchemy model
            model_cols = [c.name for c in WebhookRateLimitHit.__table__.columns]
            assert set(cols.keys()) == set(model_cols)

            model_indexes = {
                idx.name: [c.name for c in idx.columns]
                for idx in WebhookRateLimitHit.__table__.indexes
            }
            for idx_name, idx_cols in model_indexes.items():
                assert idx_name in indexes
                assert indexes[idx_name]["column_names"] == idx_cols

            # Execute downgrade()
            migration.downgrade()

            # Verify table and indexes are cleanly dropped
            insp_after = inspect(conn)
            assert "webhook_rate_limit_hits" not in insp_after.get_table_names()


def test_to_asyncpg_url_variants():
    """Test URL parsing and driver/parameter transformation for asyncpg."""
    # Case 1: Standard postgresql URL with sslmode stripped
    url1 = "postgresql://user:pass@ep-cool.neon.tech/neondb?sslmode=require"
    converted1 = _to_asyncpg_url(url1)
    assert converted1.startswith("postgresql+asyncpg://")
    assert "sslmode" not in converted1

    # Case 2: Neon pooled URL with sslmode and channel_binding stripped
    url2 = "postgresql://user:pass@ep-cool-pooler.neon.tech/neondb?sslmode=require&channel_binding=require&client_encoding=utf8"
    converted2 = _to_asyncpg_url(url2)
    assert converted2.startswith("postgresql+asyncpg://")
    assert "sslmode" not in converted2
    assert "channel_binding" not in converted2
    assert "client_encoding=utf8" in converted2

    # Case 3: Already asyncpg url
    url3 = "postgresql+asyncpg://localhost:5432/haunter_db"
    converted3 = _to_asyncpg_url(url3)
    assert converted3 == url3


def test_nullpool_on_both_engines():
    """Assert both pooled and unpooled engines use NullPool for Neon compatibility."""
    assert isinstance(engine.pool, NullPool), "Runtime engine must use NullPool"
    assert isinstance(
        engine_unpooled.pool, NullPool
    ), "Unpooled migration engine must use NullPool"


@pytest.mark.asyncio
async def test_users_github_id_unique_constraint(db: AsyncSession):
    """Assert duplicate users.github_id raises IntegrityError."""
    await truncate_all(db)
    gh_id = 9988776655

    u1 = User(github_id=gh_id, github_username="user_alpha", access_token="tok_1")
    db.add(u1)
    await db.commit()

    u2 = User(github_id=gh_id, github_username="user_beta", access_token="tok_2")
    db.add(u2)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_repos_user_owner_name_unique_per_user_cross_user_allowed(
    db: AsyncSession,
):
    """
    Assert (user_id, owner, name) uniqueness:
    - Same user duplicate repo fails with IntegrityError.
    - Two different users can track the same owner/name independently.
    """
    await truncate_all(db)

    u1 = User(github_id=901, github_username="tenant_a", access_token="t1")
    u2 = User(github_id=902, github_username="tenant_b", access_token="t2")
    db.add_all([u1, u2])
    await db.commit()
    await db.refresh(u1)
    await db.refresh(u2)
    u1_id = u1.id
    u2_id = u2.id

    # 1. User 1 adds repo
    r1 = Repo(user_id=u1_id, owner="octocat", name="spoon-knife")
    db.add(r1)
    await db.commit()
    await db.refresh(r1)
    r1_id = r1.id

    # 2. User 1 adds same repo -> IntegrityError
    r1_dup = Repo(user_id=u1_id, owner="octocat", name="spoon-knife")
    db.add(r1_dup)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # 3. User 2 adds identical owner/name -> Success
    r2 = Repo(user_id=u2_id, owner="octocat", name="spoon-knife")
    db.add(r2)
    await db.commit()
    await db.refresh(r2)
    assert r2.id != r1_id


@pytest.mark.asyncio
async def test_fk_cascade_delete_user_repos_runs(db: AsyncSession):
    """Assert deleting a User cascades to delete Repos, Runs, RunSteps, and Attempts."""
    await truncate_all(db)

    user = User(github_id=903, github_username="cascade_user", access_token="t3")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    user_id = user.id

    repo = Repo(user_id=user_id, owner="org", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    repo_id = repo.id

    run = Run(
        repo_id=repo_id,
        github_run_id=554433,
        github_delivery_id="deliv-cascade-1",
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="main",
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run_id = run.id

    step = RunStep(
        run_id=run_id, step_name="context_gatherer", input_tokens=10, output_tokens=20
    )
    attempt = Attempt(run_id=run_id, attempt_number=1, patch_text="diff --git ...")
    db.add_all([step, attempt])
    await db.commit()

    # Delete the root user via SQL-level delete to test DB ON DELETE CASCADE
    from sqlalchemy import delete as sa_delete

    await db.execute(sa_delete(User).where(User.id == user_id))
    await db.commit()

    # Verify all children were deleted
    assert (
        await db.execute(select(Repo).where(Repo.id == repo_id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(Run).where(Run.id == run_id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    ).scalars().all() == []
    assert (
        await db.execute(select(Attempt).where(Attempt.run_id == run_id))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_runs_github_delivery_id_unique(db: AsyncSession):
    """Assert github_delivery_id uniqueness constraint and nullable behavior."""
    await truncate_all(db)

    user = User(github_id=904, github_username="run_user", access_token="t4")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    user_id = user.id

    repo = Repo(user_id=user_id, owner="org", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    repo_id = repo.id

    # 1. Unique delivery ID
    run1 = Run(
        repo_id=repo_id,
        github_run_id=1001,
        github_delivery_id="deliv-unique-999",
        head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        head_branch="main",
        status="pending",
    )
    db.add(run1)
    await db.commit()

    run2 = Run(
        repo_id=repo_id,
        github_run_id=1002,
        github_delivery_id="deliv-unique-999",  # duplicate delivery id
        head_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        head_branch="main",
        status="pending",
    )
    db.add(run2)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # 2. Null delivery ID allowed multiple times
    run_null_1 = Run(
        repo_id=repo_id,
        github_run_id=1003,
        github_delivery_id=None,
        head_sha="cccccccccccccccccccccccccccccccccccccccc",
        head_branch="main",
        status="pending",
    )
    run_null_2 = Run(
        repo_id=repo_id,
        github_run_id=1004,
        github_delivery_id=None,
        head_sha="dddddddddddddddddddddddddddddddddddddddd",
        head_branch="main",
        status="pending",
    )
    db.add_all([run_null_1, run_null_2])
    await db.commit()


@pytest.mark.asyncio
async def test_runs_fallback_issue_columns_nullable_then_persisted(
    db: AsyncSession,
):
    """Feature 3: runs.fallback_issue_url/number start NULL and are writable."""
    await truncate_all(db)

    user = User(github_id=908, github_username="fallback_run_user", access_token="t8")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    repo = Repo(user_id=user.id, owner="fallback-org", name="fallback-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = Run(
        repo_id=repo.id,
        github_run_id=2001,
        head_sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        head_branch="main",
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # A run that never reaches the exhaust path has no tracking issue.
    assert run.fallback_issue_url is None
    assert run.fallback_issue_number is None

    run.fallback_issue_url = "https://github.com/fallback-org/fallback-repo/issues/12"
    run.fallback_issue_number = 12
    db.add(run)
    await db.commit()
    await db.refresh(run)

    assert run.fallback_issue_number == 12
    assert run.fallback_issue_url == (
        "https://github.com/fallback-org/fallback-repo/issues/12"
    )


@pytest.mark.asyncio
async def test_runs_is_retry_child_defaults_false_then_true(
    db: AsyncSession,
):
    """Feature 1: runs.is_retry_child defaults to False and is settable.

    The default matters for pre-existing rows: every run created before the
    one-click retry feature is an interactive PR-feedback refinement child (or
    a root run), never a retry child, so backfill-by-default is correct.
    """
    await truncate_all(db)

    user = User(github_id=909, github_username="retry_child_user", access_token="t9")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    repo = Repo(user_id=user.id, owner="retry-org", name="retry-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = Run(
        repo_id=repo.id,
        github_run_id=2002,
        head_sha="ffffffffffffffffffffffffffffffffffffffff",
        head_branch="main",
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    assert run.is_retry_child is False

    run.is_retry_child = True
    db.add(run)
    await db.commit()
    await db.refresh(run)

    assert run.is_retry_child is True


@pytest.mark.asyncio
async def test_runs_github_run_id_nullable_for_retry_child(
    db: AsyncSession,
):
    """Feature 1: a retry child may store NULL github_run_id.

    A retry child maps to no GitHub Actions workflow run and no webhook
    delivery. The column is therefore nullable, and NULL is also what keeps a
    genuine later workflow_run webhook from being dropped as a duplicate.
    """
    await truncate_all(db)

    user = User(github_id=910, github_username="retry_null_id_user", access_token="t10")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    repo = Repo(user_id=user.id, owner="retry-org", name="retry-null-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    root = Run(
        repo_id=repo.id,
        github_run_id=2003,
        head_sha="a" * 40,
        head_branch="main",
        status="error",
        conclusion="failure",
    )
    db.add(root)
    await db.commit()
    await db.refresh(root)

    child = build_retry_child(root)
    db.add(child)
    await db.commit()
    await db.refresh(child)

    assert child.id != root.id
    assert child.github_run_id is None
    assert child.github_delivery_id is None
    assert child.parent_run_id == root.id
    assert child.is_retry_child is True
    # Two rows may share NULL github_run_id under the UNIQUE index.
    second = build_retry_child(root)
    db.add(second)
    await db.commit()
    await db.refresh(second)
    assert second.github_run_id is None


@pytest.mark.asyncio
async def test_timestamps_timezone_aware(db: AsyncSession):
    """Assert created_at and updated_at timestamps on models are timezone-aware."""
    await truncate_all(db)

    user = User(github_id=905, github_username="tz_user", access_token="t5")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    assert user.created_at is not None
    assert user.created_at.tzinfo is not None
    assert user.updated_at is not None
    assert user.updated_at.tzinfo is not None


@pytest.mark.asyncio
async def test_model_configs_defaults(db: AsyncSession):
    """Assert default values on ModelConfig model."""
    await truncate_all(db)

    cfg = ModelConfig(provider="opencode_go")
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    assert cfg.model_name == "nemotron-3.5-lightning-free"
    assert cfg.base_url == "https://opencode.ai/inference/go/openai/v1"
    assert cfg.is_active is True
    assert cfg.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_repo_settings_safe_defaults_and_unique_repo(
    db: AsyncSession,
):
    await truncate_all(db)
    user = User(github_id=906, github_username="auditor_settings", access_token="t6")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    repo = Repo(
        user_id=user.id,
        owner="settings-org",
        name="settings-repo",
        auditor_github_install_id=987654,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    settings_row = RepoSettings(repo_id=repo.id)
    db.add(settings_row)
    await db.commit()
    await db.refresh(settings_row)

    assert settings_row.enable_auditor_mode is False
    assert settings_row.audit_trigger_on_pr is True
    assert settings_row.audit_trigger_on_ci_failure is True
    assert settings_row.audit_trigger_on_ci_success is False
    assert settings_row.audit_trigger_on_manual_mention is True
    # Feature 3 — fallback issue filing is opt-out, so a bare RepoSettings()
    # must already have it enabled.
    assert settings_row.file_issue_on_fallback is True
    assert repo.auditor_github_install_id == 987654

    db.add(RepoSettings(repo_id=repo.id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_audit_job_has_recoverable_dispatch_defaults(
    db: AsyncSession,
):
    await truncate_all(db)
    user = User(github_id=907, github_username="audit_outbox", access_token="t7")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    repo = Repo(user_id=user.id, owner="outbox-org", name="outbox-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    job = AuditJob(
        audit_id="audit-dddddddddddd",
        repo_id=repo.id,
        delivery_id="delivery-outbox-defaults",
        # `delivery_fingerprint` is NOT NULL by schema: a delivery id alone is
        # not an idempotency key, so the payload digest is part of the row's
        # identity and has no default to fall back on.
        delivery_fingerprint=hashlib.sha256(b"outbox-defaults-payload").hexdigest(),
        audit_type="pr_audit",
        base_sha="b" * 40,
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    assert job.attempts == 0
    assert job.dispatch_attempts == 0
    assert job.base_sha == "b" * 40
    assert len(job.delivery_fingerprint) == 64
    assert job.next_attempt_at is not None
    assert job.next_attempt_at.tzinfo is not None
    assert job.lease_expires_at is None
    assert job.last_error is None

    job.status = "skipped_no_diff"
    await db.commit()
    await db.refresh(job)
    assert job.status == "skipped_no_diff"

@pytest.mark.asyncio
async def test_session_rollback_and_expire_all_expire_model_attributes(
    db: AsyncSession,
):
    """Verify that rollback and expire_all expire persistent model attributes (including PK).

    Accessing expired attributes on an unrefreshed instance outside an async load context
    raises MissingGreenlet, proving the test session fixture accurately mirrors production
    AsyncSession expiration semantics.
    """
    await truncate_all(db)
    user = User(github_id=9099, github_username="rollback_test_user", access_token="t99")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Sanity check: accessing attributes while unexpired works cleanly
    assert user.id is not None
    assert user.github_username == "rollback_test_user"

    # 1. After db.expire_all(), attributes are expired; synchronous access raises MissingGreenlet
    db.expire_all()
    with pytest.raises(MissingGreenlet):
        _ = user.id

    # Refresh restores attributes
    await db.refresh(user)
    assert user.id is not None

    # 2. After await db.rollback(), attributes are also expired; synchronous access raises MissingGreenlet
    await db.rollback()
    with pytest.raises(MissingGreenlet):
        _ = user.id

