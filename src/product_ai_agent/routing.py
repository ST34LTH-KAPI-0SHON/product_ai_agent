"""Small explicit task contracts. The host LLM selects a route, not a keyword rule."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .models import STAGES

MODES = ("strategy", "diagnosis", "audience", "communication")
LABELS = {"strategy": "Продуктовая и коммуникационная стратегия",
          "diagnosis": "Диагностика и план проверки",
          "audience": "Выбор аудитории",
          "communication": "Коммуникация и проверка обещаний"}
DIAGNOSIS_STAGES = ["brief", "research", "analysis", "complete"]


class TaskRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    workflow_mode: Literal["strategy", "diagnosis", "audience", "communication"]
    quantitative: StrictBool = False
    routing_reason: str = Field(min_length=1, max_length=1200)


def route_config(store):
    # Projects saved before 0.3 retain their original completion contract.
    request = store.json("request.json") if store.path("request.json").is_file() else {}
    if "workflow_mode" not in request:
        return {"mode": "strategy", "label": LABELS["strategy"], "legacy": True,
                "stages": list(STAGES), "quantitative": True, "quality_required": False,
                "minimum_options": 4, "reason": "Сохранённый процесс V0 без изменения контракта."}
    route = TaskRoute.model_validate({key: request[key] for key in
                                     ["workflow_mode", "quantitative", "routing_reason"] if key in request})
    mode = route.workflow_mode
    return {"mode": mode, "label": LABELS[mode], "legacy": False,
            "stages": list(DIAGNOSIS_STAGES if mode == "diagnosis" else STAGES),
            "quantitative": route.quantitative, "quality_required": True,
            "minimum_options": 3 if mode == "communication" else 4,
            "reason": route.routing_reason}


def stage_requirements(route):
    required = {"brief": ["brief.md"], "research": ["research-plan.md"],
                "options": ["reports/options.md"], "strategy": ["reports/strategy.md"],
                "complete": ["reports/strategy.md", "reports/validation-plan.md", "reports/run-summary.md"]}
    if not route["legacy"]:
        required["analysis"] = ["reports/analysis.md"]
    if route["mode"] == "diagnosis":
        required["complete"] = ["reports/analysis.md", "reports/validation-plan.md", "reports/run-summary.md"]
    return required
