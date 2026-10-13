#!/usr/bin/env python3
"""Render and replay a bounded CSV-to-SVG contract. Python stdlib, offline."""
import argparse
import csv
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import html
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import xml.etree.ElementTree as ET

SCHEMA = "szl.figure-data-contract.v1"
RECEIPT_SCHEMA = "szl.figure-data-receipt.v1"
MAX_BYTES = 262144
MAX_ROWS = 10000
SVG_NS = "http://www.w3.org/2000/svg"
RECEIPT_KEYS = {"schema", "spec", "data", "spec_sha256", "data_sha256", "generator_sha256",
                "outputs", "signed", "independent_witness", "scientific_claims_verified", "receipt_sha256"}


class ContractError(ValueError):
    def __init__(self, reason, status="UNKNOWN"):
        super().__init__(reason)
        self.status = status


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def no_links(path):
    for part in (path, *path.parents):
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ContractError("symlink or reparse point")


def safe_root(value):
    path = Path(value).absolute()
    no_links(path)
    if not path.is_dir():
        raise ContractError("root must be an existing directory")
    return path.resolve(strict=True)


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ContractError("path must be a canonical relative POSIX path")
    path = PurePosixPath(relative)
    if relative == "." or path.is_absolute() or str(path) != relative or any(p in (".", "..") or p.endswith((" ", ".")) for p in path.parts):
        raise ContractError("unsafe relative path")
    if any(re.search(r"(?i)^(\.env|credentials?|tokens?|id_rsa|id_ed25519)(\.|$)", p) for p in path.parts):
        raise ContractError("credential-like path refused")
    target = root.joinpath(*path.parts)
    no_links(target)
    if not target.resolve(strict=False).is_relative_to(root):
        raise ContractError("path escaped root")
    return target


def read_bytes(path, limit=MAX_BYTES):
    no_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ContractError("input must be a bounded regular file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ContractError("input exceeds byte bound")
    return raw


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def read_json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError("nonfinite JSON")))
    except RecursionError as error:
        raise ContractError("JSON nesting exceeds parser bound") from error


