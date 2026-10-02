"""Fail closed if the committed public corpus differs from its reviewed inputs."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from szl_forum_corpus.cli import _load_opportunities, build, read_records


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset"
INPUT = ROOT / "examples" / "operator_topics.jsonl"


def main() -> int:
    manifest = json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))
    expected = build(
        read_records(INPUT), _load_opportunities(ROOT / "opportunities.json"), manifest["as_of"]
    )
    actual_names = {path.name for path in DATASET.iterdir() if path.is_file() and path.name != "README.md"}
    if actual_names != set(expected):
        print("dataset file set differs from deterministic build", file=sys.stderr)
        return 2
    for name, content in expected.items():
        if (DATASET / name).read_bytes() != content:
            print(f"dataset byte mismatch: {name}", file=sys.stderr)
            return 2
    print(f"verified {len(expected)} public projection files at as_of={manifest['as_of']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
