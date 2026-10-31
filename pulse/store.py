"""JSON-file storage under DATA_DIR. One file per record, so concurrent workers never
write the same file, and exclusive create gives the idempotency a unique constraint would.

    items/<url_hash>.json               created once, then enriched in place
    analyses/<profile>/<url_hash>.json  created once
    runs/<key>.json                     created once per day (or per forced rerun)
    runs/<date>.tokens                  append-only token ledger: "<run_key>\\t<tokens>"
    digests/<profile>/<date>.json       replaced on forced rerun
    deliveries/<run_key>/<profile>-<subscription>.json

"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pulse.config import get_settings


def _file(rel: str, suffix: str = ".json") -> Path:
    root = get_settings().data_dir.resolve()
    path = (root / f"{rel}{suffix}").resolve()
    if root not in path.parents:  # rel can contain request input
        raise ValueError(f"path escapes data dir: {rel!r}")
    return path


def _write_tmp(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return Path(tmp)


def load(rel: str) -> Any:
    try:
        return json.loads(_file(rel).read_text("utf-8"))
    except FileNotFoundError:
        return None


def save(rel: str, obj: Any) -> None:
    """Atomic replace: readers see the old or the new file, never half of one."""
    path = _file(rel)
    os.replace(_write_tmp(path, obj), path)


def create(rel: str, obj: Any) -> bool:
    """Write only if absent. False if the record already exists (safe under concurrency)."""
    path = _file(rel)
    tmp = _write_tmp(path, obj)
    try:
        os.link(tmp, path)  # atomic, fails if path exists
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink()


def delete(rel: str) -> None:
    _file(rel).unlink(missing_ok=True)


def names(rel_dir: str) -> list[str]:
    """Record names (file stems) in a directory, sorted."""
    d = get_settings().data_dir / rel_dir
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def scan(rel_dir: str) -> list[dict[str, Any]]:
    d = get_settings().data_dir / rel_dir
    return (
        [json.loads(p.read_text("utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
    )


def add_tokens(day: str, run_key: str, tokens: int, provider: str = "") -> None:
    path = _file(f"runs/{day}", ".tokens")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:  # small appends are atomic
        f.write(f"{run_key}\t{tokens}\t{provider}\n")


def _ledger(day: str, run_key: str | None) -> list[tuple[int, str]]:
    try:
        lines = _file(f"runs/{day}", ".tokens").read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = ((line.split("\t") + [""])[:3] for line in lines if line)
    return [(int(n), provider) for key, n, provider in rows if run_key in (None, key)]


def tokens_used(day: str, run_key: str | None = None) -> int:
    return sum(n for n, _ in _ledger(day, run_key))


def tokens_by_provider(day: str, run_key: str | None = None) -> dict[str, int]:
    totals: dict[str, int] = {}
    for n, provider in _ledger(day, run_key):
        totals[provider or "unknown"] = totals.get(provider or "unknown", 0) + n
    return totals
