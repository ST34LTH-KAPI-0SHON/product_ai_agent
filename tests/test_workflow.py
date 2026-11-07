"""Integration tests use explicitly simulated choices, not demonstration approvals."""
import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from product_ai_agent.analytics import aggregate
from product_ai_agent.ingestion import ingest_file, source_output
from product_ai_agent.storage import AgentError, Store, atomic, encode, init_project
from product_ai_agent.workflow import checkpoint, record_claim, record_decision, resume, validation


def test_full_workflow_and_fresh_reader(tmp_path):
    init_project(tmp_path / "demo", "init")
    store = Store(tmp_path / "demo")
    for name in ["brief.md", "research-plan.md", "reports/options.md", "reports/strategy.md", "reports/validation-plan.md", "reports/run-summary.md"]:
        atomic(store.path(name), b"TEST FIXTURE ONLY: explicit nonempty stage artifact")
    atomic(store.path("inputs/product.md"), b"TEST ONLY synthetic product")
    atomic(store.path("inputs/data.csv"), b"period,n,d\np1,2,5\np2,3,10\n")
    for name, op in [("product.md", "import-doc"), ("data.csv", "import-table")]:
        store.mutate(op, "ingest", {}, lambda n=name: ingest_file(store, "inputs/" + n, ",", provenance="synthetic"))
    # A transport fixture does not claim a real network test. The live smoke is recorded separately.
    store.mutate("import-web-fixture", "test", {}, lambda: source_output(store, b"synthetic HTML fixture", {
        "blocks": [{"block_id": "html-1", "text": "TEST ONLY"}], "tables": [], "format": "html"},
        ".html", "TEST ONLY", {"type": "url", "value": "https://example.org/"}, "synthetic", [], "https://example.org/"))
    table = next(s for s in store.registry("sources.jsonl") if s["kind"] == "table")
    spec = {"metric_name": "Test", "definition": "TEST ONLY ratio", "operation": "ratio_of_sums",
            "numerator_column": "n", "denominator_column": "d", "group_by": ["period"], "units": "proportion"}
    store.mutate("calc", "aggregate", {}, lambda: aggregate(store, table["id"], spec))
    store.mutate("claim", "record-claim", {}, lambda: record_claim(store, {
        "id": "clm-test", "text": "Test observation", "type": "observation",
        "evidence": [{"source_id": table["id"], "locator": "record:1"}], "calculation_ids": [],
        "basis_claim_ids": [], "limitations": ["TEST ONLY"], "status": "active", "supersedes": None,
        "created_at": "2026-11-07T12:00:00Z"}))
    options = [{"id": v, "label": v} for v in ["keep", "improve", "pivot", "close"]]
    stages = ["brief", "research", "analysis", "options", "awaiting_decision", "strategy", "complete"]
    for stage in stages[1:]:
        state = store.state()
        state.update(stage=stage, completed_stages=stages[:stages.index(stage)], status="running", pending_decision=None,
                     artifacts={"brief": "brief.md"}, next_action="Test next action")
        if stage == "awaiting_decision":
            state.update(status="awaiting_user", pending_decision={"question": "TEST choice?", "options": options})
        if stage == "strategy":
            with pytest.raises(AgentError):
                checkpoint(store, state, state["revision"], "no-choice")
            decision = {"id": "test-decision", "question": "TEST choice?", "options": options, "selected_option_id": "improve",
                        "actor": {"role": "user", "name": "SIMULATED TEST USER"}, "decided_at": datetime.now(timezone.utc).isoformat(),
                        "basis_claim_ids": ["clm-test"], "review_conditions": [], "owner": None, "metrics": None,
                        "confirmation": {"kind": "chat", "quote": "SIMULATED TEST ONLY: improve"}}
            store.mutate("record-choice", "record-decision", {}, lambda: record_decision(store, decision))
        if stage == "complete":
            state.update(status="completed", completed_stages=stages)
        op = "checkpoint-" + stage
        store.mutate(op, "checkpoint", {}, lambda s=state, o=op: checkpoint(store, s, s["revision"], o))
    assert validation(store)["valid"]
    assert resume(Store(store.root))["state"]["status"] == "completed"


def test_fixture_oracle_independent_of_aggregator():
    import csv
    path = Path(__file__).parents[1] / "examples/activation/metrics.csv"
    records = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    ratios = {}
    for period in ["2026-09", "2026-10"]:
        rows = [r for r in records if r["period"] == period]
        ratios[period] = Fraction(sum(int(r["activated_7d"]) for r in rows), sum(int(r["eligible_users"]) for r in rows))
    assert ratios == {"2026-09": Fraction(2, 5), "2026-10": Fraction(3, 10)}


def test_second_case_uses_same_calculation_code(tmp_path):
    init_project(tmp_path / "second", "init-second")
    store = Store(tmp_path / "second")
    # Different question: completed bookings / eligible requests, with unequal segment sizes.
    atomic(store.path("inputs/bookings.csv"), b"segment,completed,requests\nsmall,1,2\nlarge,3,18\n")
    source = store.mutate("import", "ingest", {}, lambda: ingest_file(store, "inputs/bookings.csv", ","))["data"]["source_id"]
    result, _, _ = aggregate(store, source, {"metric_name": "Booking completion", "definition": "Completed bookings / requests",
        "operation": "ratio_of_sums", "numerator_column": "completed", "denominator_column": "requests", "units": "proportion"})
    assert result["groups"][0]["value"] == "0.2"  # (1+3)/(2+18); not mean(1/2,3/18).
