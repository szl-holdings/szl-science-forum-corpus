"""Strict envelopes shared by the three science workflows.

Workflow records are not forum records: they never widen record-v1 rows, never carry raw forum
text and never claim scientific truth. Every check here fails closed with a typed error.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re

SCHEMA_VERSION = "szl.science-workflow.v1"
WORKFLOW_KINDS = ("paper_review", "evidence_snapshot", "change_set", "recheck_task", "recheck_queue",
                  "figure_bundle")
EVIDENCE_KINDS = ("owned", "synthetic")
VALIDATION_STATES = ("NOT_RUN", "RUNNING", "PASSED", "FAILED", "BLOCKED", "ERROR")
ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_TEXT = 4000

# HTTP status for each error code. A scientific mismatch is never an error: it is a result.
HTTP_STATUS = {
    "INVALID_INPUT": 422,
    "MALFORMED_JSON": 400,
    "UNKNOWN_FIELD": 422,
    "MISSING_FIELD": 422,
    "NOT_FOUND": 404,
    "FORBIDDEN": 403,
    "STALE_REVISION": 409,
    "REQUEST_ID_CONFLICT": 409,
    "EXISTS": 409,
    "TOO_LARGE": 413,
    "UNSUPPORTED": 415,
    "METHOD_NOT_ALLOWED": 405,
    "KERNEL_UNAVAILABLE": 503,
    "CAPABILITY_UNAVAILABLE": 503,
    "STORE_UNAVAILABLE_BEFORE_COMMIT": 503,
    "COMMIT_OUTCOME_UNKNOWN": 503,
    "COMMITTED_READBACK_UNAVAILABLE": 503,
    "STORE_INTEGRITY": 500,
}


class WorkflowError(Exception):
    """A typed, safe-to-display failure. Messages never echo submitted content."""

    def __init__(self, code: str, message: str, *, fields: list[str] | None = None,
                 retryable: bool = False):
        if code not in HTTP_STATUS:
            raise ValueError(f"unregistered workflow error code {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fields = list(fields or [])
        self.retryable = retryable

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def envelope(self, request_id: str | None = None) -> dict:
        return {"error": {"code": self.code, "message": self.message, "fields": self.fields,
                          "retryable": self.retryable, "request_id": request_id}}


def fail(code: str, message: str, *fields: str) -> None:
    raise WorkflowError(code, message, fields=list(fields))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            fail("MALFORMED_JSON", "duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> None:
    fail("MALFORMED_JSON", "non-finite number")


def check_text(value: str, field: str) -> str:
    """Reject lone surrogates and control characters other than tab and newline."""

    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        fail("INVALID_INPUT", "text contains a lone surrogate", field)
    if any(ord(character) < 32 and character not in "\t\n" for character in value):
        fail("INVALID_INPUT", "text contains a control character", field)
    return value


def check_value(value: object, field: str = "$", depth: int = 0) -> None:
    """Walk a decoded JSON value: bounded depth, finite numbers, clean strings, no bool/int mixups."""

    if depth > 32:
        fail("INVALID_INPUT", "JSON nesting exceeds 32 levels", field)
    if isinstance(value, dict):
        if len(value) > 10000:
            fail("TOO_LARGE", "object has too many members", field)
        for key, child in value.items():
            check_text(key, field)
            check_value(child, f"{field}.{key}", depth + 1)
    elif isinstance(value, list):
        if len(value) > 100000:
            fail("TOO_LARGE", "array has too many items", field)
        for index, child in enumerate(value):
            check_value(child, f"{field}[{index}]", depth + 1)
    elif isinstance(value, str):
        check_text(value, field)
    elif isinstance(value, float):
        if not math.isfinite(value):
            fail("INVALID_INPUT", "non-finite number", field)
    elif value is None or isinstance(value, (bool, int)):
        return
    else:
        fail("INVALID_INPUT", "unsupported JSON value", field)


def parse_json(raw: bytes, *, limit: int = MAX_DOCUMENT_BYTES) -> object:
    """Parse strict UTF-8 JSON bytes: size cap first, then duplicate keys and non-finite numbers."""

    if not isinstance(raw, (bytes, bytearray)):
        fail("INVALID_INPUT", "expected JSON bytes")
    if len(raw) > limit:
        fail("TOO_LARGE", f"JSON exceeds {limit} bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        fail("MALFORMED_JSON", "JSON is not valid UTF-8")
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                           parse_constant=_reject_constant)
    except WorkflowError:
        raise
    except (ValueError, RecursionError):
        fail("MALFORMED_JSON", "JSON could not be parsed")
    check_value(value)
    return value


def canonical_bytes(value: object) -> bytes:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8, finite numbers only."""

    check_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def typed_digest(raw: bytes, domain: str, profile: str) -> dict:
    """Name what a digest covers so original bytes and normalized JSON are never confused."""

    return {"algorithm": "sha256", "byte_domain": domain, "canonicalization": profile,
            "value": sha256_hex(raw)}


def exact_fields(value: object, required: set[str], optional: set[str] = frozenset(),
                 where: str = "$") -> dict:
    if not isinstance(value, dict):
        fail("INVALID_INPUT", "expected an object", where)
    missing = sorted(required - set(value))
    if missing:
        fail("MISSING_FIELD", "required fields are missing", *[f"{where}.{name}" for name in missing])
    unknown = sorted(set(value) - required - set(optional))
    if unknown:
        fail("UNKNOWN_FIELD", "unknown fields are not accepted", *[f"{where}.{name}" for name in unknown])
    return value


def identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        fail("INVALID_INPUT", "identifier must match [A-Za-z][A-Za-z0-9_.-]{0,63}", field)
    return value


def request_id(value: object) -> str:
    if not isinstance(value, str) or REQUEST_ID_RE.fullmatch(value) is None:
        fail("INVALID_INPUT", "request_id must be 32 lowercase hexadecimal characters", "request_id")
    return value


def sha256_value(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        fail("INVALID_INPUT", "expected a lowercase SHA-256 digest", field)
    return value


def bounded_text(value: object, field: str, *, maximum: int = MAX_TEXT,
                 allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        fail("INVALID_INPUT", f"text must be {'0' if allow_empty else '1'}..{maximum} characters", field)
    return check_text(value, field)


def choice(value: object, options: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in options:
        fail("INVALID_INPUT", "value is not one of the allowed options", field)
    return value


def boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        fail("INVALID_INPUT", "expected true or false", field)
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_time(value: object, field: str) -> str:
    """Accept a client time only as a labelled observation; audit order always uses server time."""

    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        fail("INVALID_INPUT", "time must be UTC YYYY-MM-DDTHH:MM:SSZ", field)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        fail("INVALID_INPUT", "time is not a real calendar time", field)
    return value


LIMITATIONS = (
    "Records preserve review history and check declared relationships; they do not decide "
    "whether a scientific claim is true.",
    "Synthetic fixture results establish software behaviour only, not researcher benefit or "
    "measured time savings.",
    "This local workbench has no authenticated identity, tenant isolation or production "
    "authorization; it is a loopback demonstration over owned or synthetic inputs.",
)
