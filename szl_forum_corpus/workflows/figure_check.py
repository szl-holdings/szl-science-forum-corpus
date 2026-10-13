"""Build 3: bounded canonical CSV-to-SVG figure package checker.

Wraps the pinned SZL figure-data contract (``kernels/figure_data_contract.py``). Only the kernel's
canonical scatter/line format is admitted. The kernel decides pass or fail; this module keeps
bundles immutable, records each check as its own resource and adds a structural diff so a reviewer
can see which row or label differs. A pass means the canonical data-to-render checks passed; it is
not scientific verification, journal compliance or publication approval.
"""

from __future__ import annotations

from decimal import localcontext
import xml.etree.ElementTree as ET

from .. import kernels
from .contracts import (EVIDENCE_KINDS, SCHEMA_VERSION, WorkflowError, bounded_text, choice,
                        exact_fields, fail, identifier, parse_json, typed_digest, utc_now)
from .permissions import require
from .store import Context, Store, Transaction, Write

BUNDLE = "figure_bundle"
CHECK = "figure_check"
KERNEL = "figure_data_contract.py"
ADAPTER_REVISION = "figure-check-adapter.v1"
ROLES = ("spec", "data", "svg", "sidecar")
LIMITS = {"spec": 16384, "data": 262144, "svg": 2 * 1024 * 1024, "sidecar": 2 * 1024 * 1024}
MAX_DIFFERENCES = 50
SVG = "{http://www.w3.org/2000/svg}"
ALLOWED_ELEMENTS = ("svg", "line", "g", "polyline", "circle", "text")
PASS_STATEMENT = "Canonical data-to-render checks passed."
FAIL_STATEMENT = "Canonical data-to-render checks failed."
LIMITATIONS = (
    "n is the number of plotted rows, not the number of independent experimental units.",
    "No independent-n inference, SD/SEM or error-bar interpretation, journal compliance, "
    "arbitrary PNG/PDF/clinical-image inspection, chemical-unit truth or image forensics.",
    "Equivalent SVG reformattings are unsupported by design and fail the canonical check.",
    "The renderer and checker share arithmetic, so this is not independent numerical replication.",
    "A self-consistent replacement of the whole bundle can evade these checks; digests do not "
    "prove that the source data are true.",
)


def _kernel():
    try:
        return kernels.load(KERNEL)
    except kernels.KernelUnavailable as error:
        raise WorkflowError("KERNEL_UNAVAILABLE", f"pinned figure-data kernel is {error.code}")


def _load(kernel, raw_spec: bytes, raw_data: bytes):
    """Run the kernel's own loader on exact bytes. No path is ever read."""

    with localcontext() as context:
        context.prec = 50
        return kernel.load_contract(None, "spec.json", raw_spec=raw_spec, raw_data=raw_data)


def _members(value: object) -> dict[str, bytes]:
    if not isinstance(value, list) or not 2 <= len(value) <= len(ROLES):
        fail("INVALID_INPUT", "members must list 2..4 bundle members", "members")
    members: dict[str, bytes] = {}
    for number, member in enumerate(value):
        where = f"members[{number}]"
        exact_fields(member, {"role", "content"}, where=where)
        role = choice(member["role"], ROLES, f"{where}.role")
        if role in members:
            fail("INVALID_INPUT", "a bundle member role appears twice", f"{where}.role")
        content = member["content"]
        if not isinstance(content, str):
            fail("INVALID_INPUT", "member content must be text", f"{where}.content")
        raw = content.encode("utf-8", "surrogatepass")
        if len(raw) > LIMITS[role]:
            fail("TOO_LARGE", f"the {role} member exceeds {LIMITS[role]} bytes", f"{where}.content")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            fail("INVALID_INPUT", "member content is not valid Unicode", f"{where}.content")
        members[role] = raw
    if not {"spec", "data"} <= set(members):
        fail("MISSING_FIELD", "a bundle needs spec and data members", "members")
    if ("svg" in members) != ("sidecar" in members):
        fail("INVALID_INPUT", "supply both the SVG and its sidecar, or neither", "members")
    return members


