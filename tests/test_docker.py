"""
Tests for the S4.11 Docker layer.

Strategy:
    * No `docker build` here — we don't have Docker in CI/the sandbox.
      Instead we assert structural invariants on the Dockerfile and
      compose files. This catches the typical regressions: a service
      gets dropped, the runtime stage stops being non-root, the
      healthcheck disappears, memory limits get edited away, etc.
    * Smoke-test the docker_healthcheck.py exit codes — it must exit
      non-zero when Redis is unreachable, and the failure message
      should be loud.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
COMPOSE_PROD = ROOT / "docker-compose.prod.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
HEALTHCHECK = ROOT / "scripts" / "docker_healthcheck.py"

# docker-compose YAML uses tags like `!reset` that PyYAML can't parse
# out of the box. We register a permissive constructor so we can still
# inspect the structure.


class _ComposeLoader(yaml.SafeLoader):
    pass


def _reset_constructor(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_ComposeLoader.add_constructor("!reset", _reset_constructor)


def _load_yaml(path: Path) -> dict:
    with path.open() as fh:
        return yaml.load(fh, Loader=_ComposeLoader)


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def compose() -> dict:
    return _load_yaml(COMPOSE)


@pytest.fixture(scope="module")
def compose_prod() -> dict:
    return _load_yaml(COMPOSE_PROD)


# --------------------------------------------------------------------------- #
# Dockerfile                                                                   #
# --------------------------------------------------------------------------- #


class TestDockerfile:
    def test_dockerfile_exists(self):
        assert DOCKERFILE.exists()
        assert DOCKERFILE.stat().st_size > 100

    def test_multi_stage_build(self, dockerfile_text):
        """builder + runtime stages must both exist. We accept either
        a literal version (`python:3.11-slim`) or the build arg
        (`python:${PYTHON_VERSION}-slim`)."""
        py = r"python:(?:\$\{[A-Z_]+\}|[\d.]+)-slim"
        assert re.search(rf"FROM\s+{py}\s+AS\s+builder", dockerfile_text)
        assert re.search(rf"FROM\s+{py}\s+AS\s+runtime", dockerfile_text)

    def test_runtime_copies_venv_from_builder(self, dockerfile_text):
        """Multi-stage works only if the venv is copied across."""
        assert "COPY --from=builder /opt/venv /opt/venv" in dockerfile_text

    def test_runtime_runs_as_non_root(self, dockerfile_text):
        """Last USER directive must be the non-root user. Anything else
        is a security regression."""
        users = re.findall(r"^USER\s+(\S+)", dockerfile_text, flags=re.MULTILINE)
        assert users, "no USER directive found"
        assert users[-1] == "polymarket"

    def test_runtime_user_is_uid_1000(self, dockerfile_text):
        """uid 1000 lines up with the default Oracle Cloud user, so
        bind-mounted volumes don't end up root-owned."""
        assert "--uid 1000" in dockerfile_text

    def test_runtime_has_entrypoint_tini(self, dockerfile_text):
        """Without tini, signal handling on `docker stop` is unreliable
        for python -m entry points."""
        assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in dockerfile_text

    def test_healthcheck_present(self, dockerfile_text):
        assert "HEALTHCHECK" in dockerfile_text
        assert "/app/scripts/docker_healthcheck.py" in dockerfile_text

    def test_pyproject_install_includes_backtest_extra(self, dockerfile_text):
        """The engine container needs pandas/pyarrow for the nightly
        batch — that lives behind the [backtest] extra."""
        assert ".[backtest]" in dockerfile_text

    def test_runtime_carries_libgomp(self, dockerfile_text):
        """lightgbm fails to import without libgomp1 on slim images."""
        assert "libgomp1" in dockerfile_text

    def test_runtime_carries_postgresql_client(self, dockerfile_text):
        """The backups container shells out to pg_dump — it must be
        available in the runtime stage."""
        marker = "FROM python:${PYTHON_VERSION}-slim AS runtime"
        idx = dockerfile_text.index(marker)
        runtime_block = dockerfile_text[idx:]
        assert "postgresql-client" in runtime_block

    def test_runtime_does_not_pull_build_essential(self, dockerfile_text):
        """build-essential must stay in the builder stage — pulling it
        into runtime adds ~250 MB and a CVE surface."""
        # Locate the runtime stage and check there's no build-essential
        # apt-get inside it.
        marker = "FROM python:${PYTHON_VERSION}-slim AS runtime"
        idx = dockerfile_text.index(marker)
        runtime_block = dockerfile_text[idx:]
        assert "build-essential" not in runtime_block

    def test_pythonpath_is_app(self, dockerfile_text):
        """src/ is laid down at /app/src — PYTHONPATH=/app keeps
        `python -m src.engine.main` working without `pip install -e`."""
        assert "PYTHONPATH=/app" in dockerfile_text


