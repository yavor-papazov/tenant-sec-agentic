"""Strict structured-output models for provisional M3 metadata."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class M3Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=200)


class M3Fact(BaseModel):
    """A fact represented as a list item for Gemini schema compatibility."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=80)
    value: str = Field(min_length=1, max_length=500)


class M3VignetteBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1200)
    facts: list[M3Fact] = Field(default_factory=list, max_length=12)
    references: list[M3Reference] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def facts_have_unique_keys(self) -> "M3VignetteBlock":
        keys = [fact.key for fact in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("vignette fact keys must be unique")
        return self


class M3NamedVignetteBlock(M3VignetteBlock):
    model_config = ConfigDict(extra="forbid")

    block: Literal[
        "legal",
        "operations",
        "privileged_access",
        "subcontracting",
        "audit_and_exit",
        "continuity",
        "vulnerability_disclosure",
        "transfers",
    ]


class M3Certification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9.-]*$")
    kind: Literal[
        "certification",
        "attestation",
        "qualification",
        "authorization",
        "assessment-label",
        "code-of-conduct",
        "scheme-draft",
    ]
    status: Literal[
        "held",
        "expired",
        "in-progress",
        "not-held",
        "unknown",
    ]
    validity: Literal["fixed", "continuous", "unknown"]
    valid_from: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    valid_until: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    scope: str | None = Field(default=None, min_length=1, max_length=500)
    offering: str | None = Field(default=None, min_length=1, max_length=160)
    regions: list[str] = Field(default_factory=list, max_length=40)
    services: list[str] = Field(default_factory=list, max_length=100)
    all_services: bool = False
    report_access: Literal["public", "nda", "none"] | None = None
    evidence: list[M3Reference] = Field(min_length=1, max_length=8)


class M3ResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vignette: list[M3NamedVignetteBlock] = Field(
        min_length=8,
        max_length=8,
    )
    certifications: list[M3Certification] = Field(
        min_length=1,
        max_length=30,
    )

    @model_validator(mode="after")
    def vignette_has_every_block_once(self) -> "M3ResearchOutput":
        blocks = [item.block for item in self.vignette]
        expected = {
            "legal",
            "operations",
            "privileged_access",
            "subcontracting",
            "audit_and_exit",
            "continuity",
            "vulnerability_disclosure",
            "transfers",
        }
        if set(blocks) != expected or len(blocks) != len(set(blocks)):
            raise ValueError("vignette must contain each required block once")
        return self
