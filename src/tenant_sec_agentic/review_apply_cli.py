"""Apply explicit human decisions from a generated review manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _adjusted_score(value: Any) -> int | str:
    if value == "mixed":
        return value
    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("adjusted_score must be 0-3 or mixed") from exc
    if score not in {0, 1, 2, 3}:
        raise ValueError("adjusted_score must be 0-3 or mixed")
    return score


def apply_manifest(manifest_path: Path, reviewer: str) -> tuple[int, int]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Review manifest must be a YAML object")
    applied = 0
    pending = 0
    for decision in manifest.get("assessments") or []:
        status = str(decision.get("decision") or "pending")
        if status == "pending":
            pending += 1
            continue
        if status not in {"approved", "adjusted", "rejected"}:
            raise ValueError(
                f"{decision.get('control')}: invalid decision {status!r}"
            )
        artifact_path = Path(str(decision.get("artifact_path"))).resolve()
        artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(artifact, dict):
            raise ValueError(f"Invalid artifact: {artifact_path}")
        if artifact.get("control") != decision.get("control"):
            raise ValueError(
                f"Control mismatch for artifact {artifact_path}"
            )
        review = dict(artifact.get("review") or {})
        review.update(
            {
                "status": status,
                "reviewer": reviewer,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "notes": decision.get("notes"),
            }
        )
        if status == "adjusted":
            review["adjusted_score"] = _adjusted_score(
                decision.get("adjusted_score")
            )
            review["adjusted_services"] = decision.get(
                "adjusted_services"
            )
            reasoning = str(decision.get("reasoning") or "").strip()
            if not reasoning:
                raise ValueError(
                    f"{decision.get('control')}: adjusted decision "
                    "requires reasoning"
                )
            review["adjustment_reasoning"] = reasoning
        else:
            review["adjusted_score"] = None
            review["adjusted_services"] = None
            review["adjustment_reasoning"] = None
        artifact["review"] = review
        artifact_path.write_text(
            yaml.safe_dump(
                artifact,
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        applied += 1
    return applied, pending


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Apply RC1 human-review manifest decisions"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    args = parser.parse_args(argv)
    applied, pending = apply_manifest(
        args.manifest.resolve(),
        args.reviewer,
    )
    print(f"Applied {applied} decision(s); {pending} remain pending")


if __name__ == "__main__":
    main()
