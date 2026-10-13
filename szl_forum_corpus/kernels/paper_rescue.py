# SPDX-License-Identifier: Apache-2.0
"""Score declared PDF evidence records against a separate adjudicated reference.

This module does not read PDF pixels, extract text, or establish scientific truth.
"""

import math
import re


RECORD_SCHEMA = "szl.paper-rescue.records.v1"
GOLD_SCHEMA = "szl.paper-rescue.gold.v1"
REPORT_SCHEMA = "szl.paper-rescue.report.v1"
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RIGHTS_BASES = frozenset(("operator-created", "public-domain", "licensed", "permission-granted"))


def require(condition, code):
    if not condition:
        raise ValueError(code)


def exact_keys(value, names):
    require(type(value) is dict and set(value) == set(names), "INVALID_FIELDS")


def bounded_text(value, maximum, code):
    require(type(value) is str and 1 <= len(value) <= maximum and value.strip() == value,
            code)
    require(not any(ord(character) < 32 for character in value), code)
    return value


def digest(value):
    require(type(value) is str and SHA256.fullmatch(value) is not None, "BAD_SHA256")
    return value


def item_id(value):
    require(type(value) is str and ID.fullmatch(value) is not None, "BAD_ID")
    return value


def locator(item, declared_pages):
    require(type(item["page"]) is int and 1 <= item["page"] <= declared_pages,
            "PAGE_OUT_OF_DECLARED_RANGE")
    box = item["bbox"]
    require(type(box) is list and len(box) == 4 and
            all(type(number) in (int, float) and math.isfinite(number) and
                0 <= number <= 1 for number in box), "BAD_BBOX")
    require(box[0] < box[2] and box[1] < box[3], "BAD_BBOX")


def validate_item(item, declared_pages):
    require(type(item) is dict, "INVALID_ITEM")
    item_id(item.get("id"))
    kind = item.get("kind")
    status = item.get("status")
    require(kind in ("cell", "caption"), "BAD_KIND")
    require(status in ("LOCATED", "UNRESOLVED"), "BAD_STATUS")
    if status == "UNRESOLVED":
        exact_keys(item, ("id", "kind", "status", "reason"))
        bounded_text(item["reason"], 256, "BAD_REASON")
        return
    fields = ("id", "kind", "status", "page", "bbox", "text")
    if kind == "cell":
        fields += ("row_header", "column_header", "unit")
    exact_keys(item, fields)
    locator(item, declared_pages)
    bounded_text(item["text"], 2048, "BAD_TEXT")
    if kind == "cell":
        bounded_text(item["row_header"], 256, "BAD_ROW_HEADER")
        bounded_text(item["column_header"], 256, "BAD_COLUMN_HEADER")
        bounded_text(item["unit"], 64, "BAD_UNIT")


def validate_items(items, declared_pages, minimum=1):
    require(type(items) is list and minimum <= len(items) <= 1000, "BAD_ITEM_COUNT")
    result = {}
    for item in items:
        validate_item(item, declared_pages)
        require(item["id"] not in result, "DUPLICATE_ITEM_ID")
        result[item["id"]] = item
    return result


def validate_rights(value):
    exact_keys(value, ("basis", "evidence_uri", "attestation_id",
                       "local_processing_authorized", "contains_patient_data"))
    require(type(value["basis"]) is str and value["basis"] in RIGHTS_BASES,
            "RIGHTS_BASIS_MISSING")
    bounded_text(value["evidence_uri"], 512, "RIGHTS_EVIDENCE_MISSING")
    bounded_text(value["attestation_id"], 128, "RIGHTS_ATTESTATION_MISSING")
    require(value["local_processing_authorized"] is True, "PROCESSING_NOT_AUTHORIZED")
    require(value["contains_patient_data"] is False, "PATIENT_DATA_NOT_ALLOWED")


def validate_pipeline(value):
    exact_keys(value, ("name", "version", "ocr_engine", "remote_services_used"))
    for name in ("name", "version", "ocr_engine"):
        bounded_text(value[name], 128, "BAD_PIPELINE")
    require(value["remote_services_used"] is False, "REMOTE_PIPELINE_NOT_ALLOWED")


