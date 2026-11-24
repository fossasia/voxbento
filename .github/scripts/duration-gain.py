"""Estimate both shard layouts using times from the same fresh run."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from pytest_split.algorithms import Algorithms

old_path, fresh_path, collection_path, count = sys.argv[1:]
old = json.loads(Path(old_path).read_text())
fresh = json.loads(Path(fresh_path).read_text())
items = [
    SimpleNamespace(nodeid=line)
    for line in Path(collection_path).read_text().splitlines()
    if line.startswith("tests/") and "::" in line
]
if not items or not fresh:
    raise SystemExit("Missing collected tests or timing measurements")


def slowest(weights: dict[str, float]) -> float:
    shards = Algorithms.duration_based_chunks.value(int(count), items, weights)
    return max(sum(fresh.get(item.nodeid, 0) for item in shard.selected) for shard in shards)


before = slowest(old)
after = slowest(fresh)
print(f"Predicted longest shard with committed timings {before:.1f}s, with refreshed timings {after:.1f}s.")
print(f"refresh={'true' if before - after >= 10 else 'false'}")
