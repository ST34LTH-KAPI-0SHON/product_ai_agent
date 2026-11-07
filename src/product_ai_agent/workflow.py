"""Evidence references, project integrity and explicit workflow checkpoints."""
import re
from datetime import datetime

from .models import Claim, Decision, Policy, Source, State, STAGES
from .storage import AgentError, digest, encode, fail, now, registry_bytes


def check_time(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        fail("VALIDATION_ERROR", "Timestamps must be ISO 8601 with a timezone.")


def locator_exists(store, source_id, locator):
    source = store.source(source_id)
    extracted = store.json(source["extracted_path"])
    if locator.startswith("block:"):
        return locator[6:] in {b["block_id"] for b in extracted["blocks"]}
    if locator.startswith("record:"):
        try:
            return any(t["sheet"] is None and int(locator[7:]) in t["row_numbers"] for t in extracted["tables"])
        except ValueError:
            return False
    if locator.startswith("sheet:"):
        from openpyxl.utils.cell import range_boundaries
        try:
            sheet, cell_range = locator[6:].rsplit("!", 1)
            c1, r1, c2, r2 = range_boundaries(cell_range)
            if any(v is None for v in (c1, r1, c2, r2)):
                return False
            return any(t["sheet"] == sheet and 1 <= c1 <= c2 <= len(t["columns"])
                       and 1 <= r1 <= r2 <= max([1, *t["row_numbers"]]) for t in extracted["tables"])
        except (ValueError, TypeError):
            return False
    return False


def check_claim(store, claim, claims):
    check_time(claim["created_at"])
    if claim["type"] in {"fact", "observation"} and not claim["evidence"]:
        fail("VALIDATION_ERROR", "A fact/observation requires primary evidence.")
    for item in claim["evidence"]:
        if not locator_exists(store, item["source_id"], item["locator"]):
            fail("VALIDATION_ERROR", f"Unresolvable evidence locator for {claim['id']}.")
    for calc_id in claim["calculation_ids"]:
        from .storage import identifier
        identifier(calc_id)
        result = store.json(f"calculations/{calc_id}/result.json")
        if result["source_id"] not in {e["source_id"] for e in claim["evidence"]}:
            fail("VALIDATION_ERROR", "Calculation source must be included in primary evidence.")
    by_id = {row["id"]: row for row in claims}
    for basis in claim["basis_claim_ids"]:
        if basis == claim["id"] or basis not in by_id:
            fail("VALIDATION_ERROR", f"Missing or self-referential basis: {basis}")
        if by_id[basis]["status"] != "active" and claim["status"] == "active":
            fail("VALIDATION_ERROR", f"Active claim uses an inactive basis: {basis}")
    if claim["type"] in {"hypothesis", "forecast", "assumption"} and not claim["limitations"]:
        fail("VALIDATION_ERROR", "Hypotheses/forecasts/assumptions require explicit limitations.")
    if claim["type"] == "recommendation" and not claim["basis_claim_ids"] and not claim["evidence"]:
        fail("VALIDATION_ERROR", "Recommendations require evidence or basis claims.")


def record_claim(store, raw):
    claim = Claim.model_validate(raw).model_dump()
    rows = store.registry("claims.jsonl")
    if any(r["id"] == claim["id"] for r in rows):
        fail("REVISION_CONFLICT", "Claim ID exists; create a new ID with supersedes for a correction.")
    if claim["status"] != "active":
        fail("INVALID_INPUT", "New claims must be active; use retract-claim for withdrawal.")
    check_claim(store, claim, rows)
    if claim["supersedes"]:
        old = next((r for r in rows if r["id"] == claim["supersedes"]), None)
        if old is None or old["status"] != "active":
            fail("INVALID_INPUT", "supersedes must name an active existing claim.")
        if old["id"] in claim["basis_claim_ids"]:
            fail("INVALID_INPUT", "Corrected claim cannot depend on the claim it supersedes.")
        old["status"] = "superseded"
    rows.append(claim)
    return {"claim_id": claim["id"]}, {"claims.jsonl": registry_bytes(rows)}, []


def retract_claim(store, claim_id, reason):
    rows = store.registry("claims.jsonl")
    found = next((r for r in rows if r["id"] == claim_id), None)
    if not found or not reason.strip():
        fail("INVALID_INPUT", "An existing claim and withdrawal reason are required.")
    found["status"] = "retracted"
    found["limitations"].append("Retracted: " + reason)
    return {"claim_id": claim_id, "status": "retracted"}, {"claims.jsonl": registry_bytes(rows)}, ["Review dependent claims and reports before continuing."]


def check_decision(store, decision, claims):
    check_time(decision["decided_at"])
    ids = [option.get("id") for option in decision["options"]]
    if any(not x for x in ids) or len(ids) != len(set(ids)) or decision["selected_option_id"] not in ids:
        fail("VALIDATION_ERROR", "Decision options require unique IDs and an existing selected option.")
    if decision["actor"].get("role") != "user":
        fail("VALIDATION_ERROR", "A recorded choice must identify the human user, not the agent.")
    confirmation = decision["confirmation"]
    if confirmation.get("kind") == "chat":
        if not confirmation.get("quote", "").strip():
            fail("VALIDATION_ERROR", "Record the actual user response; do not fabricate approval.")
    elif confirmation.get("kind") == "file":
        if not locator_exists(store, confirmation.get("source_id", ""), confirmation.get("locator", "")):
            fail("VALIDATION_ERROR", "Decision confirmation file locator does not resolve.")
    else:
        fail("VALIDATION_ERROR", "Decision requires a chat quote or a file locator.")
    by_id = {c["id"]: c for c in claims}
    if any(c not in by_id or by_id[c]["status"] != "active" for c in decision["basis_claim_ids"]):
        fail("VALIDATION_ERROR", "Decision uses a missing or inactive claim.")


def record_decision(store, raw):
    decision = Decision.model_validate(raw).model_dump()
    rows = store.registry("decisions.jsonl")
    if any(row["id"] == decision["id"] for row in rows):
        fail("REVISION_CONFLICT", "Decision ID already exists.")
    state = store.state()
    if state["stage"] != "awaiting_decision" or not state["pending_decision"]:
        fail("INVALID_INPUT", "Record a decision only at an explicit awaiting_decision checkpoint.")
    pending = state["pending_decision"]
    if decision["question"] != pending.get("question") or decision["options"] != pending.get("options"):
        fail("VALIDATION_ERROR", "Decision must answer the saved question and options.")
    check_decision(store, decision, store.registry("claims.jsonl"))
    rows.append(decision)
    return {"decision_id": decision["id"]}, {"decisions.jsonl": registry_bytes(rows)}, ["The validator checks structure; a reviewer must verify that the quoted response is real."]


def validation(store, state=None, ignore_operation=None):
    errors, warnings = [], []

    def attempt(label, callback):
        try:
            return callback()
        except Exception as exc:
            errors.append({"location": label, "message": str(exc) if isinstance(exc, AgentError) else type(exc).__name__})
            return None

    state = attempt("state.json", lambda: State.model_validate(state or store.json("state.json")).model_dump())
    attempt("policy.json", lambda: Policy.model_validate(store.json("policy.json")))
    sources = attempt("sources.jsonl", lambda: store.registry("sources.jsonl"))
    claims = attempt("claims.jsonl", lambda: store.registry("claims.jsonl"))
    decisions = attempt("decisions.jsonl", lambda: store.registry("decisions.jsonl"))
    for source in sources or []:
        attempt(f"source:{source.get('id')}", lambda s=source: Source.model_validate(s))
        attempt(f"source-hash:{source.get('id')}", lambda s=source: store.source(s["id"]))
        def source_metadata(s=source):
            check_time(s["retrieved_at"])
            if s["published_at"] is not None:
                check_time(s["published_at"])
            if s["synthetic"] != (s["provenance"] == "synthetic"):
                fail("VALIDATION_ERROR", "Source provenance and synthetic flag disagree.")
            if s["origin"].get("type") not in {"file", "url"}:
                fail("VALIDATION_ERROR", "Invalid source origin.")
        attempt(f"source-metadata:{source.get('id')}", source_metadata)
        if source.get("synthetic"):
            warnings.append(f"Synthetic source: {source['id']}")
    for claim in claims or []:
        attempt(f"claim-schema:{claim.get('id')}", lambda c=claim: Claim.model_validate(c))
        attempt(f"claim-links:{claim.get('id')}", lambda c=claim: check_claim(store, c, claims or []))
    # Detect claim cycles even if files were edited outside record-claim.
    graph = {c["id"]: c.get("basis_claim_ids", []) for c in claims or []}
    def visit(node, stack, seen):
        if node in stack:
            fail("VALIDATION_ERROR", "Cycle in claim dependencies.")
        if node in seen:
            return
        for child in graph.get(node, []):
            visit(child, stack | {node}, seen)
        seen.add(node)
    attempt("claim-dependencies", lambda: [visit(node, set(), set()) for node in graph])
    for decision in decisions or []:
        attempt(f"decision-schema:{decision.get('id')}", lambda d=decision: Decision.model_validate(d))
        attempt(f"decision-links:{decision.get('id')}", lambda d=decision: check_decision(store, d, claims or []))
    for path in store.path("calculations").glob("*/result.json"):
        rel = path.relative_to(store.root).as_posix()
        def check_calc():
            from .analytics import aggregate
            result = store.json(rel)
            source = store.source(result["source_id"])
            if result["source_sha256"] != source["sha256"]:
                fail("VALIDATION_ERROR", "Calculation source hash changed.")
            check_time(result["created_at"])
            recalculated, _, _ = aggregate(store, result["source_id"], store.json(result["spec_path"]), tool_version=result["tool_version"])
            if recalculated["calculation_id"] != result["id"] or recalculated["path"] != rel:
                fail("VALIDATION_ERROR", "Calculation identity or result path changed.")
        attempt(rel, check_calc)
    pending = attempt("operations.jsonl", store.pending)
    if any(e["operation_id"] != ignore_operation for e in pending or []):
        errors.append({"location": "operations.jsonl", "message": "Unfinished operation requires recovery."})
    for manifest_path in store.path('checkpoints').glob('*/manifest.json'):
        relative=manifest_path.relative_to(store.root).as_posix()
        def check_checkpoint(rel=relative):
            manifest=store.json(rel)
            base=str(manifest_path.parent.relative_to(store.root)).replace('\\','/')
            saved=store.json(base+'/state.json')
            if saved['revision']!=manifest['revision']:
                fail('VALIDATION_ERROR','Checkpoint revision mismatch.')
            for name,expected in manifest['hashes'].items():
                if digest(store.read(base+'/files/'+name))!=expected:
                    fail('VALIDATION_ERROR','Checkpoint file checksum mismatch: '+name)
        attempt(relative,check_checkpoint)
    if state:
        def stages():
            check_time(state["updated_at"])
            completed = state["completed_stages"]
            if completed != STAGES[:len(completed)] or len(completed) > STAGES.index(state["stage"]) + 1:
                fail("VALIDATION_ERROR", "completed_stages must be an ordered prefix consistent with the current stage.")
            if state["stage"] != "complete" and len(completed) != STAGES.index(state["stage"]):
                fail("VALIDATION_ERROR", "Finish previous stages before advancing.")
            required = {"brief": ["brief.md"], "research": ["research-plan.md"],
                        "options": ["reports/options.md"], "strategy": ["reports/strategy.md"],
                        "complete": ["reports/strategy.md", "reports/validation-plan.md", "reports/run-summary.md"]}
            for stage in completed:
                for artifact in required.get(stage, []):
                    if not store.read(artifact).strip():
                        fail("VALIDATION_ERROR", f"Empty stage artifact: {artifact}")
            if "research" in completed and not sources:
                fail("VALIDATION_ERROR", "Research needs imported sources.")
            if "analysis" in completed and (not claims or not list(store.path("calculations").glob("*/result.json"))):
                fail("VALIDATION_ERROR", "Analysis needs claims and a saved calculation.")
            if state["stage"] == "awaiting_decision":
                question = state["pending_decision"] or {}
                options = question.get("options", [])
                if not question.get("question") or len(options) < 4 or len({o.get('id') for o in options}) != len(options):
                    fail("VALIDATION_ERROR", "Awaiting decision requires a question and four distinct option IDs.")
                if state["status"] != "awaiting_user":
                    fail("VALIDATION_ERROR", "Awaiting decision must use awaiting_user status.")
            if STAGES.index(state["stage"]) >= STAGES.index("strategy") and not decisions:
                fail("VALIDATION_ERROR", "Strategy needs a recorded human decision.")
            if state["stage"] != "awaiting_decision" and state["pending_decision"] is not None:
                fail("VALIDATION_ERROR", "Clear pending_decision after moving past the choice.")
            if state["status"] == "completed" or state["stage"] == "complete":
                if state["status"] != "completed" or completed != STAGES or state["stage"] != "complete":
                    fail("VALIDATION_ERROR", "Completion requires all stages and complete/completed.")
                if {s["kind"] for s in sources or []} != {"document", "table", "web"}:
                    fail("VALIDATION_ERROR", "Complete V0 requires document, table and real web sources.")
            for name, path in state["artifacts"].items():
                store.read(path)
        attempt("workflow", stages)
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "scope": "Structural integrity and reproducible calculations; semantic quality and real user consent require review."}