def validate_records(value, pdf_sha256):
    exact_keys(value, ("schema", "pdf_sha256", "declared_pages", "rights", "pipeline", "items"))
    require(value["schema"] == RECORD_SCHEMA, "BAD_RECORD_SCHEMA")
    require(digest(value["pdf_sha256"]) == pdf_sha256, "PDF_HASH_MISMATCH")
    pages = value["declared_pages"]
    require(type(pages) is int and 1 <= pages <= 10000, "BAD_DECLARED_PAGES")
    validate_rights(value["rights"])
    validate_pipeline(value["pipeline"])
    return validate_items(value["items"], pages, minimum=0)


def validate_gold(value, pdf_sha256, declared_pages):
    exact_keys(value, ("schema", "pdf_sha256", "declared_pages", "annotation_basis",
                       "reviewer_count", "items"))
    require(value["schema"] == GOLD_SCHEMA, "BAD_GOLD_SCHEMA")
    require(digest(value["pdf_sha256"]) == pdf_sha256, "GOLD_PDF_HASH_MISMATCH")
    require(type(value["declared_pages"]) is int and
            value["declared_pages"] == declared_pages, "PAGE_DECLARATION_MISMATCH")
    basis = value["annotation_basis"]
    reviewers = value["reviewer_count"]
    require(basis in ("SYNTHETIC_FIXTURE", "INDEPENDENT_HUMAN_ADJUDICATED"),
            "BAD_ANNOTATION_BASIS")
    require(type(reviewers) is int and
            ((basis == "SYNTHETIC_FIXTURE" and reviewers == 0) or
             (basis == "INDEPENDENT_HUMAN_ADJUDICATED" and 2 <= reviewers <= 20)),
            "BAD_REVIEWER_COUNT")
    return validate_items(value["items"], declared_pages)


def box_iou(left, right):
    overlap = max(0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0, min(left[3], right[3]) - max(left[1], right[1]))
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return overlap / (left_area + right_area - overlap)


def score(records, gold, pdf_sha256):
    """Return a conservative comparison to *declared*, not independently verified, gold."""
    candidates = validate_records(records, pdf_sha256)
    references = validate_gold(gold, pdf_sha256, records["declared_pages"])
    require(not (set(candidates) - set(references)), "UNREGISTERED_CANDIDATE_ID")
    counts = {name: 0 for name in (
        "matched_reference", "abstained", "missing", "reference_locator_mismatch",
        "reference_content_mismatch", "unsupported_located_claim")}
    findings = []
    for name, expected in references.items():
        observed = candidates.get(name)
        if observed is None:
            result = "MISSING"
            counts["missing"] += 1
        elif observed["kind"] != expected["kind"]:
            raise ValueError("KIND_MISMATCH")
        elif observed["status"] == "UNRESOLVED":
            result = "ABSTAINED"
            counts["abstained"] += 1
        elif expected["status"] == "UNRESOLVED":
            result = "UNSUPPORTED_LOCATED_CLAIM"
            counts["unsupported_located_claim"] += 1
        elif observed["page"] != expected["page"] or box_iou(
                observed["bbox"], expected["bbox"]) < 0.5:
            result = "REFERENCE_LOCATOR_MISMATCH"
            counts["reference_locator_mismatch"] += 1
        elif any(observed[field] != expected[field] for field in (
                ("text", "row_header", "column_header", "unit") if
                expected["kind"] == "cell" else ("text",))):
            result = "REFERENCE_CONTENT_MISMATCH"
            counts["reference_content_mismatch"] += 1
        else:
            result = "MATCHED_DECLARED_REFERENCE"
            counts["matched_reference"] += 1
        findings.append({"id": name, "result": result})
    unsafe = sum(counts[name] for name in (
        "reference_locator_mismatch", "reference_content_mismatch", "unsupported_located_claim"))
    return {"schema": REPORT_SCHEMA,
            "status": "REFERENCE_MISMATCH" if unsafe else "SCORED_REVIEW_REQUIRED",
            "denominator": len(references), "counts": counts, "findings": findings,
            "annotation_basis_declared": gold["annotation_basis"],
            "pdf_sha256": pdf_sha256, "rights_verified": False,
            "pipeline_remote_services_verified": False,
            "human_adjudication_verified": False, "visual_truth_verified": False,
            "actual_pdf_page_count_verified": False, "scientific_truth_verified": False,
            "clinical_use_qualified": False}
