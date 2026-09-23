"""Deterministic per-control YAML and HTML reports."""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema import FormatChecker

from tenant_sec_agentic import __version__

logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def build_assessment_yaml(
    *,
    control_id: str,
    provider_slug: str,
    provider_display_name: str,
    assessment_id: str,
    offering: dict[str, Any],
    services_in_scope: list[dict[str, Any]],
    session_id: str,
    methodology_version: str,
    doc_fetch: dict[str, Any],
    assessor: dict[str, Any],
    skeptic: dict[str, Any] | None,
    consistency: dict[str, Any] | None,
    deep_research: dict[str, Any] | None,
    recommended_score: Any,
    recommended_services: dict[str, Any] | None,
    result_status: str,
    overall_confidence: str,
    status: str,
    prompt_hashes: dict[str, str],
    error_detail: str | None = None,
) -> dict[str, Any]:
    assessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    urls = [d.get("url") for d in (doc_fetch.get("docs_fetched") or []) if d.get("url")]

    out: dict[str, Any] = {
        "control": control_id,
        "provider": provider_slug,
        "provider_display_name": provider_display_name,
        "assessment_id": assessment_id,
        "offering": offering,
        "services_in_scope": services_in_scope,
        "pipeline_version": __version__,
        "methodology_version": methodology_version,
        "assessed_at": assessed_at,
        "session_id": session_id,
        "prompt_hashes": prompt_hashes,
        "doc_fetch": {
            "urls_consulted": urls,
            "doc_quality": doc_fetch.get("doc_quality", "absent"),
            "services_with_docs": doc_fetch.get("services_with_docs", []),
            "services_without_docs": doc_fetch.get("services_without_docs", []),
            "docs_fetched": doc_fetch.get("docs_fetched", []),
        },
        "assessor": assessor,
        "skeptic": skeptic,
        "consistency": consistency,
        "deep_research": deep_research,
        "recommended_score": recommended_score,
        "recommended_services": recommended_services,
        "result_status": result_status,
        "overall_confidence": overall_confidence,
        "status": status,
        "review": {
            "status": "pending",
            "reviewer": None,
            "reviewed_at": None,
            "adjusted_score": None,
            "adjusted_services": None,
            "adjustment_reasoning": None,
            "notes": None,
        },
    }
    if error_detail:
        out["error_detail"] = error_detail
    return out