def _contract_state(kernel, members: dict[str, bytes]) -> tuple[str, dict | None, str | None]:
    """LOADED, or CAPTION_MISMATCH (admitted but never renderable); anything else is refused."""

    try:
        _spec, _points, summary, _, _ = _load(kernel, members["spec"], members["data"])
    except kernel.ContractError as error:
        if error.status == "MISMATCH":
            return "CAPTION_MISMATCH", None, str(error)
        fail("INVALID_INPUT", f"figure-data contract refused the input: {error}", "members")
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError) as error:
        fail("INVALID_INPUT", f"figure-data contract refused the input: {type(error).__name__}",
             "members")
    return "LOADED", summary, None


def _inventory(tx: Transaction, members: dict[str, bytes]) -> dict:
    return {role: {"blob": tx.put_blob(raw, f"figure_{role}"), "size": len(raw),
                   "digest": typed_digest(raw, f"submitted {role} member bytes", "none")}
            for role, raw in sorted(members.items())}


def _bundle(context: Context, bundle_id: str, evidence_kind: str, provenance: str,
            inventory: dict, state: str, summary: dict | None, reason: str | None,
            derived_from: str | None, origin: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "workflow_kind": BUNDLE, "bundle_id": bundle_id,
        "created_by": context.principal_id, "evidence_kind": evidence_kind,
        "provenance": provenance, "origin": origin, "derived_from": derived_from,
        "inventory": inventory, "contract_state": state, "contract_reason": reason,
        "summary": summary,
        "kernel_binding": dict(kernels.binding(KERNEL), adapter_revision=ADAPTER_REVISION),
        "latest_check": None,
        "limitations": list(LIMITATIONS),
    }


def register_bundle(store: Store, context: Context, request_id: str, payload: dict) -> dict:
    """Admit one closed-inventory bundle from member bytes. Supplied filenames are never paths."""

    require(context, "register")
    exact_fields(payload, {"bundle_id", "evidence_kind", "provenance", "members"}, {"derived_from"})
    bundle_id = identifier(payload["bundle_id"], "bundle_id")
    evidence_kind = choice(payload["evidence_kind"], EVIDENCE_KINDS, "evidence_kind")
    provenance = bounded_text(payload["provenance"], "provenance", maximum=512)
    derived_from = payload.get("derived_from")
    if derived_from is not None:
        identifier(derived_from, "derived_from")
        store.get(context, BUNDLE, derived_from)
    members = _members(payload["members"])
    kernel = _kernel()
    state, summary, reason = _contract_state(kernel, members)

    def apply(tx: Transaction):
        projection = _bundle(context, bundle_id, evidence_kind, provenance, _inventory(tx, members),
                             state, summary, reason, derived_from, "submitted")
        return ([Write(bundle_id, BUNDLE, "bundle_registered", projection,
                       {"roles": sorted(members), "contract_state": state,
                        "digests": {role: item["digest"]["value"]
                                    for role, item in projection["inventory"].items()}})],
                {"bundle_id": bundle_id, "contract_state": state})

    return store.command(context, request_id, "figure.register_bundle", {"payload": payload},
                         {bundle_id: None}, apply)


def render_bundle(store: Store, context: Context, request_id: str, bundle_id: str,
                  new_bundle_id: str) -> dict:
    """Render the canonical SVG and sidecar from admitted CSV into a new candidate bundle."""

    require(context, "compute")
    identifier(new_bundle_id, "new_bundle_id")
    kernel = _kernel()
    source = store.get(context, BUNDLE, bundle_id)
    if source["contract_state"] != "LOADED":
        fail("INVALID_INPUT", "the caption assertions disagree with the CSV; nothing was rendered",
             "bundle_id")
    raw_spec = store.blob(source["inventory"]["spec"]["blob"])
    raw_data = store.blob(source["inventory"]["data"]["blob"])
    try:
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            spec, points, summary, _, _ = kernel.load_contract(None, "spec.json", raw_spec=raw_spec,
                                                               raw_data=raw_data)
            svg = kernel.make_svg(spec, points, summary)
            plotted = kernel.encoded(kernel.sidecar(spec, points, summary))
            kernel.inspect_svg(svg, spec, points, summary)
    except kernel.ContractError as error:
        fail("INVALID_INPUT", f"figure-data contract refused the render: {error}", "bundle_id")
    members = {"spec": raw_spec, "data": raw_data, "svg": svg, "sidecar": plotted}

    def apply(tx: Transaction):
        projection = _bundle(context, new_bundle_id, source["evidence_kind"], source["provenance"],
                             _inventory(tx, members), "LOADED", summary, None, bundle_id,
                             "rendered_by_pinned_kernel")
        return ([Write(new_bundle_id, BUNDLE, "bundle_rendered", projection,
                       {"source_bundle": bundle_id,
                        "digests": {role: item["digest"]["value"]
                                    for role, item in projection["inventory"].items()}})],
                {"bundle_id": new_bundle_id, "points": len(points)})

    return store.command(context, request_id, "figure.render", {"bundle_id": bundle_id,
                                                                "new_bundle_id": new_bundle_id},
                         {new_bundle_id: None}, apply)


