"""Aggregate approved assessments into providers/{slug}.yaml (PRD 6.6)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from tenant_sec_agentic.artifacts import control_entry_for_provider_schema


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
        score = a.get("recommended_score")
        assessor = a.get("assessor") or {}
        rec_s = a.get("recommended_services")
        entry = control_entry_for_provider_schema(score, assessor, rec_s)
        return True, entry
    if st == "adjusted":
        adj = rev.get("adjusted_score")
        if adj is None:
            return False, None
        assessor = dict(a.get("assessor") or {})
        if adj == "mixed":
            services = rev.get("adjusted_services") or {}
            entry = {
                "score": "mixed",
                "summary": str(rev.get("adjustment_reasoning") or assessor.get("evidence", ""))[
                    :500
                ],
                "services": services,
                "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }
        else:
            entry = {
                "score": int(adj),
                "evidence": str(
                    rev.get("adjustment_reasoning") or assessor.get("evidence", "")
                ),
                "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }
        return True, entry
    return False, None


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
    args = p.parse_args(argv)

    repo = args.repo_root.resolve()
    slug = args.provider
    adir = args.assessments_dir or (repo.parent / "assessments")
    adir = adir.resolve()
    prov_folder = adir / slug
    if not prov_folder.is_dir():
        sys.exit(f"Missing assessments folder: {prov_folder}")

    assessments = load_assessments(prov_folder)
    controls: dict = {}
    pending = 0
    for a in assessments:
        cid = a.get("control")
        if not cid:
            continue
        rev = (a.get("review") or {}).get("status")
        if rev == "pending":
            pending += 1
        ok, entry = approved_entry(a)
        if ok and entry:
            controls[cid] = entry

    if pending and not args.allow_partial:
        sys.exit(
            f"Refusing to write profile: {pending} assessment(s) still have "
            f"review.status=pending. Approve in YAML or pass --allow-partial."
        )

    schema_path = repo / "schema" / "provider.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    existing_path = repo / "providers" / f"{slug}.yaml"
    if existing_path.is_file():
        base = yaml.safe_load(existing_path.read_text(encoding="utf-8"))
        services = base.get("services_in_scope")
        display_name = base.get("display_name", slug)
        revision = int(base.get("revision", 0)) + 1
    else:
        sys.exit(
            "No existing providers/{slug}.yaml — aggregation needs services_in_scope. "
            "Create a stub provider profile in the repo first, then re-run."
        )

    methodology_version = "1.0"
    if assessments:
        methodology_version = str(assessments[0].get("methodology_version", "1.0"))

    profile = {
        "provider": slug,
        "display_name": display_name,
        "assessed_by": "community:pipeline",
        "assessed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "methodology_version": methodology_version,
        "revision": revision,
        "services_in_scope": services,
        "controls": {**base.get("controls", {}), **controls},
    }

    v = Draft202012Validator(schema)
    errs = [e.message for e in v.iter_errors(profile)]
    if errs:
        sys.exit("Profile validation failed:\n" + "\n".join(errs))

    out_path = repo / "providers" / f"{slug}.yaml"
    out_path.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"Wrote {out_path} ({len(controls)} controls from assessments)")


if __name__ == "__main__":
    main()
