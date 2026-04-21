import json
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd


def _serializable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    return value


class BacktestCache:
    def __init__(self, root: str | Path = "data_cache") -> None:
        self.root = Path(root)

    def write_records(
        self,
        dataset: str,
        shard: str,
        records: Iterable[dict[str, Any]],
        *,
        dedupe_keys: tuple[str, ...] = (),
    ) -> Path:
        path = self.root / dataset / f"{shard}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [_serializable(dict(record)) for record in records]
        frame = pd.DataFrame(rows)
        if dedupe_keys and not frame.empty:
            available_keys = [key for key in dedupe_keys if key in frame.columns]
            if available_keys:
                frame = frame.drop_duplicates(available_keys, keep="first")
        frame.to_parquet(path, index=False)
        return path

    def read_records(self, dataset: str) -> list[dict[str, Any]]:
        dataset_dir = self.root / dataset
        if not dataset_dir.exists():
            return []
        frames = [pd.read_parquet(path) for path in sorted(dataset_dir.glob("*.parquet"))]
        if not frames:
            return []
        frame = pd.concat(frames, ignore_index=True)
        return frame.to_dict(orient="records")

    def read_manifest(self, dataset: str) -> dict[str, Any]:
        path = self.root / "manifest" / f"{dataset}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def mark_done(
        self,
        dataset: str,
        shard_key: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        manifest_dir = self.root / "manifest"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        path = manifest_dir / f"{dataset}.json"
        manifest = self.read_manifest(dataset)
        manifest[shard_key] = {
            "done": True,
            "completed_at": datetime.now().isoformat(),
            **(metadata or {}),
        }
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    def is_done(self, dataset: str, shard_key: str) -> bool:
        return bool(self.read_manifest(dataset).get(shard_key, {}).get("done"))
