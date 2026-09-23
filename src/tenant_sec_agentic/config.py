"""Load and validate assessment-config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ProviderConfig:
    slug: str
    display_name: str
    docs_base_url: str
    assessment_id: str
    offering: dict[str, Any]
    services_in_scope: list[dict[str, Any]]


@dataclass
class LimitsConfig:
    max_model_calls_per_run: int = 40
    max_input_tokens_per_run: int = 750_000
    max_output_tokens_per_run: int = 100_000
    max_output_tokens_per_call: int = 8_192
    model_timeout_seconds: int = 120
    max_model_turns_per_agent: int = 8
    max_retries_per_run: int = 12
    max_retries_per_call: int = 2
    max_search_calls_per_run: int = 40
    max_fetch_calls_per_run: int = 30
    max_deep_research_per_run: int = 5
    max_search_calls_per_deep_research: int = 5
    max_fetch_calls_per_deep_research: int = 3
    max_projected_cost_usd: float = 10.0
    max_parallel_assessments: int = 1


@dataclass
class PathsConfig:
    repo_root: Path
    assessments_dir: Path
    reports_dir: Path


@dataclass
class SessionConfig:
    database_path: Path
    resume_existing: bool = True


@dataclass
class AssessmentConfig:
    provider: ProviderConfig
    controls_scope: str | list[str]
    models: dict[str, str]
    limits: LimitsConfig
    paths: PathsConfig
    session: SessionConfig
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    vertex_traffic_class: str = "standard"
    mock_mode: bool = False

    def model_for(self, role: str) -> str:
        defaults = {
            "doc_fetch": "vertex_ai/gemini-3.5-flash",
            "consistency": "vertex_ai/gemini-3.5-flash",
            "assessor": "vertex_ai/gemini-3.5-flash",
            "skeptic": "vertex_ai/gemini-3.5-flash",
            "deep_research": "vertex_ai/gemini-3.5-flash",
        }
        return self.models.get(role, defaults.get(role, defaults["assessor"]))


def load_assessment_config(path: Path) -> AssessmentConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a mapping: {path}")

    p = raw.get("provider") or {}
    provider = ProviderConfig(
        slug=str(p["slug"]),
        display_name=str(p["display_name"]),
        docs_base_url=str(p["docs_base_url"]),
        assessment_id=str(p["assessment_id"]),
        offering=dict(p["offering"]),
        services_in_scope=list(p.get("services_in_scope") or []),
    )
    if not provider.services_in_scope:
        raise ValueError("provider.services_in_scope must be non-empty")
    required_offering = {"id", "name", "partition", "regions", "edition", "cohort"}
    missing_offering = sorted(required_offering - set(provider.offering))
    if missing_offering:
        raise ValueError(
            "provider.offering is missing: " + ", ".join(missing_offering)
        )
    identity_parts = provider.assessment_id.split("/")
    if (
        not identity_parts
        or identity_parts[0] != provider.slug
        or str(provider.offering["id"]) not in identity_parts[1:]
    ):
        raise ValueError(
            "provider.assessment_id must start with slug and include offering.id"
        )

    ctrl = raw.get("controls") or {}
    scope = ctrl.get("scope", "all")
    if scope != "all" and not isinstance(scope, list):
        raise ValueError("controls.scope must be 'all' or a list of control IDs")

    models = dict(raw.get("models") or {})
    lim = raw.get("limits") or {}
    limits = LimitsConfig(
        max_model_calls_per_run=int(lim.get("max_model_calls_per_run", 40)),
        max_input_tokens_per_run=int(
            lim.get("max_input_tokens_per_run", 750_000)
        ),
        max_output_tokens_per_run=int(
            lim.get("max_output_tokens_per_run", 100_000)
        ),
        max_output_tokens_per_call=int(
            lim.get("max_output_tokens_per_call", 8_192)
        ),
        model_timeout_seconds=int(lim.get("model_timeout_seconds", 120)),
        max_model_turns_per_agent=int(
            lim.get("max_model_turns_per_agent", 8)
        ),
        max_retries_per_run=int(lim.get("max_retries_per_run", 12)),
        max_retries_per_call=int(lim.get("max_retries_per_call", 2)),
        max_search_calls_per_run=int(lim.get("max_search_calls_per_run", 40)),
        max_fetch_calls_per_run=int(lim.get("max_fetch_calls_per_run", 30)),
        max_deep_research_per_run=int(lim.get("max_deep_research_per_run", 5)),
        max_search_calls_per_deep_research=int(
            lim.get("max_search_calls_per_deep_research", 5)
        ),
        max_fetch_calls_per_deep_research=int(
            lim.get("max_fetch_calls_per_deep_research", 3)
        ),
        max_projected_cost_usd=float(
            lim.get("max_projected_cost_usd", 10.0)
        ),
        max_parallel_assessments=int(lim.get("max_parallel_assessments", 1)),
    )
    if limits.max_parallel_assessments != 1:
        raise ValueError("The first metered pilot must run sequentially")
    for name, value in vars(limits).items():
        if name == "max_projected_cost_usd":
            if value <= 0:
                raise ValueError(f"limits.{name} must be positive")
        elif int(value) <= 0:
            raise ValueError(f"limits.{name} must be positive")

    paths_raw = raw.get("paths") or {}
    base = path.parent
    repo_root = (base / str(paths_raw.get("repo_root", "."))).resolve()
    assessments_dir = (base / str(paths_raw.get("assessments_dir", "./assessments"))).resolve()
    reports_dir = (base / str(paths_raw.get("reports_dir", "./reports"))).resolve()
    paths = PathsConfig(
        repo_root=repo_root,
        assessments_dir=assessments_dir,
        reports_dir=reports_dir,
    )

    sess = raw.get("session") or {}
    session = SessionConfig(
        database_path=(base / str(sess.get("database_path", "./pipeline_sessions.db"))).resolve(),
        resume_existing=bool(sess.get("resume_existing", True)),
    )

    traffic_class = str(raw.get("vertex_traffic_class", "standard"))
    if traffic_class not in {"standard", "priority", "flex"}:
        raise ValueError(
            "vertex_traffic_class must be standard, priority, or flex"
        )

    return AssessmentConfig(
        provider=provider,
        controls_scope=scope,
        models=models,
        limits=limits,
        paths=paths,
        session=session,
        pricing={
            str(model): {
                "input_per_million": float(price["input_per_million"]),
                "output_per_million": float(price["output_per_million"]),
            }
            for model, price in (raw.get("pricing") or {}).items()
        },
        vertex_traffic_class=traffic_class,
        mock_mode=bool(raw.get("mock_mode", False)),
    )


def config_to_serializable(cfg: AssessmentConfig) -> dict[str, Any]:
    """Store in session state (JSON-serializable)."""
    return {
        "provider": {
            "slug": cfg.provider.slug,
            "display_name": cfg.provider.display_name,
            "docs_base_url": cfg.provider.docs_base_url,
            "assessment_id": cfg.provider.assessment_id,
            "offering": cfg.provider.offering,
            "services_in_scope": cfg.provider.services_in_scope,
        },
        "controls_scope": cfg.controls_scope,
        "models": cfg.models,
        "limits": vars(cfg.limits),
        "paths": {
            "repo_root": str(cfg.paths.repo_root),
            "assessments_dir": str(cfg.paths.assessments_dir),
            "reports_dir": str(cfg.paths.reports_dir),
        },
        "session": {
            "database_path": str(cfg.session.database_path),
            "resume_existing": cfg.session.resume_existing,
        },
        "pricing": cfg.pricing,
        "vertex_traffic_class": cfg.vertex_traffic_class,
        "mock_mode": cfg.mock_mode,
    }


def config_from_state(data: dict[str, Any]) -> AssessmentConfig:
    p = data["provider"]
    paths = data["paths"]
    sess = data["session"]
    lim = data["limits"]
    return AssessmentConfig(
        provider=ProviderConfig(
            slug=p["slug"],
            display_name=p["display_name"],
            docs_base_url=p["docs_base_url"],
            assessment_id=p["assessment_id"],
            offering=dict(p["offering"]),
            services_in_scope=list(p["services_in_scope"]),
        ),
        controls_scope=data["controls_scope"],
        models=dict(data["models"]),
        limits=LimitsConfig(**lim),
        paths=PathsConfig(
            repo_root=Path(paths["repo_root"]),
            assessments_dir=Path(paths["assessments_dir"]),
            reports_dir=Path(paths["reports_dir"]),
        ),
        session=SessionConfig(
            database_path=Path(sess["database_path"]),
            resume_existing=bool(sess["resume_existing"]),
        ),
        pricing={
            str(model): dict(price)
            for model, price in (data.get("pricing") or {}).items()
        },
        vertex_traffic_class=str(
            data.get("vertex_traffic_class", "standard")
        ),
        mock_mode=bool(data.get("mock_mode", False)),
    )
