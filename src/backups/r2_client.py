"""
Cloudflare R2 wrapper (S4.12).

Thin shim over boto3 — R2 speaks the S3 API but with a custom
endpoint URL and a single region (`auto`). We isolate boto3 here so
the rest of the package can stub `R2Client` in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

# boto3 is loaded lazily so importing this module doesn't fail on a
# host without the optional dep installed (e.g. someone running the
# engine container without the backups extra). The class methods that
# actually need boto3 import it on first use.


class R2Error(RuntimeError):
    """Wraps boto3 client errors with a stable type for callers."""


class R2Object:
    __slots__ = ("key", "size", "last_modified")

    def __init__(self, key: str, size: int, last_modified: datetime) -> None:
        self.key = key
        self.size = size
        self.last_modified = last_modified

    def __repr__(self) -> str:  # pragma: no cover — debug helper
        return f"R2Object(key={self.key!r}, size={self.size}, mtime={self.last_modified.isoformat()})"


class R2Client:
    """Minimal R2 surface: put_file, list, delete.

    Construction is cheap (boto3.client is lazy) — call sites can
    instantiate per-job without worrying about pooling.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region: str = "auto",
    ) -> None:
        if not endpoint_url:
            raise R2Error("R2_ENDPOINT_URL is required")
        if not access_key_id or not secret_access_key:
            raise R2Error("R2 access key id + secret are required")
        if not bucket:
            raise R2Error("R2 bucket name is required")
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket = bucket
        self.region = region
        self._client = None  # boto3.client, lazily created

    # --------------------------------------------------------------- #
    # Internals                                                       #
    # --------------------------------------------------------------- #

    def _ensure_client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover
                raise R2Error(
                    "boto3 is not installed — `pip install '.[]'` and "
                    "ensure the backups dependency is present."
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._client

    # --------------------------------------------------------------- #
    # Public API                                                      #
    # --------------------------------------------------------------- #

    def put_file(self, *, local_path: Path | str, key: str) -> int:
        """Upload `local_path` under `key`. Returns bytes uploaded."""
        path = Path(local_path)
        if not path.exists():
            raise R2Error(f"local file does not exist: {path}")
        size = path.stat().st_size
        try:
            self._ensure_client().upload_file(
                Filename=str(path),
                Bucket=self.bucket,
                Key=key,
                ExtraArgs={"ContentType": "application/octet-stream"},
            )
        except Exception as exc:
            raise R2Error(f"upload failed for {key!r}: {exc}") from exc
        return size

    def list_objects(self, *, prefix: str = "") -> Iterator[R2Object]:
        """Iterate over objects under `prefix`. Paginates internally."""
        client = self._ensure_client()
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    last_mod = obj.get("LastModified")
                    if last_mod is None:
                        last_mod = datetime.now(timezone.utc)
                    yield R2Object(
                        key=obj["Key"],
                        size=int(obj.get("Size", 0)),
                        last_modified=last_mod,
                    )
        except Exception as exc:
            raise R2Error(f"list_objects failed (prefix={prefix!r}): {exc}") from exc

    def delete_objects(self, *, keys: Iterable[str]) -> int:
        """Bulk-delete via S3 DeleteObjects. Returns count actually
        removed. No-op (returns 0) if `keys` is empty."""
        keys = list(keys)
        if not keys:
            return 0
        client = self._ensure_client()
        deleted = 0
        # S3 DeleteObjects is capped at 1000 keys / request.
        for chunk_start in range(0, len(keys), 1000):
            chunk = keys[chunk_start : chunk_start + 1000]
            try:
                resp = client.delete_objects(
                    Bucket=self.bucket,
                    Delete={
                        "Objects": [{"Key": k} for k in chunk],
                        "Quiet": True,
                    },
                )
            except Exception as exc:
                raise R2Error(f"delete_objects failed: {exc}") from exc
            errors = resp.get("Errors") or []
            if errors:
                raise R2Error(
                    f"delete_objects partial failure: {errors[:5]!r}"
                )
            deleted += len(chunk)
        return deleted
