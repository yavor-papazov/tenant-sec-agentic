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

    url: str = ""
    title: str = ""
    relevance: Literal["high", "medium", "low"] = "medium"


class DocFetchStructured(BaseModel):
    docs_fetched: list[DocFetchedItem] = Field(default_factory=list)
    services_with_docs: list[str] = Field(default_factory=list)
    services_without_docs: list[str] = Field(default_factory=list)
    doc_quality: Literal["comprehensive", "adequate", "thin", "absent", "error"] = "absent"


class ServiceScore(BaseModel):
    status: AssessmentStatus = "unknown"
    score: int | None = Field(default=None, ge=0, le=3)
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

    @model_validator(mode="after")
    def score_matches_status(self) -> "AssessorStructured":
        if self.status == "assessed" and self.score is None:
            raise ValueError("assessed result requires score")
        if self.status != "assessed" and self.score is not None:
            raise ValueError("non-assessed result prohibits score")
        if self.score == "mixed" and not self.services:
            raise ValueError("mixed result requires services")
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
    reference_score: int | Literal["mixed"] | None = None
    current_score: int | Literal["mixed"] | None = None
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