# --------------------------------------------------------------------------- #
# .dockerignore                                                                #
# --------------------------------------------------------------------------- #


class TestDockerignore:
    def test_dockerignore_exists(self):
        assert DOCKERIGNORE.exists()

    @pytest.mark.parametrize(
        "pattern",
        [".git/", ".venv/", ".pytest_cache/", "tests/", ".env", "*.log"],
    )
    def test_critical_excludes(self, pattern):
        text = DOCKERIGNORE.read_text()
        assert pattern in text, f".dockerignore missing {pattern!r}"


# --------------------------------------------------------------------------- #
# docker-compose.yml                                                           #
# --------------------------------------------------------------------------- #


class TestCompose:
    # Updated 2026-05-17: was 7 (sprint-0 only); now 20 to match the live
    # compose file after R6→R13 daemons landed. R6 spine = onchain +
    # crawler + falcon_refresher + maintenance; R13 = calibration;
    # sprint-2 (R8/R9/R10) = strategy_classifier + follower_volume +
    # causal; sprint-3 (R11/R12/R7) = book_l3 + microstructure + social +
    # crossmarket + mempool. Sprint-2/3 services are profile-gated, so
    # they don't spawn on a vanilla `up -d`, but they ARE defined.
    EXPECTED_SERVICES = {
        # core
        "postgres", "redis", "observer", "engine", "registry", "api",
        "backups", "maintenance",
        # R6 spine
        "onchain", "crawler", "falcon_refresher",
        # R13
        "calibration",
        # sprint-2 (R8/R9/R10) — profile-gated
        "strategy_classifier", "follower_volume", "causal",
        # sprint-3 (R11/R12/R7) — profile-gated
        "book_l3", "microstructure", "social", "crossmarket", "mempool",
    }

    def test_compose_parses(self, compose):
        assert "services" in compose

    def test_all_app_services_present(self, compose):
        assert set(compose["services"].keys()) == self.EXPECTED_SERVICES

    @pytest.mark.parametrize(
        "service,module",
        [
            ("observer", "src.observer.main"),
            ("engine", "src.engine.main"),
            ("registry", "src.registry.main"),
            ("backups", "src.backups.main"),
        ],
    )
    def test_service_command_runs_module(self, compose, service, module):
        cmd = compose["services"][service]["command"]
        assert cmd[:3] == ["python", "-m", module]

    @pytest.mark.parametrize(
        "service", ["observer", "engine", "registry", "api", "backups"]
    )
    def test_app_service_depends_on_backends(self, compose, service):
        deps = compose["services"][service]["depends_on"]
        assert "postgres" in deps and deps["postgres"]["condition"] == "service_healthy"
        assert "redis" in deps and deps["redis"]["condition"] == "service_healthy"

    @pytest.mark.parametrize(
        "service", ["observer", "engine", "registry", "backups"]
    )
    def test_app_service_uses_shared_image(self, compose, service):
        cfg = compose["services"][service]
        assert cfg.get("image") == "polymarket-bot:latest"
        assert cfg["build"]["dockerfile"] == "Dockerfile"

    @pytest.mark.parametrize("service", ["observer", "engine", "registry"])
    def test_app_service_has_healthcheck(self, compose, service):
        hc = compose["services"][service].get("healthcheck", {})
        cmd = hc.get("test", [])
        assert any("docker_healthcheck.py" in str(part) for part in cmd), (
            f"{service} healthcheck must invoke docker_healthcheck.py"
        )

    def test_postgres_and_redis_have_healthchecks(self, compose):
        assert "test" in compose["services"]["postgres"]["healthcheck"]
        assert "test" in compose["services"]["redis"]["healthcheck"]

    def test_app_services_inject_compose_network_urls(self, compose):
        """Inside the compose network, services reach Postgres/Redis by
        DNS name, not localhost. .env's localhost values would 502."""
        for svc in ("observer", "engine", "registry", "api", "backups"):
            env = compose["services"][svc]["environment"]
            assert "@postgres:5432" in env["DATABASE_URL"]
            assert "redis://redis:6379" in env["REDIS_URL"]

    def test_backups_service_healthcheck_uses_pg_dump(self, compose):
        """Backups runs an idle scheduler — its healthcheck is a quick
        pg_dump --version (binary present + ELF runs), not the full
        Redis+DB probe."""
        hc = compose["services"]["backups"]["healthcheck"]
        cmd = " ".join(str(p) for p in hc["test"])
        assert "pg_dump --version" in cmd


