"""
Tests for src.backups.r2_client (S4.12).

Strategy:
    * Don't actually hit Cloudflare R2 — monkeypatch `_ensure_client`
      to return a `MagicMock` whose calls we can introspect.
    * Cover the validation paths in __init__ + the three public
      methods (put_file / list_objects / delete_objects), including
      the chunking logic in delete_objects (>1000 keys).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.backups.r2_client import R2Client, R2Error, R2Object


# --------------------------------------------------------------------------- #
# Constructor validation                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs,err",
    [
        # missing endpoint
        (
            dict(endpoint_url="", access_key_id="k", secret_access_key="s", bucket="b"),
            "ENDPOINT_URL",
        ),
        # missing access key
        (
            dict(
                endpoint_url="https://r2",
                access_key_id="",
                secret_access_key="s",
                bucket="b",
            ),
            "access key",
        ),
        # missing secret
        (
            dict(
                endpoint_url="https://r2",
                access_key_id="k",
                secret_access_key="",
                bucket="b",
            ),
            "access key",
        ),
        # missing bucket
        (
            dict(
                endpoint_url="https://r2",
                access_key_id="k",
                secret_access_key="s",
                bucket="",
            ),
            "bucket",
        ),
    ],
)
def test_constructor_rejects_missing_fields(kwargs, err):
    with pytest.raises(R2Error, match=err):
        R2Client(**kwargs)


def test_constructor_stores_fields_and_defers_boto3():
    """Construction should NOT instantiate boto3 (lazy)."""
    c = R2Client(
        endpoint_url="https://r2.example",
        access_key_id="AK",
        secret_access_key="SK",
        bucket="my-bucket",
    )
    assert c.endpoint_url == "https://r2.example"
    assert c.bucket == "my-bucket"
    assert c.region == "auto"
    assert c._client is None  # not yet created


# --------------------------------------------------------------------------- #
# put_file                                                                     #
# --------------------------------------------------------------------------- #


def _client() -> R2Client:
    return R2Client(
        endpoint_url="https://r2.example",
        access_key_id="AK",
        secret_access_key="SK",
        bucket="b",
    )


def test_put_file_uploads_and_returns_size(tmp_path):
    c = _client()
    fake = MagicMock()
    c._client = fake

    f = tmp_path / "x.dump"
    payload = b"PGDMP" * 200
    f.write_bytes(payload)

    size = c.put_file(local_path=f, key="postgres/2026/04/x.dump")
    assert size == len(payload)
    fake.upload_file.assert_called_once()
    kwargs = fake.upload_file.call_args.kwargs
    assert kwargs["Filename"] == str(f)
    assert kwargs["Bucket"] == "b"
    assert kwargs["Key"] == "postgres/2026/04/x.dump"
    assert kwargs["ExtraArgs"]["ContentType"] == "application/octet-stream"


def test_put_file_missing_local_raises():
    c = _client()
    c._client = MagicMock()
    with pytest.raises(R2Error, match="does not exist"):
        c.put_file(local_path=Path("/no/such/file.dump"), key="k")


def test_put_file_wraps_boto_error(tmp_path):
    c = _client()
    fake = MagicMock()
    fake.upload_file.side_effect = RuntimeError("boom")
    c._client = fake
    f = tmp_path / "x.dump"
    f.write_bytes(b"data")
    with pytest.raises(R2Error, match="upload failed"):
        c.put_file(local_path=f, key="x")


# --------------------------------------------------------------------------- #
# list_objects                                                                 #
# --------------------------------------------------------------------------- #


def _make_paginator(pages: list[dict]) -> MagicMock:
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    return paginator


def test_list_objects_paginates_and_yields_r2_objects():
    c = _client()
    fake = MagicMock()
    pages = [
        {
            "Contents": [
                {
                    "Key": "postgres/a.dump",
                    "Size": 100,
                    "LastModified": datetime(2026, 4, 1, tzinfo=timezone.utc),
                },
                {
                    "Key": "postgres/b.dump",
                    "Size": 200,
                    "LastModified": datetime(2026, 4, 2, tzinfo=timezone.utc),
                },
            ]
        },
        {
            "Contents": [
                {
                    "Key": "postgres/c.dump",
                    "Size": 300,
                    "LastModified": datetime(2026, 4, 3, tzinfo=timezone.utc),
                }
            ]
        },
    ]
    fake.get_paginator.return_value = _make_paginator(pages)
    c._client = fake

    objs = list(c.list_objects(prefix="postgres/"))
    assert [o.key for o in objs] == [
        "postgres/a.dump",
        "postgres/b.dump",
        "postgres/c.dump",
    ]
    assert [o.size for o in objs] == [100, 200, 300]
    fake.get_paginator.assert_called_once_with("list_objects_v2")
    paginate_kwargs = fake.get_paginator.return_value.paginate.call_args.kwargs
    assert paginate_kwargs["Bucket"] == "b"
    assert paginate_kwargs["Prefix"] == "postgres/"


def test_list_objects_empty_bucket_returns_no_objects():
    c = _client()
    fake = MagicMock()
    fake.get_paginator.return_value = _make_paginator([{"Contents": []}])
    c._client = fake
    assert list(c.list_objects(prefix="x/")) == []


def test_list_objects_handles_missing_contents_key():
    """S3 omits the Contents key entirely when the prefix is empty."""
    c = _client()
    fake = MagicMock()
    fake.get_paginator.return_value = _make_paginator([{}])  # no Contents
    c._client = fake
    assert list(c.list_objects(prefix="empty/")) == []


def test_list_objects_supplies_default_last_modified():
    """If the S3 response is missing LastModified, we fall back to now()."""
    c = _client()
    fake = MagicMock()
    fake.get_paginator.return_value = _make_paginator(
        [{"Contents": [{"Key": "x.dump", "Size": 5}]}]
    )
    c._client = fake
    [obj] = list(c.list_objects(prefix=""))
    assert obj.last_modified.tzinfo is not None
    # Sanity: within ~5 seconds of now.
    delta = abs(
        (datetime.now(timezone.utc) - obj.last_modified).total_seconds()
    )
    assert delta < 5


def test_list_objects_wraps_boto_error():
    c = _client()
    fake = MagicMock()
    fake.get_paginator.side_effect = RuntimeError("bad creds")
    c._client = fake
    with pytest.raises(R2Error, match="list_objects failed"):
        list(c.list_objects(prefix="x/"))


# --------------------------------------------------------------------------- #
# delete_objects                                                               #
# --------------------------------------------------------------------------- #


def test_delete_objects_empty_returns_zero_no_call():
    c = _client()
    fake = MagicMock()
    c._client = fake
    assert c.delete_objects(keys=[]) == 0
    fake.delete_objects.assert_not_called()


def test_delete_objects_under_1000_single_request():
    c = _client()
    fake = MagicMock()
    fake.delete_objects.return_value = {"Errors": []}
    c._client = fake

    keys = [f"k-{i}" for i in range(50)]
    assert c.delete_objects(keys=keys) == 50
    assert fake.delete_objects.call_count == 1
    payload = fake.delete_objects.call_args.kwargs
    assert payload["Bucket"] == "b"
    assert len(payload["Delete"]["Objects"]) == 50
    assert payload["Delete"]["Quiet"] is True


def test_delete_objects_chunks_at_1000():
    """Boundary: 2500 keys → 3 calls (1000 + 1000 + 500)."""
    c = _client()
    fake = MagicMock()
    fake.delete_objects.return_value = {"Errors": []}
    c._client = fake
    keys = [f"k-{i}" for i in range(2500)]
    assert c.delete_objects(keys=keys) == 2500
    assert fake.delete_objects.call_count == 3
    sizes = [
        len(call.kwargs["Delete"]["Objects"])
        for call in fake.delete_objects.call_args_list
    ]
    assert sizes == [1000, 1000, 500]


def test_delete_objects_raises_on_partial_errors():
    c = _client()
    fake = MagicMock()
    fake.delete_objects.return_value = {
        "Errors": [{"Key": "k-3", "Code": "AccessDenied", "Message": "nope"}]
    }
    c._client = fake
    with pytest.raises(R2Error, match="partial failure"):
        c.delete_objects(keys=["k-1", "k-2", "k-3"])


def test_delete_objects_wraps_boto_error():
    c = _client()
    fake = MagicMock()
    fake.delete_objects.side_effect = RuntimeError("network blip")
    c._client = fake
    with pytest.raises(R2Error, match="delete_objects failed"):
        c.delete_objects(keys=["k-1"])


# --------------------------------------------------------------------------- #
# R2Object                                                                     #
# --------------------------------------------------------------------------- #


def test_r2object_holds_attrs():
    ts = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
    obj = R2Object(key="x.dump", size=42, last_modified=ts)
    assert obj.key == "x.dump"
    assert obj.size == 42
    assert obj.last_modified == ts
