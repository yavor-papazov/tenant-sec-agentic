"""Shared human-review decision validation and artifact updates."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REVIEW_DECISIONS = frozenset({"approved", "adjusted", "rejected"})
PROVIDER_ID_RE = re.compile(r"^[a-z0-9_-]+$")
CONTROL_ID_RE = re.compile(r"^[a-z][a-z0-9.-]*$")
REVIEWER_ID_RE = re.compile(
    r"^(self|community:[a-z0-9_-]+|accredited:[a-z0-9_-]+)$"
)


def validate_reviewer_identity(reviewer: str) -> None:
    if not REVIEWER_ID_RE.fullmatch(reviewer):
        raise ValueError(
            "reviewer must match self, community:{handle}, or accredited:{org}"
        )


def parse_adjusted_score(value: Any) -> int | str:
    if value == "mixed":
        return value
    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("adjusted_score must be 0-3 or mixed") from exc
    if score not in {0, 1, 2, 3}:
        raise ValueError("adjusted_score must be 0-3 or mixed")
    return score


def validate_decision_fields(
    decision: str,
    *,
    adjusted_score: Any = None,
    adjusted_services: Any = None,
    reasoning: str | None = None,
) -> None:
    if decision not in REVIEW_DECISIONS:
        raise ValueError(
            f"invalid decision {decision!r}; expected approved, adjusted, or rejected"
        )
    if decision != "adjusted":
        return
    parse_adjusted_score(adjusted_score)
    if adjusted_score == "mixed":
        if not isinstance(adjusted_services, dict) or not adjusted_services:
            raise ValueError("mixed adjusted_score requires adjusted_services map")
    reasoning_text = str(reasoning or "").strip()
    if not reasoning_text:
        raise ValueError("adjusted decision requires reasoning")


def build_review_block(
    decision: str,
    reviewer: str,
    *,
    reviewed_at: str | None = None,
    notes: str | None = None,
    adjusted_score: Any = None,
    adjusted_services: Any = None,
    reasoning: str | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_decision_fields(
        decision,
        adjusted_score=adjusted_score,
        adjusted_services=adjusted_services,
        reasoning=reasoning,
    )
    review = dict(existing or {})
    review.update(
        {
            "status": decision,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at
            or datetime.now(timezone.utc).isoformat(),
            "notes": notes,
        }
    )
    if decision == "adjusted":
        review["adjusted_score"] = parse_adjusted_score(adjusted_score)
        review["adjusted_services"] = adjusted_services
        review["adjustment_reasoning"] = str(reasoning or "").strip()
    else:
        review["adjusted_score"] = None
        review["adjusted_services"] = None
        review["adjustment_reasoning"] = None
    return review


def apply_review_to_artifact(
    artifact: dict[str, Any],
    decision: str,
    reviewer: str,
    *,
    notes: str | None = None,
    adjusted_score: Any = None,
    adjusted_services: Any = None,
    reasoning: str | None = None,
    reviewed_at: str | None = None,
) -> None:
    artifact["review"] = build_review_block(
        decision,
        reviewer,
        reviewed_at=reviewed_at,
        notes=notes,
        adjusted_score=adjusted_score,
        adjusted_services=adjusted_services,
        reasoning=reasoning,
        existing=artifact.get("review") if isinstance(artifact.get("review"), dict) else {},
    )


def resolve_assessments_root(assessments_dir: Path) -> Path:
    return assessments_dir.expanduser().resolve()


def resolve_provider_dir(assessments_root: Path, provider: str) -> Path:
    if not PROVIDER_ID_RE.fullmatch(provider):
        raise ValueError(f"invalid provider id: {provider!r}")
    folder = (assessments_root / provider).resolve()
    if not folder.is_relative_to(assessments_root):
        raise ValueError("provider path escapes assessments root")
    if not folder.is_dir():
        raise ValueError(f"missing assessments folder: {folder}")
    return folder


def artifact_path_for_control(
    assessments_root: Path, provider: str, control_id: str
) -> Path:
    if not CONTROL_ID_RE.fullmatch(control_id):
        raise ValueError(f"invalid control id: {control_id!r}")
    provider_dir = resolve_provider_dir(assessments_root, provider)
    path = (provider_dir / f"{control_id}.yaml").resolve()
    if not path.is_relative_to(assessments_root):
        raise ValueError("artifact path escapes assessments root")
    return path


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_artifact(path: Path, artifact: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        yaml.safe_dump(artifact, sort_keys=False, allow_unicode=True),
    )


def load_artifact(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid artifact: {path}")
    return data
