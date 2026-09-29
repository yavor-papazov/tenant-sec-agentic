"""Load and summarize assessment artifacts for the local review UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from tenant_sec_agentic.pipeline import final_recommendation, recommendation_provenance
from tenant_sec_agentic.review_decisions import (
    apply_review_to_artifact,
    artifact_path_for_control,
    atomic_write_artifact,
    load_artifact,
    resolve_assessments_root,
    resolve_provider_dir,
)

FilterKind = Literal["all", "pending"]
REVIEWED_STATUSES = frozenset({"approved", "adjusted", "rejected"})

PROVENANCE_LABELS = {
    "deep_research_revised": "Deep research revised assessment",
    "skeptic_downgrade": "Skeptic downgrade applied",
    "initial_assessor": "Initial assessor recommendation",
}


def review_status(artifact: dict[str, Any]) -> str:
    review = artifact.get("review") if isinstance(artifact.get("review"), dict) else {}
    status = str(review.get("status") or "pending")
    if status in REVIEWED_STATUSES:
        return status
    return "pending"


def is_pending(artifact: dict[str, Any]) -> bool:
    return review_status(artifact) == "pending"


def list_assessment_files(provider_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in provider_dir.glob("*.yaml")
        if not path.name.endswith(".invalid.yaml")
    )


def load_provider_artifacts(
    assessments_root: Path, provider: str
) -> list[tuple[Path, dict[str, Any]]]:
    folder = resolve_provider_dir(assessments_root, provider)
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in list_assessment_files(folder):
        data = load_artifact(path)
        loaded.append((path, data))
    return loaded


def _control_id(path: Path, data: dict[str, Any]) -> str:
    return str(data.get("control") or path.stem)


def sorted_control_ids(artifacts: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    return sorted(_control_id(path, data) for path, data in artifacts)


def _pending_id_set(artifacts: list[tuple[Path, dict[str, Any]]]) -> set[str]:
    return {
        _control_id(path, data)
        for path, data in artifacts
        if is_pending(data)
    }


def summarize_controls(
    artifacts: list[tuple[Path, dict[str, Any]]],
    *,
    filter_kind: FilterKind = "all",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, data in artifacts:
        pending = is_pending(data)
        if filter_kind == "pending" and not pending:
            continue
        rows.append(
            {
                "control": _control_id(path, data),
                "review_status": review_status(data),
                "result_status": data.get("result_status"),
                "recommended_score": data.get("recommended_score"),
                "overall_confidence": data.get("overall_confidence"),
                "status": data.get("status"),
            }
        )
    rows.sort(key=lambda row: row["control"])
    return rows


def progress_counts(artifacts: list[tuple[Path, dict[str, Any]]]) -> dict[str, int]:
    pending = sum(1 for _, data in artifacts if is_pending(data))
    total = len(artifacts)
    return {
        "total": total,
        "pending": pending,
        "reviewed": total - pending,
    }


def _assessor_view(assessor: dict[str, Any]) -> dict[str, Any]:
    evidence_items = [
        item
        for item in assessor.get("evidence_items") or []
        if isinstance(item, dict)
    ]
    claims = [item for item in assessor.get("claims") or [] if isinstance(item, dict)]
    criteria = [
        item
        for item in assessor.get("criteria_results") or []
        if isinstance(item, dict)
    ]
    return {
        "evidence": assessor.get("evidence"),
        "confidence": assessor.get("confidence"),
        "services": assessor.get("services"),
        "sources_used": assessor.get("sources_used") or [],
        "evidence_items": evidence_items,
        "claims": claims,
        "criteria_results": criteria,
    }


def control_detail(data: dict[str, Any]) -> dict[str, Any]:
    initial_assessor = (
        data.get("assessor") if isinstance(data.get("assessor"), dict) else {}
    )
    skeptic = data.get("skeptic") if isinstance(data.get("skeptic"), dict) else {}
    consistency = (
        data.get("consistency") if isinstance(data.get("consistency"), dict) else {}
    )
    raw_deep = data.get("deep_research")
    deep_research = raw_deep if isinstance(raw_deep, dict) else None

    result_status, rec_score, rec_services, confidence, final_assessor = (
        final_recommendation(initial_assessor, skeptic, deep_research)
    )
    provenance = recommendation_provenance(initial_assessor, skeptic, deep_research)

    unresolved: list[dict[str, Any]] = []
    for flag in skeptic.get("skepticism_flags") or []:
        if isinstance(flag, dict):
            unresolved.append({"source": "skeptic", **flag})
    for flag in consistency.get("consistency_flags") or []:
        if isinstance(flag, dict):
            unresolved.append({"source": "consistency", **flag})
    for flag in initial_assessor.get("flags") or []:
        if isinstance(flag, dict):
            unresolved.append({"source": "assessor", **flag})
        elif flag:
            unresolved.append({"source": "assessor", "detail": str(flag)})
    if data.get("error_detail"):
        unresolved.append(
            {
                "source": "pipeline",
                "severity": "error",
                "detail": str(data.get("error_detail")),
            }
        )
    deep_questions = skeptic.get("deep_research_questions") or []
    return {
        "control": data.get("control"),
        "provider": data.get("provider"),
        "result_status": result_status,
        "recommended_score": rec_score,
        "recommended_services": rec_services,
        "overall_confidence": confidence,
        "status": data.get("status"),
        "recommendation_provenance": provenance,
        "recommendation_provenance_label": PROVENANCE_LABELS.get(
            provenance,
            provenance,
        ),
        "assessor": _assessor_view(final_assessor),
        "context": {
            "initial_assessor": _assessor_view(initial_assessor),
            "deep_research": deep_research,
        },
        "skeptic": {
            "reasoning": skeptic.get("reasoning"),
            "skepticism_flags": skeptic.get("skepticism_flags") or [],
            "deep_research_questions": deep_questions,
        },
        "consistency": consistency,
        "unresolved_issues": unresolved,
        "review": data.get("review") or {},
    }


def pending_navigation(
    artifacts: list[tuple[Path, dict[str, Any]]], control_id: str
) -> dict[str, str | None]:
    pending_ids = sorted(_pending_id_set(artifacts))
    if control_id not in pending_ids:
        return {"previous": None, "next": None}
    index = pending_ids.index(control_id)
    return {
        "previous": pending_ids[index - 1] if index > 0 else None,
        "next": pending_ids[index + 1]
        if index + 1 < len(pending_ids)
        else None,
    }


def next_pending_after_save(
    artifacts: list[tuple[Path, dict[str, Any]]], saved_control_id: str
) -> str | None:
    """Next pending control after a save, in portfolio sort order."""
    all_ids = sorted_control_ids(artifacts)
    pending_ids = sorted(_pending_id_set(artifacts))
    if not pending_ids:
        return None
    if saved_control_id in all_ids:
        index = all_ids.index(saved_control_id)
        for control_id in all_ids[index + 1 :]:
            if control_id in pending_ids:
                return control_id
    return pending_ids[0]


def save_review_decision(
    assessments_root: Path,
    provider: str,
    control_id: str,
    reviewer: str,
    *,
    decision: str,
    notes: str | None = None,
    adjusted_score: Any = None,
    adjusted_services: Any = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    root = resolve_assessments_root(assessments_root)
    path = artifact_path_for_control(root, provider, control_id)
    if not path.is_file():
        raise FileNotFoundError(f"missing artifact: {path}")
    artifact = load_artifact(path)
    if str(artifact.get("control") or control_id) != control_id:
        raise ValueError(f"control mismatch for {path}")
    apply_review_to_artifact(
        artifact,
        decision,
        reviewer,
        notes=notes,
        adjusted_score=adjusted_score,
        adjusted_services=adjusted_services,
        reasoning=reasoning,
    )
    atomic_write_artifact(path, artifact)
    return artifact.get("review") or {}
