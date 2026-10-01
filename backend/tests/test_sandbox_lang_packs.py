"""
Feature 7: Sandbox language packs + monorepo scoping.

Hermetic and DB-free. Covers four things that can silently rot:

  - ``detect_language()`` — the new packs, the documented precedence order,
    and the legacy python/typescript outcomes.
  - **Template parity** — every file in ``workflow_templates/`` parses as YAML,
    declares the keys the sandbox runner and GitHub Actions require, and every
    language ``detect_language()`` can return has a template that actually
    exists on disk. A renamed or half-written template fails here instead of
    failing a live verification run.
  - Monorepo rendering — ``RepoSettings.working_dir`` / ``test_command`` reach
    the workflow YAML as valid YAML, and malformed values are rejected rather
    than silently dropped.
  - ``_file_priority_tier()`` — new-language and monorepo-subdirectory tiers
    without moving any existing python/typescript file.
"""

from __future__ import annotations

import io
import re
import tarfile

import pytest

from app.config import settings
from app.sandbox._seed_tarball import _file_priority_tier, parse_tar_to_files
from app.sandbox.github_actions_runner import (
    _load_workflow_template,
    render_workflow_content,
    resolve_sandbox_overrides,
    select_workflow_filename,
)
from app.sandbox.mirror import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    detect_language,
)
from app.services.repo_settings import validate_test_command, validate_working_dir

TEMPLATE_DIR_MARKER = "workflow_templates"


def _template_names() -> list[str]:
    """Names of every workflow template shipped in the repository."""
    from app.sandbox import github_actions_runner

    import os

    directory = os.path.join(
        os.path.dirname(os.path.abspath(github_actions_runner.__file__)),
        "workflow_templates",
    )
    return sorted(name for name in os.listdir(directory) if name.endswith(".yml"))


# ---------------------------------------------------------------------------
# detect_language: the packs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["cmd/server/main.go", "go.mod", "go.sum"], "go"),
        (["internal/store/store_test.go"], "go"),
        (["src/lib.rs", "Cargo.toml", "Cargo.lock"], "rust"),
        (["src/main.rs"], "rust"),
        (["src/main/java/com/acme/App.java", "pom.xml"], "java"),
        (["app/build.gradle.kts", "src/main/kotlin/App.kt"], "java"),
        (["Dockerfile"], "docker"),
        (["docker-compose.yml"], "docker"),
        (["deploy/docker-compose.production.yaml"], "docker"),
        (["Dockerfile.prod"], "docker"),
    ],
)
def test_detect_language_each_pack(paths: list[str], expected: str) -> None:
    assert detect_language(paths) == expected


def test_detect_language_prefers_manifest_over_incidental_source() -> None:
    """A manifest outvotes a stray source file from another language.

    Tooling repos are full of one-off scripts: a repo declaring ``go.mod`` with
    three ``.py`` helper scripts must still verify with the Go pack, or the
    sandbox would run pytest against a repository that has no tests to run.
    """
    assert detect_language(["go.mod", "tools/a.py", "tools/b.py", "tools/c.py"]) == "go"
    assert detect_language(["pyproject.toml", "vendor/x.ts", "vendor/y.ts"]) == "py"
    assert detect_language(["Cargo.toml", "scripts/x.go", "scripts/y.go"]) == "rust"


def test_detect_language_majority_source_wins_when_no_manifest() -> None:
    """With no manifest, the language with more files wins."""
    assert detect_language(["a.py", "b.go", "c.go"]) == "go"
    assert detect_language(["a.ts", "b.ts", "c.py"]) == "ts"
    assert detect_language(["a.ts", "b.tsx"]) == "ts"


def test_detect_language_ties_break_by_documented_precedence() -> None:
    """Equal scores resolve to SUPPORTED_LANGUAGES order, py first."""
    assert SUPPORTED_LANGUAGES[0] == "py"
    assert detect_language(["a.py", "b.ts"]) == "py"
    assert detect_language(["pyproject.toml", "package.json"]) == "py"
    assert detect_language(["go.mod", "Cargo.toml"]) == "go"
    assert detect_language(["Cargo.toml", "pom.xml"]) == "rust"
    assert detect_language(["pom.xml", "Dockerfile.prod"]) == "java"


