"""Fail-closed publication checks for methodology 2.0 provider profiles."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from tenant_sec_agentic.repo import load_all_controls

VIGNETTE_BLOCKS = {
    "legal",
    "operations",
    "privileged_access",
    "subcontracting",
    "audit_and_exit",
    "continuity",
    "vulnerability_disclosure",
    "transfers",
}


def profile_completeness_errors(
    profile: dict[str, Any],
    controls: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if str(profile.get("methodology_version")) != "2.0":
        errors.append("methodology_version must be 2.0")
    review_status = str(profile.get("review_status") or "")
    if review_status not in {"unreviewed", "human-reviewed"}:
        errors.append(
            "review_status must be unreviewed or human-reviewed"
        )
    results = profile.get("controls") or {}
    exceptions = profile.get("service_scope_exceptions") or {}
    tenant_controls = {
        control_id: control
        for control_id, control in controls.items()
        if str(control.get("surface") or "tenant") == "tenant"
    }
    for control_id, control in sorted(tenant_controls.items()):
        entry = results.get(control_id)
        if not isinstance(entry, dict):
            errors.append(f"{control_id}: missing tenant-control result")
            continue
        status = str(
            entry.get("status")
            or ("assessed" if "score" in entry else "unknown")
        )
        if status == "unknown" and review_status == "unreviewed":
            if not str(entry.get("evidence") or "").strip():
                errors.append(
                    f"{control_id}: provisional unknown has no explanation"
                )
            continue
        if status not in {"assessed", "not_applicable"}:
            errors.append(
                f"{control_id}: publication state {status!r} is incomplete"
            )
            continue
        if status == "not_applicable":
            continue
        if not entry.get("references"):
            errors.append(f"{control_id}: assessed result has no references")
        if not entry.get("evidence_items"):
            errors.append(f"{control_id}: assessed result has no evidence items")
        if not entry.get("claims"):
            errors.append(f"{control_id}: assessed result has no claims")
        if len(entry.get("criteria_results") or []) < 4:
            errors.append(
                f"{control_id}: assessed result lacks four criteria results"
            )
        if (
            bool(control.get("service_scoped"))
            and entry.get("score") != "mixed"
            and control_id not in exceptions
        ):
            errors.append(
                f"{control_id}: service-scoped result must be mixed or "
                "have a written exception"
            )

    vignette = profile.get("vignette") or {}
    for block in sorted(VIGNETTE_BLOCKS):
        value = vignette.get(block)
        if not isinstance(value, dict):
            errors.append(f"vignette.{block}: missing M3 block")
            continue
        if not str(value.get("summary") or "").strip():
            errors.append(f"vignette.{block}: missing summary")
        if not value.get("references"):
            errors.append(f"vignette.{block}: missing references")

    certifications = profile.get("certifications") or []
    if not certifications:
        errors.append("certifications: M3 inventory is empty")
    assessed_at = _date_or_none(profile.get("assessed_at"))
    for record in certifications:
        cert_id = str(record.get("id") or "unknown")
        if not record.get("evidence"):
            errors.append(f"certifications.{cert_id}: missing evidence")
        if not record.get("status"):
            errors.append(f"certifications.{cert_id}: missing status")
        if not record.get("validity"):
            errors.append(f"certifications.{cert_id}: missing validity")
        valid_until = _date_or_none(record.get("valid_until"))
        if (
            record.get("status") == "held"
            and record.get("validity") == "fixed"
            and valid_until is None
        ):
            errors.append(
                f"certifications.{cert_id}: fixed held record has no expiry"
            )
        if assessed_at and valid_until and valid_until < assessed_at:
            errors.append(
                f"certifications.{cert_id}: expired before assessment date"
            )
    return errors


def _date_or_none(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Check methodology 2.0 publication completeness"
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--provider", action="append", required=True)
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    controls = load_all_controls(repo)
    schema = json.loads(
        (repo / "schema" / "provider.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )
    all_errors: list[str] = []
    for provider in args.provider:
        path = repo / "providers" / f"{provider}.yaml"
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema_errors = [
            error.message for error in validator.iter_errors(profile)
        ]
        completeness_errors = profile_completeness_errors(
            profile,
            controls,
        )
        all_errors.extend(
            f"{provider}: {message}"
            for message in schema_errors + completeness_errors
        )
    if all_errors:
        raise SystemExit(
            "RC1 publication check failed:\n" + "\n".join(all_errors)
        )
    print(
        f"RC1 publication checks passed for {len(args.provider)} provider(s)"
    )


if __name__ == "__main__":
    main()
