"""Immutable decision snapshots and factual, readable comparisons between them."""
import json

from .followup import optional_json, timestamp
from .storage import AgentError, Store, digest, encode, fail, now, registry_bytes

DOCUMENTS = ("reports/analysis.md", "reports/options.md", "reports/strategy.md", "reports/validation-plan.md")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def subject(store, card):
    rows = {c["id"]: c for c in store.registry("claims.jsonl")}
    selected = {c for reason in card["reasons"] for c in reason["claim_ids"]}
    while True:
        expanded = selected | {c for key in selected for c in rows.get(key, {}).get("basis_claim_ids", [])}
        if expanded == selected:
            break
        selected = expanded
    if not selected.issubset(rows):
        fail("VALIDATION_ERROR", "Нельзя сохранить версию с отсутствующими основаниями.")
    claims = [rows[key] for key in sorted(selected)]
    source_ids = {e["source_id"] for c in claims for e in c["evidence"]}
    sources = [{k: s[k] for k in ("id", "title", "sha256", "extracted_sha256", "origin", "synthetic")}
               for s in store.registry("sources.jsonl") if s["id"] in source_ids]
    request = optional_json(store, "request.json") or {}
    documents = {}
    for name in DOCUMENTS:
        try:
            documents[name] = digest(store.read(name))
        except AgentError as exc:
            if exc.code != "INVALID_INPUT" or exc.message != "Missing project file: " + name:
                raise
    return {"card": card, "claims": claims, "sources": sorted(sources, key=lambda s: s["id"]),
            "decision": (store.registry("decisions.jsonl") or [None])[-1], "documents": documents,
            "mode": request.get("workflow_mode", "strategy"), "depth": request.get("depth", "full")}


def current_reference(store):
    card = optional_json(store, "reports/decision-brief.json")
    return "subject-" + digest(canonical(subject(store, card)))[:32] if card else None


def index(store):
    try:
        return store.registry("versions.jsonl")
    except AgentError as exc:
        if exc.code == "INVALID_INPUT" and exc.message == "Missing project file: versions.jsonl":
            return []
        raise


def validate_versions(store):
    previous = None
    for number, row in enumerate(index(store), 1):
        if row.get("id") != f"v{number:04}" or row.get("path") != f"versions/v{number:04}.json":
            fail("VALIDATION_ERROR", "Нарушен порядок версий решения.")
        raw = store.read(row["path"])
        saved = store.json(row["path"])
        if digest(raw) != row.get("sha256") or saved.get("previous_sha256") != previous:
            fail("VALIDATION_ERROR", "Изменён архив версии решения.")
        if saved.get("id") != row["id"] or saved.get("subject_sha256") != row.get("subject_sha256") or saved.get("subject_sha256") != digest(canonical(saved["subject"])):
            fail("VALIDATION_ERROR", "Содержимое версии решения не соответствует отпечатку.")
        timestamp(saved["recorded_at"])
        previous = digest(raw)


def prepare_versions(store, prepared, explanation, revision, require_reason=False):
    """Return writes for the same transaction as save. Never overwrite a snapshot."""
    validate_versions(store)
    rows = index(store)
    writes = {}
    old_card = optional_json(store, "reports/decision-brief.json")
    card = optional_json(prepared, "reports/decision-brief.json")
    if card is None:
        return writes

    def append(value, reason, origin, state_revision, explanation_origin="automatic"):
        number = len(rows) + 1
        ident = f"v{number:04}"
        record = {"schema_version": 1, "id": ident, "recorded_at": now(), "origin": origin,
                  "state_revision": state_revision, "explanation": reason,
                  "explanation_origin": explanation_origin,
                  "previous_sha256": rows[-1]["sha256"] if rows else None,
                  "subject_sha256": digest(canonical(value)), "subject": value}
        path = f"versions/{ident}.json"
        payload = encode(record)
        writes[path] = payload
        rows.append({"id": ident, "path": path, "sha256": digest(payload),
                     "subject_sha256": record["subject_sha256"], "state_revision": state_revision})

    had_previous = bool(rows) or bool(old_card and store.state()["stage"] in {"awaiting_decision", "complete"})
    if not rows and old_card and had_previous:
        append(subject(store, old_card), "Сохранён прежний текущий вывод перед обновлением; это не дата его первоначального создания.",
               "existing_card", store.state()["revision"])
    value = subject(prepared, card)
    changed = not rows or rows[-1]["subject_sha256"] != digest(canonical(value))
    if changed:
        if had_previous and require_reason and (not explanation or len(explanation.strip()) < 10):
            fail("INVALID_INPUT", "Объясните изменение вывода или оснований в change_reason; прежняя версия будет сохранена.")
        append(value, explanation or ("Первый сохранённый вывод." if not had_previous else "Обновлена карточка или её основания; подробная причина не была записана в прежнем формате."),
               "save", revision, "recorded" if explanation else "automatic")
    if writes:
        writes["versions.jsonl"] = registry_bytes(rows)
    return writes


