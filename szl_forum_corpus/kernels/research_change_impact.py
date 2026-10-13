# SPDX-License-Identifier: Apache-2.0
"""Compare retained research graphs without executing an experiment or promoting a claim."""
import datetime
import hashlib
import heapq
import json
import math
import re


def szl_change_json_bound(value):
    """Reject deep, non-JSON or nonfinite metadata before graph processing."""
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 64 or count > 100000:
            raise ValueError("JSON exceeds the depth or value-count bound")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is None or isinstance(item, (str, bool, int)):
            pass
        elif isinstance(item, float) and math.isfinite(item):
            pass
        else:
            raise ValueError("Only finite JSON values are supported")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError, UnicodeError) as error:
        raise ValueError("Invalid JSON value") from error
    if len(encoded) > 2097152:
        raise ValueError("Input exceeds 2 MiB")
    return encoded


def szl_change_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise ValueError("Times must be UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ValueError("Invalid UTC time") from error


def szl_change_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("Identifiers must contain 1..128 characters")
    return value


def szl_change_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase SHA-256 digest")
    return value


def szl_change_order(dependencies):
    children = {key: set() for key in dependencies}
    counts = {key: len(parents) for key, parents in dependencies.items()}
    for key, parents in dependencies.items():
        for parent in parents:
            if parent not in dependencies:
                raise ValueError("Dangling dependency: " + parent)
            children[parent].add(key)
    ready = [key for key, count in counts.items() if not count]
    heapq.heapify(ready)
    order = []
    while ready:
        key = heapq.heappop(ready)
        order.append(key)
        for child in sorted(children[key]):
            counts[child] -= 1
            if not counts[child]:
                heapq.heappush(ready, child)
    if len(order) != len(dependencies):
        raise ValueError("Cycle in research graph; use a new id for a later run")
    return order, children


def szl_change_graph(graph):
    if not isinstance(graph, dict) or graph.get("schema") != "szl.research-anatomy.v1":
        raise ValueError("Expected a research-anatomy.v1 snapshot")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > 1000:
        raise ValueError("Each snapshot must contain 0..1000 nodes")
    index, deps, edge_count = {}, {}, 0
    kinds = {"question", "claim", "paper", "dataset", "code", "model", "kernel", "run", "proof", "decision"}
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Nodes must be objects")
        key = szl_change_id(node.get("id"))
        if key in index:
            raise ValueError("Duplicate node id: " + key)
        if not isinstance(node.get("kind"), str) or node["kind"] not in kinds or not isinstance(node.get("title"), str) or not 1 <= len(node["title"]) <= 2048:
            raise ValueError("Invalid node kind or title")
        parents = node.get("depends_on", [])
        if not isinstance(parents, list) or len(parents) > 1000:
            raise ValueError("Invalid dependencies")
        for parent in parents:
            szl_change_id(parent)
        if len(set(parents)) != len(parents):
            raise ValueError("Duplicate dependency")
        if node.get("sha256") is not None:
            szl_change_hash(node["sha256"])
        if "needs_recheck" in node and type(node["needs_recheck"]) is not bool:
            raise ValueError("needs_recheck must be boolean")
        if not isinstance(node.get("evidence_status", "active"), str) or node.get("evidence_status", "active") not in {"active", "retracted", "corrected"}:
            raise ValueError("Invalid evidence_status")
        for field in ("observed_at", "expires_at"):
            if field in node:
                szl_change_time(node[field])
        if "expires_at" in node and "observed_at" not in node:
            raise ValueError("Expiry requires observed_at")
        if "expires_at" in node and szl_change_time(node["expires_at"]) <= szl_change_time(node["observed_at"]):
            raise ValueError("Expiry must follow observation")
        index[key], deps[key] = node, set(parents)
        edge_count += len(parents)
    links = graph.get("evidence_links", [])
    if not isinstance(links, list) or len(links) > 10000:
        raise ValueError("Invalid evidence_links")
    seen_links = set()
    for link in links:
        if not isinstance(link, dict) or not {"claim", "evidence", "relation"} <= set(link):
            raise ValueError("Invalid evidence link")
        claim, evidence = link["claim"], link["evidence"]
        if not isinstance(claim, str) or not isinstance(evidence, str) or claim not in index or evidence not in index:
            raise ValueError("Dangling evidence link")
        if index[claim]["kind"] not in {"claim", "decision", "proof"} or not isinstance(link["relation"], str) or link["relation"] not in {"supports", "contradicts", "qualifies"}:
            raise ValueError("Invalid evidence relation or target")
        identity = (claim, evidence, link["relation"])
        if identity in seen_links:
            raise ValueError("Duplicate evidence link")
        seen_links.add(identity)
        deps[claim].add(evidence)
    if edge_count + len(links) > 10000:
        raise ValueError("Each snapshot is limited to 10000 declared edges")
    order, children = szl_change_order(deps)
    return index, deps, order, children


def szl_plan_research_changes(document):
    """Report the declared impact of two snapshots, preserving removed edges in impact reachability."""
    encoded = szl_change_json_bound(document)
    if not isinstance(document, dict) or set(document) != {"schema", "baseline", "current", "observed_digests", "required_claims", "as_of"}:
        raise ValueError("Missing or unknown change-impact fields")
    if document["schema"] != "szl.research-change-impact.v1":
        raise ValueError("Expected research-change-impact.v1")
    as_of = szl_change_time(document["as_of"])
    old, old_deps, old_order, old_children = szl_change_graph(document["baseline"])
    new, new_deps, new_order, new_children = szl_change_graph(document["current"])
    required = document["required_claims"]
    if not isinstance(required, list) or not 1 <= len(required) <= 1000:
        raise ValueError("Declare 1..1000 required claim ids")
    for key in required:
        szl_change_id(key)
        node = new.get(key, old.get(key))
        if node is not None and node["kind"] not in {"claim", "proof", "decision"}:
            raise ValueError("Required targets must be claims, proofs or decisions")
    if len(set(required)) != len(required):
        raise ValueError("Duplicate required claim")
    observed = document["observed_digests"]
    if not isinstance(observed, dict) or any(key not in new or new[key].get("sha256") is None for key in observed):
        raise ValueError("Observations must refer to current pinned nodes")
    for value in observed.values():
        if value is not None:
            szl_change_hash(value)
    reasons = {}
    removed = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    for key in removed:
        reasons[key] = {"NODE_REMOVED"}
    for key in added:
        reasons[key] = {"NODE_ADDED"}
    for key in sorted(set(old) & set(new)):
        # Include all declared node metadata. An edited description or scope can change a claim.
        before = {field: value for field, value in old[key].items() if field != "depends_on"}
        after = {field: value for field, value in new[key].items() if field != "depends_on"}
        if before != after:
            reasons.setdefault(key, set()).add("NODE_DECLARATION_CHANGED")
    removed_edges, added_edges = [], []
    for key in sorted(set(old) | set(new)):
        for parent in sorted(old_deps.get(key, set()) - new_deps.get(key, set())):
            removed_edges.append({"dependency": parent, "dependent": key})
            reasons.setdefault(key, set()).add("DEPENDENCY_REMOVED")
        for parent in sorted(new_deps.get(key, set()) - old_deps.get(key, set())):
            added_edges.append({"dependency": parent, "dependent": key})
            reasons.setdefault(key, set()).add("DEPENDENCY_ADDED")
    # Changing a supports link to contradicts retains the edge but changes the evidence meaning.
    for key in sorted(set(old) | set(new)):
        before = sorted(json.dumps(link, sort_keys=True) for link in document["baseline"].get("evidence_links", []) if link["claim"] == key)
        after = sorted(json.dumps(link, sort_keys=True) for link in document["current"].get("evidence_links", []) if link["claim"] == key)
        if before != after:
            reasons.setdefault(key, set()).add("EVIDENCE_LINKS_CHANGED")
    observations, unavailable = [], []
    artifact_kinds = {"paper", "dataset", "code", "model", "kernel", "run", "proof"}
    for key in sorted(new):
        node = new[key]
        digest = node.get("sha256")
        if digest is None:
            binding = "UNPINNED" if node["kind"] in artifact_kinds else "NO_ARTIFACT_DECLARED"
        elif key not in observed:
            binding = "NOT_OBSERVED"
        elif observed[key] is None:
            binding = "UNAVAILABLE"
        elif observed[key] == digest:
            binding = "MATCH_ON_SUPPLIED_DIGEST"
        else:
            binding = "DIGEST_CHANGED"
        observations.append({"id": key, "binding": binding})
        if binding in {"UNPINNED", "NOT_OBSERVED", "UNAVAILABLE", "DIGEST_CHANGED"}:
            reasons.setdefault(key, set()).add(binding)
            if binding != "DIGEST_CHANGED":
                unavailable.append(key)
        if node.get("needs_recheck") is True:
            reasons.setdefault(key, set()).add("PERSISTENT_RECHECK")
        if node.get("evidence_status", "active") != "active":
            reasons.setdefault(key, set()).add("RETRACTED_OR_CORRECTED")
        if "expires_at" in node and szl_change_time(node["expires_at"]) <= as_of:
            reasons.setdefault(key, set()).add("EXPIRED_OBSERVATION")
        if "observed_at" in node and szl_change_time(node["observed_at"]) > as_of:
            reasons.setdefault(key, set()).add("FUTURE_OBSERVATION")
    children = {key: old_children.get(key, set()) | new_children.get(key, set()) for key in set(old) | set(new)}
    # One deterministic shortest witness per affected node avoids exponential path enumeration.
    queue = [(0, key, (key,)) for key in sorted(reasons)]
    heapq.heapify(queue)
    witnesses = {}
    while queue:
        distance, key, path = heapq.heappop(queue)
        if key in witnesses:
            continue
        witnesses[key] = path
        for child in sorted(children[key]):
            if child not in witnesses:
                heapq.heappush(queue, (distance + 1, child, path + (child,)))
    claims = []
    for key in sorted(required):
        state = "MISSING_REQUIRED_CLAIM" if key not in new else "RECHECK_REQUIRED" if key in witnesses else "NO_DECLARED_IMPACT"
        claims.append({"id": key, "state": state, "witness_path": list(witnesses.get(key, ())), "truth_verified": False})
    affected = sorted(witnesses)
    return {"schema": "szl.research-change-impact-report.v1",
            "status": "REVIEW_REQUIRED" if affected or any(key not in new for key in required) else "NO_DECLARED_IMPACT",
            "input_sha256": hashlib.sha256(encoded).hexdigest(),
            "baseline_sha256": hashlib.sha256(szl_change_json_bound(document["baseline"])).hexdigest(),
            "current_sha256": hashlib.sha256(szl_change_json_bound(document["current"])).hexdigest(),
            "as_of": document["as_of"], "added_nodes": added, "removed_nodes": removed,
            "added_edges": added_edges, "removed_edges": removed_edges,
            "seeds": [{"id": key, "reasons": sorted(reasons[key])} for key in sorted(reasons)],
            "affected_nodes": affected, "unavailable_evidence": unavailable,
            "recheck_order": [key for key in new_order if key in witnesses],
            "retired_affected_nodes": [key for key in old_order if key not in new and key in witnesses],
            "claims": claims, "observations": observations, "execution_authority": "NONE",
            "experiments_executed": False, "observations_independently_verified": False,
            "scientific_validity": "NOT_MEASURED"}