# ---- localization -------------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag[len(SVG):] if tag.startswith(SVG) else tag


def _svg_differences(raw: bytes, canonical: bytes) -> list[dict]:
    """A structural diff against the kernel's canonical SVG, used only to locate a mismatch."""

    if b"<!" in raw or b"<?" in raw:
        return [{"scope": "svg.document", "issue": "declaration_or_processing_instruction"}]
    try:
        observed = ET.fromstring(raw)
    except ET.ParseError:
        return [{"scope": "svg.document", "issue": "malformed_xml"}]
    expected = ET.fromstring(canonical)
    differences: list[dict] = []
    for element in observed.iter():
        if _local(element.tag) not in ALLOWED_ELEMENTS or _has_reference(element):
            differences.append(_unexpected(element))
    if observed.tag != expected.tag or observed.attrib != expected.attrib:
        differences.append({"scope": "svg.root", "issue": "root_differs"})
    obs_children, exp_children = list(observed), list(expected)
    obs_by = {}
    for child in obs_children:
        if _local(child.tag) not in ("g", "text"):
            continue
        key = child.get("id")
        if key in obs_by or key not in ("series", "x-unit", "y-unit", "caption"):
            differences.append({"scope": "svg.element", "issue": "unexpected_or_duplicate_id",
                                "element": _local(child.tag)})
        else:
            obs_by[key] = child
    exp_lines = [child for child in exp_children if _local(child.tag) == "line"]
    obs_lines = [child for child in obs_children if _local(child.tag) == "line"]
    if [line.attrib for line in obs_lines] != [line.attrib for line in exp_lines]:
        differences.append({"scope": "svg.axis", "issue": "axis_lines_differ"})
    exp_group = next(child for child in exp_children if child.get("id") == "series")
    obs_group = obs_by.get("series")
    if obs_group is None:
        differences.append({"scope": "svg.series", "issue": "series_missing"})
    else:
        if obs_group.attrib != exp_group.attrib:
            differences.append({"scope": "svg.series", "field": "data-kind",
                                "expected": exp_group.get("data-kind"),
                                "observed": obs_group.get("data-kind")})
        differences.extend(_series_differences(list(obs_group), list(exp_group)))
    for label in ("x-unit", "y-unit", "caption"):
        exp_label = next(child for child in exp_children if child.get("id") == label)
        obs_label = obs_by.get(label)
        if obs_label is None:
            differences.append({"scope": "svg.label", "locator": {"label": label},
                                "issue": "label_missing"})
        elif obs_label.text != exp_label.text or obs_label.attrib != exp_label.attrib:
            differences.append({"scope": "svg.label", "locator": {"label": label}, "field": "text",
                                "expected": exp_label.text, "observed": obs_label.text})
    if not differences and raw != canonical:
        differences.append({"scope": "svg.document", "issue": "non_canonical_formatting"})
    return differences


def _has_reference(element) -> bool:
    return any(name == "href" or name.endswith("}href") for name in element.attrib)


def _unexpected(element) -> dict:
    return {"scope": "svg.element", "element": _local(element.tag),
            "issue": "external_reference" if _has_reference(element) else "unexpected_element"}


