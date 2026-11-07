"""Explicit commitments and a read-only next-action view. No background scheduler."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .models import IDENTIFIER
from .storage import AgentError, digest, encode, fail, now


def optional_json(store, name):
    try:
        return store.json(name)
    except AgentError as exc:
        if exc.code == "INVALID_INPUT" and exc.message == "Missing project file: " + name:
            return None
        raise


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Use an ISO timestamp with timezone, not an ambiguous local date.")
    return parsed


class Commitment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=IDENTIFIER)
    action: str = Field(min_length=3, max_length=700)
    deliverable: str = Field(min_length=3, max_length=700)
    owner: str | None = Field(default=None, min_length=1, max_length=160)
    due_at: str | None = None
    status: Literal["proposed", "open", "done", "cancelled"] = "proposed"
    blocked_reason: str | None = Field(default=None, min_length=3, max_length=700)
    confirmation_quote: str | None = Field(default=None, min_length=1, max_length=2000)
    confirmation_kind: Literal["user", "synthetic"] = "user"
    resolution: str | None = Field(default=None, min_length=3, max_length=1000)
    basis_version: str | None = Field(default=None, min_length=1, max_length=96)

    @field_validator("due_at")
    @classmethod
    def aware_deadline(cls, value):
        if value is not None:
            timestamp(value)
        return value

    @model_validator(mode="after")
    def explicit_commitment(self):
        if (self.status != "proposed" or self.owner or self.due_at) and not self.confirmation_quote:
            raise ValueError("Assignment, deadline and status need the actual human instruction or confirmation.")
        if self.status in {"done", "cancelled"} and not self.resolution:
            raise ValueError("Closing an obligation needs an explicit result or cancellation reason.")
        if self.status in {"done", "cancelled"} and self.blocked_reason:
            raise ValueError("Closed obligations cannot remain blocked.")
        return self


class TrackingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=0)
    tasks: list[Commitment] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({t.id for t in self.tasks}) != len(self.tasks):
            raise ValueError("A task can be updated only once in a single transaction.")
        return self


def validate_tracking(store):
    record = optional_json(store, "tasks.json")
    if record is None:
        return
    revision = record.get("revision")
    if type(revision) is not int or not 1 <= revision <= 10000 or record.get("schema_version") != 1:
        fail("VALIDATION_ERROR", "Повреждён реестр обязательств.")
    previous = None
    for number in range(1, revision + 1):
        saved = store.json(f"follow-up/rev-{number:06}.json")
        if saved.get("revision") != number or saved.get("previous_sha256") != previous:
            fail("VALIDATION_ERROR", "Нарушена история обязательств.")
        timestamp(saved["updated_at"])
        tasks = [Commitment.model_validate(t) for t in saved["tasks"]]
        if len(tasks) > 200 or len({t.id for t in tasks}) != len(tasks):
            fail("VALIDATION_ERROR", "Повторяющиеся обязательства.")
        previous = digest(encode(saved))
    if saved != record:
        fail("VALIDATION_ERROR", "Текущее состояние обязательств не совпадает с последней версией.")


def prepare_tracking(store, raw, current_version):
    update = TrackingUpdate.model_validate(raw)
    validate_tracking(store)
    previous = optional_json(store, "tasks.json")
    revision = previous["revision"] if previous else 0
    if revision != update.expected_revision:
        fail("REVISION_CONFLICT", "Обязательства уже изменились. Прочитайте now и повторите обновление с текущей версией.")
    tasks = {t["id"]: t for t in previous["tasks"]} if previous else {}
    for task in update.tasks:
        value = task.model_dump()
        if value["confirmation_kind"] == "synthetic" and not (optional_json(store, "request.json") or {}).get("synthetic"):
            fail("VALIDATION_ERROR", "Учебные подтверждения допустимы только в явно синтетическом проекте.")
        if value["basis_version"] and value["basis_version"] not in {current_version, tasks.get(task.id, {}).get("basis_version")}:
            fail("VALIDATION_ERROR", "Обязательство должно ссылаться на текущий вывод или свою сохранённую прежнюю версию.")
        value["basis_version"] = value["basis_version"] or tasks.get(task.id, {}).get("basis_version") or current_version
        tasks[task.id] = value
    if len(tasks) > 200:
        fail("LIMIT_EXCEEDED", "В одном исследовании поддерживается до 200 обязательств.")
    record = {"schema_version": 1, "revision": revision + 1, "updated_at": now(),
              "previous_sha256": digest(encode(previous)) if previous else None,
              "tasks": list(tasks.values())}
    writes = {"tasks.json": encode(record), f"follow-up/rev-{revision + 1:06}.json": encode(record)}
    return {"revision": revision + 1, "tasks": len(tasks)}, writes, []


def next_actions(store, card=None, quality=None, as_of=None):
    """Read current dates/status without mutating a project, including legacy studies."""
    instant = timestamp(as_of) if as_of else datetime.now(timezone.utc)
    state = store.state()
    record = optional_json(store, "tasks.json")
    validate_tracking(store)
    from .versions import current_reference
    reference = current_reference(store)
    if record:
        tasks = record["tasks"]
    else:
        tasks = [{"id": f"step-{i}", "action": s["action"], "deliverable": s["deliverable"],
                  "owner": None, "due_at": None, "status": "proposed", "blocked_reason": None,
                  "basis_version": reference, "when": s["when"]}
                 for i, s in enumerate((card or {}).get("first_steps", []), 1)]
    shown = []
    for task in tasks:
        active = task["status"] in {"open", "proposed"}
        shown.append({**task,
                      "overdue": bool(active and task.get("due_at") and timestamp(task["due_at"]) < instant),
                      "needs_owner": bool(active and not task.get("owner")),
                      "needs_date": bool(active and not task.get("due_at")),
                      "needs_recheck": bool(active and task.get("basis_version") and task["basis_version"] != reference)})
    shown.sort(key=lambda t: (t["status"] in {"done", "cancelled"}, not t["overdue"],
                              t.get("due_at") or "9999", t["id"]))
    review = optional_json(store, "review.json")
    pending_review = bool(review and review.get("status") == "pending")
    stale = bool(card and quality and quality.get("required") and not quality["ready"])
    choice = state["pending_decision"]
    if pending_review or stale:
        primary = {"kind": "review", "action": "Перепроверить рекомендацию по текущим данным и решению.",
                   "reason": "Прежние основания или самопроверка требуют пересмотра."}
    elif choice:
        primary = {"kind": "choice", "action": choice["question"], "reason": "Нужен существенный выбор человека."}
    else:
        active = [t for t in shown if t["status"] == "open"]
        if any(t["needs_recheck"] for t in active):
            primary = {"kind": "review_tasks", "action": "Сверить обязательства с новой версией рекомендации.",
                       "reason": "Часть задач согласована на прежних основаниях."}
        elif active:
            task = active[0]
            primary = {"kind": "task", "task_id": task["id"],
                       "action": "Устранить препятствие: " + task["blocked_reason"] if task.get("blocked_reason") else task["action"],
                       "reason": "Срок прошёл; уточните состояние, не отмечайте выполнение автоматически." if task["overdue"] else task["deliverable"]}
        elif any(t["status"] == "proposed" for t in shown):
            primary = {"kind": "proposal", "action": "Согласовать ближайшее предложенное действие и ответственного.",
                       "reason": "Рекомендация ещё не стала обязательством человека."}
        elif record:
            primary = {"kind": "monitor", "action": "Проверить условия пересмотра и новые данные.",
                       "reason": "Все сохранённые обязательства закрыты; завершённые задачи не повторяются."}
        else:
            primary = {"kind": "continue", "action": state["next_action"], "reason": "Продолжить с сохранённого места."}
    return {"as_of": instant.isoformat(), "snapshot_at": record["updated_at"] if record else state["updated_at"],
            "tasks_revision": record["revision"] if record else 0,
            "basis_version": reference, "primary": primary, "tasks": shown,
            "open_questions": state["open_questions"], "review_required": pending_review or stale,
            "note": "Это план и учёт подтверждений. Фонового мониторинга и автоматического выполнения нет."}