def validate_internal_assessment(data: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    for k in ("control", "provider", "result_status", "status"):
        if k not in data:
            errs.append(f"missing {k}")
    score = data.get("recommended_score")
    result_status = data.get("result_status")
    if score not in (None, 0, 1, 2, 3, "mixed"):
        errs.append(f"invalid recommended_score: {score!r}")
    if result_status == "assessed" and score is None:
        errs.append("assessed result requires recommended_score")
    if result_status != "assessed" and score is not None:
        errs.append("non-assessed result prohibits recommended_score")
    if score == "mixed":
        rs = data.get("recommended_services")
        if not isinstance(rs, dict) or not rs:
            errs.append("mixed score requires recommended_services map")
    if result_status == "assessed":
        assessor = data.get("assessor") or {}
        if not assessor.get("evidence_items"):
            errs.append("assessed result requires evidence_items")
        if not assessor.get("claims"):
            errs.append("assessed result requires claims")
        if {
            value.get("level")
            for value in assessor.get("criteria_results") or []
        } != {0, 1, 2, 3}:
            errs.append("assessed result requires L0-L3 criteria_results")
    return errs


def control_entry_for_provider_schema(
    result_status: str,
    recommended_score: Any,
    assessor: dict[str, Any],
    recommended_services: dict[str, Any] | None,
    overall_confidence: str,
) -> dict[str, Any]:
    """Single control block for provider.schema.json."""
    verified = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    evidence = str(assessor.get("evidence") or "").strip()
    sources = _source_references(assessor.get("sources_used") or [])
    if result_status != "assessed":
        out = {
            "status": result_status,
            "evidence": evidence or "No sufficient authoritative evidence was found.",
            "confidence": overall_confidence,
            "verified_at": verified,
        }
        if sources:
            out["references"] = sources
        return out

    if recommended_score == "mixed":
        services_out: dict[str, Any] = {}
        src = assessor.get("services") or {}
        for sid, recommended in (recommended_services or {}).items():
            source_service = src.get(sid) if isinstance(src.get(sid), dict) else {}
            value = (
                recommended
                if isinstance(recommended, dict)
                else {"status": "assessed", "score": recommended}
            )
            status = str(value.get("status") or "unknown")
            service_entry: dict[str, Any] = {
                "status": status,
                "evidence": str(
                    value.get("evidence")
                    or source_service.get("evidence")
                    or "No service-specific evidence was captured."
                ),
                "confidence": str(
                    value.get("confidence")
                    or source_service.get("confidence")
                    or overall_confidence
                ),
            }
            if status == "assessed" and value.get("score") is not None:
                service_entry["score"] = int(value["score"])
            service_refs = _source_references(
                value.get("sources_used")
                or source_service.get("sources_used")
                or []
            )
            if service_refs:
                service_entry["references"] = service_refs
                for reference in service_refs:
                    if reference not in sources:
                        sources.append(reference)
            services_out[sid] = service_entry
        summ = evidence[:500] or "Per-service maturity varies."
        return {
            "status": "assessed",
            "score": "mixed",
            "summary": summ,
            "services": services_out,
            "verified_at": verified,
            "confidence": overall_confidence,
            "references": sources,
            **_claim_bundle(assessor),
        }
    return {
        "status": "assessed",
        "score": int(recommended_score),
        "evidence": evidence or "No evidence captured.",
        "verified_at": verified,
        "confidence": overall_confidence,
        "references": sources,
        **_claim_bundle(assessor),
    }


def _claim_bundle(assessor: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_items": list(assessor.get("evidence_items") or []),
        "claims": list(assessor.get("claims") or []),
        "criteria_results": list(assessor.get("criteria_results") or []),
    }


def _source_references(urls: list[Any]) -> list[dict[str, str]]:
    seen: set[str] = set()
    references = []
    for value in urls:
        url = str(value).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        references.append({"url": url, "title": url})
    return references


def validate_control_against_provider_schema(
    repo_root: Path,
    provider_slug: str,
    display_name: str,
    assessment_id: str,
    offering: dict[str, Any],
    services_in_scope: list[dict[str, Any]],
    control_id: str,
    control_entry: dict[str, Any],
) -> list[str]:
    schema_path = repo_root / "schema" / "provider.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    profile = {
        "provider": provider_slug,
        "assessment_id": assessment_id,
        "display_name": display_name,
        "assessed_by": "community:pipeline",
        "assessed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "methodology_version": "2.0",
        "revision": 1,
        "offering": offering,
        "services_in_scope": services_in_scope,
        "controls": {control_id: control_entry},
    }
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [e.message for e in validator.iter_errors(profile)]
    if control_entry.get("status") == "assessed" and not control_entry.get(
        "references"
    ):
        errors.append("methodology-2 assessed result requires references")
    if control_entry.get("score") == "mixed":
        for service_id, service in (
            control_entry.get("services") or {}
        ).items():
            if (
                service.get("status") == "assessed"
                and not service.get("references")
            ):
                errors.append(
                    f"methodology-2 assessed service '{service_id}' "
                    "requires references"
                )
    return errors


def write_assessment_files(
    *,
    assessments_dir: Path,
    reports_dir: Path,
    provider_slug: str,
    control_id: str,
    assessment: dict[str, Any],
    control_def: dict[str, Any],
    schema_errors: list[str],
) -> tuple[Path, Path]:
    assessments_dir.mkdir(parents=True, exist_ok=True)
    prov_reports = reports_dir / provider_slug
    prov_reports.mkdir(parents=True, exist_ok=True)

    base = assessments_dir / provider_slug / f"{control_id}.yaml"
    if schema_errors:
        inv = base.with_suffix(".invalid.yaml")
        inv.parent.mkdir(parents=True, exist_ok=True)
        assessment = dict(assessment)
        assessment["status"] = "validation_error"
        assessment["schema_validation_errors"] = schema_errors
        inv.write_text(yaml.safe_dump(assessment, sort_keys=False, allow_unicode=True), encoding="utf-8")
        html_path = prov_reports / f"{control_id}.html"
        html_path.write_text(
            render_control_html(control_def, assessment, schema_errors),
            encoding="utf-8",
        )
        return inv, html_path

    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(yaml.safe_dump(assessment, sort_keys=False, allow_unicode=True), encoding="utf-8")
    html_path = prov_reports / f"{control_id}.html"
    html_path.write_text(render_control_html(control_def, assessment, []), encoding="utf-8")
    return base, html_path


