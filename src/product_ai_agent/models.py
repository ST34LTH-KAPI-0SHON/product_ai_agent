"""Version-one on-disk records. Semantic research review remains separate."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

STAGES = ["brief", "research", "analysis", "options", "awaiting_decision", "strategy", "complete"]
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1


class State(Record):
    project_id: str = Field(pattern=IDENTIFIER)
    revision: StrictInt = Field(ge=0)
    stage: Literal["brief", "research", "analysis", "options", "awaiting_decision", "strategy", "complete"]
    status: Literal["running", "paused", "awaiting_user", "blocked", "completed"]
    completed_stages: list[str]
    next_action: str = Field(min_length=1)
    open_questions: list[str]
    artifacts: dict[str, str]
    pending_decision: dict | None
    last_completed_operation_id: str | None
    updated_at: str
    transition_reason: str | None = None


class Policy(Record):
    allowed_urls: list[str] = []
    max_bytes: StrictInt = Field(default=10485760, gt=0, le=10485760)
    timeout_seconds: StrictInt = Field(default=20, gt=0, le=20)
    max_redirects: StrictInt = Field(default=5, ge=0, le=5)
    max_retries: StrictInt = Field(default=2, ge=0, le=2)


class Source(Record):
    id: str = Field(pattern=IDENTIFIER)
    kind: Literal["document", "table", "web"]
    title: str
    origin: dict[str, str]
    final_url: str | None
    retrieved_at: str
    published_at: str | None
    snapshot_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extracted_path: str
    extracted_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_version: str
    provenance: Literal["public", "user_provided", "synthetic"]
    synthetic: bool
    warnings: list[str]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    locator: str


class Claim(Record):
    id: str = Field(pattern=IDENTIFIER)
    text: str = Field(min_length=1)
    type: Literal["fact", "observation", "hypothesis", "forecast", "recommendation", "assumption"]
    evidence: list[Evidence]
    calculation_ids: list[str]
    basis_claim_ids: list[str]
    limitations: list[str]
    status: Literal["active", "superseded", "retracted"]
    supersedes: str | None
    created_at: str


class Decision(Record):
    id: str = Field(pattern=IDENTIFIER)
    question: str = Field(min_length=1)
    options: list[dict] = Field(min_length=2)
    selected_option_id: str
    actor: dict
    decided_at: str
    basis_claim_ids: list[str]
    review_conditions: list[str]
    owner: str | None
    metrics: list[str] | None
    confirmation: dict


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    operator: Literal["eq", "in", "gte", "lte"]
    value: str | int | float | bool | list[str | int | float | bool]


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    metric_name: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    sheet: str | None = None
    group_by: list[str] = []
    operation: Literal["count_rows", "sum", "mean", "ratio_of_sums"]
    value_column: str | None = None
    numerator_column: str | None = None
    denominator_column: str | None = None
    filters: list[Filter] = []
    missing_policy: Literal["error", "exclude"] = "error"
    units: str = Field(min_length=1)
    period: dict | None = None
    eligibility_column: str | None = None
    bounded_ratio: bool = False
