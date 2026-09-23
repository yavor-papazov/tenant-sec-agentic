"""Pydantic schemas for LLM structured outputs (ADK output_schema)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AssessmentStatus = Literal[
    "assessed",
    "unknown",
    "conflicting",
    "not_assessed",
    "not_applicable",
    "out_of_scope",
]


class DocFetchedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(default="", max_length=2048)
    title: str = Field(default="", max_length=120)
    relevance: Literal["high", "medium", "low"] = "medium"


class DocFetchStructured(BaseModel):
    docs_fetched: list[DocFetchedItem] = Field(
        default_factory=list,
        max_length=3,
    )
    services_with_docs: list[str] = Field(default_factory=list)
    services_without_docs: list[str] = Field(default_factory=list)
    doc_quality: Literal["comprehensive", "adequate", "thin", "absent", "error"] = "absent"


class ServiceScore(BaseModel):
    status: AssessmentStatus = "unknown"
    score: int | None = Field(
        ge=0,
        le=3,
        description="Required: 0-3 when assessed, otherwise null",
    )
    evidence: str = ""
    confidence: Literal["high", "medium", "low"] = "low"
    sources_used: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def score_matches_status(self) -> "ServiceScore":
        if self.status == "assessed" and self.score is None:
            raise ValueError("assessed service requires score")
        if self.status != "assessed" and self.score is not None:
            raise ValueError("non-assessed service prohibits score")
        return self


class AssessorEvidenceItem(BaseModel):
    id: str = Field(pattern=r"^ev-[a-z0-9-]+$")
    url: str
    title: str = Field(max_length=160)
    source_class: Literal[
        "regulator",
        "binding",
        "api",
        "technical",
        "support",
        "secondary",
    ]
    quote: str = Field(min_length=1, max_length=1200)
    services: list[str] = Field(default_factory=list)


class AssessmentClaim(BaseModel):
    id: str = Field(pattern=r"^cl-[a-z0-9-]+$")
    assertion: str = Field(min_length=1, max_length=500)
    result: Literal["supported", "unsupported", "contradicted"]
    evidence_item_ids: list[str] = Field(min_length=1)
    services: list[str] = Field(default_factory=list)


class CriterionResult(BaseModel):
    level: int = Field(ge=0, le=3)
    met: bool
    reasoning: str = Field(min_length=1, max_length=800)
    claim_ids: list[str] = Field(default_factory=list)


class AssessorStructured(BaseModel):
    status: AssessmentStatus
    score: int | Literal["mixed"] | None = Field(
        description=(
            "Required when status is assessed; null for every non-assessed "
            "status"
        )
    )
    services: dict[str, ServiceScore] = Field(default_factory=dict)
    evidence: str
    confidence: Literal["high", "medium", "low"] = "low"
    flags: list[str] = Field(default_factory=list)
    deep_research_recommended: bool = False
    sources_used: list[str] = Field(default_factory=list)
    evidence_items: list[AssessorEvidenceItem] = Field(default_factory=list)
    claims: list[AssessmentClaim] = Field(default_factory=list)
    criteria_results: list[CriterionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def score_matches_status(self) -> "AssessorStructured":
        if self.status == "assessed" and self.score is None:
            raise ValueError("assessed result requires score")
        if self.status != "assessed" and self.score is not None:
            raise ValueError("non-assessed result prohibits score")
        if self.score == "mixed" and not self.services:
            raise ValueError("mixed result requires services")
        if self.status == "assessed":
            if not self.evidence_items or not self.claims:
                raise ValueError(
                    "assessed result requires evidence_items and claims"
                )
            if {result.level for result in self.criteria_results} != {
                0,
                1,
                2,
                3,
            }:
                raise ValueError(
                    "assessed result requires criteria results for L0-L3"
                )
            evidence_ids = {item.id for item in self.evidence_items}
            for claim in self.claims:
                if set(claim.evidence_item_ids) - evidence_ids:
                    raise ValueError(
                        f"claim {claim.id} references unknown evidence"
                    )
            claim_ids = {claim.id for claim in self.claims}
            for result in self.criteria_results:
                if set(result.claim_ids) - claim_ids:
                    raise ValueError(
                        f"L{result.level} references unknown claims"
                    )
        return self


class SkepticFlag(BaseModel):
    type: str = "unknown"
    detail: str = ""
    severity: Literal["info", "warning", "critical"] = "info"


class SkepticStructured(BaseModel):
    status: AssessmentStatus
    score: int | Literal["mixed"] | None = Field(
        description=(
            "Required when status is assessed; prohibited for every "
            "non-assessed status"
        ),
    )
    services: dict[str, ServiceScore] = Field(default_factory=dict)
    reasoning: str
    skepticism_flags: list[SkepticFlag] = Field(default_factory=list)
    deep_research_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def score_matches_status(self) -> "SkepticStructured":
        if self.status == "assessed" and self.score is None:
            raise ValueError("assessed result requires score")
        if self.status != "assessed" and self.score is not None:
            raise ValueError("non-assessed result prohibits score")
        return self


class ConsistencyFlag(BaseModel):
    type: str = ""
    reference_provider: str = ""
    reference_score: (
        int | Literal["mixed", "L0", "L1", "L2", "L3"] | None
    ) = None
    current_score: (
        int | Literal["mixed", "L0", "L1", "L2", "L3"] | None
    ) = None
    detail: str = ""
    recommendation: str = ""


class ConsistencyStructured(BaseModel):
    consistency_flags: list[ConsistencyFlag] = Field(default_factory=list)
    deep_research_triggered: bool = False
    overall_consistency: Literal["consistent", "suspicious", "inconsistent"] = "consistent"


class QuestionFinding(BaseModel):
    question: str = ""
    answer: str = ""
    sources: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "unresolved"] = "unresolved"


class DeepResearchStructured(BaseModel):
    questions_investigated: list[QuestionFinding] = Field(default_factory=list)
    revised_assessment: AssessorStructured | None = None
    unresolved: list[str] = Field(default_factory=list)