def _series_differences(observed: list, expected: list) -> list[dict]:
    differences = []
    exp_line = [mark for mark in expected if _local(mark.tag) == "polyline"]
    obs_line = [mark for mark in observed if _local(mark.tag) == "polyline"]
    if exp_line or obs_line:
        exp_points = exp_line[0].get("points", "").split() if exp_line else []
        obs_points = obs_line[0].get("points", "").split() if obs_line else []
        if not obs_line or not exp_line:
            differences.append({"scope": "svg.series", "issue": "polyline_presence_differs"})
        elif obs_points != exp_points or obs_line[0].attrib != exp_line[0].attrib:
            differences.append({"scope": "svg.series", "issue": "polyline_differs",
                                "expected_vertices": len(exp_points),
                                "observed_vertices": len(obs_points)})
    exp_marks = [mark for mark in expected if _local(mark.tag) == "circle"]
    obs_marks = {}
    order = []
    for mark in observed:
        if _local(mark.tag) != "circle":
            continue  # polylines are compared above; foreign elements are reported once, globally
        row = mark.get("data-row")
        if row is None or row in obs_marks:
            differences.append({"scope": "svg.point", "issue": "point_without_unique_row"})
            continue
        obs_marks[row] = mark
        order.append(row)
    expected_rows = [mark.get("data-row") for mark in exp_marks]
    for mark in exp_marks:
        row = mark.get("data-row")
        seen = obs_marks.get(row)
        if seen is None:
            differences.append({"scope": "svg.point", "locator": {"row": row},
                                "issue": "point_missing"})
            continue
        for field in ("cx", "cy", "r", "fill"):
            if seen.get(field) != mark.get(field):
                differences.append({"scope": "svg.point", "locator": {"row": row}, "field": field,
                                    "expected": mark.get(field), "observed": seen.get(field)})
    for row in order:
        if row not in expected_rows:
            differences.append({"scope": "svg.point", "locator": {"row": row},
                                "issue": "point_unexpected"})
    present = [row for row in order if row in expected_rows]
    if present != [row for row in expected_rows if row in obs_marks]:
        differences.append({"scope": "svg.point", "issue": "point_order_differs"})
    return differences


def _sidecar_differences(value: object, expected: dict) -> list[dict]:
    if not isinstance(value, dict):
        return [{"scope": "sidecar.document", "issue": "not_an_object"}]
    differences = []
    for key in sorted(set(value) | set(expected)):
        if key not in expected:
            differences.append({"scope": "sidecar.field", "locator": {"field": key},
                                "issue": "unexpected_field"})
        elif key not in value:
            differences.append({"scope": "sidecar.field", "locator": {"field": key},
                                "issue": "missing_field"})
        elif key == "summary" and isinstance(value[key], dict):
            for name in sorted(set(value[key]) | set(expected[key])):
                if value[key].get(name) != expected[key].get(name):
                    differences.append({"scope": "sidecar.summary", "locator": {"field": name},
                                        "expected": expected[key].get(name),
                                        "observed": value[key].get(name)})
        elif key == "points" and isinstance(value[key], list):
            exp_points = {point["id"]: point for point in expected[key]}
            seen = []
            for point in value[key]:
                row = point.get("id") if isinstance(point, dict) else None
                if not isinstance(row, str) or row not in exp_points or row in seen:
                    differences.append({"scope": "sidecar.point", "issue": "point_unexpected",
                                        "locator": {"row": row if isinstance(row, str) else None}})
                    continue
                seen.append(row)
                for field in sorted(set(point) | {"id", "x", "y"}):
                    if point.get(field) != exp_points[row].get(field):
                        differences.append({"scope": "sidecar.point", "locator": {"row": row},
                                            "field": field, "expected": exp_points[row].get(field),
                                            "observed": point.get(field)})
            for row in exp_points:
                if row not in seen:
                    differences.append({"scope": "sidecar.point", "locator": {"row": row},
                                        "issue": "point_missing"})
            if seen != [row for row in exp_points if row in seen]:
                differences.append({"scope": "sidecar.point", "issue": "point_order_differs"})
        elif value[key] != expected[key]:
            differences.append({"scope": "sidecar.field", "locator": {"field": key},
                                "issue": "value_differs"})
    return differences


def _bounded(differences: list[dict]) -> dict:
    return {"differences": differences[:MAX_DIFFERENCES],
            "truncated": len(differences) > MAX_DIFFERENCES}


# ---- checks -------------------------------------------------------------------------------------