def checkpoint(store, raw, expected_revision, operation_id):
    old = store.state()
    if old["revision"] != expected_revision:
        fail("REVISION_CONFLICT", "State revision has changed; read resume before retrying.")
    candidate = State.model_validate(raw).model_dump()
    if candidate["project_id"] != old["project_id"] or candidate["revision"] != expected_revision:
        fail("REVISION_CONFLICT", "Candidate must retain project ID and current revision.")
    delta = STAGES.index(candidate["stage"]) - STAGES.index(old["stage"])
    if delta > 1:
        fail("VALIDATION_ERROR", "Checkpoint cannot skip stages.")
    if delta < 0 and not candidate["transition_reason"]:
        fail("VALIDATION_ERROR", "Returning to an earlier stage requires transition_reason.")
    if old["stage"] == "awaiting_decision" and candidate["stage"] == "strategy":
        pending = old["pending_decision"] or {}
        matching = [d for d in store.registry("decisions.jsonl") if d["question"] == pending.get("question")
                    and d["options"] == pending.get("options")
                    and datetime.fromisoformat(d["decided_at"].replace("Z", "+00:00")) >= datetime.fromisoformat(old["updated_at"].replace("Z", "+00:00"))]
        if not matching:
            fail("VALIDATION_ERROR", "A fresh human decision for the current saved question is required.")
    candidate.update(revision=expected_revision + 1, updated_at=now(), last_completed_operation_id=operation_id)
    report = validation(store, candidate, ignore_operation=operation_id)
    if not report["valid"]:
        fail("VALIDATION_ERROR", str(report["errors"]))
    base = f"checkpoints/rev-{candidate['revision']:06}"
    writes = {"state.json": encode(candidate), f"{base}/state.json": encode(candidate)}
    snapshot_files = ["sources.jsonl", "claims.jsonl", "decisions.jsonl", "operations.jsonl", "policy.json"]
    snapshot_files += list(dict.fromkeys(candidate["artifacts"].values()))
    hashes = {}
    for path in snapshot_files:
        payload = store.read(path)
        writes[f"{base}/files/{path}"] = payload
        hashes[path] = digest(payload)
    writes[f"{base}/manifest.json"] = encode({"revision": candidate["revision"], "hashes": hashes})
    return {"revision": candidate["revision"], "stage": candidate["stage"], "checkpoint": base}, writes, report["warnings"]


def resume(store):
    report = validation(store)
    if not report["valid"]:
        fail("VALIDATION_ERROR", str(report["errors"]))
    state = store.state()
    paths = ["state.json", "sources.jsonl", "claims.jsonl", "decisions.jsonl"]
    paths += [p for p in ["brief.md", "research-plan.md"] if store.path(p).is_file()]
    paths += list(state["artifacts"].values())
    paths += [s["extracted_path"] for s in store.registry("sources.jsonl")]
    return {"state": state, "read_paths": list(dict.fromkeys(paths)),
            "instruction_entry": "instructions/entry.md (relative to repository)",
            "warnings": report["warnings"], "cost": None}