def test_detect_language_docker_is_a_fallback_only() -> None:
    """Docker never outranks a code language, however many Dockerfiles there are."""
    assert detect_language(["main.go", "Dockerfile", "docker-compose.yml"]) == "go"
    assert detect_language(["app.py", "Dockerfile"]) == "py"
    # Only container evidence -> the container pack.
    assert detect_language(["Dockerfile", "docker/Dockerfile"]) == "docker"


def test_detect_language_unknown_falls_back_to_default() -> None:
    """No recognisable evidence, and the empty case, both fall back to py."""
    assert DEFAULT_LANGUAGE == "py"
    assert detect_language([]) == "py"
    assert detect_language(["README.md", "docs/notes.txt", "LICENSE"]) == "py"
    assert detect_language(None) == "py"
    assert detect_language(["unknown-extension.xyz"]) == "py"


def test_detect_language_is_case_and_separator_insensitive() -> None:
    assert detect_language(["SRC/Main.GO", "Go.Mod"]) == "go"
    assert detect_language(["src\\lib.rs", "Cargo.toml"]) == "rust"
    assert detect_language(["POM.XML"]) == "java"


def test_detect_language_always_returns_a_registered_language() -> None:
    """Whatever the input, the result is a key the template mapping knows."""
    samples = [
        ["a.py"],
        ["a.ts"],
        ["a.go"],
        ["a.rs"],
        ["a.java"],
        ["Dockerfile"],
        ["a.txt"],
        [],
        ["package.json", "Cargo.toml", "pom.xml", "go.mod", "a.py"],
    ]
    for sample in samples:
        assert detect_language(sample) in SUPPORTED_LANGUAGES


# ---------------------------------------------------------------------------
# Template parity
# ---------------------------------------------------------------------------


def test_every_template_is_wellformed_yaml_with_required_keys() -> None:
    """Each shipped template parses as YAML and declares what Actions needs.

    PyYAML is not a runtime dependency of the backend, so the structural check
    is done on the exact key layout the runner relies on rather than through a
    full parse. ``test_templates_parse_with_a_yaml_parser`` below runs the real
    parse when PyYAML happens to be installed.
    """
    required_top_level = ("name:", "on:", "permissions:", "env:", "jobs:")
    for name in _template_names():
        content = _load_workflow_template(name)
        for key in required_top_level:
            assert re.search(rf"^{re.escape(key)}", content, re.MULTILINE), (
                f"{name} is missing top-level {key!r}"
            )
        # Least privilege: the sandbox reads code, it never writes or deploys.
        assert "contents: read" in content, f"{name} must not widen permissions"
        assert "${{ secrets." not in content, f"{name} must not consume secrets"
        # Trigger branches the runner actually pushes to.
        assert "sandbox/**" in content, f"{name} missing sandbox trigger"
        assert "haunter-flake-check-**" in content, f"{name} missing flake trigger"
        # Monorepo markers — render_workflow_content replaces these, and a
        # template missing one would silently ignore the user's scope.
        assert 'HAUNTER_WORKING_DIR: "."' in content, f"{name} missing dir marker"
        assert 'HAUNTER_TEST_COMMAND: ""' in content, f"{name} missing cmd marker"
        assert 'cd "${HAUNTER_WORKING_DIR:-.}"' in content, (
            f"{name} never applies HAUNTER_WORKING_DIR"
        )
        # A job with a runner, a timeout and steps that each do something.
        assert "runs-on: ubuntu-latest" in content, f"{name} missing runner"
        assert "timeout-minutes:" in content, f"{name} missing timeout"
        assert "actions/checkout@v4" in content, f"{name} must check out the code"
        assert "- name: Run tests" in content, f"{name} has no test step"


