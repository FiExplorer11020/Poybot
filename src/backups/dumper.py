"""
pg_dump invocation (S4.12).

We shell out to the postgresql-client `pg_dump` binary baked into the
runtime image (see Dockerfile). Custom format + max compression so
the output:
    * is restorable with pg_restore (parallel restore, selective
      table restore, etc.)
    * is internally compressed (pg_dump's -Z9 ≈ gzip -9 ≈ 5–8× for
      our schema).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class PgDumpError(RuntimeError):
    """pg_dump exited non-zero, timed out, or wasn't on PATH."""


@dataclass(frozen=True)
class _ParsedDsn:
    host: str
    port: int
    database: str
    user: str
    password: str


def _parse_dsn(dsn: str) -> _ParsedDsn:
    """Split a `postgresql://user:pass@host:port/db` DSN. asyncpg /
    psycopg accept variants — we only need the canonical form."""
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise PgDumpError(f"unsupported DSN scheme: {parsed.scheme!r}")
    if not parsed.hostname or not parsed.path or parsed.path == "/":
        raise PgDumpError(f"DSN missing host or database: {dsn!r}")
    return _ParsedDsn(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=parsed.username or "",
        password=parsed.password or "",
    )


def run_pg_dump(
    *,
    dsn: str,
    output_path: Path | str,
    timeout_s: int = 1800,
    pg_dump_binary: str | None = None,
) -> Path:
    """Run `pg_dump` against `dsn`, writing a custom-format archive
    to `output_path`. Returns the resolved path on success.

    Raises:
        PgDumpError: if pg_dump can't be located, exits non-zero, or
            exceeds `timeout_s`.
    """
    binary = pg_dump_binary or shutil.which("pg_dump")
    if not binary:
        raise PgDumpError(
            "pg_dump not found on PATH — runtime image must include "
            "postgresql-client (see Dockerfile)."
        )

    dsn_parts = _parse_dsn(dsn)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = [
        binary,
        "--host", dsn_parts.host,
        "--port", str(dsn_parts.port),
        "--username", dsn_parts.user,
        "--dbname", dsn_parts.database,
        "--format", "custom",
        "--compress", "9",
        "--no-owner",
        "--no-acl",
        "--file", str(output_path),
    ]

    env = os.environ.copy()
    if dsn_parts.password:
        # PGPASSWORD avoids a tty prompt and avoids leaking the secret
        # into the argv visible via `ps`.
        env["PGPASSWORD"] = dsn_parts.password

    try:
        completed = subprocess.run(
            args,
            env=env,
            timeout=timeout_s,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Best-effort cleanup of a partial file.
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise PgDumpError(
            f"pg_dump timed out after {timeout_s}s ({exc.cmd!r})"
        ) from exc
    except FileNotFoundError as exc:
        raise PgDumpError(f"pg_dump binary missing: {exc}") from exc

    if completed.returncode != 0:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        # pg_dump errors on stderr; the first ~1 KB is enough.
        raise PgDumpError(
            f"pg_dump exited {completed.returncode}: "
            f"{completed.stderr.strip()[:1000]}"
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise PgDumpError(
            f"pg_dump succeeded but {output_path} is missing or empty"
        )

    return output_path