def evaluate(kernel, members: dict[str, bytes]) -> dict:
    """Check exact member bytes with the pinned kernel. Mismatches are results, not errors."""

    mismatches: list[dict] = []
    canonical_format = "not_evaluated"
    try:
        spec, points, summary, _, _ = _load(kernel, members["spec"], members["data"])
    except kernel.ContractError as error:
        mismatches.append({"member": "spec.caption", "kernel_operation": "load_contract",
                           "kernel_reason": str(error), "kernel_status": error.status,
                           "localization": "unavailable",
                           "localization_note": "the kernel reports only that the caption "
                                                "assertions disagree, not which one"})
        points = None
    if points is not None:
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            expected_sidecar = kernel.sidecar(spec, points, summary)
            canonical_svg = kernel.make_svg(spec, points, summary)
            try:
                supplied = kernel.read_json(members["sidecar"])
            except (kernel.ContractError, ValueError, UnicodeError):
                supplied = None
                sidecar_parse = False
            else:
                sidecar_parse = True
            if not sidecar_parse or supplied != expected_sidecar:
                mismatches.append(dict(
                    {"member": "sidecar", "kernel_operation": "sidecar",
                     "kernel_reason": "plotted data differs", "kernel_status": "MISMATCH",
                     "localization": "adapter_structural_diff"},
                    **_bounded(_sidecar_differences(supplied, expected_sidecar) if sidecar_parse
                               else [{"scope": "sidecar.document", "issue": "malformed_json"}])))
            try:
                kernel.inspect_svg(members["svg"], spec, points, summary)
                canonical_format = "canonical"
            except kernel.ContractError as error:
                differences = _svg_differences(members["svg"], canonical_svg)
                canonical_format = ("unsupported_formatting" if any(
                    item.get("issue") == "non_canonical_formatting" for item in differences)
                    else "not_canonical")
                mismatches.append(dict(
                    {"member": "svg", "kernel_operation": "inspect_svg",
                     "kernel_reason": str(error), "kernel_status": error.status,
                     "localization": "adapter_structural_diff"}, **_bounded(differences)))
    passed = not mismatches
    return {
        "execution": "PASSED" if passed else "FAILED",
        "integrity": "match" if passed else "mismatch",
        "canonical_format": canonical_format,
        "statement": PASS_STATEMENT if passed else FAIL_STATEMENT,
        "plotted_rows": None if points is None else len(points),
        "mismatches": mismatches,
        "states": {"software_validity": "PASSED" if passed else "FAILED",
                   "source_authenticity": "NOT_ASSESSED",
                   "statistical_interpretation": "NOT_ASSESSED",
                   "human_publication_review": "NOT_PERFORMED"},
    }


def check_bundle(store: Store, context: Context, request_id: str, bundle_id: str,
                 expected_revision: int, check_id: str) -> dict:
    """Record one immutable check of a bundle's exact members and point the bundle at it."""

    require(context, "compute")
    identifier(check_id, "check_id")
    kernel = _kernel()

    def apply(tx: Transaction):
        bundle = tx.get(BUNDLE, bundle_id)
        if not {"svg", "sidecar"} <= set(bundle["inventory"]):
            fail("INVALID_INPUT", "render or supply the canonical SVG and sidecar before checking",
                 "bundle_id")
        members = {role: tx.get_blob(item["blob"]) for role, item in bundle["inventory"].items()}
        result = evaluate(kernel, members)
        now = utc_now()
        check = dict(result, **{
            "schema_version": SCHEMA_VERSION, "workflow_kind": CHECK, "check_id": check_id,
            "bundle_id": bundle_id, "bundle_revision": bundle["revision"], "checked_at": now,
            "checked_by": context.principal_id,
            "inventory": {role: item["digest"] for role, item in bundle["inventory"].items()},
            "kernel_binding": bundle["kernel_binding"], "limitations": list(LIMITATIONS),
        })
        projection = dict(bundle)
        projection.pop("revision")
        projection["latest_check"] = {"check_id": check_id, "execution": result["execution"],
                                      "checked_at": now}
        return ([Write(check_id, CHECK, "figure_checked", check,
                       {"bundle_id": bundle_id, "execution": result["execution"],
                        "mismatches": len(result["mismatches"])}),
                 Write(bundle_id, BUNDLE, "bundle_check_recorded", projection,
                       {"check_id": check_id, "execution": result["execution"]})],
                {"check_id": check_id, "execution": result["execution"]})

    return store.command(context, request_id, "figure.check",
                         {"bundle_id": bundle_id, "check_id": check_id},
                         {bundle_id: expected_revision, check_id: None}, apply)


def package(store: Store, context: Context, bundle_id: str) -> dict:
    """The closed-inventory private package for an authorized reader, with digests re-checked."""

    require(context, "read")
    bundle = store.get(context, BUNDLE, bundle_id)
    return {"bundle_id": bundle_id, "revision": bundle["revision"],
            "members": {role: {"digest": item["digest"],
                               "content": store.blob(item["blob"]).decode("utf-8")}
                        for role, item in sorted(bundle["inventory"].items())},
            "limitations": list(LIMITATIONS)}
