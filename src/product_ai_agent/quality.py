"""Fresh, structured self-review plus reproducible integrity checks.

The host LLM supplies the semantic assessment. These checks establish that the
assessment is complete, references current material and exposes known limits;
they do not establish that a source entails a claim or that a strategy is good.
"""
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .storage import AgentError, digest, fail, now

MODES = ("diagnosis", "audience", "communication", "strategy")
REVIEW_PATH = "reports/quality-review.json"
SUBJECT_DOCUMENTS = (
    "brief.md", "research-plan.md", "reports/analysis.md", "reports/options.md",
    "reports/strategy.md", "reports/validation-plan.md", "reports/run-summary.md",
)
CHECK_IDS = {"evidence", "alternatives", "promises", "next_step"}
SCOPE = (
    "Автоматические проверки охватывают полноту, ссылки на активные основания, явные ограничения "
    "и соответствие самопроверки текущим материалам. Содержательные оценки предоставляет агент "
    "в готовой среде; независимая экспертиза и истинность выводов этим не подтверждены."
)


class ReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReviewCheck(ReviewRecord):
    id: Literal["evidence", "alternatives", "promises", "next_step"]
    status: Literal["pass", "revise", "unknown"]
    explanation: str = Field(min_length=20, max_length=2000)
    claim_ids: list[str] = Field(default_factory=list, max_length=40)
    limitation: str | None = Field(default=None, min_length=8, max_length=1000)


class ReviewAlternative(ReviewRecord):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
    label: str = Field(min_length=3, max_length=200)
    action: str = Field(min_length=15, max_length=1400)
    objection: str = Field(min_length=20, max_length=1400)
    kind: Literal["keep", "close", "change", "investigate"]


class ReviewedPromise(ReviewRecord):
    text: str = Field(min_length=10, max_length=1400)
    claim_ids: list[str] = Field(min_length=1, max_length=40)


class QualityReview(ReviewRecord):
    strongest_objection: str = Field(min_length=20, max_length=2000)
    response_to_objection: str = Field(min_length=20, max_length=2000)
    checks: list[ReviewCheck] = Field(min_length=4, max_length=4)
    alternatives: list[ReviewAlternative] = Field(min_length=2, max_length=12)
    promises: list[ReviewedPromise] = Field(default_factory=list, max_length=20)
    promises_applicability: str = Field(min_length=15, max_length=1400)

    @model_validator(mode="after")
    def unique_entries(self):
        if {item.id for item in self.checks} != CHECK_IDS:
            raise ValueError("Provide exactly one assessment for each of the four review criteria.")
        ids = [item.id for item in self.alternatives]
        actions = [" ".join(item.action.casefold().split()) for item in self.alternatives]
        if len(set(ids)) != len(ids) or len(set(actions)) != len(actions):
            raise ValueError("Alternatives need unique IDs and different actions.")
        for item in [*self.checks, *self.promises]:
            if any(not value.strip() for value in item.claim_ids) or len(set(item.claim_ids)) != len(item.claim_ids):
                raise ValueError("Claim references must be nonempty and unique within each review item.")
        return self


class SavedReview(ReviewRecord):
    schema_version: Literal[1] = 1
    mode: Literal["diagnosis", "audience", "communication", "strategy"]
    subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    assessment: QualityReview

    @field_validator("created_at")
    @classmethod
    def dated_review(cls, value):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Review timestamp needs an explicit timezone.")
        return value


def _optional_read(store, name):
    # Overlay can contain a prepared document that is not on disk yet.
    try:
        return store.read(name)
    except FileNotFoundError:
        return None
    except AgentError as exc:
        if exc.code == "INVALID_INPUT" and exc.message.startswith("Missing project file:"):
            return None
        raise