def test_templates_parse_with_a_yaml_parser() -> None:
    """Real YAML parse, skipped when PyYAML is not installed."""
    yaml = pytest.importorskip("yaml")
    for name in _template_names():
        parsed = yaml.safe_load(_load_workflow_template(name))
        assert isinstance(parsed, dict), f"{name} did not parse into a mapping"
        # `on` is parsed by YAML 1.1 as the boolean True, hence both spellings.
        triggers = parsed.get("on", parsed.get(True))
        assert triggers is not None, f"{name} has no trigger block"
        assert "permissions" in parsed, f"{name} has no permissions block"
        jobs = parsed["jobs"]
        assert jobs, f"{name} declares no jobs"
        for job_name, job in jobs.items():
            assert "runs-on" in job, f"{name}:{job_name} missing runs-on"
            assert job.get("timeout-minutes"), f"{name}:{job_name} missing timeout"
            steps = job.get("steps") or []
            assert steps, f"{name}:{job_name} has no steps"
            for step in steps:
                assert "uses" in step or "run" in step, (
                    f"{name}:{job_name} step does nothing: {step}"
                )


def test_every_detected_language_has_a_shipped_template() -> None:
    """Catches template-name drift in both directions.

    A language detect_language() can return but has no template for would fail
    every verification run for that language at runtime; a template nobody can
    reach is dead weight in the package.
    """
    shipped = set(_template_names())
    registered = {
        select_workflow_filename(language, settings)
        for language in SUPPORTED_LANGUAGES
    }
    missing = registered - shipped
    assert not missing, f"languages registered without a template file: {missing}"
    assert set(SUPPORTED_LANGUAGES) == {"py", "ts", "go", "rust", "java", "docker"}


def test_no_orphan_template() -> None:
    """Every template on disk is reachable from a supported language."""
    registered = {
        select_workflow_filename(language, settings)
        for language in SUPPORTED_LANGUAGES
    }
    orphans = set(_template_names()) - registered
    assert not orphans, f"templates no language can select: {orphans}"


def test_select_workflow_filename_uses_settings_override() -> None:
    """Config can rename a pack's template; unknown languages fall back to py."""

    class _Cfg:
        github_sandbox_workflow_filename_py = "custom-py.yml"
        github_sandbox_workflow_filename_ts = ""
        github_sandbox_workflow_filename_go = "custom-go.yml"
        github_sandbox_workflow_filename_rust = "haunter-test-rust.yml"
        github_sandbox_workflow_filename_java = "haunter-test-java.yml"
        github_sandbox_workflow_filename_docker = "haunter-test-docker.yml"

    cfg = _Cfg()
    assert select_workflow_filename("py", cfg) == "custom-py.yml"
    # A blank configured value must not point the sandbox at an empty filename.
    assert select_workflow_filename("ts", cfg) == "haunter-test-ts.yml"
    assert select_workflow_filename("go", cfg) == "custom-go.yml"
    # Unknown language -> default pack, not a crash.
    assert select_workflow_filename("cobol", cfg) == "custom-py.yml"
    # Real settings ship every pack.
    for language in SUPPORTED_LANGUAGES:
        assert select_workflow_filename(language, settings).endswith(".yml")


def test_builtin_defaults_match_shipped_templates() -> None:
    """The runner's built-in filename table cannot drift from the directory."""
    shipped = set(_template_names())
    from app.sandbox.github_actions_runner import _WORKFLOW_FILENAME_BY_LANGUAGE

    assert set(_WORKFLOW_FILENAME_BY_LANGUAGE) == set(SUPPORTED_LANGUAGES)
    for _language, (_attribute, default_name) in _WORKFLOW_FILENAME_BY_LANGUAGE.items():
        assert default_name in shipped, f"{default_name} is not shipped"


# ---------------------------------------------------------------------------
# Monorepo rendering
# ---------------------------------------------------------------------------


def test_render_defaults_are_a_no_op() -> None:
    """No override, or an explicit repo root, leaves the template untouched."""
    for name in _template_names():
        content = _load_workflow_template(name)
        assert render_workflow_content(content) == content
        assert render_workflow_content(content, working_dir=".") == content
        assert render_workflow_content(content, working_dir="", test_command="") == content


