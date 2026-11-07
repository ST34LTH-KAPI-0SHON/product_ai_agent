"""Task-level contracts: route selection changes work, not evidence standards."""
import copy
import zipfile

import pytest

from product_ai_agent.analytics import aggregate
from product_ai_agent.experience import choose, save, session, start
from product_ai_agent.ingestion import ingest_file, source_output
from product_ai_agent.presentation import export_project
from product_ai_agent.routing import DIAGNOSIS_STAGES, route_config
from product_ai_agent.storage import AgentError, Store, atomic, encode
from product_ai_agent.workflow import resume, validation


def new_task(tmp_path, monkeypatch, mode="diagnosis", quantitative=False, table=False):
    monkeypatch.chdir(tmp_path)
    product = tmp_path / "product.md"
    product.write_text("TEST ONLY synthetic product: the service supports a weekly progress summary.", encoding="utf-8")
    materials = [product]
    if table:
        data = tmp_path / "metrics.csv"
        data.write_text("period,active,total\np1,20,100\np2,10,100\n", encoding="utf-8")
        materials.append(data)
    result = start("projects/route-test", "TEST task", "What should the product team check next?",
                   materials, synthetic=True, mode=mode, quantitative=quantitative,
                   routing_reason="TEST ONLY: select the process by the requested decision.")
    return Store(tmp_path / result["project"])


def sourced_claim(store):
    source = next(s for s in store.registry("sources.jsonl") if s["kind"] == "document")
    block = store.json(source["extracted_path"])["blocks"][0]
    return {"id": "clm-route-capability", "text": "The TEST service supports a weekly progress summary.",
            "type": "fact", "evidence": [{"source_id": source["id"], "locator": "block:" + block["block_id"]}],
            "calculation_ids": [], "basis_claim_ids": [], "limitations": ["TEST ONLY synthetic input."],
            "status": "active", "supersedes": None, "created_at": "2026-11-07T12:00:00Z"}


def card():
    return {"title": "TEST recommendation", "recommendation": "Verify comprehension of the weekly summary before changing the product.",
            "reasons": [{"text": "The product supports the summary; response to it has not been measured.", "claim_ids": ["clm-route-capability"]}],
            "confidence": "Capability is described in a synthetic document; user value is untested.",
            "first_steps": [{"action": "Verify understanding of the weekly summary", "deliverable": "An anonymised list of comprehension barriers",
                             "when": "After an owner and consent are agreed", "owner": None}],
            "do_not": ["Do not promise an improvement in retention."], "success_rule": "Agree a comparison and a harm threshold before a rollout.",
            "unknowns": ["The owner and real response are unknown."], "revisit_when": ["Real evidence contradicts the capability or the need."],
            "expected_effect": "No business effect has been measured.", "synthetic": True}


def assessment(mode="diagnosis"):
    alternatives = [
        {"id": "keep", "label": "Keep current approach", "action": "Keep the present summary and perform no new research.",
         "objection": "The existing comprehension problem, if any, would remain untested.", "kind": "keep"},
        {"id": "investigate", "label": "Check comprehension", "action": "Observe consented users interpreting the existing weekly summary.",
         "objection": "A small qualitative sample cannot establish population prevalence.", "kind": "investigate"},
        {"id": "close", "label": "Stop this communication", "action": "Stop the weekly-summary campaign while preserving the product.",
         "objection": "Stopping may remove useful information without evidence of harm.", "kind": "close"},
        {"id": "change", "label": "Change the summary", "action": "Test a rewritten summary against the existing summary.",
         "objection": "Changing wording now may target the wrong comprehension barrier.", "kind": "change"},
    ]
    if mode == "diagnosis":
        alternatives = alternatives[:2]
    elif mode == "communication":
        alternatives = [alternatives[i] for i in (0, 2, 3)]
    return {"strongest_objection": "The available synthetic capability does not show that users need the proposed diagnostic work.",
            "response_to_objection": "Treat the recommendation as a bounded next check and do not claim validated demand or permission to launch.",
            "checks": [{"id": key, "status": "pass", "explanation": explanation,
                        "claim_ids": ["clm-route-capability"] if key in {"evidence", "promises"} else []}
                       for key, explanation in [
                           ("evidence", "The narrow capability statement is sourced; user response is explicitly left unknown."),
                           ("alternatives", "Different actions include holding the current approach and each has a substantive objection."),
                           ("promises", "Only the documented weekly-summary capability is promised; retention growth is not promised."),
                           ("next_step", "The next check names a deliverable and condition of starting after appointing an owner.")]],
            "alternatives": alternatives,
            "promises": [] if mode == "diagnosis" else [{"text": "The service supports a weekly progress summary.", "claim_ids": ["clm-route-capability"]}],
            "promises_applicability": "Diagnosis adds no new product promise." if mode == "diagnosis" else "The capability promise is constrained to the synthetic source description."}


