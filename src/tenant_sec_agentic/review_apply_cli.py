"""Apply explicit human decisions from a generated review manifest."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from tenant_sec_agentic.review_decisions import (
    apply_review_to_artifact,
    atomic_write_artifact,
    load_artifact,
)


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
        artifact = load_artifact(artifact_path)
        if artifact.get("control") != decision.get("control"):
            raise ValueError(
                f"Control mismatch for artifact {artifact_path}"
            )
        apply_review_to_artifact(
            artifact,
            status,
            reviewer,
            notes=decision.get("notes"),
            adjusted_score=decision.get("adjusted_score"),
            adjusted_services=decision.get("adjusted_services"),
            reasoning=decision.get("reasoning"),
        )
        atomic_write_artifact(artifact_path, artifact)
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
