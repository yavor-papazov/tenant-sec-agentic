"""Parse structured agent outputs from LLM text."""

from __future__ import annotations

import re
from typing import Any


def _norm_score(s: str) -> int | str:
    t = s.strip().lower()
    if t == "mixed":
        return "mixed"
    if t.isdigit():
        v = int(t)
        if 0 <= v <= 3:
            return v
    raise ValueError(f"invalid score: {s!r}")


def parse_assessor_output(text: str) -> dict[str, Any]:
    """Parse PRD Section 4.3.3 format into assessor_output schema."""
    out: dict[str, Any] = {
        "score": 0,
        "services": {},
        "evidence": "",
        "confidence": "low",
        "flags": [],
        "deep_research_recommended": False,
        "sources_used": [],
    }
    score_m = re.search(
        r"^SCORE:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE
    )
    if not score_m:
        raise ValueError("missing SCORE line")
    out["score"] = _norm_score(score_m.group(1))

    conf_m = re.search(
        r"^CONFIDENCE:\s*(\w+)", text, re.MULTILINE | re.IGNORECASE
    )
    if conf_m:
        c = conf_m.group(1).lower()
        if c in ("high", "medium", "low"):
            out["confidence"] = c

    dr_m = re.search(
        r"^DEEP_RESEARCH_RECOMMENDED:\s*(true|false)",
        text,
        re.MULTILINE | re.IGNORECASE,
    )
    if dr_m:
        out["deep_research_recommended"] = dr_m.group(1).lower() == "true"

    ev_m = re.search(
        r"^EVIDENCE:\s*(.*?)(?=^SERVICES:|^FLAGS:|^SOURCES_USED:|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if ev_m:
        out["evidence"] = ev_m.group(1).strip()

    if out["score"] == "mixed":
        services: dict[str, Any] = {}
        sec_m = re.search(
            r"^SERVICES:\s*(.*?)(?=^FLAGS:|^SOURCES_USED:|\Z)",
            text,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if sec_m:
            block = sec_m.group(1)
            for line in block.splitlines():
                line = line.strip()
                if not line.startswith("-"):
                    continue
                # - service_id: N
                m = re.match(
                    r"^-\s*([a-zA-Z0-9_.-]+):\s*(\d|mixed)\s*$",
                    line,
                    re.IGNORECASE,
                )
                if not m:
                    continue
                sid, sc = m.group(1), m.group(2)
                if sc.lower() == "mixed":
                    raise ValueError("nested mixed not supported in SERVICES list")
                services[sid] = {"score": int(sc), "evidence": ""}
        # evidence lines under each service: look for "evidence:" continuation (simplified)
        for sid in list(services.keys()):
            pat = rf"-\s*{re.escape(sid)}:.*?\n\s*evidence:\s*(.+?)(?=\n-|\n\n|\Z)"
            em = re.search(pat, text, re.IGNORECASE | re.DOTALL)
            if em:
                services[sid]["evidence"] = em.group(1).strip()
        out["services"] = services

    flags_m = re.search(
        r"^FLAGS:\s*(.*?)(?=^SOURCES_USED:|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if flags_m:
        block = flags_m.group(1).strip()
        if block and block.lower() not in ("none", "[]", "- none"):
            for line in block.splitlines():
                line = line.strip().lstrip("-").strip()
                if line:
                    out["flags"].append(line)

    src_m = re.search(
        r"^SOURCES_USED:\s*(.*)\Z", text, re.MULTILINE | re.DOTALL | re.IGNORECASE
    )
    if src_m:
        for line in src_m.group(1).splitlines():
            line = line.strip().lstrip("-").strip()
            if line.startswith("http"):
                out["sources_used"].append(line)

    return out


def parse_skeptic_output(text: str) -> dict[str, Any]:
    verdict = "confirm"
    t = text.upper()
    if "FLAG_DEEP_RESEARCH" in t.replace(" ", "_") or "FLAG DEEP RESEARCH" in t:
        verdict = "flag_deep_research"
    elif re.search(r"\bDOWNGRADE\b", text, re.IGNORECASE):
        verdict = "downgrade"
    elif re.search(r"\bCONFIRM\b", text, re.IGNORECASE):
        verdict = "confirm"

    orig = "mixed"
    rec = "mixed"
    om = re.search(
        r"original\s*score:\s*(\d|mixed)", text, re.IGNORECASE
    )
    rm = re.search(
        r"recommended\s*score:\s*(\d|mixed)", text, re.IGNORECASE
    )
    if om:
        orig = _norm_score(om.group(1))
    if rm:
        rec = _norm_score(rm.group(1))

    flags: list[dict[str, str]] = []
    for m in re.finditer(
        r"skepticism_flags?:\s*\[([^\]]*)\]", text, re.IGNORECASE | re.DOTALL
    ):
        pass
    # Simple bullet extraction for flags
    for line in text.splitlines():
        line_st = line.strip()
        if "type:" in line_st.lower() and "severity:" in line_st.lower():
            flags.append(
                {
                    "type": "unknown",
                    "detail": line_st,
                    "severity": "info",
                }
            )

    questions: list[str] = []
    qsec = re.search(
        r"(?:deep[_ ]research[_ ]questions|questions):\s*(.*)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if qsec:
        for line in qsec.group(1).splitlines():
            line = line.strip().lstrip("-").strip()
            if line and len(line) > 5:
                questions.append(line)
                if len(questions) >= 12:
                    break

    return {
        "verdict": verdict,
        "original_score": orig,
        "recommended_score": rec,
        "recommended_services": {},
        "reasoning": text.strip()[:8000],
        "skepticism_flags": flags,
        "deep_research_questions": questions,
    }


def parse_consistency_output(text: str) -> dict[str, Any]:
    overall = "consistent"
    tl = text.lower()
    if "inconsistent" in tl:
        overall = "inconsistent"
    elif "suspicious" in tl:
        overall = "suspicious"

    flags: list[dict[str, Any]] = []
    if "mixed" in tl and "flat" in tl:
        flags.append(
            {
                "type": "mixed-elsewhere-flat-here",
                "reference_provider": "",
                "reference_score": "mixed",
                "current_score": 0,
                "detail": "Heuristic match from model output; verify manually.",
                "recommendation": "Reassess with per-service granularity",
            }
        )

    dr = "deep_research_triggered" in tl or "flag" in tl and "research" in tl

    return {
        "consistency_flags": flags,
        "deep_research_triggered": bool(dr and overall != "consistent"),
        "overall_consistency": overall,
    }


def parse_deep_research_output(text: str) -> dict[str, Any]:
    return {
        "questions_investigated": [],
        "revised_assessment": {},
        "unresolved": [],
        "raw": text[:12000],
    }
