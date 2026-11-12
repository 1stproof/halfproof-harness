"""Explicit P1 version-1 wire translation for immutable P4 board records.

This module is called only after Board authenticates a worker/controller. It is
not a second unguarded API. Private sealed reviews never occur in records and so
cannot be translated before their atomic reveal.
"""
import json
from .contracts_v3 import CONTRACT_VERSION, record_sha256


def translate(board, db, record):
    row = dict(record)
    body = json.loads(row["body"])
    scope = {"contract_version": CONTRACT_VERSION, "cohort_id": board.cohort_id,
             "problem_id": board.problem_id, "arm_id": board.arm_id}

    def object_id(ref):
        return f"board:{ref}"

    def dependency(ref):
        original = board._record(db, ref)
        return {"object_id": object_id(ref), "version": 1,
                "sha256": original["artifact"] or record_sha256(original)}

    deps = [dependency(ref) for ref in json.loads(row["deps"])]
    base = {**scope, "object_id": object_id(row["id"]), "version": 1,
            "author_id": row["author"], "created_at_unix_ms": int(row["created"] * 1000),
            "dependencies": deps}
    kind = row["kind"]
    if kind == "strategy":
        return [{**base, "approach": body["approach"], "assumptions": [body["assumptions"]],
                 "target_obligation_id": body["target"], "original_proposer_id": row["author"]}]
    if kind == "assignment":
        return [{**base, "obligation_id": body["obligation"], "method_id": body["method"],
                 "owner_id": body["owner"], "assignment_version": body["assignment_version"],
                 "lineage_id": body["lineage_id"], "cost_envelope_microusd": body["cost_microdollars"],
                 "deadline_unix_ms": int(body["deadline"] * 1000),
                 "independent_check": body.get("independent_check", False)}]
    if kind == "result":
        size = db.execute("SELECT length(content) FROM artifacts WHERE hash=?", (row["artifact"],)).fetchone()[0]
        # A content address, not a host filesystem path. P6 materializes the bytes
        # from the frozen snapshot without resolving a worker-supplied path.
        artifact = {"path": "board-artifacts/" + row["artifact"], "sha256": row["artifact"], "bytes": size}
        return [{**base, "claim": body["claim"], "scope": body["scope"], "artifact": artifact,
                 "parent_claims": deps, "computation_pointers": [body["pointers"]] if body.get("claim_type") == "computation" else [],
                 "source_pointers": [body["pointers"]], "limitations": [body["limitations"]],
                 "assignment_version": body["assignment_version"], "lineage_id": body["lineage_id"]}]
    if kind == "help-request":
        milestone = board._record(db, body["milestone_id"])
        return [{**base, "obligation_id": body["obligation"], "distinct_contribution": body["contribution"],
                 "completion_condition": body["completion_condition"], "milestone_sha256": milestone["artifact"]}]
    if kind == "review-pair":
        candidate = json.loads(board._record(db, body["candidate_id"])["body"])
        reviews = []
        for review in body["reviews"]:
            reviewer = review["reviewer"]
            exposure = body["exposure"][reviewer]
            if not isinstance(exposure, str):
                exposure = json.dumps(exposure, sort_keys=True, separators=(",", ":"))
            reviews.append({**scope, "review_id": f"review:{body['pair_id']}:{reviewer}",
                "pair_id": str(body["pair_id"]), "ticket_id": body["tickets"][reviewer],
                "lineage_id": candidate.get("lineage_id", "final-bundle"),
                "reviewer_id": reviewer, "reviewer_family": review["family"],
                "candidate_sha256": body["candidate_hash"], "checked_scope": review["scope"],
                "verdict": review["verdict"], "relevance_supported": review["relevant"],
                "evidence_pointers": [review["evidence"]], "prior_exposure": [exposure] if exposure else [],
                "dependency_author": reviewer in body.get("dependency_authors", []),
                "committed_at_unix_ms": int(review["committed_at"] * 1000),
                "revealed_at_unix_ms": int(row["created"] * 1000)})
        return reviews
    if kind == "allocation":
        return [{**scope, "event_id": object_id(row["id"]), "rule": body["rule"],
                 "eligible_evidence_ids": [object_id(ref) for ref in body["evidence_ids"]],
                 "old_owner_id": body["old_owner"], "new_owner_id": body["new_owner"],
                 "committed_microusd": body["cost_microdollars"], "deadline_unix_ms": int(body["deadline"] * 1000),
                 "assignment_version": body["assignment_version"], "helper_permission_id": body["helper_permission_id"]}]
    if kind in {"dissent", "duplicate-nomination", "invalidation"}:
        admitted = kind == "invalidation"
        root = body.get("root")
        affected = [dependency(root)] if admitted else deps
        return [{**base, "scope": f"record {root} and transitive dependents" if admitted else body["scope"],
                 "objection": body["reason"] if admitted else body["evidence"],
                 "affected_versions": affected,
                 "suspend_or_recheck": [object_id(ref) for ref in body.get("affected_records", [])],
                 "ticket_id": body["cause"] if admitted and not body["permanent"] else None,
                 "status": ("withdrawn" if body["permanent"] else "admitted") if admitted else "unticketed"}]
    return []  # Local operational/audit records have no P1 scientific record shape.
