"""Aggregate approved assessments into providers/{slug}.yaml (PRD 6.6)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from tenant_sec_agentic.artifacts import (
    canonicalize_service_map,
    control_entry_for_provider_schema,
    validate_internal_assessment,
)
from tenant_sec_agentic.pipeline import prompt_hashes
from tenant_sec_agentic.repo import load_all_controls

ASSESSOR_ID = re.compile(
    r"^(self|community:[a-z0-9_-]+|accredited:[a-z0-9_-]+)$"
)


def load_assessments(folder: Path) -> list[dict]:
    out = []
    for p in sorted(folder.glob("*.yaml")):
        if p.name.endswith(".invalid.yaml"):
            continue
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        out.append(data)
    return out


def approved_entry(a: dict) -> tuple[bool, dict | None]:
    rev = a.get("review") or {}
    st = rev.get("status")
    if st == "approved":
        result_status = str(a.get("result_status") or "unknown")
        score = a.get("recommended_score")
        assessor = a.get("assessor") or {}
        rec_s = a.get("recommended_services")
        entry = control_entry_for_provider_schema(
            result_status,
            score,
            assessor,
            rec_s,
            str(a.get("overall_confidence") or "low"),
        )
        return True, entry
    if st == "adjusted":
        adj = rev.get("adjusted_score")
        if adj is None:
            return False, None
        assessor = dict(a.get("assessor") or {})
        assessor["evidence"] = str(
            rev.get("adjustment_reasoning") or assessor.get("evidence", "")
        )
        if adj == "mixed":
            services = rev.get("adjusted_services") or {}
            entry = control_entry_for_provider_schema(
                "assessed",
                "mixed",
                assessor,
                services,
                str(a.get("overall_confidence") or "low"),
            )
        else:
            entry = control_entry_for_provider_schema(
                "assessed",
                int(adj),
                assessor,
                None,
                str(a.get("overall_confidence") or "low"),
            )
        return True, entry
    return False, None


def provisional_entry(a: dict) -> tuple[bool, dict | None]:
    """Convert a current, valid recommendation without claiming review."""
    if a.get("status") != "needs_human_review":
        return False, None
    hashes = a.get("prompt_hashes") or {}
    if any(
        hashes.get(stage) != digest
        for stage, digest in prompt_hashes().items()
    ):
        return False, None
    if validate_internal_assessment(a):
        return False, None
    return True, control_entry_for_provider_schema(
        str(a.get("result_status") or "unknown"),
        a.get("recommended_score"),
        a.get("assessor") or {},
        a.get("recommended_services"),
        str(a.get("overall_confidence") or "low"),
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Aggregate assessments to provider profile")
    p.add_argument("--repo-root", type=Path, required=True)
    p.add_argument("--provider", type=str, required=True)
    p.add_argument(
        "--assessments-dir",
        type=Path,
        help="Default: repo_root/../assessments or ./assessments",
    )
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow writing profile when some reviews still pending (not recommended)",
    )
    p.add_argument(
        "--provisional-unreviewed",
        action="store_true",
        help=(
            "Publish current recommendations as an explicitly unreviewed RC "
            "without marking assessments approved"
        ),
    )
    args = p.parse_args(argv)
    if args.allow_partial and args.provisional_unreviewed:
        p.error("--allow-partial and --provisional-unreviewed are mutually exclusive")

    repo = args.repo_root.resolve()
    slug = args.provider
    adir = args.assessments_dir or (repo.parent / "assessments")
    adir = adir.resolve()
    prov_folder = adir / slug
    if not prov_folder.is_dir():
        sys.exit(f"Missing assessments folder: {prov_folder}")

    assessments = load_assessments(prov_folder)
    controls: dict = {}
    unapproved: list[str] = []
    for a in assessments:
        cid = a.get("control")
        if not cid:
            continue
        rev = (a.get("review") or {}).get("status")
        ok, entry = approved_entry(a)
        if not ok and args.provisional_unreviewed:
            ok, entry = provisional_entry(a)
        if ok and entry:
            controls[cid] = entry
        else:
            unapproved.append(str(cid))

    if unapproved and not args.allow_partial:
        sys.exit(
            f"Refusing to write profile: {len(unapproved)} assessment(s) "
            "are not approved or adjusted. Complete human review or pass "
            "--allow-partial."
        )
    reviewers = {
        str((assessment.get("review") or {}).get("reviewer") or "")
        for assessment in assessments
        if (assessment.get("review") or {}).get("status")
        in {"approved", "adjusted"}
    }
    reviewers.discard("")
    if (
        not args.allow_partial
        and not args.provisional_unreviewed
        and len(reviewers) != 1
    ):
        sys.exit(
            "Refusing to write profile: approved assessments must have "
            "one consistent reviewer identity"
        )
    assessed_by = (
        "community:pipeline"
        if args.provisional_unreviewed
        else next(iter(reviewers), "community:pipeline")
    )
    if not ASSESSOR_ID.fullmatch(assessed_by):
        sys.exit(
            "Reviewer identity must match self, community:{handle}, or "
            "accredited:{org}"
        )

    schema_path = repo / "schema" / "provider.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    existing_path = repo / "providers" / f"{slug}.yaml"
    if existing_path.is_file():
        base = yaml.safe_load(existing_path.read_text(encoding="utf-8"))
        display_name = base.get("display_name", slug)
        revision = int(base.get("revision", 0)) + 1
    else:
        sys.exit(
            "No existing providers/{slug}.yaml — aggregation needs services_in_scope. "
            "Create a stub provider profile in the repo first, then re-run."
        )
    metadata_path = repo / "providers" / "_metadata" / f"{slug}.yaml"
    metadata = (
        yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        if metadata_path.is_file()
        else {}
    )
    if not isinstance(metadata, dict):
        sys.exit(f"Provider metadata must be a mapping: {metadata_path}")

    methodology_version = "1.0"
    if assessments:
        methodology_version = str(assessments[0].get("methodology_version", "2.0"))
    first = assessments[0] if assessments else {}
    services = first.get("services_in_scope")
    assessment_id = first.get("assessment_id")
    offering = first.get("offering")
    if not services or not assessment_id or not offering:
        sys.exit("Assessments are missing strict v2 identity/scope metadata")
    identity_errors = [
        str(a.get("control") or "unknown")
        for a in assessments
        if a.get("assessment_id") != assessment_id
        or a.get("offering") != offering
        or a.get("services_in_scope") != services
    ]
    if identity_errors:
        sys.exit(
            "Assessment identity/scope mismatch: " + ", ".join(identity_errors)
        )
    for entry in controls.values():
        if entry.get("score") == "mixed":
            entry["services"] = canonicalize_service_map(
                entry.get("services") or {},
                services,
            )

    profile = {
        "provider": slug,
        "assessment_id": assessment_id,
        "display_name": display_name,
        "assessed_by": assessed_by,
        "assessed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "methodology_version": methodology_version,
        "revision": revision,
        "review_status": (
            "unreviewed"
            if args.provisional_unreviewed
            else "human-reviewed"
        ),
        "review_note": (
            "1.0-RC1 recommendations are AI-assisted and have not completed "
            "human review."
            if args.provisional_unreviewed
            else "All published recommendations completed human review."
        ),
        "offering": offering,
        "services_in_scope": services,
        "controls": controls,
    }
    for field in ("vignette", "certifications", "service_scope_exceptions"):
        if field in metadata:
            profile[field] = metadata[field]
        elif field in base:
            profile[field] = base[field]
    if args.provisional_unreviewed:
        control_defs = load_all_controls(repo)
        exceptions = dict(profile.get("service_scope_exceptions") or {})
        for control_id, entry in controls.items():
            definition = control_defs.get(control_id) or {}
            if (
                definition.get("service_scoped")
                and entry.get("status") == "assessed"
                and entry.get("score") != "mixed"
                and control_id not in exceptions
            ):
                exceptions[control_id] = (
                    "Provisional RC uses the assessor's uniform portfolio-level "
                    "recommendation; service-level scope remains subject to "
                    "human review."
                )
        if exceptions:
            profile["service_scope_exceptions"] = exceptions

    v = Draft202012Validator(schema, format_checker=FormatChecker())
    errs = [e.message for e in v.iter_errors(profile)]
    for control_id, entry in controls.items():
        if entry.get("status") == "assessed":
            if not entry.get("references"):
                errs.append(
                    f"{control_id}: assessed v2 result requires references"
                )
            if not entry.get("confidence"):
                errs.append(
                    f"{control_id}: assessed v2 result requires confidence"
                )
    if errs:
        sys.exit("Profile validation failed:\n" + "\n".join(errs))

    out_path = repo / "providers" / f"{slug}.yaml"
    out_path.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"Wrote {out_path} ({len(controls)} controls from assessments)")


if __name__ == "__main__":
    main()
