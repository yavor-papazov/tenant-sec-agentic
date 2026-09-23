"""Load controls, methodology, and provider profiles from tenant-sec repo checkout."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def validate_repo_layout(repo_root: Path) -> None:
    """Fail fast if expected directories are missing."""
    need = [
        repo_root / "controls",
        repo_root / "schema" / "provider.schema.json",
        repo_root / "providers",
    ]
    missing = [str(p) for p in need if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "tenant-sec repo layout invalid; missing:\n  - " + "\n  - ".join(missing)
        )


def load_methodology_or_interim(repo_root: Path) -> tuple[str, bool]:
    """Returns (text, used_interim_fallback)."""
    path = repo_root / "METHODOLOGY.md"
    if path.is_file():
        return path.read_text(encoding="utf-8"), False
    logger.warning(
        "METHODOLOGY.md not found under %s. Using PRD Section 4.4 interim criteria. "
        "Add METHODOLOGY.md to tenant-sec for authoritative rubric.",
        repo_root,
    )
    return _interim_methodology_text(), True


def _interim_methodology_text() -> str:
    return """INTERIM MATURITY CRITERIA (fallback until METHODOLOGY.md exists in repo)

Level 0 — Impossible from tenant space
The platform does not expose this capability through any tenant-accessible interface
(console, API, CLI, SDK, IaC). L0 requires affirmative official evidence, a
reproducible interface/live test, or provider confirmation that the capability
is unavailable. Missing documentation is unknown and carries no score.

Level 1 — Requires own processing loop
The outcome is achievable only through custom tenant-built automation. The provider does
not offer a managed path.

Level 2 — Available managed, limited
A managed capability exists but with constrained scope, configurability, or expressiveness.

Level 3 — Available managed, customizable
Full managed capability with tenant-configurable parameters, policies, and broad coverage.

Mixed — composite for service-scoped controls with different levels per service.
"""


def iter_control_files(controls_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(controls_dir.rglob("*.yaml")):
        if p.name.startswith("_"):
            continue
        files.append(p)
    return files


def load_all_controls(repo_root: Path) -> dict[str, dict[str, Any]]:
    """control_id -> parsed YAML dict."""
    controls_dir = repo_root / "controls"
    out: dict[str, dict[str, Any]] = {}
    for path in iter_control_files(controls_dir):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid control YAML {path}: {e}") from e
        if not isinstance(data, dict) or "id" not in data:
            raise ValueError(f"Control file missing id: {path}")
        cid = str(data["id"])
        if cid in out:
            raise ValueError(f"Duplicate control id {cid}: {path}")
        out[cid] = data
    return out


def resolve_control_scope(
    scope: str | list[str], controls: dict[str, dict[str, Any]]
) -> list[str]:
    if scope == "all":
        return sorted(controls.keys())
    missing = [c for c in scope if c not in controls]
    if missing:
        raise ValueError(
            "Unknown control id(s) in scope (not found under controls/): "
            + ", ".join(missing)
        )
    return list(scope)


def load_provider_profiles_scores(
    providers_dir: Path, exclude_slug: str
) -> dict[str, dict[str, Any]]:
    """For ConsistencyAgent: other providers' control scores."""
    peer: dict[str, dict[str, Any]] = {}
    for path in sorted(providers_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        slug = str(data.get("provider", ""))
        if not slug or slug == exclude_slug:
            continue
        controls = data.get("controls") or {}
        simplified: dict[str, Any] = {}
        for cid, entry in controls.items():
            if not isinstance(entry, dict):
                continue
            simplified[cid] = {
                "score": entry.get("score"),
                "services": list((entry.get("services") or {}).keys())
                if entry.get("score") == "mixed"
                else None,
            }
        peer[slug] = {"controls": simplified}
    return peer


def extract_score_for_control(
    profile_controls: dict[str, Any], control_id: str
) -> Any:
    entry = profile_controls.get(control_id)
    if not isinstance(entry, dict):
        return None
    return entry.get("score")
