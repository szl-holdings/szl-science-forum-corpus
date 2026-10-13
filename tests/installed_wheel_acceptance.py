"""Installed-wheel acceptance for the bundled science workflow fixtures.

This file is not collected by ``unittest discover`` (its name does not start with ``test``). CI runs
it in the ``installed-wheel`` job, and ``tests/test_workflow_packaging.py`` reuses its probe offline.

Driver mode (default):

1. copy the build inputs out of the checkout into a fresh temporary directory;
2. build the wheel there with pip (pip's default build isolation downloads setuptools);
3. check the wheel contents: every fixture file and pinned kernel file is present with the
   checkout's exact bytes, and no ``tests/`` path is shipped;
4. create a clean virtual environment in that temporary directory, outside the checkout, and
   install the wheel with ``--no-index --no-deps``;
5. copy this file next to it and run its probe with the environment's interpreter in isolated
   mode (``-I``) from the temporary directory, so the checkout can never be imported.

Probe mode (``--probe``): import the installed package, start the loopback workbench on its
default fixture path, load all three bundled fixtures over HTTP and compare the readbacks with
the installed hand-written expectations. It also checks that ``fixtures=None`` stays BLOCKED and
that an explicit ``fixtures=`` directory still works. ``--expect-blocked`` instead requires the
loader to be BLOCKED and to record nothing (the negative control for a layout without fixtures).

Exit status is 0 only when every check passed. Nothing here publishes, uploads or deploys.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlencode
import zipfile

BASE = "/operator/science"
API = "/api/science/v1"
FIXTURE_DIR = "szl_forum_corpus/workflows/fixtures"
FIXTURE_FILES = (
    "PROVENANCE.md",
    "change_recheck/baseline.json", "change_recheck/current.json",
    "change_recheck/expected.json", "change_recheck/later.json",
    "figure_check/data.csv", "figure_check/expected.json", "figure_check/spec.json",
    "paper_review/expected.json", "paper_review/records.json", "paper_review/reference.json",
)
KERNEL_FILES = ("SOURCES.json", "NOTICE", "paper_rescue.py", "research_change_impact.py",
                "figure_data_contract.py")
BUILD_INPUTS = ("pyproject.toml", "LICENSE", "README.md", "setup.py", "setup.cfg", "MANIFEST.in")


# ---- probe (runs inside the installed environment) -----------------------------------------------

class _Client:
    def __init__(self, port: int):
        self.port = port

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body or None, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def get_json(self, path: str):
        status, _, body = self.request("GET", path)
        return status, json.loads(body) if status == 200 else None


def probe(arguments: argparse.Namespace) -> int:
    if arguments.site:
        sys.path.insert(0, arguments.site)
    import szl_forum_corpus
    from szl_forum_corpus.web import workflow_routes as routes
    from szl_forum_corpus.workflows import paper_review
    from szl_forum_corpus.workflows.contracts import WorkflowError
    from szl_forum_corpus.workflows.store import Store

    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, detail: object = None) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    package = Path(szl_forum_corpus.__file__).resolve().parent
    checkout = Path(arguments.checkout).resolve() if arguments.checkout else None
    root = Path(arguments.site).resolve() if arguments.site else Path(sys.prefix).resolve()
    check("package_imported_from_install", package.is_relative_to(root), str(package))
    check("package_outside_checkout", checkout is None or not package.is_relative_to(checkout),
          str(package))
    default = Path(str(routes.DEFAULT_FIXTURES)).resolve()
    check("default_fixtures_inside_package", default.is_relative_to(package), str(default))

    work = Path(tempfile.mkdtemp(prefix="szl-wheel-probe-"))
    try:
        store = Store(work / "workflows.sqlite3")
        server = routes.LocalServer(store, 0)  # no fixtures argument: the default workbench path
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        try:
            client = _Client(server.server_port)
            origin = {"Origin": f"http://{server.app.host}",
                      "Content-Type": "application/x-www-form-urlencoded"}
            status, capabilities = client.get_json(f"{API}/capabilities")
            state = None if capabilities is None else capabilities["fixtures"]
            status, _, overview = client.request("GET", BASE)
            overview_text = overview.decode("utf-8")

            def load(which: str):
                body = urlencode({"csrf": server.app.csrf, "request_id": secrets.token_hex(16),
                                  "which": which}).encode("utf-8")
                status, headers, _ = client.request("POST", f"{BASE}/fixtures", body, origin)
                return status, headers.get("Location")

            if arguments.expect_blocked:
                check("capability_blocked", state == "BLOCKED", state)
                check("overview_reports_blocked",
                      "BLOCKED: the bundled synthetic fixtures are not present" in overview_text)
                check("load_refused", load("paper")[0] == 503)
                check("nothing_recorded", store.list(server.app.context, paper_review.KIND) == [])
            else:
                _available(check, client, server, store, load, routes, state, overview_text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        if not arguments.expect_blocked and server.app._fixtures_ready():
            _controls(check, work, routes, Store, WorkflowError, paper_review)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    ok = all(item["ok"] for item in checks.values())
    print(json.dumps({"ok": ok, "mode": "expect_blocked" if arguments.expect_blocked else "available",
                      "python": sys.version.split()[0], "package": str(package), "checks": checks},
                     indent=2, sort_keys=True))
    return 0 if ok else 1


def _available(check, client, server, store, load, routes, state, overview_text) -> None:
    fixtures = routes.DEFAULT_FIXTURES
    check("capability_available", state == "AVAILABLE", state)
    check("overview_offers_loader", "Load synthetic paper case" in overview_text)
    present = [name for name in FIXTURE_FILES
               if _child(fixtures, name).is_file()]
    check("fixture_files_installed", present == list(FIXTURE_FILES),
          sorted(set(FIXTURE_FILES) - set(present)))
    if present != list(FIXTURE_FILES):
        return  # nothing further can be read back; the failed checks above say why
    provenance = _child(fixtures, "PROVENANCE.md").read_text(encoding="utf-8")
    check("provenance_readable", "synthetic fixture" in provenance and "hand-authored" in provenance)

    for which, location in (("paper", f"{BASE}/paper-review/demo-paper"),
                            ("recheck", f"{BASE}/rechecks"), ("figure", f"{BASE}/figures")):
        status, where = load(which)
        check(f"load_{which}", (status, where) == (303, location), [status, where])

    # Paper review: the frozen comparison and summary match the hand-written expectations.
    expected = json.loads(_child(fixtures, "paper_review/expected.json").read_text(encoding="utf-8"))
    _, case = client.get_json(f"{API}/paper-cases/demo-paper")
    _, summary = client.get_json(f"{API}/paper-cases/demo-paper/summary")
    if case is None or summary is None:
        check("paper_readback", False, "paper case or summary not readable")
    else:
        findings = {key: value["kernel_result"]
                    for key, value in case["comparison"]["findings"].items()}
        check("paper_kernel_results", findings == expected["kernel_results"])
        check("paper_summary", all(summary[key] == expected[key] for key in (
            "initial_extraction", "initial_locator", "initial_discrepancies",
            "unresolved_reference_items")) and summary["declared_reference_items"]
            == expected["denominator"])
        check("paper_provenance_label", case["provenance"] == routes.FIXTURE_PROVENANCE,
              case["provenance"])

    # Evidence rechecks: tasks, queue order and known paths match the expectations.
    expected = json.loads(_child(fixtures, "change_recheck/expected.json")
                          .read_text(encoding="utf-8"))["first"]
    _, queue = client.get_json(f"{API}/recheck-tasks")
    if queue is None:
        check("recheck_readback", False, "queue not readable")
    else:
        tasks = {task["claim_id"]: task for task in queue["tasks"]}
        check("recheck_task_claims", sorted(tasks) == expected["task_claims"], sorted(tasks))
        check("recheck_queue_order", [task["claim_id"] for task in queue["tasks"]]
              == expected["queue_order"])
        check("recheck_paths", all(tasks[claim]["affected_paths"] == expected["affected_paths"][claim]
                                   for claim in expected["task_claims"] if claim in tasks))
        check("recheck_complete_coverage", queue["coverage_warning"] is None)

    # Figure checks: the rendered version passes and the edited copy fails.
    expected = json.loads(_child(fixtures, "figure_check/expected.json").read_text(encoding="utf-8"))
    _, passed = client.get_json(f"{API}/figure-checks/demo-figure.r1.check")
    _, failed = client.get_json(f"{API}/figure-checks/demo-figure.edited.check")
    check("figure_rendered_passes", passed is not None and passed["execution"] == "PASSED"
          and passed["plotted_rows"] == expected["plotted_rows"])
    check("figure_edited_fails", failed is not None and failed["execution"] == "FAILED")


def _controls(check, work, routes, Store, WorkflowError, paper_review) -> None:
    # fixtures=None is reported BLOCKED and records nothing.
    store = Store(work / "none.sqlite3")
    app = routes.WorkflowApp(store, "127.0.0.1:1", fixtures=None)
    try:
        app.load_fixture("paper")
        refused = None
    except WorkflowError as error:
        refused = error.code
    check("none_control_blocked", not app._fixtures_ready() and refused == "CAPABILITY_UNAVAILABLE"
          and store.list(app.context, paper_review.KIND) == [], refused)
    # An explicit fixtures= directory still overrides the bundled default.
    override = work / "override"
    shutil.copytree(Path(str(routes.DEFAULT_FIXTURES)), override)
    store = Store(work / "override.sqlite3")
    app = routes.WorkflowApp(store, "127.0.0.1:1", fixtures=override)
    location = app.load_fixture("paper")
    case = store.get(app.context, paper_review.KIND, "demo-paper")
    check("explicit_override_loads", location == f"{BASE}/paper-review/demo-paper"
          and paper_review.summary(case)["initial_discrepancies"] == 5)


def _child(root, relative: str):
    location = root
    for part in relative.split("/"):
        location = location.joinpath(part)
    return location


# ---- driver (runs from the checkout) -------------------------------------------------------------

def _run(command: list[str], **options) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    environment["PYTHONUTF8"] = "1"
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, env=environment, text=True, capture_output=True, **options)
    sys.stdout.write(result.stdout)
    sys.stdout.write(result.stderr)
    sys.stdout.flush()
    if result.returncode != 0:
        raise SystemExit(f"command failed with exit status {result.returncode}")
    return result


def inspect_wheel(wheel: Path, checkout: Path) -> dict:
    expected = {f"{FIXTURE_DIR}/{name}": checkout / FIXTURE_DIR / name for name in FIXTURE_FILES}
    expected.update({f"szl_forum_corpus/kernels/{name}": checkout / "szl_forum_corpus" / "kernels" / name
                     for name in KERNEL_FILES})
    on_disk = sorted(path.relative_to(checkout).as_posix()
                     for path in (checkout / FIXTURE_DIR).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = sorted(name for name in expected if name not in names)
        differing = sorted(name for name in expected
                           if name in names and archive.read(name) != expected[name].read_bytes())
    shipped_fixtures = sorted(name for name in names if name.startswith(FIXTURE_DIR + "/"))
    return {
        "wheel": wheel.name, "entries": len(names), "fixture_entries": shipped_fixtures,
        "missing": missing, "differing": differing,
        "unlisted_checkout_fixtures": sorted(set(on_disk) - set(shipped_fixtures)),
        "tests_paths_shipped": sorted(name for name in names if name.startswith("tests/")),
        "ok": not missing and not differing and set(on_disk) == set(shipped_fixtures)
              and not any(name.startswith("tests/") for name in names),
    }


def driver(arguments: argparse.Namespace) -> int:
    checkout = Path(__file__).resolve().parents[1]
    work = Path(tempfile.mkdtemp(prefix="szl-installed-wheel-")).resolve()
    if work.is_relative_to(checkout):
        raise SystemExit("the temporary directory must be outside the checkout")
    try:
        if arguments.wheel:
            wheel = Path(arguments.wheel).resolve()
        else:
            source = work / "src"
            shutil.copytree(checkout / "szl_forum_corpus", source / "szl_forum_corpus",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in BUILD_INPUTS:
                if (checkout / name).is_file():
                    shutil.copy2(checkout / name, source / name)
            command = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-cache-dir",
                       "--wheel-dir", str(work / "dist"), str(source)]
            if arguments.no_build_isolation:
                command.insert(4, "--no-build-isolation")
            _run(command)
            wheels = sorted((work / "dist").glob("szl_science_forum_corpus-*.whl"))
            if len(wheels) != 1:
                raise SystemExit(f"expected exactly one built wheel, found {len(wheels)}")
            wheel = wheels[0]
        contents = inspect_wheel(wheel, checkout)
        print(json.dumps({"wheel_contents": contents}, indent=2, sort_keys=True), flush=True)

        environment = work / "venv"
        _run([sys.executable, "-m", "venv", str(environment)])
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run([str(python), "-m", "pip", "install", "--no-deps", "--no-index", "--no-cache-dir",
              "--disable-pip-version-check", str(wheel)])
        shutil.copy2(Path(__file__), work / "probe.py")
        result = subprocess.run([str(python), "-I", "-B", str(work / "probe.py"), "--probe",
                                 "--checkout", str(checkout)], cwd=work, text=True,
                                capture_output=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        report = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {"ok": False}
        ok = contents["ok"] and result.returncode == 0 and report.get("ok") is True
        print(f"installed-wheel acceptance: {'PASSED' if ok else 'FAILED'} "
              f"({sum(item['ok'] for item in report.get('checks', {}).values())}/"
              f"{len(report.get('checks', {}))} probe checks; wheel contents "
              f"{'ok' if contents['ok'] else 'NOT ok'}; temporary directory {work})", flush=True)
        return 0 if ok else 1
    finally:
        if arguments.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expect-blocked", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--site", help=argparse.SUPPRESS)
    parser.add_argument("--checkout", help=argparse.SUPPRESS)
    parser.add_argument("--wheel", help="test this built wheel instead of building one")
    parser.add_argument("--no-build-isolation", action="store_true",
                        help="build with the current environment's setuptools (offline)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    arguments = parser.parse_args(argv)
    return probe(arguments) if arguments.probe else driver(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
