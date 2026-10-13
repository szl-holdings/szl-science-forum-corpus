"""Pinned SZL Skills kernels: byte-verified before use, never fetched or edited here.

Each kernel file is a byte-identical copy of an upstream szl-skills source at the commit in
SOURCES.json. A kernel whose bytes differ from its pin is reported BLOCKED and is never imported,
so a workflow cannot silently run code other than the reviewed kernel.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "SOURCES.json").read_text(encoding="utf-8"))
_LOADED: dict[str, ModuleType] = {}


class KernelUnavailable(RuntimeError):
    """A pinned kernel cannot be used; the workflow must report an unavailable capability."""

    def __init__(self, code: str, kernel: str):
        super().__init__(f"{code}: {kernel}")
        self.code = code
        self.kernel = kernel


def _observed_sha256(filename: str) -> str | None:
    path = ROOT / filename
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def binding(filename: str) -> dict:
    """Return the exact source binding a workflow records alongside a kernel result."""

    entry = MANIFEST["kernels"][filename]
    return {
        "repository": MANIFEST["repository"],
        "commit": MANIFEST["commit"],
        "upstream_path": entry["upstream_path"],
        "sha256": entry["sha256"],
    }


def load(filename: str) -> ModuleType:
    """Import a pinned kernel after checking its bytes against SOURCES.json."""

    if filename not in MANIFEST["kernels"]:
        raise KernelUnavailable("KERNEL_NOT_PINNED", filename)
    if filename in _LOADED:
        return _LOADED[filename]
    observed = _observed_sha256(filename)
    if observed is None:
        raise KernelUnavailable("KERNEL_SOURCE_MISSING", filename)
    if observed != MANIFEST["kernels"][filename]["sha256"]:
        raise KernelUnavailable("KERNEL_DIGEST_MISMATCH", filename)
    module = importlib.import_module(f"{__name__}.{filename[:-3]}")
    _LOADED[filename] = module
    return module


def capability_manifest() -> list[dict]:
    """Report every pinned kernel as AVAILABLE or BLOCKED, with expected and observed digests."""

    report = []
    for filename, entry in sorted(MANIFEST["kernels"].items()):
        observed = _observed_sha256(filename)
        report.append({
            "kernel": filename,
            **binding(filename),
            "observed_sha256": observed,
            "state": "AVAILABLE" if observed == entry["sha256"] else "BLOCKED",
            "operations": list(entry["operations"]),
        })
    return report
