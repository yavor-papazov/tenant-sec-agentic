"""Generate concise human-review bundles from current assessment artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tenant_sec_agentic.artifacts import validate_internal_assessment
from tenant_sec_agentic.pipeline import prompt_hashes


def _load_current(folder: Path) -> list[dict[str, Any]]:
    expected_hashes = prompt_hashes()
    artifacts: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.yaml")):
        if path.name.endswith(".invalid.yaml"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        hashes = data.get("prompt_hashes") or {}
        if any(hashes.get(key) != value for key, value in expected_hashes.items()):
            continue
        if data.get("status") != "needs_human_review":
            continue
        if validate_internal_assessment(data):
            continue
        data["_artifact_path"] = str(path)
        artifacts.append(data)
    return artifacts


def _text(value: Any) -> str:
    return str(value or "").strip()


def render_bundle(provider: str, artifacts: list[dict[str, Any]]) -> str:
    scores = Counter(_text(item.get("recommended_score")) for item in artifacts)
    confidence = Counter(_text(item.get("overall_confidence")) for item in artifacts)
    flagged = [
        item
        for item in artifacts
        if item.get("result_status") != "assessed"
        or item.get("overall_confidence") != "high"
        or (item.get("skeptic") or {}).get("skepticism_flags")
        or (item.get("consistency") or {}).get("consistency_flags")
    ]
    lines = [
        f"# {provider} RC1 assessment review",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Current artifacts: {len(artifacts)}",
        f"Recommendations: {dict(sorted(scores.items()))}",
        f"Confidence: {dict(sorted(confidence.items()))}",
        f"Priority-review items: {len(flagged)}",
        "",
        "Review every recommendation below. Record decisions in the companion "
        "YAML manifest; `approved` accepts the recommendation and `adjusted` "
        "requires an adjusted score and reasoning.",
        "",
        "## Priority-review queue",
        "",
    ]
    for item in flagged:
        lines.append(
            f"- `{item.get('control')}`: recommendation "
            f"{item.get('recommended_score')}, "
            f"{item.get('overall_confidence')} confidence"
        )
    if not flagged:
        lines.append("- None")

    for item in artifacts:
        assessor = item.get("assessor") or {}
        skeptic = item.get("skeptic") or {}
        consistency = item.get("consistency") or {}
        evidence_by_id = {
            value.get("id"): value
            for value in assessor.get("evidence_items") or []
            if isinstance(value, dict)
        }
        lines.extend(
            [
                "",
                f"## {item.get('control')}",
                "",
                f"- Recommendation: `{item.get('recommended_score')}` "
                f"({item.get('result_status')})",
                f"- Confidence: `{item.get('overall_confidence')}`",
                f"- Consistency: `{consistency.get('overall_consistency', 'unknown')}`",
                "",
                "### Assessment",
                "",
                _text(assessor.get("evidence")) or "No narrative evidence.",
                "",
                "### Atomic claims",
                "",
            ]
        )
        for claim in assessor.get("claims") or []:
            evidence_ids = claim.get("evidence_item_ids") or []
            lines.append(
                f"- **{claim.get('result', 'unknown')}** — "
                f"{_text(claim.get('assertion'))}"
            )
            for evidence_id in evidence_ids:
                evidence = evidence_by_id.get(evidence_id) or {}
                quote = _text(evidence.get("quote"))
                url = _text(evidence.get("url"))
                if quote or url:
                    lines.append(f"  - {quote} ([source]({url}))")
        lines.extend(["", "### Skeptic review", ""])
        lines.append(_text(skeptic.get("reasoning")) or "No skeptic narrative.")
        for flag in skeptic.get("skepticism_flags") or []:
            lines.append(
                f"- `{flag.get('severity', 'unknown')}` "
                f"{flag.get('type', 'flag')}: {_text(flag.get('detail'))}"
            )
        for flag in consistency.get("consistency_flags") or []:
            lines.append(
                f"- Consistency `{flag.get('severity', 'unknown')}`: "
                f"{_text(flag.get('detail'))}"
            )
    return "\n".join(lines) + "\n"


def _manifest(provider: str, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "instructions": (
            "Set decision to approved or adjusted after reviewing each item. "
            "Adjusted decisions require adjusted_score and reasoning."
        ),
        "assessments": [
            {
                "control": item.get("control"),
                "artifact_path": item.get("_artifact_path"),
                "recommended_score": item.get("recommended_score"),
                "confidence": item.get("overall_confidence"),
                "decision": "pending",
                "adjusted_score": None,
                "adjusted_services": None,
                "reasoning": None,
                "notes": None,
            }
            for item in artifacts
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate RC1 human-review bundles"
    )
    parser.add_argument("--assessments-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provider", action="append", required=True)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for provider in args.provider:
        artifacts = _load_current(args.assessments_dir.resolve() / provider)
        markdown_path = output_dir / f"{provider}.md"
        manifest_path = output_dir / f"{provider}.yaml"
        markdown_path.write_text(
            render_bundle(provider, artifacts),
            encoding="utf-8",
        )
        manifest_path.write_text(
            yaml.safe_dump(
                _manifest(provider, artifacts),
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {markdown_path} and {manifest_path} "
            f"({len(artifacts)} current artifacts)"
        )


if __name__ == "__main__":
    main()
