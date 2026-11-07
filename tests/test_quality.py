"""Quality gates bind a declared self-review to its exact research materials."""
import pytest

from product_ai_agent.ingestion import ingest_file
from product_ai_agent.quality import REVIEW_PATH, evaluate_review, prepare_review
from product_ai_agent.storage import AgentError, Store, atomic, encode, init_project, registry_bytes
from product_ai_agent.workflow import record_claim


@pytest.fixture
def study(tmp_path):
    root = tmp_path / "quality-study"
    init_project(root, "init")
    store = Store(root)
    atomic(store.path("inputs/product.md"), b"TEST ONLY product permits customer-created reminders.\n")
    source = store.mutate("ingest", "test", {}, lambda: ingest_file(store, "inputs/product.md", provenance="synthetic"))["data"]["source_id"]
    claim = {"id": "fact-reminders", "text": "TEST ONLY: customers can create reminders.", "type": "fact",
             "evidence": [{"source_id": source, "locator": "block:line-000001"}], "calculation_ids": [],
             "basis_claim_ids": [], "limitations": ["Synthetic test input"], "status": "active",
             "supersedes": None, "created_at": "2026-11-07T12:00:00Z"}
    store.mutate("claim", "test", {}, lambda: record_claim(store, claim))
    atomic(store.path("brief.md"), b"TEST ONLY: choose the next reminder investigation.\n")
    brief = {"title": "TEST ONLY reminder recommendation", "recommendation": "Check reminder setup before making a broad change.",
             "reasons": [{"text": "Reminder setup is an existing capability.", "claim_ids": [claim["id"]]}],
             "confidence": "Capability is supported by a synthetic product description; demand is unmeasured.",
             "first_steps": [{"action": "Observe reminder setup", "deliverable": "A documented setup barrier or its absence",
                              "when": "Before changing the product", "owner": None}],
             "unknowns": ["Demand has not been measured."], "synthetic": True}
    return store, brief


def assessment(mode="diagnosis"):
    result = {
        "strongest_objection": "Setup observations may miss customers who never attempt to create a reminder.",
        "response_to_objection": "Include non-users in the observation and retain uncertainty about prevalence.",
        "checks": [{"id": name, "status": "pass", "explanation": "TEST ONLY: the stated criterion was checked against the provided research material.",
                    "claim_ids": ["fact-reminders"] if name == "evidence" else [], "limitation": None}
                   for name in ["evidence", "alternatives", "promises", "next_step"]],
        "alternatives": [
            {"id": "keep", "label": "Keep current setup", "kind": "keep", "action": "Retain the current reminder setup without a change.",
             "objection": "A genuine setup barrier could continue to harm affected users."},
            {"id": "observe", "label": "Observe reminder setup", "kind": "investigate", "action": "Observe reminder setup before selecting any interface change.",
             "objection": "Small qualitative observations cannot establish the prevalence of the barrier."},
        ],
        "promises": [],
        "promises_applicability": "This diagnostic recommendation makes no new customer-facing promise.",
    }
    if mode != "diagnosis":
        result["alternatives"].append({"id": "close", "label": "Stop reminder promotion", "kind": "close",
                                       "action": "Stop promoting reminders until the audience and benefit are clearer.",
                                       "objection": "Stopping promotion could lose useful demand signals and existing value."})
        if mode in {"audience", "strategy"}:
            result["alternatives"].append({"id": "change", "label": "Change reminder setup", "kind": "change",
                                           "action": "Change reminder setup for a limited group with a concurrent comparison.",
                                           "objection": "An interface change may spend resources without fixing the actual barrier."})
        result["promises"] = [{"text": "Customers can create their own reminders.", "claim_ids": ["fact-reminders"]}]
        result["promises_applicability"] = "The proposed communication mentions only an already documented product capability."
    return result


def save_review(store, brief, review=None, mode="diagnosis"):
    value = prepare_review(store, brief, review or assessment(mode), mode)
    atomic(store.path(REVIEW_PATH), encode(value))
    return value


@pytest.mark.parametrize("mode", ["diagnosis", "audience", "communication", "strategy"])
def test_supported_modes_and_visible_scope(study, mode):
    store, brief = study
    save_review(store, brief, mode=mode)
    result = evaluate_review(store, brief, mode)
    assert result["ready"] and result["verdict"] == "ready"
    assert "независимая экспертиза" in result["scope"]
    assert result["assessment"]["strongest_objection"] == assessment(mode)["strongest_objection"]


def test_missing_and_damaged_reviews_do_not_pass(study):
    store, brief = study
    assert evaluate_review(store, brief, "diagnosis")["blockers"][0]["code"] == "REVIEW_MISSING"
    atomic(store.path(REVIEW_PATH), b'{"not-a-review": true}')
    assert evaluate_review(store, brief, "diagnosis")["blockers"][0]["code"] == "REVIEW_INVALID"