def test_render_injects_working_dir_and_test_command() -> None:
    """A monorepo scope reaches every template as valid, quoted YAML."""
    yaml = pytest.importorskip("yaml")
    for name in _template_names():
        content = _load_workflow_template(name)
        rendered = render_workflow_content(
            content, working_dir="packages/api", test_command="pytest -q tests/unit"
        )
        assert 'HAUNTER_WORKING_DIR: "packages/api"' in rendered
        assert 'HAUNTER_TEST_COMMAND: "pytest -q tests/unit"' in rendered
        # Exactly one declaration of each; the raw marker is gone (replaced,
        # not duplicated), and every other use still reads the rendered value.
        assert len(re.findall(r"^\s+HAUNTER_WORKING_DIR:", rendered, re.MULTILINE)) == 1
        assert len(re.findall(r"^\s+HAUNTER_TEST_COMMAND:", rendered, re.MULTILINE)) == 1
        assert 'HAUNTER_WORKING_DIR: "."' not in rendered
        assert 'HAUNTER_TEST_COMMAND: ""' not in rendered
        parsed = yaml.safe_load(rendered)
        assert parsed["env"]["HAUNTER_WORKING_DIR"] == "packages/api"
        assert parsed["env"]["HAUNTER_TEST_COMMAND"] == "pytest -q tests/unit"


def test_render_only_replaces_the_first_marker() -> None:
    """A template that mentions the marker twice must not be corrupted."""
    content = 'env:\n  HAUNTER_WORKING_DIR: "."\njobs:\n  a:\n    env:\n      HAUNTER_WORKING_DIR: "."\n'
    rendered = render_workflow_content(content, working_dir="svc")
    assert rendered.count('HAUNTER_WORKING_DIR: "svc"') == 1
    assert rendered.count('HAUNTER_WORKING_DIR: "."') == 1


def test_render_rejects_traversal_and_absolute_paths() -> None:
    """A hostile working_dir must fail loudly, never reach a rendered workflow."""
    content = _load_workflow_template("haunter-test-py.yml")
    for hostile in ("../etc", "/etc", "a/../../b", "./..", "x/./y", "a" * 256):
        with pytest.raises(ValueError):
            render_workflow_content(content, working_dir=hostile)
    assert render_workflow_content(content) == content


def test_render_rejects_malformed_test_command() -> None:
    """Oversized or NUL-bearing commands are rejected at the render boundary."""
    content = _load_workflow_template("haunter-test-go.yml")
    with pytest.raises(ValueError):
        render_workflow_content(content, test_command="x" * 1001)
    with pytest.raises(ValueError):
        render_workflow_content(content, test_command="pytest\x00 --evil")


def test_render_escapes_yaml_metacharacters() -> None:
    """A command containing quotes/newlines stays a single valid YAML scalar."""
    yaml = pytest.importorskip("yaml")
    content = _load_workflow_template("haunter-test-ts.yml")
    rendered = render_workflow_content(
        content, test_command='npm test -- --grep "a: b"\n'
    )
    # Surrounding whitespace is stripped by the validator, so the command stays
    # a single one-line scalar; the embedded quotes are escaped, not dropped.
    assert (
        'HAUNTER_TEST_COMMAND: "npm test -- --grep \\"a: b\\""' in rendered
    ), "quotes must be escaped so the scalar cannot terminate early"
    parsed = yaml.safe_load(rendered)
    assert parsed["env"]["HAUNTER_TEST_COMMAND"] == 'npm test -- --grep "a: b"'


def test_render_fails_when_template_lacks_a_marker() -> None:
    """Template drift must fail the attempt, not silently ignore the scope."""
    with pytest.raises(ValueError, match="render marker"):
        render_workflow_content("name: x\njobs: {}\n", working_dir="svc")
    with pytest.raises(ValueError, match="render marker"):
        render_workflow_content("name: x\njobs: {}\n", test_command="pytest")


def test_resolve_sandbox_overrides_is_neutral_without_a_run() -> None:
    """No run id means no DB access and the neutral scope."""
    import asyncio

    resolved = asyncio.run(resolve_sandbox_overrides(None))
    assert resolved.working_dir is None
    assert resolved.test_command is None


# ---------------------------------------------------------------------------
# RepoSettings validators
# ---------------------------------------------------------------------------


def test_validate_working_dir_accepts_repo_relative_paths() -> None:
    assert validate_working_dir(None) is None
    assert validate_working_dir("") == "."
    assert validate_working_dir("   ") == "."
    assert validate_working_dir(".") == "."
    assert validate_working_dir("packages/api") == "packages/api"
    assert validate_working_dir("  packages/api  ") == "packages/api"
    assert validate_working_dir("apps/web") == "apps/web"
    assert validate_working_dir("packages/api/") == "packages/api"
    assert validate_working_dir("packages\\api") == "packages/api"
    assert validate_working_dir("a" * 255) == "a" * 255


