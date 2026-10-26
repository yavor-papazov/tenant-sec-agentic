"""Apply explicit human decisions from a generated review manifest."""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tenant_sec_agentic.artifacts import (
    assessment_review_payload_digest,
    validate_release_ready_assessment,
)
from tenant_sec_agentic.config import (
    AssessmentConfig,
    load_assessment_config,
)
from tenant_sec_agentic.pipeline import (
    assessment_inputs_match,
    current_assessment_input_hashes,
    prompt_hashes,
)


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


def apply_manifest(
    manifest_path: Path,
    reviewer: str,
    cfg: AssessmentConfig | None = None,
) -> tuple[int, int]:
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
        raw = artifact_path.read_bytes()
        expected_digest = str(decision.get("artifact_digest") or "")
        actual_digest = hashlib.sha256(raw).hexdigest()
        if not expected_digest or expected_digest != actual_digest:
            raise ValueError(
                f"{decision.get('control')}: artifact changed after the "
                "review manifest was generated"
            )
        artifact = yaml.safe_load(raw.decode("utf-8"))
        if not isinstance(artifact, dict):
            raise ValueError(f"Invalid artifact: {artifact_path}")
        if artifact.get("control") != decision.get("control"):
            raise ValueError(
                f"Control mismatch for artifact {artifact_path}"
            )
        validation_errors = validate_release_ready_assessment(artifact)
        hashes = artifact.get("prompt_hashes") or {}
        if any(
            hashes.get(stage) != digest
            for stage, digest in prompt_hashes().items()
        ):
            validation_errors.append("assessment prompts are stale")
        if cfg is not None:
            inputs = artifact.get("input_hashes") or {}
            expected_inputs = current_assessment_input_hashes(
                cfg,
                str(artifact.get("control") or ""),
            )
            if not assessment_inputs_match(inputs, expected_inputs):
                validation_errors.append("assessment inputs are stale")
        if validation_errors:
            raise ValueError(
                f"{decision.get('control')}: artifact is not release-ready: "
                + "; ".join(validation_errors)
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
            adjusted_score = _adjusted_score(
                decision.get("adjusted_score")
            )
            adjusted_services = decision.get(
                "adjusted_services"
            )
            reasoning = str(decision.get("reasoning") or "").strip()
            if not reasoning:
                raise ValueError(
                    f"{decision.get('control')}: adjusted decision "
                    "requires reasoning"
                )
            adjusted_assessor = decision.get("adjusted_assessor")
            if not isinstance(adjusted_assessor, dict):
                raise ValueError(
                    f"{decision.get('control')}: adjusted decision requires "
                    "a complete adjusted_assessor claim bundle"
                )
            adjusted_assessor = dict(adjusted_assessor)
            if adjusted_assessor.get("status") != "assessed":
                raise ValueError(
                    f"{decision.get('control')}: adjusted_assessor must be "
                    "assessed"
                )
            if adjusted_assessor.get("score") != adjusted_score:
                raise ValueError(
                    f"{decision.get('control')}: adjusted_assessor score "
                    "does not match adjusted_score"
                )
            adjusted_assessor["evidence"] = str(
                adjusted_assessor.get("evidence") or reasoning
            )
            if adjusted_score == "mixed":
                adjusted_assessor["services"] = adjusted_services or {}
            probe = dict(artifact)
            probe.update(
                {
                    "status": "needs_human_review",
                    "result_status": "assessed",
                    "recommended_score": adjusted_score,
                    "recommended_services": (
                        adjusted_services
                        if adjusted_score == "mixed"
                        else None
                    ),
                    "assessor": adjusted_assessor,
                }
            )
            adjusted_errors = validate_release_ready_assessment(probe)
            if adjusted_errors:
                raise ValueError(
                    f"{decision.get('control')}: adjusted claim bundle is "
                    "not release-ready: "
                    + "; ".join(adjusted_errors)
                )
            review["adjusted_score"] = adjusted_score
            review["adjusted_services"] = adjusted_services
            review["adjusted_assessor"] = adjusted_assessor
            review["adjustment_reasoning"] = reasoning
        else:
            review["adjusted_score"] = None
            review["adjusted_services"] = None
            review["adjusted_assessor"] = None
            review["adjustment_reasoning"] = None
        artifact["review"] = review
        review["payload_digest"] = assessment_review_payload_digest(
            artifact
        )
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
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = load_assessment_config(args.config.resolve())
    applied, pending = apply_manifest(
        args.manifest.resolve(),
        args.reviewer,
        cfg,
    )
    print(f"Applied {applied} decision(s); {pending} remain pending")


if __name__ == "__main__":
    main()
