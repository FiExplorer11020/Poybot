"""
Tests for src.backups.dumper (S4.12).

Strategy:
    * Don't actually run pg_dump — patch subprocess.run + shutil.which
      and assert the args/env passed to it.
    * Cover DSN parsing edge cases. urlparse is forgiving in some weird
      ways that we want to guard against (e.g. ports beyond 65535,
      missing db, wrong scheme).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.backups import dumper as dumper_module
from src.backups.dumper import PgDumpError, run_pg_dump


# --------------------------------------------------------------------------- #
# DSN parsing                                                                 #
# --------------------------------------------------------------------------- #


def test_parse_dsn_canonical():
    parsed = dumper_module._parse_dsn(
        "postgresql://alice:secret@db.example:5433/polymarket"
    )
    assert parsed.host == "db.example"
    assert parsed.port == 5433
    assert parsed.database == "polymarket"
    assert parsed.user == "alice"
    assert parsed.password == "secret"


def test_parse_dsn_default_port():
    parsed = dumper_module._parse_dsn("postgresql://u:p@host/db")
    assert parsed.port == 5432


def test_parse_dsn_postgres_alias():
    parsed = dumper_module._parse_dsn("postgres://u:p@host/db")
    assert parsed.database == "db"


@pytest.mark.parametrize(
    "bad_dsn",
    [
        "mysql://u:p@host/db",
        "https://example.com",
        "postgres://u:p@host",
        "postgres:///",
    ],
)
def test_parse_dsn_rejects_invalid(bad_dsn):
    with pytest.raises(PgDumpError):
        dumper_module._parse_dsn(bad_dsn)


# --------------------------------------------------------------------------- #
# run_pg_dump                                                                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_pg_dump(monkeypatch, tmp_path):
    """Stub shutil.which + subprocess.run so run_pg_dump's I/O is
    completely deterministic. Captures the argv/env passed."""
    captures: dict = {}

    monkeypatch.setattr(
        dumper_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    def fake_run(args, env, timeout, capture_output, text, check):
        captures["args"] = args
        captures["env"] = env
        captures["timeout"] = timeout
        # Simulate pg_dump writing the output file.
        out = Path(args[args.index("--file") + 1])
        out.write_bytes(b"PGDMP fake archive bytes" * 100)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dumper_module.subprocess, "run", fake_run)
    return captures


def test_run_pg_dump_happy_path(fake_pg_dump, tmp_path):
    out = tmp_path / "dump" / "polymarket.dump"
    result = run_pg_dump(
        dsn="postgresql://alice:secret@db.example:5433/polymarket",
        output_path=out,
        timeout_s=300,
    )
    assert result == out
    assert out.exists()
    args = fake_pg_dump["args"]
    # Argv must NOT contain the password — that goes via PGPASSWORD env.
    assert "secret" not in args
    assert fake_pg_dump["env"]["PGPASSWORD"] == "secret"
    # Format + compression + safety flags.
    assert "--format" in args and args[args.index("--format") + 1] == "custom"
    assert "--compress" in args and args[args.index("--compress") + 1] == "9"
    assert "--no-owner" in args and "--no-acl" in args
    assert "--host" in args and args[args.index("--host") + 1] == "db.example"
    assert "--port" in args and args[args.index("--port") + 1] == "5433"
    assert "--dbname" in args and args[args.index("--dbname") + 1] == "polymarket"


def test_run_pg_dump_creates_parent_dir(fake_pg_dump, tmp_path):
    """Output directory shouldn't exist beforehand — dumper makes it."""
    nested = tmp_path / "deep" / "nested" / "out.dump"
    assert not nested.parent.exists()
    run_pg_dump(
        dsn="postgresql://u:p@h/d",
        output_path=nested,
        timeout_s=60,
    )
    assert nested.exists()


def test_run_pg_dump_no_password_omits_pgpassword_env(monkeypatch, tmp_path):
    """A passwordless DSN (e.g. trust auth) must not poke PGPASSWORD —
    setting it to "" can break psql/pg_dump behavior."""
    monkeypatch.setattr(dumper_module.shutil, "which", lambda n: f"/usr/bin/{n}")
    captured = {}

    def fake_run(args, env, **kwargs):
        captured["env"] = env
        out = Path(args[args.index("--file") + 1])
        out.write_bytes(b"x")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dumper_module.subprocess, "run", fake_run)
    out = tmp_path / "out.dump"
    run_pg_dump(dsn="postgresql://u@host/db", output_path=out, timeout_s=60)
    assert "PGPASSWORD" not in captured["env"]


def test_run_pg_dump_raises_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(dumper_module.shutil, "which", lambda n: None)
    with pytest.raises(PgDumpError, match="not found on PATH"):
        run_pg_dump(
            dsn="postgresql://u:p@h/d",
            output_path=tmp_path / "x.dump",
            timeout_s=60,
        )


def test_run_pg_dump_propagates_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(dumper_module.shutil, "which", lambda n: "/usr/bin/pg_dump")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="pg_dump: error: connection to server failed: timeout expired",
        )

    monkeypatch.setattr(dumper_module.subprocess, "run", fake_run)
    out = tmp_path / "x.dump"
    with pytest.raises(PgDumpError, match="exited 1.*timeout expired"):
        run_pg_dump(dsn="postgresql://u:p@h/d", output_path=out, timeout_s=60)
    # Partial file must be cleaned up on failure.
    assert not out.exists()


def test_run_pg_dump_handles_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(dumper_module.shutil, "which", lambda n: "/usr/bin/pg_dump")

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(dumper_module.subprocess, "run", fake_run)
    out = tmp_path / "x.dump"
    out.write_bytes(b"partial")  # simulate partial write

    with pytest.raises(PgDumpError, match="timed out after 5s"):
        run_pg_dump(dsn="postgresql://u:p@h/d", output_path=out, timeout_s=5)
    assert not out.exists()


def test_run_pg_dump_raises_when_output_empty(monkeypatch, tmp_path):
    """pg_dump returned 0 but didn't write anything — broken postgres-client."""
    monkeypatch.setattr(dumper_module.shutil, "which", lambda n: "/usr/bin/pg_dump")

    def fake_run(args, **kwargs):
        out = Path(args[args.index("--file") + 1])
        out.write_bytes(b"")  # empty file
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dumper_module.subprocess, "run", fake_run)
    out = tmp_path / "x.dump"
    with pytest.raises(PgDumpError, match="missing or empty"):
        run_pg_dump(dsn="postgresql://u:p@h/d", output_path=out, timeout_s=60)