@pytest.mark.parametrize(
    "hostile",
    ["../evil", "packages/../../etc", "..", "/abs/path", "a//b", "pkg/./x", "a" * 256],
)
def test_validate_working_dir_rejects_unsafe_values(hostile: str) -> None:
    with pytest.raises(ValueError):
        validate_working_dir(hostile)


@pytest.mark.parametrize(
    "hostile",
    ["pkg dir", "pkg;rm -rf /", "pkg$(whoami)", "pkg`id`", "pkg|tee", "pkg>out", "*"],
)
def test_validate_working_dir_rejects_shell_metacharacters(hostile: str) -> None:
    """The value is interpolated into a shell `cd`, so it must be inert."""
    with pytest.raises(ValueError):
        validate_working_dir(hostile)


def test_validate_test_command_bounds() -> None:
    assert validate_test_command(None) is None
    assert validate_test_command("") is None
    assert validate_test_command("   ") is None
    assert validate_test_command("  pytest -q  ") == "pytest -q"
    assert validate_test_command("go test -race ./...") == "go test -race ./..."
    assert validate_test_command("x" * 1000) == "x" * 1000


@pytest.mark.parametrize("hostile", ["x" * 1001, "pytest\x00", "pytest\x00 -q"])
def test_validate_test_command_rejects_malformed(hostile: str) -> None:
    with pytest.raises(ValueError):
        validate_test_command(hostile)


def test_working_dir_survives_the_full_write_path() -> None:
    """Validators are what the settings service calls; keep them in sync.

    A validator that accepts a value the renderer rejects (or vice versa) would
    let a bad override reach a live verification run.
    """
    content = _load_workflow_template("haunter-test-py.yml")
    for candidate in ("packages/api", "apps/web", "a/b/c", "x" * 255):
        normalized = validate_working_dir(candidate)
        assert normalized is not None
        render_workflow_content(content, working_dir=normalized)


# ---------------------------------------------------------------------------
# Seed tarball tiers
# ---------------------------------------------------------------------------


def test_file_priority_tier_python_tiers_unchanged() -> None:
    """Regression guard: every pre-existing python verdict must be identical."""
    assert _file_priority_tier("pytest.ini") == 0
    assert _file_priority_tier("pyproject.toml") == 0
    assert _file_priority_tier("setup.cfg") == 0
    assert _file_priority_tier("setup.py") == 0
    assert _file_priority_tier("tox.ini") == 0
    assert _file_priority_tier("conftest.py") == 0
    assert _file_priority_tier(".python-version") == 0
    assert _file_priority_tier("backend/pytest.ini") == 0
    assert _file_priority_tier("requirements.txt") == 1
    assert _file_priority_tier("backend/requirements-dev.txt") == 1
    assert _file_priority_tier("Pipfile") == 1
    assert _file_priority_tier("Pipfile.lock") == 1
    assert _file_priority_tier("poetry.lock") == 1
    assert _file_priority_tier("tests/test_unit.py") == 2
    assert _file_priority_tier("backend/tests/unit/test_api.py") == 2
    assert _file_priority_tier("test_foo.py") == 2
    assert _file_priority_tier("src/thing_test.py") == 2
    assert _file_priority_tier("src/main.py") == 3
    assert _file_priority_tier("README.md") == 3


def test_file_priority_tier_typescript_tiers_unchanged() -> None:
    assert _file_priority_tier("package.json") == 0
    assert _file_priority_tier("frontend/package.json") == 0
    assert _file_priority_tier("package-lock.json") == 1
    assert _file_priority_tier("frontend/src/app.test.ts") == 2
    assert _file_priority_tier("src/__tests__/app.js") == 2
    assert _file_priority_tier("src/index.ts") == 3