def subject_sha256(store, brief, mode):
    """Hash the exact input to self-review, independent of JSON key order."""
    if mode not in MODES:
        fail("VALIDATION_ERROR", "Неизвестный сценарий проверки рекомендации.")
    documents = {}
    for name in SUBJECT_DOCUMENTS:
        data = _optional_read(store, name)
        if data is not None:
            documents[name] = digest(data)
    claims = [row for row in store.registry("claims.jsonl") if row["status"] == "active"]
    material = {
        "brief": brief, "mode": mode, "documents": documents,
        "claims": sorted(claims, key=lambda row: row["id"]),
        "sources": sorted(store.registry("sources.jsonl"), key=lambda row: row["id"]),
        "decisions": store.registry("decisions.jsonl"),
    }
    return digest(json.dumps(material, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _report(blockers=None, warnings=None, assessment=None):
    blockers, warnings = blockers or [], warnings or []
    return {"ready": not blockers,
            "verdict": "revise" if blockers else ("ready_with_limits" if warnings else "ready"),
            "blockers": blockers, "warnings": warnings, "assessment": assessment, "scope": SCOPE}


def _evaluate(store, brief, mode, saved):
    blockers, warnings = [], []

    def block(code, message):
        blockers.append({"code": code, "message": message})

    if saved.mode != mode or saved.subject_sha256 != subject_sha256(store, brief, mode):
        block("REVIEW_STALE", "Проверка относится к прежним выводам или материалам. Проведите её заново.")
    assessment = saved.assessment
    active = {row["id"]: row for row in store.registry("claims.jsonl") if row["status"] == "active"}
    for reason in brief.get("reasons", []):
        refs = reason.get("claim_ids", [])
        if not refs or any(ref not in active for ref in refs):
            block("REASON_REFERENCE", "Основание рекомендации отсутствует либо отозвано.")
    if not brief.get("reasons"):
        block("REASONS_MISSING", "У рекомендации нет явных проверяемых оснований.")
    for item in [*assessment.checks, *assessment.promises]:
        if any(ref not in active for ref in item.claim_ids):
            block("REVIEW_REFERENCE", "Проверка ссылается на отсутствующее или неактивное утверждение.")
    evidence_check = next(item for item in assessment.checks if item.id == "evidence")
    if not evidence_check.claim_ids:
        block("EVIDENCE_REFERENCE", "Проверка оснований должна назвать проверенные утверждения.")

    minimum = {"diagnosis": 2, "communication": 3, "audience": 4, "strategy": 4}[mode]
    kinds = {item.kind for item in assessment.alternatives}
    if len(assessment.alternatives) < minimum or "keep" not in kinds:
        block("ALTERNATIVES_INCOMPLETE", "Нужны разные варианты, включая сохранение текущего положения.")
    if mode != "diagnosis" and "close" not in kinds:
        block("CLOSE_OPTION_MISSING", "Нужно явно рассмотреть прекращение направления или коммуникации.")
    if mode != "diagnosis" and not assessment.promises:
        block("PROMISES_MISSING", "Проверьте хотя бы одно продуктовое или коммуникационное обещание.")
    for promise in assessment.promises:
        if any(active.get(ref, {}).get("type") not in {"fact", "observation"} for ref in promise.claim_ids):
            block("PROMISE_UNSUPPORTED", "Обещание должно опираться на факты или наблюдения; гипотеза не подтверждает возможность продукта.")

    steps = brief.get("first_steps", [])
    if not steps or any(not isinstance(step.get(key), str) or not step[key].strip()
                        for step in steps for key in ("action", "deliverable", "when")):
        block("NEXT_STEP_INCOMPLETE", "Для следующего шага нужны действие, ожидаемый результат и срок либо условие начала.")
    disclosures = [brief.get("confidence", ""), *brief.get("unknowns", [])]
    for check in assessment.checks:
        if check.status == "revise":
            block("SELF_REVIEW_REVISE", f"Агент обнаружил проблему по критерию {check.id}: {check.explanation}")
        elif check.status == "unknown":
            if not check.limitation or not any(check.limitation.casefold() in str(text).casefold() for text in disclosures):
                block("UNDISCLOSED_LIMIT", f"Неопределённость по критерию {check.id} должна быть явно указана в карточке.")
            else:
                warnings.append({"code": "DISCLOSED_LIMIT", "criterion": check.id, "message": check.limitation})
    return _report(blockers, warnings, assessment.model_dump())


def prepare_review(store, brief, assessment, mode):
    """Validate a host assessment and return data to save atomically with its subject.

    No target files are written here. Call on the final prepared Overlay, then
    include the returned record in the same transaction as the decision card.
    """
    try:
        review = QualityReview.model_validate(assessment)
    except ValidationError:
        fail("VALIDATION_ERROR", "Проверка рекомендации неполная: заполните четыре критерия, возражение, ответ и разные варианты.")
    saved = SavedReview(mode=mode, subject_sha256=subject_sha256(store, brief, mode),
                        created_at=now(), assessment=review)
    report = _evaluate(store, brief, mode, saved)
    if not report["ready"]:
        fail("VALIDATION_ERROR", "Проверка рекомендации: " + " ".join(item["message"] for item in report["blockers"]))
    return saved.model_dump()


def evaluate_review(store, brief, mode):
    """Read-only readiness report. Missing or stale reviews never pass silently."""
    try:
        payload = _optional_read(store, REVIEW_PATH)
        if payload is None:
            return _report([{"code": "REVIEW_MISSING", "message": "Перед выдачей рекомендации проведите её содержательную проверку."}])
        saved = SavedReview.model_validate_json(payload)
        return _evaluate(store, brief, mode, saved)
    except (ValidationError, ValueError, KeyError, TypeError):
        return _report([{"code": "REVIEW_INVALID", "message": "Сохранённая проверка рекомендации повреждена или имеет неверный формат."}])
    except AgentError as exc:
        return _report([{"code": "REVIEW_INPUT_INVALID", "message": exc.message}])
