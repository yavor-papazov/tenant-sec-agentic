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
    services_in_scope: list[dict[str, str]]


@dataclass
class LimitsConfig:
    max_deep_research_per_run: int = 10
    max_search_calls_per_deep_research: int = 10
    max_fetch_calls_per_deep_research: int = 5
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
    mock_mode: bool = False

    def model_for(self, role: str) -> str:
        return self.models.get(role, "gemini/gemini-2.0-flash")


def load_assessment_config(path: Path) -> AssessmentConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a mapping: {path}")

    p = raw.get("provider") or {}
    provider = ProviderConfig(
        slug=str(p["slug"]),
        display_name=str(p["display_name"]),
        docs_base_url=str(p["docs_base_url"]),
        services_in_scope=list(p.get("services_in_scope") or []),
    )
    if not provider.services_in_scope:
        raise ValueError("provider.services_in_scope must be non-empty")

    ctrl = raw.get("controls") or {}
    scope = ctrl.get("scope", "all")
    if scope != "all" and not isinstance(scope, list):
        raise ValueError("controls.scope must be 'all' or a list of control IDs")

    models = dict(raw.get("models") or {})
    lim = raw.get("limits") or {}
    limits = LimitsConfig(
        max_deep_research_per_run=int(lim.get("max_deep_research_per_run", 10)),
        max_search_calls_per_deep_research=int(
            lim.get("max_search_calls_per_deep_research", 10)
        ),
        max_fetch_calls_per_deep_research=int(
            lim.get("max_fetch_calls_per_deep_research", 5)
        ),
        max_parallel_assessments=int(lim.get("max_parallel_assessments", 1)),
    )

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

    return AssessmentConfig(
        provider=provider,
        controls_scope=scope,
        models=models,
        limits=limits,
        paths=paths,
        session=session,
        mock_mode=bool(raw.get("mock_mode", False)),
    )


def config_to_serializable(cfg: AssessmentConfig) -> dict[str, Any]:
    """Store in session state (JSON-serializable)."""
    return {
        "provider": {
            "slug": cfg.provider.slug,
            "display_name": cfg.provider.display_name,
            "docs_base_url": cfg.provider.docs_base_url,
            "services_in_scope": cfg.provider.services_in_scope,
        },
        "controls_scope": cfg.controls_scope,
        "models": cfg.models,
        "limits": {
            "max_deep_research_per_run": cfg.limits.max_deep_research_per_run,
            "max_search_calls_per_deep_research": cfg.limits.max_search_calls_per_deep_research,
            "max_fetch_calls_per_deep_research": cfg.limits.max_fetch_calls_per_deep_research,
            "max_parallel_assessments": cfg.limits.max_parallel_assessments,
        },
        "paths": {
            "repo_root": str(cfg.paths.repo_root),
            "assessments_dir": str(cfg.paths.assessments_dir),
            "reports_dir": str(cfg.paths.reports_dir),
        },
        "session": {
            "database_path": str(cfg.session.database_path),
            "resume_existing": cfg.session.resume_existing,
        },
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
            services_in_scope=list(p["services_in_scope"]),
        ),
        controls_scope=data["controls_scope"],
        models=dict(data["models"]),
        limits=LimitsConfig(
            max_deep_research_per_run=int(lim["max_deep_research_per_run"]),
            max_search_calls_per_deep_research=int(
                lim["max_search_calls_per_deep_research"]
            ),
            max_fetch_calls_per_deep_research=int(
                lim["max_fetch_calls_per_deep_research"]
            ),
            max_parallel_assessments=int(lim["max_parallel_assessments"]),
        ),
        paths=PathsConfig(
            repo_root=Path(paths["repo_root"]),
            assessments_dir=Path(paths["assessments_dir"]),
            reports_dir=Path(paths["reports_dir"]),
        ),
        session=SessionConfig(
            database_path=Path(sess["database_path"]),
            resume_existing=bool(sess["resume_existing"]),
        ),
        mock_mode=bool(data.get("mock_mode", False)),
    )
