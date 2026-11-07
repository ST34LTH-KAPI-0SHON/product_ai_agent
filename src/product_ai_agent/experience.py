"""A small host-facing interface. No model calls or autonomous agent loop."""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .ingestion import ingest_file
from .models import STAGES, Claim, State
from .storage import Store, atomic, digest, encode, fail, init_project, now, project_path, registry_bytes
from .workflow import checkpoint, record_claim, record_decision, resume, validation

LABELS = {"brief": "Уточняю задачу", "research": "Изучаю материалы", "analysis": "Проверяю объяснения",
          "options": "Сравниваю действия", "awaiting_decision": "Нужен ваш выбор", "strategy": "Готовлю план",
          "complete": "Решение и план готовы"}
DOCUMENTS = {"brief.md", "research-plan.md", "reports/analysis.md", "reports/options.md", "reports/strategy.md",
             "reports/validation-plan.md", "reports/run-summary.md"}


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1)
    deliverable: str = Field(min_length=1)
    when: str = Field(min_length=1)
    owner: str | None = None


class DecisionBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=180)
    recommendation: str = Field(min_length=1, max_length=700)
    reasons: list[Reason] = Field(min_length=1, max_length=6)
    confidence: str = Field(min_length=1, max_length=500)
    first_steps: list[Step] = Field(min_length=1, max_length=5)
    do_not: list[str] = Field(min_length=1)
    success_rule: str = Field(min_length=1)
    unknowns: list[str]
    revisit_when: list[str] = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    synthetic: bool


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    documents: dict[str, str] = {}
    claims: list[dict] = []
    decision_brief: DecisionBrief | None = None
    pending_decision: dict | None = None
    open_questions: list[str] = []
    next_action: str = Field(min_length=1)
    review_resolution: str | None = None


class Overlay(Store):
    """Validate a prepared multi-file update before publishing any target file."""
    def __init__(self, root, writes):
        super().__init__(root)
        self.writes = writes

    def read(self, name):
        self.path(name)
        return self.writes[name] if name in self.writes else super().read(name)


def operation(kind, data):
    return kind + "-" + digest(encode(data))[:24]


def set_active(root):
    workspace = Path.cwd().resolve()
    relative = root.relative_to(workspace).as_posix()
    target = workspace / ".agent" / "workspace.json"
    from .storage import no_links
    no_links(target)
    atomic(target, encode({"active_project": relative, "updated_at": now()}))


def active_project(value=None):
    if value:
        return project_path(value)
    config = Path.cwd() / ".agent/workspace.json"
    if not config.exists():
        fail("INVALID_INPUT", "Исследование ещё не выбрано. Начните новую задачу командой start или укажите --project.")
    from .storage import no_links
    no_links(config)
    return project_path(json.loads(config.read_text(encoding="utf-8"))["active_project"])


def session(value=None):
    if not value and not (Path.cwd()/'.agent/workspace.json').exists():
        candidates=[]
        for path in (Path.cwd()/'projects').glob('*/state.json'):
            try:
                state=Store(path.parent).state()
                candidates.append({'project':path.parent.relative_to(Path.cwd()).as_posix(),
                                   'progress':LABELS[state['stage']],'updated_at':state['updated_at']})
            except Exception:
                continue
        if len(candidates)==1:
            value=candidates[0]['project']
        else:
            return {'needs_project':True,'projects':candidates,
                    'message':'Выберите сохранённое исследование.' if candidates else 'Сохранённых исследований ещё нет. Можно начать демо или свою задачу.'}
    root = active_project(value)
    store = Store(root)
    context = resume(store)
    state = context["state"]
    summary = store.json("reports/decision-brief.json") if store.path("reports/decision-brief.json").is_file() else None
    review=store.json('review.json') if store.path('review.json').is_file() else None
    needs_review=bool(review and review.get('status')=='pending')
    return {"project": root.relative_to(Path.cwd()).as_posix(), "progress": 'Появились новые данные — нужно проверить решение' if needs_review else LABELS[state["stage"]],
            "stage": state["stage"], "status": state["status"], "next_action": state["next_action"],
            "pending_decision": state["pending_decision"], "decision_brief": summary,
            "open_questions": state["open_questions"], "read_paths": context["read_paths"], "needs_review":needs_review,"review":review,
            "instruction": "Read instructions/product.md. Speak plainly; keep tool details out of user-facing replies."}