def number(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", value):
        raise ContractError("numeric data and assertions must be decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ContractError("invalid decimal") from error
    if not result.is_finite() or len(result.as_tuple().digits) > 32 or result.copy_abs() > Decimal("1e100") or (result and result.copy_abs() < Decimal("1e-100")):
        raise ContractError("decimal outside supported range")
    return result


def decimal_text(value):
    with localcontext() as context:
        context.prec = 50
        return str(value.normalize()) if value else "0"


def load_contract(root, spec_path, *, raw_spec=None, raw_data=None):
    if not isinstance(spec_path, str) or not spec_path.endswith(".json"):
        raise ContractError("spec must name a JSON file")
    if raw_spec is None:
        raw_spec = read_bytes(safe_path(root, spec_path), 16384)
    spec = read_json(raw_spec)
    required = {"schema", "data", "id_column", "x", "y", "kind", "caption"}
    if not isinstance(spec, dict) or set(spec) != required or spec["schema"] != SCHEMA:
        raise ContractError("unsupported plot specification")
    if spec["kind"] not in ("scatter", "line"):
        raise ContractError("only scatter and line are supported")
    for axis in ("x", "y"):
        a = spec[axis]
        if not isinstance(a, dict) or set(a) != {"column", "unit"}:
            raise ContractError("axis requires column and unit")
        if any(not isinstance(a[k], str) or not 1 <= len(a[k]) <= 80 or
               any(not (32 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
                        0x10000 <= ord(c) <= 0x10FFFF) for c in a[k]) for k in a):
            raise ContractError("invalid axis text")
    columns = (spec["id_column"], spec["x"]["column"], spec["y"]["column"])
    if any(not isinstance(c, str) for c in columns) or len(set(columns)) != 3:
        raise ContractError("id, x and y require distinct columns")
    if not isinstance(spec["data"], str) or not spec["data"].endswith(".csv"):
        raise ContractError("data must name a CSV file")
    if raw_data is None:
        raw_data = read_bytes(safe_path(root, spec["data"]))
    reader = csv.reader(io.StringIO(raw_data.decode("utf-8"), newline=""), strict=True)
    header = next(reader, None)
    if not header or len(header) > 64 or len(set(header)) != len(header) or any(not c for c in header) or not set(columns) <= set(header):
        raise ContractError("invalid CSV header")
    indices = [header.index(c) for c in columns]
    points, seen = [], set()
    for row in reader:
        if len(points) >= MAX_ROWS or len(row) != len(header):
            raise ContractError("CSV row bound or width violation")
        row_id, x, y = (row[i] for i in indices)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", row_id) or row_id in seen:
            raise ContractError("row IDs must be unique safe identifiers")
        seen.add(row_id)
        points.append({"id": row_id, "x": number(x), "y": number(y)})
    if not points:
        raise ContractError("CSV has no observations")
    summary = {"n": len(points), "x_min": min(p["x"] for p in points), "x_max": max(p["x"] for p in points),
               "y_min": min(p["y"] for p in points), "y_max": max(p["y"] for p in points)}
    caption = spec["caption"]
    if not isinstance(caption, dict) or set(caption) != set(summary) or type(caption["n"]) is not int:
        raise ContractError("caption requires n and x/y min/max assertions")
    asserted = {k: caption[k] if k == "n" else number(caption[k]) for k in summary}
    if asserted != summary:
        raise ContractError("caption assertions disagree with the CSV", "MISMATCH")
    summary = {k: v if k == "n" else decimal_text(v) for k, v in summary.items()}
    return spec, points, summary, raw_spec, raw_data


def coordinates(points):
    def scale(values, low, high):
        with localcontext() as context:
            context.prec = 50
            minimum, maximum = min(values), max(values)
            if minimum == maximum:
                return [f"{(Decimal(low) + Decimal(high)) / 2:.6f}" for _ in values]
            return [f"{Decimal(low) + (v - minimum) * (Decimal(high) - Decimal(low)) / (maximum - minimum):.6f}" for v in values]
    return list(zip(scale([p["x"] for p in points], 60, 600), scale([p["y"] for p in points], 400, 40)))


def caption_text(summary):
    return "n={n}; x=[{x_min}, {x_max}]; y=[{y_min}, {y_max}]".format(**summary)


def make_svg(spec, points, summary):
    pixels = coordinates(points)
    chunks = [f'<svg xmlns="{SVG_NS}" width="640" height="480" viewBox="0 0 640 480">',
              '<line x1="60" y1="400" x2="600" y2="400" stroke="black"/>',
              '<line x1="60" y1="400" x2="60" y2="40" stroke="black"/>',
              f'<g id="series" data-kind="{spec["kind"]}">']
    if spec["kind"] == "line":
        chunks.append('<polyline points="' + " ".join(x + "," + y for x, y in pixels) + '" fill="none" stroke="#1764ab"/>')
    for point, (x, y) in zip(points, pixels):
        chunks.append(f'<circle data-row="{point["id"]}" cx="{x}" cy="{y}" r="3" fill="#1764ab"/>')
    chunks.append('</g>')
    labels = [("x-unit", "330", "425", spec["x"]["column"] + " (" + spec["x"]["unit"] + ")"),
              ("y-unit", "60", "25", spec["y"]["column"] + " (" + spec["y"]["unit"] + ")"),
              ("caption", "60", "455", caption_text(summary))]
    for label, x, y, text in labels:
        chunks.append(f'<text id="{label}" x="{x}" y="{y}" font-size="12">{html.escape(text)}</text>')
    return ("\n".join(chunks) + "\n</svg>\n").encode("utf-8")


def sidecar(spec, points, summary):
    return {"schema": SCHEMA, "kind": spec["kind"], "x": spec["x"], "y": spec["y"], "summary": summary,
            "points": [{"id": p["id"], "x": decimal_text(p["x"]), "y": decimal_text(p["y"])} for p in points]}


def render(root, spec_path, output):
    spec, points, summary, raw_spec, raw_data = load_contract(root, spec_path)
    directory = safe_path(root, output)
    if directory.exists() or not directory.parent.is_dir():
        raise ContractError("output directory must be new with an existing parent")
    svg, plotted = make_svg(spec, points, summary), encoded(sidecar(spec, points, summary))
    inspect_svg(svg, spec, points, summary)
    receipt = {"schema": RECEIPT_SCHEMA, "spec": spec_path, "data": spec["data"],
               "spec_sha256": digest(raw_spec), "data_sha256": digest(raw_data),
               "generator_sha256": digest(read_bytes(Path(__file__), 131072)),
               "outputs": {"figure.svg": digest(svg), "plotted-data.json": digest(plotted)},
               "signed": False, "independent_witness": False, "scientific_claims_verified": False}
    receipt["receipt_sha256"] = digest(encoded(receipt))
    directory.mkdir()
    for name, raw in (("figure.svg", svg), ("plotted-data.json", plotted), ("receipt.json", encoded(receipt))):
        with (directory / name).open("xb") as stream:
            stream.write(raw)
    return {"status": "MATCH", "operation": "render", "output": output, "points": len(points), "signed": False}


def inspect_svg(raw, spec, points, summary):
    """Parse the generated geometry and visible labels instead of trusting output hashes."""
    if b"<!" in raw or b"<?" in raw:
        raise ContractError("SVG declarations and processing instructions refused", "MISMATCH")
    if raw != make_svg(spec, points, summary):
        raise ContractError("SVG is not the canonical generated artifact", "MISMATCH")
    try:
        svg = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ContractError("SVG syntax differs", "MISMATCH") from error
    tag = lambda name: "{" + SVG_NS + "}" + name
    if svg.tag != tag("svg") or svg.attrib != {"width": "640", "height": "480", "viewBox": "0 0 640 480"}:
        raise ContractError("SVG root differs", "MISMATCH")
    children = list(svg)
    if len(children) != 6:
        raise ContractError("unexpected SVG element", "MISMATCH")
    for node, attributes in zip(children[:2], ({"x1":"60","y1":"400","x2":"600","y2":"400","stroke":"black"},
                                              {"x1":"60","y1":"400","x2":"60","y2":"40","stroke":"black"})):
        if node.tag != tag("line") or node.attrib != attributes or len(node):
            raise ContractError("SVG axis differs", "MISMATCH")
    group = children[2]
    if group.tag != tag("g") or group.attrib != {"id": "series", "data-kind": spec["kind"]}:
        raise ContractError("SVG series differs", "MISMATCH")
    marks, pixels = list(group), coordinates(points)
    if spec["kind"] == "line":
        line = marks.pop(0) if marks else None
        attrs = {"points": " ".join(x + "," + y for x, y in pixels), "fill": "none", "stroke": "#1764ab"}
        if line is None or line.tag != tag("polyline") or line.attrib != attrs or len(line):
            raise ContractError("SVG line ordering differs", "MISMATCH")
    if len(marks) != len(points):
        raise ContractError("SVG point count differs", "MISMATCH")
    for mark, point, (x, y) in zip(marks, points, pixels):
        attrs = {"data-row": point["id"], "cx": x, "cy": y, "r": "3", "fill": "#1764ab"}
        if mark.tag != tag("circle") or mark.attrib != attrs or len(mark):
            raise ContractError("SVG point mapping differs", "MISMATCH")
    labels = [("x-unit", "330", "425", spec["x"]["column"] + " (" + spec["x"]["unit"] + ")"),
              ("y-unit", "60", "25", spec["y"]["column"] + " (" + spec["y"]["unit"] + ")"),
              ("caption", "60", "455", caption_text(summary))]
    for node, (label, x, y, text) in zip(children[3:], labels):
        if node.tag != tag("text") or node.attrib != {"id": label, "x": x, "y": y, "font-size": "12"} or node.text != text or len(node):
            raise ContractError("SVG caption or unit differs", "MISMATCH")


def verify(root, output):
    directory = safe_path(root, output)
    receipt = read_json(read_bytes(directory / "receipt.json", 16384))
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        raise ContractError("unsupported receipt")
    if set(receipt) != RECEIPT_KEYS:
        raise ContractError("unsupported receipt fields", "MISMATCH")
    claimed = receipt.pop("receipt_sha256", None)
    if claimed != digest(encoded(receipt)) or receipt.get("signed") is not False or receipt.get("independent_witness") is not False or receipt.get("scientific_claims_verified") is not False:
        raise ContractError("receipt changed or unsupported evidence claim", "MISMATCH")
    if receipt.get("generator_sha256") != digest(read_bytes(Path(__file__), 131072)):
        raise ContractError("verification helper differs from recorded generator", "MISMATCH")
    raw_spec = read_bytes(safe_path(root, receipt.get("spec")), 16384)
    raw_data = read_bytes(safe_path(root, receipt.get("data")))
    if receipt.get("spec_sha256") != digest(raw_spec) or receipt.get("data_sha256") != digest(raw_data):
        raise ContractError("input bytes differ", "MISMATCH")
    spec, points, summary, _, _ = load_contract(root, receipt.get("spec"), raw_spec=raw_spec, raw_data=raw_data)
    if receipt.get("data") != spec["data"]:
        raise ContractError("input path differs", "MISMATCH")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {"figure.svg", "plotted-data.json"}:
        raise ContractError("unsupported output manifest")
    raw_svg = read_bytes(directory / "figure.svg", 2097152)
    raw_points = read_bytes(directory / "plotted-data.json", 2097152)
    if outputs != {"figure.svg": digest(raw_svg), "plotted-data.json": digest(raw_points)}:
        raise ContractError("output bytes differ", "MISMATCH")
    if read_json(raw_points) != sidecar(spec, points, summary):
        raise ContractError("plotted data differs", "MISMATCH")
    inspect_svg(raw_svg, spec, points, summary)
    return {"status": "MATCH", "operation": "verify", "points": len(points), "signed": False,
            "independent_witness": False, "scientific_claims_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("render", "verify"))
    parser.add_argument("path", help="spec JSON for render; existing bundle directory for verify")
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", help="new bundle directory for render")
    args = parser.parse_args()
    try:
        with localcontext() as context:
            context.prec = 50
            root = safe_root(args.root)
            if args.operation == "render":
                if args.output is None:
                    raise ContractError("render requires --output")
                result = render(root, args.path, args.output)
            else:
                if args.output is not None:
                    raise ContractError("verify does not accept --output")
                result = verify(root, args.path)
    except ContractError as error:
        result = {"status": error.status, "reason": str(error)}
    except (OSError, UnicodeError, json.JSONDecodeError, csv.Error, ET.ParseError, InvalidOperation, KeyError, TypeError) as error:
        result = {"status": "UNKNOWN", "reason": type(error).__name__}
    print(json.dumps(result, sort_keys=True))
    return {"MATCH": 0, "MISMATCH": 1, "UNKNOWN": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