# --------------------------------------------------------------------------- #
# docker-compose.prod.yml                                                      #
# --------------------------------------------------------------------------- #


class TestComposeProd:
    # Updated 2026-05-17: postgres 300→1024M (R6-R13 schema needs bigger
    # buffers + 500 max_connections), redis 64→160M (R7/R11 streams),
    # observer 300→350M, engine 600→700M (jax/lightgbm), api 200→300M
    # (R6-R13 endpoint surface + v2 dashboard SQL).
    @pytest.mark.parametrize(
        "service,limit",
        [
            ("postgres", "1024M"),
            ("redis", "160M"),
            ("observer", "350M"),
            ("engine", "700M"),
            ("registry", "200M"),
            ("api", "300M"),
            ("backups", "200M"),
        ],
    )
    def test_memory_limits(self, compose_prod, service, limit):
        deploy = compose_prod["services"][service]["deploy"]
        assert deploy["resources"]["limits"]["memory"] == limit

    @pytest.mark.parametrize(
        "service",
        ["postgres", "redis", "observer", "engine", "registry", "api", "backups"],
    )
    def test_restart_policy(self, compose_prod, service):
        assert compose_prod["services"][service]["restart"] == "unless-stopped"

    @pytest.mark.parametrize(
        "service", ["observer", "engine", "registry", "api", "backups"]
    )
    def test_app_logging_capped(self, compose_prod, service):
        """Without log rotation, json-file fills the VM disk in days.
        Cap at 50 MB / service (10m × 5 files)."""
        logging = compose_prod["services"][service]["logging"]
        assert logging["driver"] == "json-file"
        assert logging["options"]["max-size"] == "10m"
        assert logging["options"]["max-file"] == "5"


# --------------------------------------------------------------------------- #
# docker_healthcheck.py                                                        #
# --------------------------------------------------------------------------- #


class TestHealthcheckScript:
    def test_exits_nonzero_when_redis_unreachable(self):
        env = os.environ.copy()
        env["REDIS_URL"] = "redis://127.0.0.1:9/0"  # nothing listens here
        env["DATABASE_URL"] = "postgresql://nope:nope@127.0.0.1:9/nope"
        result = subprocess.run(
            [sys.executable, str(HEALTHCHECK)],
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 1
        # Loud failure mode — the message must be on stderr so docker
        # logs it.
        stderr = result.stderr.decode()
        assert "Redis unreachable" in stderr or "Postgres unreachable" in stderr

    def test_exits_nonzero_without_database_url(self):
        env = os.environ.copy()
        env.pop("DATABASE_URL", None)
        # Use a Redis URL that fails fast so we get to the DB check.
        env["REDIS_URL"] = "redis://127.0.0.1:9/0"
        result = subprocess.run(
            [sys.executable, str(HEALTHCHECK)],
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 1