def start(project=None, title="Новое исследование", question=None, materials=(), allowed_urls=(), synthetic=False, demo=False):
    from .web import canonical
    if demo:
        question = question or "Почему снизилась активация и какое действие выбрать для продукта?"
        title = "Командный ритм: снижение активации"
        base = Path.cwd() / "examples/activation"
        materials = [base / name for name in ["product.md", "metrics.csv", "notes.md"]]
        synthetic = True
    if not question or not question.strip():
        fail("INVALID_INPUT", "Нужен вопрос или решение, с которым требуется помощь.")
    # Inspect all explicit input files before creating a project. No arbitrary folder scans.
    prepared = []
    names = set()
    from .storage import no_links
    for item in materials:
        path = Path(item).absolute()
        no_links(path)
        if path.suffix.lower() not in {".md", ".txt", ".docx", ".csv", ".xlsx"} or not path.is_file():
            fail("UNSUPPORTED_FORMAT", f"Неподдерживаемый материал: {path.name}")
        if path.name.lower().startswith((".env", "credentials", "id_rsa")):
            fail("ACCESS_DENIED", "Секреты не могут быть материалами исследования.")
        if path.stat().st_size > 10485760:
            fail("LIMIT_EXCEEDED", "Материал превышает 10 МиБ.")
        if path.name in names:
            fail("INVALID_INPUT", "Материалы с одинаковыми именами нужно переименовать до импорта.")
        names.add(path.name)
        payload = path.read_bytes()
        from .ingestion import parse_file
        parse_file(payload, path.suffix.lower())
        prepared.append((path.name, payload))
    urls = list(dict.fromkeys(canonical(u) for u in allowed_urls))
    root = project_path(project or ("projects/local-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]))
    init_project(root, "start-project")
    store = Store(root)
    if store.path("request.json").exists():
        fail("INVALID_INPUT", "Этот проект уже начат. Используйте session для продолжения или другое имя.")
    request = {"title": title, "question": question, "synthetic": synthetic, "created_at": now()}
    writes = {"request.json": encode(request), "inputs/request.md": ("# Задача\n\n" + question + "\n").encode()}
    writes.update({"inputs/" + name: data for name, data in prepared})
    policy = store.policy(); policy["allowed_urls"] = urls
    writes["policy.json"] = encode(policy)
    store.mutate("start-materials", "start", {"request": request, "inputs": {n: digest(d) for n, d in prepared}, "urls": urls},
                 lambda: ({"prepared": len(prepared)}, writes, []))
    sources = []
    for name, data in prepared:
        params = {"name": name, "sha256": digest(data), "synthetic": synthetic}
        op = operation("import", params)
        result = store.mutate(op, "ingest-file", params, lambda n=name: ingest_file(store, "inputs/" + n, provenance="synthetic" if synthetic else "user_provided"))
        sources.append(result["data"])
    set_active(root)
    return {**session(str(root)), "imported_sources": sources, "allowed_urls": urls,
            "message": "Материалы подготовлены. Агент прочитает их и уточнит только то, что влияет на решение."}


def check_brief(store, brief):
    rows = {c["id"]: c for c in store.registry("claims.jsonl")}
    for reason in brief.reasons:
        if any(c not in rows or rows[c]["status"] != "active" for c in reason.claim_ids):
            fail("VALIDATION_ERROR", "Вывод ссылается на отсутствующее или отозванное основание.")
    if any(s["synthetic"] for s in store.registry("sources.jsonl")) and not brief.synthetic:
        fail("VALIDATION_ERROR", "Учебные основания должны быть обозначены в карточке решения.")


def save(project, input_path):
    root = active_project(project)
    store = Store(root)
    raw = store.json(input_path)
    update = Update.model_validate(raw)
    if update.stage not in STAGES:
        fail("INVALID_INPUT", "Неизвестный этап исследования.")
    if update.stage in {"awaiting_decision", "complete"} and not update.decision_brief and not store.path("reports/decision-brief.json").is_file():
        fail("INVALID_INPUT", "Перед выбором и завершением нужна понятная карточка рекомендации.")
    op = operation("save", raw)
    def action():
        writes = {}
        overlay = Overlay(root, writes)
        review=store.json('review.json') if store.path('review.json').is_file() else None
        if review and review.get('status')=='pending' and update.stage=='complete':
            if not update.decision_brief or not update.review_resolution or not update.review_resolution.strip():
                fail('VALIDATION_ERROR','Новые данные требуют явного пересмотра карточки и объяснения, что изменилось.')
            writes['review.json']=encode({**review,'status':'resolved','resolution':update.review_resolution,'resolved_at':now()})
        for name, text in update.documents.items():
            if name not in DOCUMENTS or not text.strip():
                fail("INVALID_INPUT", "Допустимы только непустые документы исследования.")
            writes[name] = text.encode("utf-8")
        for claim in update.claims:
            _, changes, _ = record_claim(overlay, claim)
            writes.update(changes)
        if update.decision_brief:
            check_brief(overlay, update.decision_brief)
            writes["reports/decision-brief.json"] = encode(update.decision_brief.model_dump())
        old = store.state()
        target = STAGES.index(update.stage)
        current = STAGES.index(old["stage"])
        if target < current:
            fail("INVALID_INPUT", "Для пересмотра завершённого этапа используйте checkpoint с явной причиной; историю не обнулять.")
        # The host can save several completed stages in one validated transaction.
        # Each intermediate checkpoint is still verified and archived.
        indices = list(range(current + 1, target + 1)) or [current]
        for index in indices:
            state = overlay.state()
            stage = STAGES[index]
            state.update(stage=stage, status="completed" if stage == "complete" else ("awaiting_user" if stage == "awaiting_decision" else "running"),
                         completed_stages=STAGES if stage == "complete" else STAGES[:index],
                         next_action=update.next_action, open_questions=update.open_questions,
                         pending_decision=update.pending_decision if stage == "awaiting_decision" else None)
            state["artifacts"].update({name: name for name in DOCUMENTS if name in writes or store.path(name).is_file()})
            if "reports/decision-brief.json" in writes or store.path("reports/decision-brief.json").is_file():
                state["artifacts"]["decision_brief"] = "reports/decision-brief.json"
            _, changes, _ = checkpoint(overlay, state, state["revision"], op)
            writes.update(changes)
        return {"stage": update.stage, "revision": overlay.state()["revision"]}, writes, []
    store.mutate(op, "save", raw, action)
    set_active(root)
    return session(str(root))


def choose(project, option, quote, actor=None):
    root = active_project(project); store = Store(root)
    state = store.state(); pending = state["pending_decision"]
    if state["stage"] != "awaiting_decision" or not pending:
        fail("INVALID_INPUT", "Сейчас нет ожидающего выбора. Сначала прочитайте session.")
    if not quote.strip():
        fail("INVALID_INPUT", "Нужна подлинная формулировка ответа пользователя.")
    raw = {"option": option, "quote": quote, "revision": state["revision"]}
    op = operation("choose", raw)
    def action():
        writes = {}; overlay = Overlay(root, writes)
        card=store.json('reports/decision-brief.json') if store.path('reports/decision-brief.json').is_file() else None
        basis=sorted({c for reason in card['reasons'] for c in reason['claim_ids']}) if card else []
        decision = {"schema_version": 1, "id": "decision-" + digest(encode(raw))[:20], "question": pending["question"],
                    "options": pending["options"], "selected_option_id": option, "actor": {"role": "user", "name": actor},
                    "decided_at": now(), "basis_claim_ids": basis, "review_conditions": card['revisit_when'] if card else [], "owner": None, "metrics": None,
                    "confirmation": {"kind": "chat", "quote": quote}}
        _, changes, _ = record_decision(overlay, decision); writes.update(changes)
        candidate = dict(state)
        candidate.update(stage="strategy", status="running", completed_stages=STAGES[:5], pending_decision=None,
                         next_action="Подготовить согласованный план выбранного действия и условия проверки.")
        _, changes, _ = checkpoint(overlay, candidate, state["revision"], op); writes.update(changes)
        return {"decision_id": decision["id"], "selected_option": option}, writes, []
    store.mutate(op, "choose", raw, action)
    return session(str(root))


def pause(project=None):
    root=active_project(project);store=Store(root);state=store.state()
    if state['status'] in {'paused','completed','awaiting_user'}:
        return session(str(root))
    raw={'revision':state['revision'],'stage':state['stage']};op=operation('pause',raw)
    def action():
        state['status']='paused'
        state['next_action']='Продолжить с сохранённого шага: '+state['next_action']
        return checkpoint(store,state,state['revision'],op)
    store.mutate(op,'pause',raw,action)
    return session(str(root))


def impact(project,source_id,replaces):
    root=active_project(project);store=Store(root)
    store.source(source_id);store.source(replaces)
    if source_id==replaces:fail('INVALID_INPUT','Нужны разные версии источника.')
    claims=store.registry('claims.jsonl');affected={c['id'] for c in claims if c['status']=='active' and any(e['source_id']==replaces for e in c['evidence'])}
    while True:
        expanded=affected|{c['id'] for c in claims if c['status']=='active' and affected.intersection(c['basis_claim_ids'])}
        if expanded==affected:break
        affected=expanded
    decisions=[d for d in store.registry('decisions.jsonl') if affected.intersection(d['basis_claim_ids'])]
    result={'new_source':source_id,'previous_source':replaces,'requires_review':bool(affected),
            'affected_claims':[{'id':c['id'],'text':c['text'],'type':c['type']} for c in claims if c['id'] in affected],
            'affected_decisions':[{'id':d['id'],'selected_option':d['selected_option_id']} for d in decisions],
            'next_action':'Сопоставить новые данные с этими основаниями, пересчитать показатели и предложить пересмотр. Старые результаты не подменять.' if affected else 'Прямых ссылок на прежний источник не найдено; смысловую связь должен проверить агент.'}
    if affected:
        params={'new_source':source_id,'previous_source':replaces}
        store.mutate(operation('impact',params),'impact',params,lambda:({'requires_review':True},
            {'review.json':encode({**result,'status':'pending','created_at':now()})},[]))
    return result