def documents():
    return {"brief.md": "TEST ONLY: identify the next check, not a rollout decision.",
            "research-plan.md": "Check the capability description and identify missing user evidence.",
            "reports/analysis.md": "The synthetic source describes a weekly summary; response and demand remain unknown.",
            "reports/validation-plan.md": "After consent and owner assignment, observe comprehension; agree a comparison before a rollout.",
            "reports/run-summary.md": "TEST ONLY: one document, qualitative check, no API spend measured and no launch performed."}


def write_save(store, update, name="update"):
    path = "drafts/" + name + ".json"
    atomic(store.path(path), encode(update))
    return save(str(store.root), path)


def complete_diagnosis(store):
    return write_save(store, {"stage": "complete", "documents": documents(), "claims": [sourced_claim(store)],
                              "decision_brief": card(), "quality_review": assessment(),
                              "next_action": "Appoint an owner before the proposed diagnostic check."})


def await_decision(store, mode):
    review = assessment(mode)
    options = [{"id": a["id"], "label": a["label"]} for a in review["alternatives"]]
    docs = documents()
    docs["reports/options.md"] = "TEST ONLY: keep, stop or change communication, with distinct risks."
    return write_save(store, {"stage": "awaiting_decision", "documents": docs, "claims": [sourced_claim(store)],
                              "decision_brief": card(), "quality_review": review,
                              "pending_decision": {"question": "Which TEST action do you actually choose?", "options": options},
                              "next_action": "Wait for the real human answer."})


def test_qualitative_diagnosis_finishes_with_one_document(tmp_path, monkeypatch):
    store = new_task(tmp_path, monkeypatch)
    result = complete_diagnosis(store)
    assert result["stage"] == "complete" and result["status"] == "completed"
    assert result["route"]["stages"] == DIAGNOSIS_STAGES
    assert not result["route"]["quantitative"]
    assert store.state()["completed_stages"] == DIAGNOSIS_STAGES
    assert result["quality"]["ready"] and result["quality"]["required"]
    assert [s["kind"] for s in store.registry("sources.jsonl")] == ["document"]
    assert not list(store.path("calculations").glob("*/result.json"))
    assert not store.registry("decisions.jsonl") and result["pending_decision"] is None
    assert not store.path("reports/strategy.md").exists()
    assert validation(store)["valid"]


def test_quantitative_diagnosis_cannot_hide_a_missing_calculation(tmp_path, monkeypatch):
    store = new_task(tmp_path, monkeypatch, quantitative=True)
    with pytest.raises(AgentError) as exc:
        complete_diagnosis(store)
    assert exc.value.code == "VALIDATION_ERROR"
    assert store.state()["stage"] == "brief" and store.state()["revision"] == 0
    assert not store.path("reports/decision-brief.json").exists()


def test_strategy_cannot_skip_the_human_choice_with_a_passed_review(tmp_path, monkeypatch):
    store = new_task(tmp_path, monkeypatch, mode="strategy")
    await_decision(store, "strategy")
    before = copy.deepcopy(store.state())
    with pytest.raises(AgentError):
        write_save(store, {"stage": "complete", "documents": {"reports/strategy.md": "TEST: an unapproved strategy."},
                           "quality_review": assessment("strategy"), "next_action": "This must not be accepted."}, "not-approved")
    assert store.state() == before
    assert not store.registry("decisions.jsonl")


def test_communication_accepts_three_options_and_preserves_real_choice_gate(tmp_path, monkeypatch):
    store = new_task(tmp_path, monkeypatch, mode="communication")
    result = await_decision(store, "communication")
    assert result["stage"] == "awaiting_decision" and result["status"] == "awaiting_user"
    assert len(result["pending_decision"]["options"]) == 3
    assert {a["kind"] for a in result["quality"]["assessment"]["alternatives"]} == {"keep", "close", "change"}
    assert not store.registry("decisions.jsonl")
    assert validation(store)["valid"]


def test_route_and_qualitative_result_survive_portable_export(tmp_path, monkeypatch):
    store = new_task(tmp_path, monkeypatch)
    complete_diagnosis(store)
    package = export_project(str(store.root), True)
    restored = tmp_path / "restored"
    with zipfile.ZipFile(package["path"]) as z:
        z.extractall(restored)
    target = restored / store.state()["project_id"]
    recovered = session(str(target))
    assert recovered["stage"] == "complete" and recovered["quality"]["ready"]
    assert recovered["route"]["mode"] == "diagnosis" and not recovered["route"]["quantitative"]
    assert route_config(Store(target))["reason"] == route_config(store)["reason"]
    assert resume(Store(target))["state"]["completed_stages"] == DIAGNOSIS_STAGES