class CheckpointStore(Store):
    def __init__(self, store, prefix):
        super().__init__(store.root)
        self.prefix = prefix

    def read(self, name):
        try:
            return super().read(self.prefix + name)
        except AgentError as exc:
            if exc.code == "INVALID_INPUT" and exc.message == "Missing project file: " + self.prefix + name:
                fail("INVALID_INPUT", "Missing project file: " + name)
            raise


def history(store):
    validate_versions(store)
    records = [store.json(r["path"]) for r in index(store)]
    if records:
        return records
    # Read-only compatibility: earlier releases archived cards in checkpoints.
    for path in sorted(store.path("checkpoints").glob("*/state.json")):
        prefix = path.parent.relative_to(store.root).as_posix() + "/files/"
        archived = CheckpointStore(store, prefix)
        card = optional_json(archived, "reports/decision-brief.json")
        if card:
            value = subject(archived, card)
            fingerprint = digest(canonical(value))
            if not records or records[-1]["subject_sha256"] != fingerprint:
                state = store.json(path.relative_to(store.root).as_posix())
                records.append({"id": f"rev-{state['revision']:06}", "recorded_at": state["updated_at"],
                                "origin": "checkpoint", "subject_sha256": fingerprint, "subject": value,
                                "explanation": "Снимок прежней версии из checkpoint; отдельное объяснение изменения не сохранялось."})
    return records


def list_versions(store):
    return [{k: r[k] for k in ("id", "recorded_at", "origin", "explanation")} |
            {"title": r["subject"]["card"]["title"], "recommendation": r["subject"]["card"]["recommendation"]}
            for r in history(store)]


def compare(store, from_id=None, to_id=None):
    records = history(store)
    if not records:
        return {"has_previous": False, "message": "Сохранённых версий рекомендации ещё нет."}
    by_id = {r["id"]: r for r in records}
    newer = by_id.get(to_id) if to_id else records[-1]
    older = by_id.get(from_id) if from_id else (records[-2] if len(records) > 1 else None)
    if (to_id and newer is None) or (from_id and older is None):
        fail("INVALID_INPUT", "Неизвестная версия. Прочитайте versions и укажите сохранённые ID.")
    if older is None:
        return {"has_previous": False, "to": newer["id"], "message": "Это первый сохранённый вывод; прежний не выдумывается."}
    old, new = older["subject"], newer["subject"]
    before, after = old["card"], new["card"]
    grounds_before = {c["id"]: c for c in old["claims"]}
    grounds_after = {c["id"]: c for c in new["claims"]}
    added = [c for key, c in grounds_after.items() if key not in grounds_before]
    removed = [c for key, c in grounds_before.items() if key not in grounds_after]
    updated = [{"id": key, "before": grounds_before[key], "after": value}
               for key, value in grounds_after.items() if key in grounds_before and value != grounds_before[key]]
    return {"has_previous": True, "from": older["id"], "to": newer["id"],
            "explanation": newer["explanation"], "explanation_origin": newer.get("explanation_origin", "archive"),
            "recommendation": {"changed": before["recommendation"] != after["recommendation"],
                               "before": before["recommendation"], "after": after["recommendation"]},
            "grounds": {"added": added, "removed": removed, "updated": updated},
            "sources_changed": old["sources"] != new["sources"],
            "choice_changed": old["decision"] != new["decision"],
            "unknowns_added": [v for v in after["unknowns"] if v not in before["unknowns"]],
            "unknowns_removed": [v for v in before["unknowns"] if v not in after["unknowns"]],
            "steps_changed": before["first_steps"] != after["first_steps"],
            "updated_documents": [p for p in sorted(set(old["documents"]) | set(new["documents"])) if old["documents"].get(p) != new["documents"].get(p)],
            "scope": "Различия сохранённых версий; объяснение изменения предоставил агент, причинность автоматически не устанавливается."}
