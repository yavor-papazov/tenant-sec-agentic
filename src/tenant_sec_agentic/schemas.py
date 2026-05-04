"""Pydantic schemas for LLM structured outputs (ADK output_schema)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DocFetchedItem(BaseModel):
    url: str = ""
    title: str = ""
    content: str = ""
    relevance: Literal["high", "medium", "low"] = "medium"


class DocFetchStructured(BaseModel):
    docs_fetched: list[DocFetchedItem] = Field(default_factory=list)
    services_with_docs: list[str] = Field(default_factory=list)
    services_without_docs: list[str] = Field(default_factory=list)
    doc_quality: Literal["comprehensive", "adequate", "thin", "absent", "error"] = "absent"


class ServiceScore(BaseModel):
    score: int = Field(ge=0, le=3)
    evidence: str = ""


class AssessorStructured(BaseModel):
    score: int | Literal["mixed"] = 0
    services: dict[str, ServiceScore] = Field(default_factory=dict)
    evidence: str = ""
    confidence: Literal["high", "medium", "low"] = "low"
    flags: list[str] = Field(default_factory=list)
    deep_research_recommended: bool = False
    sources_used: list[str] = Field(default_factory=list)


class SkepticFlag(BaseModel):
    type: str = "unknown"
    detail: str = ""
    severity: Literal["info", "warning", "critical"] = "info"


class SkepticStructured(BaseModel):
    verdict: Literal["confirm", "downgrade", "flag_deep_research"] = "confirm"
    original_score: int | Literal["mixed"] = 0
    recommended_score: int | Literal["mixed"] = 0
    recommended_services: dict[str, ServiceScore] = Field(default_factory=dict)
    reasoning: str = ""
    skepticism_flags: list[SkepticFlag] = Field(default_factory=list)
    deep_research_questions: list[str] = Field(default_factory=list)


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
