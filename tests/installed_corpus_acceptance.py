"""Build/inspect/install a real wheel and run the corpus CLI outside the checkout.

No forum, provider, server or model calls. Like installed_wheel_acceptance.py,
the driver creates a fresh venv and installs only the locally built wheel.
Use --no-build-isolation when setuptools/wheel are already installed offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_SOURCES = {
    "szl_forum_corpus/data/opportunities.json": ROOT / "opportunities.json",
    "szl_forum_corpus/data/record-v2.schema.json": ROOT / "schema/record-v2.schema.json",
}


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
    }
    return subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True,
        check=True, timeout=180,
    )


def synthetic_record() -> dict:
    return {
        "source_id": "ai4science:900001:1",
        "source_url": "https://ai4science.discourse.group/t/synthetic-offline-fixture/900001",
        "topic_id": 900001, "post_number": 1, "access": "members_only",
        "publication_rights": "operator_authorized", "source_content_license": None,
        "rights_evidence": "Original synthetic offline fixture; not an actual forum observation",
        "public_projection_approved": True, "title": "Synthetic resource acceptance fixture",
        "summary": "Original synthetic metadata for offline packaging validation only.",
        "need_ids": ["artifact_replay"], "review_state": "operator_labeled",
        "attribution": "synthetic_operator", "observed_at": "2026-11-02", "posted_at": None,
    }


def generated_files(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="szl-installed-corpus-")).resolve()
    try:
        assert not work.is_relative_to(ROOT)
        source = work / "src"
        shutil.copytree(ROOT / "szl_forum_corpus", source / "szl_forum_corpus",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "LICENSE", "README.md"):
            shutil.copy2(ROOT / name, source / name)
        command = [
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-cache-dir",
            "--wheel-dir", str(work / "dist"),
        ]
        if args.no_build_isolation:
            command.append("--no-build-isolation")
        run([*command, str(source)], work)
        wheels = list((work / "dist").glob("*.whl"))
        assert len(wheels) == 1
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert not any(name.startswith("tests/") for name in names)
            for name, reviewed in RESOURCE_SOURCES.items():
                assert archive.read(name) == reviewed.read_bytes(), name
            resource_hashes = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in RESOURCE_SOURCES
            }
        environment = work / "venv"
        run([sys.executable, "-m", "venv", str(environment)], work)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([
            str(python), "-m", "pip", "install", "--no-deps", "--no-index",
            "--no-cache-dir", "--disable-pip-version-check", str(wheel),
        ], work)
        # Remove even the build staging package: the isolated interpreter must
        # use the installed wheel, with neither staging nor checkout on its path.
        shutil.rmtree(source)
        record = work / "synthetic.jsonl"
        record.write_text(json.dumps(synthetic_record()) + "\n", encoding="utf-8")
        build_args = [
            "-B", "-m", "szl_forum_corpus", "build", str(record), "--as-of", "2026-11-02",
        ]
        run([sys.executable, *build_args, "--out", str(work / "checkout-output")], ROOT)
        run([str(python), "-I", *build_args, "--out", str(work / "installed-output")], work)
        assert generated_files(work / "installed-output") == generated_files(work / "checkout-output")
        # An explicit custom opportunities path remains supported.
        explicit = work / "opportunities.json"
        explicit.write_bytes(RESOURCE_SOURCES["szl_forum_corpus/data/opportunities.json"].read_bytes())
        run([
            str(python), "-I", *build_args, "--out", str(work / "explicit-output"),
            "--opportunities", str(explicit),
        ], work)
        assert generated_files(work / "explicit-output") == generated_files(work / "installed-output")
        probe = """
from importlib import resources
import hashlib, json
from pathlib import Path
import sys
import szl_forum_corpus
from szl_forum_corpus.admission import audit_corpus_admission
from szl_forum_corpus.release_validation import validate_release_contract
root = Path(sys.argv[1])
manifest_raw = (root / 'manifest.json').read_bytes()
manifest = json.loads(manifest_raw)
validate_release_contract(manifest, (root / 'sources.public.jsonl').read_bytes())
assert manifest['model_training_authorized'] is False
assert 'model_training' in manifest['release_contract']['excluded_purposes']
receipt = audit_corpus_admission(root, manifest_sha256=hashlib.sha256(manifest_raw).hexdigest())
assert receipt['state'] == 'INVALID' and receipt['admission_passed'] is False
print(json.dumps({'package': szl_forum_corpus.__file__,
                 'admission': receipt['state'],
                 'resource_sizes': {name: len(resources.files('szl_forum_corpus').joinpath('data', name).read_bytes())
                                    for name in ('opportunities.json', 'record-v2.schema.json')}}))
"""
        inspected = json.loads(run([
            str(python), "-I", "-B", "-c", probe, str(work / "installed-output"),
        ], work).stdout)
        assert Path(inspected["package"]).resolve().is_relative_to(environment)
        print(json.dumps({
            "ok": True, "wheel": str(wheel), "wheel_entries": len(names),
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "resource_sha256": resource_hashes,
            "default_and_explicit_builds_equal_checkout": True,
            "generated_files": sorted(generated_files(work / "installed-output")),
            "isolated_probe": inspected,
        }, indent=2, sort_keys=True))
        return 0
    except (OSError, subprocess.CalledProcessError, AssertionError, ValueError) as exc:
        print(f"installed-corpus acceptance failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr, file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"kept {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