def render_control_html(
    control_def: dict[str, Any],
    assessment: dict[str, Any],
    schema_errors: list[str],
) -> str:
    """Standalone HTML per PRD Section 7.1."""
    provider = assessment.get("provider", "")
    cid = assessment.get("control", "")
    cname = control_def.get("name", cid)
    status = str(assessment.get("status", "")).upper()
    conf = str(assessment.get("overall_confidence", "low")).upper()
    rec = assessment.get("recommended_score")
    result_status = str(assessment.get("result_status", "unknown"))
    rec_label = (
        "MIXED"
        if rec == "mixed"
        else f"L{rec}"
        if isinstance(rec, int)
        else result_status.upper()
    )

    conf_color = {"HIGH": "#1b5e20", "MEDIUM": "#f57c00", "LOW": "#c62828"}.get(conf, "#333")
    status_color = {
        "NEEDS_HUMAN_REVIEW": "#f57c00",
        "VALIDATION_ERROR": "#c62828",
        "PARSE_ERROR": "#c62828",
        "API_ERROR": "#c62828",
        "ERROR": "#c62828",
    }.get(status.replace(" ", "_"), "#333")

    criteria = control_def.get("criteria") or {}
    crit_html = "".join(
        f"<p><strong>{k}</strong><br/>{_esc(str(v))}</p>"
        for k, v in criteria.items()
    )

    df = assessment.get("doc_fetch") or {}
    assessor = assessment.get("assessor") or {}
    skeptic = assessment.get("skeptic") or {}
    consistency = assessment.get("consistency") or {}
    dr = assessment.get("deep_research")

    # Distribution bars for mixed
    bar_section = ""
    if rec == "mixed" and isinstance(assessment.get("recommended_services"), dict):
        counts = {0: 0, 1: 0, 2: 0, 3: 0}
        for v in assessment["recommended_services"].values():
            if isinstance(v, int) and v in counts:
                counts[v] += 1
        total = sum(counts.values()) or 1
        colors = {0: "#b71c1c", 1: "#ef6c00", 2: "#fbc02d", 3: "#2e7d32"}
        parts = []
        for lvl in (0, 1, 2, 3):
            pct = 100 * counts[lvl] // total
            parts.append(
                f'<div class="bar" style="background:{colors[lvl]};width:{pct}%;" '
                f'title="L{lvl}: {counts[lvl]}"></div>'
            )
        bar_section = (
            '<div class="dist">'
            + "".join(parts)
            + "</div>"
            + f"<p>L0: {counts[0]} · L1: {counts[1]} · L2: {counts[2]} · L3: {counts[3]}</p>"
        )

    schema_block = ""
    if schema_errors:
        schema_block = (
            "<div class='err'><strong>Schema validation</strong><ul>"
            + "".join(f"<li>{_esc(e)}</li>" for e in schema_errors)
            + "</ul></div>"
        )

    sk_verdict = str((skeptic or {}).get("verdict", "n/a")).upper()
    sk_color = {"CONFIRM": "#2e7d32", "DOWNGRADE": "#f57c00", "FLAG_DEEP_RESEARCH": "#c62828"}.get(
        sk_verdict, "#333"
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>{_esc(cname)} — { _esc(provider)}</title>
<style>
html {{ color-scheme: light; background: #fff; }}
body {{ font-family: system-ui, sans-serif; margin: 24px; max-width: 960px; color: #222; background: #fff; }}
header {{ border-bottom: 2px solid #ccc; padding-bottom: 12px; margin-bottom: 16px; }}
.dist {{ display: flex; height: 22px; border: 1px solid #999; margin: 8px 0; }}
.bar {{ height: 100%; }}
details {{ margin: 12px 0; border: 1px solid #ddd; padding: 8px; }}
summary {{ cursor: pointer; font-weight: 600; }}
.err {{ background: #ffebee; padding: 8px; margin: 8px 0; }}
@media print {{
  details {{ display: block; }}
  details > * {{ display: block !important; }}
  .dist .bar {{ background: #fff !important; border-left: 4px solid #000; }}
}}
</style></head><body>
<header>
  <h1>{_esc(display_name_from_assessment(assessment))} — {_esc(str(cname))}</h1>
  <p>Control ID: <code>{_esc(cid)}</code></p>
  <p>Assessed: {_esc(str(assessment.get('assessed_at','')))} · Pipeline: v{_esc(str(assessment.get('pipeline_version','')))}</p>
  <p>Status: <span style="color:{status_color}">{_esc(status)}</span></p>
</header>
{schema_block}
<section>
  <h2>Recommended score: {_esc(rec_label)}</h2>
  {bar_section}
  <p>Confidence: <span style="color:{conf_color};font-weight:600">{_esc(conf)}</span></p>
</section>
<details><summary>Control definition</summary>{crit_html}</details>
<details><summary>Documentation retrieved</summary>
  <p>Quality: {_esc(str(df.get('doc_quality')))}</p>
  <p>With docs: {_esc(', '.join(df.get('services_with_docs') or []))}</p>
  <p>Without docs: {_esc(', '.join(df.get('services_without_docs') or []))}</p>
  <ul>{''.join(f"<li><a href='{_esc(u)}'>{_esc(u)}</a></li>" for u in (df.get('urls_consulted') or []))}</ul>
</details>
<details open><summary>Assessor</summary>
  <pre style="white-space:pre-wrap">{_esc(yaml.safe_dump(assessor, allow_unicode=True))}</pre>
</details>
<details open><summary>Skeptic</summary>
  <p>Verdict: <span style="color:{sk_color};font-weight:600">{_esc(sk_verdict)}</span></p>
  <pre style="white-space:pre-wrap">{_esc(yaml.safe_dump(skeptic, allow_unicode=True))}</pre>
</details>
<details><summary>Consistency</summary>
  <pre style="white-space:pre-wrap">{_esc(yaml.safe_dump(consistency, allow_unicode=True))}</pre>
</details>
<details><summary>Deep research</summary>
  <pre style="white-space:pre-wrap">{_esc(yaml.safe_dump(dr, allow_unicode=True) if dr else 'null')}</pre>
</details>
<details><summary>Human review</summary>
  <pre style="white-space:pre-wrap">{_esc(yaml.safe_dump(assessment.get('review') or {}, allow_unicode=True))}</pre>
</details>
</body></html>"""


def display_name_from_assessment(assessment: dict[str, Any]) -> str:
    return str(assessment.get("provider_display_name") or assessment.get("provider", ""))


def render_index_html(
    provider_slug: str,
    display_name: str,
    pipeline_version: str,
    rows: list[dict[str, Any]],
) -> str:
    """Summary dashboard (Section 7.2)."""
    # rows: {control_id, domain, score, confidence, skeptic_verdict, status, href}
    def sort_key(r: dict[str, Any]) -> tuple[int, str, str]:
        status = r.get("status", "")
        pri = 0
        if "error" in status.lower() or "parse" in status.lower():
            pri = 0
        elif status == "needs_human_review":
            pri = 1
        else:
            pri = 2
        conf = (r.get("confidence") or "low").lower()
        conf_pri = {"low": 0, "medium": 1, "high": 2}.get(conf, 3)
        return (pri, conf_pri, r.get("domain", ""), r.get("control_id", ""))

    rows = sorted(rows, key=sort_key)
    total = len(rows)
    dist = {
        "L0": 0,
        "L1": 0,
        "L2": 0,
        "L3": 0,
        "mixed": 0,
        "unknown": 0,
        "other": 0,
    }
    for r in rows:
        s = r.get("score")
        if s == "mixed":
            dist["mixed"] += 1
        elif s in (0, 1, 2, 3):
            dist[f"L{s}"] += 1
        elif str(s).lower() == "unknown":
            dist["unknown"] += 1
        else:
            dist["other"] += 1

    table = []
    for r in rows:
        bg = "#fff"
        st = str(r.get("status", ""))
        if "error" in st:
            bg = "#ffcdd2"
        elif st == "needs_human_review":
            bg = "#ffe0b2"
        table.append(
            "<tr style='background:{}'>".format(bg)
            + f"<td>{_esc(r.get('domain',''))}</td>"
            + f"<td><a href='{_esc(r.get('href',''))}'>{_esc(r.get('control_id',''))}</a></td>"
            + f"<td>{_esc(str(r.get('score')))}</td>"
            + f"<td>{_esc(str(r.get('confidence')))}</td>"
            + f"<td>{_esc(str(r.get('skeptic_verdict')))}</td>"
            + f"<td>{_esc(str(r.get('status')))}</td>"
            + "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>{_esc(display_name)} — assessments</title>
<style>
html {{ color-scheme: light; background: #fff; }}
body {{ font-family: system-ui, sans-serif; margin: 24px; color: #222; background: #fff; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; font-size: 14px; }}
th {{ background: #f5f5f5; }}
</style></head><body>
<h1>{_esc(display_name)}</h1>
<p>Provider: <code>{_esc(provider_slug)}</code> · Pipeline v{_esc(pipeline_version)}</p>
<p>Controls: {total} · Distribution: L0 {dist['L0']}, L1 {dist['L1']}, L2 {dist['L2']}, L3 {dist['L3']}, mixed {dist['mixed']}, unknown {dist['unknown']}, other non-score {dist['other']}</p>
<table><thead><tr><th>Domain</th><th>Control</th><th>Score</th><th>Confidence</th><th>Skeptic</th><th>Status</th></tr></thead>
<tbody>{''.join(table)}</tbody></table>
</body></html>"""