def test_file_priority_tier_new_packs() -> None:
    assert _file_priority_tier("go.mod") == 0
    assert _file_priority_tier("go.work") == 0
    assert _file_priority_tier("Cargo.toml") == 0
    assert _file_priority_tier("Cargo.lock") == 1
    assert _file_priority_tier("go.sum") == 1
    assert _file_priority_tier("pom.xml") == 0
    assert _file_priority_tier("build.gradle") == 0
    assert _file_priority_tier("Dockerfile") == 0
    assert _file_priority_tier("docker-compose.yml") == 0
    assert _file_priority_tier("cmd/server/main.go") == 3
    assert _file_priority_tier("src/lib.rs") == 3
    assert _file_priority_tier("src/main/java/App.java") == 3


def test_file_priority_tier_monorepo_subdirectories() -> None:
    """A monorepo package's config and tests rank exactly like the root's.

    This is what makes ``RepoSettings.working_dir`` usable: the subtree the
    user scoped the sandbox to is the subtree whose config and tests survive
    ``seed_max_files``.
    """
    assert _file_priority_tier("packages/api/go.mod") == 0
    assert _file_priority_tier("packages/api/internal/store/store_test.go") == 2
    assert _file_priority_tier("apps/web/package.json") == 0
    assert _file_priority_tier("apps/web/src/Button.test.tsx") == 2
    assert _file_priority_tier("crates/engine/Cargo.toml") == 0
    assert _file_priority_tier("crates/engine/src/engine_test.rs") == 2
    assert _file_priority_tier("services/billing/pom.xml") == 0
    assert _file_priority_tier("services/billing/src/test/java/BillingTest.java") == 2
    assert _file_priority_tier("deploy/docker-compose.production.yml") == 0


def _build_tar(files: dict[str, bytes]) -> bytes:
    """Build an uncompressed GitHub-style tarball rooted at ``repo-sha/``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for rel_path, payload in files.items():
            info = tarfile.TarInfo(name=f"repo-sha/{rel_path}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_parse_tar_keeps_every_pack_manifest_under_the_cap() -> None:
    """Tier 0 wins the cap for all six packs, not just python."""
    tar_bytes = _build_tar(
        {
            "go.mod": b"module example\n",
            "Cargo.toml": b"[package]\n",
            "pom.xml": b"<project/>\n",
            "package.json": b"{}\n",
            "pyproject.toml": b"[tool.pytest]\n",
            "Dockerfile": b"FROM scratch\n",
        }
    )
    seeded = parse_tar_to_files(tar_bytes, max_files=3)
    assert len(seeded) == 3
    # Sorted by (tier, path), so the first three tier-0 files win.
    assert set(seeded) == {"Cargo.toml", "Dockerfile", "go.mod"}


def test_parse_tar_keeps_monorepo_package_config_and_tests_under_the_cap() -> None:
    """A large monorepo still seeds the scoped package's config and tests."""
    files: dict[str, bytes] = {
        "go.mod": b"module example\n",
        "packages/api/go.mod": b"module example/api\n",
        "packages/api/internal/api_test.go": b"package api\n",
        "packages/api/internal/handler.go": b"package api\n",
    }
    # 40 unrelated source files would otherwise crowd the cap.
    for index in range(40):
        files[f"cmd/service{index}/main.go"] = b"package main\n"
    seeded = parse_tar_to_files(_build_tar(files), max_files=3)
    assert "packages/api/go.mod" in seeded
    assert "packages/api/internal/api_test.go" in seeded
    assert "cmd/service0/main.go" not in seeded


def test_parse_tar_still_skips_workflows_and_oversized_files() -> None:
    """Existing security and size filters are untouched by the tier work."""
    tar_bytes = _build_tar(
        {
            ".github/workflows/release.yml": b"name: release\n",
            "big.bin": b"x" * (5 * 1024 * 1024 + 1),
            "src/main.go": b"package main\n",
        }
    )
    seeded = parse_tar_to_files(tar_bytes, max_files=100)
    assert ".github/workflows/release.yml" not in seeded
    assert "big.bin" not in seeded
    assert seeded == {"src/main.go": b"package main\n"}


def test_parse_tar_ignores_symlinks() -> None:
    """Symlink members stay excluded — a tier change must not open this up."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name="repo-sha/go.mod")
        payload = b"module example\n"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo(name="repo-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    seeded = parse_tar_to_files(buffer.getvalue(), max_files=10)
    assert seeded == {"go.mod": b"module example\n"}
