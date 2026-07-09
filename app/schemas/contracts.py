"""Typed API contracts — mirrors openapi.yaml v1.0 (A3 contract freeze, task #3).

Contract invariants (enforced by tests/contract/):
- Dashboard ALWAYS returns all four scoring-module keys, each ok|unavailable.
- A recommendation always carries a non-empty per_module_breakdown.
- Single failed module => HTTP 200 with the module flagged; 503 reserved for
  total API/DB outage. Out-of-scope ticker => 404 SECTOR_NOT_COVERED.

Note on numerics: DB stores NUMERIC (decimal-safe); the JSON edge uses float
per openapi.yaml `number`. All money-path arithmetic (engine, Step 4+) is
Decimal internally; floats appear only at serialization.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

SCORING_MODULES = ("technical", "fundamental", "chip", "news")

# FR-39: the served value is compliance-owned config (Settings.disclaimer_text,
# A3 ruling; ASCII-only per A8 final) — this mirror is the pydantic model default.
from app.core.config import get_settings  # noqa: E402

DISCLAIMER = get_settings().disclaimer_text
DISCLAIMER_VERSION = get_settings().disclaimer_version


class ModuleStatus(StrEnum):
    ok = "ok"
    unavailable = "unavailable"


class CompositeCall(StrEnum):
    STRONG_SELL = "STRONG_SELL"
    SELL = "SELL"
    HOLD = "HOLD"
    BUY = "BUY"
    STRONG_BUY = "STRONG_BUY"
    SUPPRESSED = "SUPPRESSED"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PerModuleBreakdown(BaseModel):
    module: Literal["technical", "fundamental", "chip", "news"]
    signal_score: float | None = Field(None, ge=-2.0, le=2.0)
    weight_assigned: float
    weight_effective: float  # renormalised when a module is unavailable
    status: ModuleStatus
    # v1.2.6 §9 intra-module completeness: distinct from availability — a
    # subfields-partial module (chip nets-only) stays ok/in-composite but
    # discloses the gap (GF-CHIP-PARTIAL-intra-module).
    subfields_complete: bool = True
    subfields_note: str | None = None


class TargetPrice(BaseModel):
    bear: float | None = None
    base: float | None = None
    bull: float | None = None


class Recommendation(BaseModel):
    # Nullable by design: a SUPPRESSED row ("Analysis Only — Insufficient
    # Data") carries NO score/target/confidence (contract v1.1+, FR-35/37).
    composite_signal: float | None = Field(None, ge=-2.0, le=2.0)
    composite_call: CompositeCall
    target_price: TargetPrice | None = None
    confidence_level: ConfidenceLevel | None = None
    confidence_pct: float | None = None
    conflict_flag: bool = False
    reduced_confidence: bool = False
    horizon_months: Literal[3, 6, 12] = 6
    data_completeness: float = Field(ge=0.0, le=1.0)
    methodology_version: str
    per_module_breakdown: list[PerModuleBreakdown] = Field(min_length=1)
    suppressed_reason: str | None = None


class ModuleSummary(BaseModel):
    status: ModuleStatus
    signal_score: float | None = Field(None, ge=-2.0, le=2.0)
    headline_metric: str | None = None


class DashboardModules(BaseModel):
    technical: ModuleSummary
    fundamental: ModuleSummary
    chip: ModuleSummary
    news: ModuleSummary


class Dashboard(BaseModel):
    ticker: str
    rec_date: date | None
    recommendation: Recommendation | None
    modules: DashboardModules
    supply_chain_available: bool = False
    # ADR-009 (task #20, A3 additive ruling): server-authoritative Refresh
    # availability — ISO timestamp while inside the on-demand cooldown, null
    # once Refresh is available. Never derived client-side (drift = dead-end).
    next_refresh_at: datetime | None = None
    disclaimer: str = DISCLAIMER  # FR-39: server includes disclaimer in payload
    disclaimer_version: str = DISCLAIMER_VERSION  # audit trace (criterion 7)


class ModuleDetail(BaseModel):
    """Base lens-detail envelope (contract v1.2.1); per-lens extended fields
    (TechnicalDetail latest/series, FundamentalDetail ratios, ...) are filled
    by the ingestion steps (tasks #8/#9/#12)."""

    module: str
    status: ModuleStatus
    signal_score: float | None = Field(None, ge=-2.0, le=2.0)
    as_of: date | None = None
    series: list[dict] = []


class SupplyChainNode(BaseModel):
    id: int
    name: str
    ticker: str | None = None
    role: Literal["fab", "upstream_supplier", "downstream_customer"]


class SupplyChainEdge(BaseModel):
    from_: int = Field(alias="from")
    to: int
    relationship_type: str

    model_config = {"populate_by_name": True}


class SupplyChainGraph(BaseModel):
    nodes: list[SupplyChainNode] = []
    edges: list[SupplyChainEdge] = []


class BacktestResult(BaseModel):
    window_months: Literal[3, 6, 12] = 6
    rolling_accuracy_full: float | None = None  # NULL when insufficient history
    rolling_accuracy_partial: float | None = None
    estimated_return: float | None = None
    benchmark: str
    insufficient_history: bool = True
    methodology_version: str
    disclaimer: str = DISCLAIMER
    disclaimer_version: str = DISCLAIMER_VERSION


class DecisionAnnotation(BaseModel):
    decision: Literal["followed", "ignored", "partial"]
    transaction_price: float | None = None  # encrypted at rest (NFR-05)
    notes: str | None = None  # encrypted at rest (NFR-05)


class RecommendationLogEntry(Recommendation):
    ticker: str
    rec_date: date
    annotation: DecisionAnnotation | None = None


class ModuleWeights(BaseModel):
    technical: float = Field(0.30, ge=0.0, le=1.0)
    fundamental: float = Field(0.30, ge=0.0, le=1.0)
    chip: float = Field(0.25, ge=0.0, le=1.0)
    news: float = Field(0.15, ge=0.0, le=1.0)


class WeightConfig(BaseModel):
    module_weights: ModuleWeights = ModuleWeights()
    horizon_months: Literal[3, 6, 12] = 6


class PipelineSource(BaseModel):
    source_name: str
    status: Literal["ok", "unavailable", "running", "error", "never_run"]
    consecutive_bad_days: int = 0  # R-01: alert after 2


class PipelineStatus(BaseModel):
    run_date: date | None = None
    completed_at: datetime | None = None
    sla_met: bool | None = None  # completed by 07:00 TW (NFR-01)
    sources: list[PipelineSource] = []


class ApiError(BaseModel):
    # COVERAGE_POOL_FULL: FR-61 at-cap 409 (A3 additive ruling, adopted task #14).
    code: Literal["SECTOR_NOT_COVERED", "UNAUTHORIZED", "VALIDATION_ERROR",
                  "TOTAL_OUTAGE", "COVERAGE_POOL_FULL"]
    message: str


class LoginRequest(BaseModel):
    username: str
    password: str
    client: Literal["web", "ios"]


class TokenBundle(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