@pytest.mark.parametrize("changed", ["card", "document", "claim", "source", "decision"])
def test_review_is_stale_when_its_subject_changes(study, changed):
    store, brief = study
    save_review(store, brief)
    if changed == "card":
        brief["recommendation"] = "Select a different reminder experiment after new information."
    elif changed == "document":
        atomic(store.path("brief.md"), b"The decision question changed.\n")
    elif changed == "claim":
        rows = store.registry("claims.jsonl")
        rows[0]["limitations"].append("The product description may be outdated.")
        atomic(store.path("claims.jsonl"), registry_bytes(rows))
    elif changed == "source":
        rows = store.registry("sources.jsonl")
        rows[0]["warnings"].append("The source has a newly reported limitation.")
        atomic(store.path("sources.jsonl"), registry_bytes(rows))
    else:
        decision = {"schema_version": 1, "id": "decision-test", "question": "TEST ONLY: which action?",
                    "options": [{"id": "keep", "label": "Keep current setup"}, {"id": "observe", "label": "Observe setup"}],
                    "selected_option_id": "keep", "actor": {"role": "user", "name": None},
                    "decided_at": "2026-11-07T12:00:00Z", "basis_claim_ids": ["fact-reminders"],
                    "review_conditions": [], "owner": None, "metrics": None,
                    "confirmation": {"kind": "chat", "quote": "SIMULATED TEST ONLY: keep"}}
        atomic(store.path("decisions.jsonl"), registry_bytes([decision]))
    result = evaluate_review(store, brief, "diagnosis")
    assert not result["ready"]
    assert "REVIEW_STALE" in {item["code"] for item in result["blockers"]}


def test_prepared_documents_are_part_of_the_review(study):
    store, brief = study
    class PreparedStore(Store):
        def read(self, name):
            return b"Prepared analysis not yet published." if name == "reports/analysis.md" else super().read(name)
    overlay = PreparedStore(store.root)
    saved = prepare_review(overlay, brief, assessment(), "diagnosis")
    atomic(store.path(REVIEW_PATH), encode(saved))
    # Review sees the prepared document, while the unmodified store lacks it.
    assert evaluate_review(overlay, brief, "diagnosis")["ready"]
    assert not evaluate_review(store, brief, "diagnosis")["ready"]
    atomic(store.path("reports/analysis.md"), b"Prepared analysis not yet published.")
    assert evaluate_review(store, brief, "diagnosis")["ready"]


@pytest.mark.parametrize("target", ["reason", "check", "promise", "retracted"])
def test_missing_or_inactive_references_block_readiness(study, target):
    store, brief = study
    review = assessment("strategy")
    if target == "reason":
        brief["reasons"][0]["claim_ids"] = ["missing-claim"]
    elif target == "check":
        review["checks"][0]["claim_ids"] = ["missing-claim"]
    elif target == "promise":
        review["promises"][0]["claim_ids"] = ["missing-claim"]
    else:
        rows = store.registry("claims.jsonl")
        rows[0]["status"] = "retracted"
        atomic(store.path("claims.jsonl"), registry_bytes(rows))
    with pytest.raises(AgentError):
        prepare_review(store, brief, review, "strategy")


def test_hypothesis_does_not_establish_a_product_promise(study):
    store, brief = study
    rows = store.registry("claims.jsonl")
    rows[0]["type"] = "hypothesis"
    atomic(store.path("claims.jsonl"), registry_bytes(rows))
    with pytest.raises(AgentError, match="Обещание"):
        prepare_review(store, brief, assessment("communication"), "communication")


def test_unknown_must_be_disclosed_in_the_decision_card(study):
    store, brief = study
    review = assessment()
    review["checks"][0].update(status="unknown", limitation="Demand has not been measured.")
    save_review(store, brief, review)
    result = evaluate_review(store, brief, "diagnosis")
    assert result["ready"] and result["verdict"] == "ready_with_limits"
    assert result["warnings"][0]["criterion"] == "evidence"
    brief["unknowns"] = []
    with pytest.raises(AgentError, match="Неопределённость"):
        prepare_review(store, brief, review, "diagnosis")


def test_disagreement_requires_revision(study):
    store, brief = study
    review = assessment()
    review["checks"][3]["status"] = "revise"
    with pytest.raises(AgentError, match="Агент обнаружил проблему"):
        prepare_review(store, brief, review, "diagnosis")


@pytest.mark.parametrize("failure", ["blank_objection", "duplicate_checks", "duplicate_actions", "missing_close", "missing_keep", "blank_step", "no_promises"])
def test_empty_or_ceremonial_reviews_are_rejected(study, failure):
    store, brief = study
    review = assessment("strategy")
    if failure == "blank_objection":
        review["strongest_objection"] = " " * 25
    elif failure == "duplicate_checks":
        review["checks"][1]["id"] = "evidence"
    elif failure == "duplicate_actions":
        review["alternatives"][1]["action"] = "  " + review["alternatives"][0]["action"] + "  "
    elif failure == "missing_close":
        review["alternatives"][2]["kind"] = "change"
    elif failure == "missing_keep":
        review["alternatives"][0]["kind"] = "change"
    elif failure == "blank_step":
        brief["first_steps"][0]["deliverable"] = "   "
    else:
        review["promises"] = []
    with pytest.raises(AgentError):
        prepare_review(store, brief, review, "strategy")


def test_json_key_order_is_not_a_research_change(study):
    store, brief = study
    save_review(store, brief)
    reordered = dict(reversed(list(brief.items())))
    assert evaluate_review(store, reordered, "diagnosis")["ready"]


def test_self_review_does_not_assert_independent_semantic_truth(study):
    store, brief = study
    # Deliberately wrong interpretation: integrity alone cannot refute prose.
    brief["reasons"][0]["text"] = "TEST ONLY deliberately unsupported semantic interpretation."
    save_review(store, brief)
    result = evaluate_review(store, brief, "diagnosis")
    assert result["ready"]
    assert "истинность выводов этим не подтверждены" in result["scope"]
