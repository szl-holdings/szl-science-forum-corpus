"""Packaging: the bundled workflow fixtures ship as package data and load from an install layout.

These checks are offline. The real wheel build, clean virtual environment and HTTP load run in
the CI ``installed-wheel`` job (``tests/installed_wheel_acceptance.py``); here the same probe runs
against a package copy assembled from the package-data declaration, outside the checkout.
"""

import json
from importlib import resources
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

import szl_forum_corpus
from szl_forum_corpus.web import workflow_routes as routes

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(szl_forum_corpus.__file__).resolve().parent
FIXTURES = PACKAGE / "workflows" / "fixtures"
ACCEPTANCE = Path(__file__).resolve().parent / "installed_wheel_acceptance.py"
# The declaration merged in #31: kernel metadata only, so an installed workbench had no fixtures.
MERGED_31_PACKAGE_DATA = {"szl_forum_corpus.kernels": ["SOURCES.json", "NOTICE"]}


def declared_package_data() -> dict[str, list[str]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]


def shipped_files(package_data: dict[str, list[str]]) -> set[Path]:
    """What setuptools puts in the wheel: modules of regular packages plus declared data globs."""

    files = set()
    for init in PACKAGE.rglob("__init__.py"):
        if "__pycache__" not in init.parts:
            files.update(init.parent.glob("*.py"))
    for package, patterns in package_data.items():
        directory = ROOT.joinpath(*package.split("."))
        for pattern in patterns:
            files.update(path for path in directory.glob(pattern) if path.is_file())
    return files


def fixture_files() -> set[Path]:
    return {path for path in FIXTURES.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts}


class PackagingTests(unittest.TestCase):
    def test_default_fixture_path_is_package_data_not_the_checkout(self):
        self.assertEqual(Path(str(routes.DEFAULT_FIXTURES)),
                         Path(str(resources.files("szl_forum_corpus.workflows") / "fixtures")))
        self.assertTrue(Path(str(routes.DEFAULT_FIXTURES)).resolve().is_relative_to(PACKAGE))
        self.assertTrue(routes.DEFAULT_FIXTURES.joinpath("PROVENANCE.md").is_file())
        # One copy only: the old checkout-relative location is gone, so nothing can drift.
        self.assertFalse((ROOT / "tests" / "fixtures" / "science_workflows").exists())
        self.assertEqual(routes.FIXTURE_PROVENANCE,
                         (FIXTURES / "PROVENANCE.md").relative_to(ROOT).as_posix())

    def test_package_data_declares_every_bundled_fixture(self):
        files = fixture_files()
        self.assertEqual(len(files), 11)
        self.assertEqual(files - shipped_files(declared_package_data()), set())

    def test_merged_declaration_would_ship_no_fixture(self):
        # Negative control: the same coverage check reports the #31 declaration as shipping none.
        self.assertEqual(fixture_files() & shipped_files(MERGED_31_PACKAGE_DATA), set())

    def run_probe(self, package_data, *extra):
        with tempfile.TemporaryDirectory(prefix="szl-layout-") as directory:
            work = Path(directory).resolve()
            self.assertFalse(work.is_relative_to(ROOT))
            site = work / "site"
            for path in shipped_files(package_data):
                target = site / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            shutil.copy2(ACCEPTANCE, work / "probe.py")
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(work / "probe.py"), "--probe", "--site", str(site),
                 "--checkout", str(ROOT), *extra],
                cwd=work, capture_output=True, text=True, timeout=300)
        report = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
        return result, report

    def test_layout_outside_checkout_loads_all_three_fixtures_through_the_default_path(self):
        result, report = self.run_probe(declared_package_data())
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
        self.assertTrue(report["ok"])
        for name in ("package_imported_from_install", "package_outside_checkout",
                     "default_fixtures_inside_package", "capability_available", "load_paper",
                     "load_recheck", "load_figure", "paper_summary", "paper_kernel_results",
                     "recheck_task_claims", "recheck_paths", "figure_rendered_passes",
                     "figure_edited_fails", "provenance_readable", "none_control_blocked",
                     "explicit_override_loads"):
            self.assertTrue(report["checks"][name]["ok"], name)

    def test_layout_without_declared_fixtures_is_blocked_and_records_nothing(self):
        result, report = self.run_probe(MERGED_31_PACKAGE_DATA, "--expect-blocked")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
        self.assertTrue(all(report["checks"][name]["ok"] for name in (
            "capability_blocked", "overview_reports_blocked", "load_refused", "nothing_recorded")))
        # The same probe in available mode fails on that layout, so the positive test can fail.
        result, report = self.run_probe(MERGED_31_PACKAGE_DATA)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["checks"]["capability_available"]["ok"])


if __name__ == "__main__":
    unittest.main()