@pytest.mark.parametrize("changed", ["source", "card", "document"])
def test_changes_invalidate_review_but_session_remains_readable(tmp_path, monkeypatch, changed):
    store = new_task(tmp_path, monkeypatch)
    complete_diagnosis(store)
    if changed == "source":
        atomic(store.path("inputs/new.md"), b"TEST ONLY: new observations change the evidence set.")
        store.mutate("new-material", "ingest-file", {}, lambda: ingest_file(store, "inputs/new.md", provenance="synthetic"))
    elif changed == "card":
        revised = store.json("reports/decision-brief.json")
        revised["recommendation"] = "Read the newly available evidence before this diagnostic check."
        atomic(store.path("reports/decision-brief.json"), encode(revised))
    else:
        atomic(store.path("reports/analysis.md"), b"TEST ONLY: the analysis has been substantively revised.")
    result = session(str(store.root))
    assert result["stage"] == "complete"
    assert not result["quality"]["ready"]
    assert "REVIEW_STALE" in {b["code"] for b in result["quality"]["blockers"]}
    assert not validation(store)["valid"]


def test_stale_research_can_be_moved_for_review_but_not_exported_as_ready_reports(tmp_path, monkeypatch):
    store = new_task(tmp_path, monkeypatch)
    complete_diagnosis(store)
    atomic(store.path("reports/analysis.md"), b"TEST ONLY: newly revised analysis must be reviewed.")
    with pytest.raises(AgentError):
        export_project(str(store.root), False)
    package = export_project(str(store.root), True)
    restored = tmp_path / "stale-restored"
    with zipfile.ZipFile(package["path"]) as z:
        z.extractall(restored)
    recovered = session(str(restored / store.state()["project_id"]))
    assert not recovered["quality"]["ready"]
    assert "REVIEW_STALE" in {b["code"] for b in recovered["quality"]["blockers"]}


@pytest.mark.parametrize("with_web", [False, True])
def test_legacy_projects_keep_their_old_completion_contract(tmp_path, monkeypatch, with_web):
    store = new_task(tmp_path, monkeypatch, mode="strategy", table=True)
    request = store.json("request.json")
    for key in ("workflow_mode", "quantitative", "routing_reason"):
        request.pop(key)
    atomic(store.path("request.json"), encode(request))
    table = next(s for s in store.registry("sources.jsonl") if s["kind"] == "table")
    store.mutate("legacy-calc", "aggregate", {}, lambda: aggregate(store, table["id"],
                 {"metric_name": "TEST legacy activation", "definition": "active/total", "operation": "ratio_of_sums",
                  "numerator_column": "active", "denominator_column": "total", "units": "proportion"}))
    if with_web:
        store.mutate("legacy-web-fixture", "test", {}, lambda: source_output(store, b"TEST ONLY web fixture",
                     {"format": "html", "blocks": [{"block_id": "html-1", "text": "TEST ONLY web fixture"}], "tables": []},
                     ".html", "TEST ONLY web", {"type": "url", "value": "https://example.org/"}, "synthetic", [], "https://example.org/"))
    review = assessment("strategy")
    docs = documents()
    docs["reports/options.md"] = "TEST ONLY: four distinct actions and objections."
    result = write_save(store, {"stage": "awaiting_decision", "documents": docs, "claims": [sourced_claim(store)],
                               "decision_brief": card(), "pending_decision": {"question": "TEST ONLY real-choice gate?",
                               "options": [{"id": a["id"], "label": a["label"]} for a in review["alternatives"]]},
                               "next_action": "Wait for a genuine choice; this test will use a simulated response."})
    assert result["route"]["legacy"] and not result["quality"]["required"]
    assert not result["quality"]["ready"]
    choose(str(store.root), "investigate", "SIMULATED TEST ONLY: investigate")
    final = {"stage": "complete", "documents": {"reports/strategy.md": "TEST ONLY legacy product and communication strategy."},
             "next_action": "No actual rollout was performed."}
    if with_web:
        assert write_save(store, final, "legacy-final")["stage"] == "complete"
        assert validation(store)["valid"]
    else:
        with pytest.raises(AgentError):
            write_save(store, final, "legacy-final")
        assert store.state()["stage"] == "strategy"
