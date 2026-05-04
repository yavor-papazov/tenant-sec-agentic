"""Assessment orchestrator: ADK BaseAgent + per-control agent chain."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.models.lite_llm import LiteLlm
from google.genai import types
from typing_extensions import override

from tenant_sec_agentic import __version__
from tenant_sec_agentic.artifacts import (
    build_assessment_yaml,
    control_entry_for_provider_schema,
    validate_control_against_provider_schema,
    validate_internal_assessment,
    write_assessment_files,
    render_index_html,
)
from tenant_sec_agentic.config import AssessmentConfig, config_from_state, config_to_serializable
from tenant_sec_agentic.repo import load_all_controls, resolve_control_scope
from tenant_sec_agentic.schemas import (
    AssessorStructured,
    ConsistencyStructured,
    DeepResearchStructured,
    DocFetchStructured,
    SkepticStructured,
)
from tenant_sec_agentic.web_tools import STATE_DEEP_FETCH_COUNT, STATE_DEEP_SEARCH_COUNT, doc_fetch_tools

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _prompt_hash(name: str) -> str:
    p = PROMPTS_DIR / name
    h = hashlib.sha256(p.read_bytes())
    return h.hexdigest()[:16]


def prompt_hashes() -> dict[str, str]:
    return {
        "doc_fetch": _prompt_hash("doc_fetch.md"),
        "assessor": _prompt_hash("assessor.md"),
        "skeptic": _prompt_hash("skeptic.md"),
        "consistency": _prompt_hash("consistency.md"),
        "deep_research": _prompt_hash("deep_research.md"),
    }


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


async def _drain_agent(agent: LlmAgent, ctx: InvocationContext) -> None:
    async for _ in agent.run_async(ctx):
        pass


async def _with_retries_drain(
    agent: LlmAgent, ctx: InvocationContext, retries: int = 5
) -> None:
    delays = (2, 4, 8, 16, 32)
    last: BaseException | None = None
    for i in range(retries):
        try:
            await _drain_agent(agent, ctx)
            return
        except Exception as e:
            last = e
            if i < len(delays):
                await asyncio.sleep(delays[i])
            logger.warning("LLM/API retry %s after error: %s", i + 1, e)
    assert last is not None
    raise last


class AssessmentPipeline(BaseAgent):
    """Runs DocFetch → Assessor → Skeptic → Consistency → DeepResearch → Artifacts per control."""

    name: str = "assessment_pipeline"
    description: str = "tenant-sec assessment pipeline orchestrator"

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        st = ctx.session.state
        cfg = config_from_state(st["assessment_config"])
        methodology_text = st.get("methodology_text", "")
        methodology_version = st.get("methodology_version", "1.0")
        peer_profiles = st.get("peer_profiles") or {}
        controls_index: dict[str, Any] = json.loads(st["controls_json"])
        queue: list[dict[str, Any]] = st.get("assessment_queue") or []

        repo_root = cfg.paths.repo_root
        slug = cfg.provider.slug

        hashes = prompt_hashes()

        for item in queue:
            if item.get("status") in ("completed", "skipped"):
                continue
            cid = item["control"]
            item["status"] = "in_progress"
            control_def = controls_index.get(cid)
            if not control_def:
                logger.error("missing control %s", cid)
                item["status"] = "error"
                continue

            st["current_control_id"] = cid
            st["pipeline_mode"] = "doc_fetch"
            st.pop(STATE_DEEP_SEARCH_COUNT, None)
            st.pop(STATE_DEEP_FETCH_COUNT, None)
            for k in (
                "doc_fetch_output",
                "assessor_output",
                "skeptic_output",
                "consistency_output",
                "deep_research_output",
            ):
                st.pop(k, None)

            doc_fetch_out: dict[str, Any] = {}
            assessor_out: dict[str, Any] = {}
            skeptic_out: dict[str, Any] | None = None
            consistency_out: dict[str, Any] | None = None
            deep_out: dict[str, Any] | None = None
            final_status = "needs_human_review"
            err_detail: str | None = None

            try:
                if cfg.mock_mode:
                    doc_fetch_out = _mock_doc_fetch(cfg, control_def)
                    st["doc_fetch_output"] = doc_fetch_out
                    assessor_out = _mock_assessor(control_def, cfg)
                    st["assessor_output"] = assessor_out
                    skip_skeptic_m = (
                        assessor_out.get("score") == 0
                        and doc_fetch_out.get("doc_quality") == "absent"
                    )
                    if not skip_skeptic_m:
                        skeptic_out = _mock_skeptic(assessor_out)
                        st["skeptic_output"] = skeptic_out
                    else:
                        skeptic_out = None
                        st.pop("skeptic_output", None)
                    if peer_profiles:
                        consistency_out = _mock_consistency()
                        st["consistency_output"] = consistency_out
                    else:
                        consistency_out = None
                        st.pop("consistency_output", None)
                else:
                    await self._run_doc_fetch(ctx, cfg, methodology_text, control_def)
                    doc_fetch_out = st.get("doc_fetch_output") or {}
                    await self._run_assessor(ctx, cfg, methodology_text, control_def)
                    assessor_out = st.get("assessor_output") or {}

                    skip_skeptic = (
                        assessor_out.get("score") == 0
                        and doc_fetch_out.get("doc_quality") == "absent"
                    )
                    if not skip_skeptic:
                        await self._run_skeptic(ctx, cfg, control_def)
                        skeptic_out = st.get("skeptic_output") or {}
                    else:
                        skeptic_out = None

                    skip_consistency = not peer_profiles
                    if not skip_consistency:
                        await self._run_consistency(ctx, cfg, control_def, peer_profiles)
                        consistency_out = st.get("consistency_output") or {}
                    else:
                        consistency_out = None

                    if _should_deep_research(
                        st, cfg, assessor_out, skeptic_out, consistency_out
                    ):
                        used = int(st.get("deep_research_invocations", 0))
                        if used < cfg.limits.max_deep_research_per_run:
                            st["deep_research_invocations"] = used + 1
                            st["pipeline_mode"] = "deep_research"
                            st["deep_research_max_search"] = (
                                cfg.limits.max_search_calls_per_deep_research
                            )
                            st["deep_research_max_fetch"] = (
                                cfg.limits.max_fetch_calls_per_deep_research
                            )
                            st.pop(STATE_DEEP_SEARCH_COUNT, None)
                            st.pop(STATE_DEEP_FETCH_COUNT, None)
                            await self._run_deep_research(ctx, cfg, control_def)
                            deep_out = st.get("deep_research_output")

            except Exception as e:
                logger.exception("control %s failed: %s", cid, e)
                item["status"] = "error"
                final_status = "error"
                err_detail = str(e)

            rec_score, rec_services, conf = _final_recommendation(
                assessor_out, skeptic_out, deep_out
            )

            control_entry = control_entry_for_provider_schema(
                rec_score, assessor_out, rec_services
            )
            schema_errs = validate_control_against_provider_schema(
                repo_root,
                slug,
                cfg.provider.display_name,
                cfg.provider.services_in_scope,
                cid,
                control_entry,
            )

            assessment_status = "error" if err_detail else final_status
            assessment = build_assessment_yaml(
                control_id=cid,
                provider_slug=slug,
                provider_display_name=cfg.provider.display_name,
                session_id=ctx.session.id,
                methodology_version=methodology_version,
                doc_fetch=doc_fetch_out,
                assessor=assessor_out,
                skeptic=skeptic_out,
                consistency=consistency_out,
                deep_research=deep_out,
                recommended_score=rec_score,
                recommended_services=rec_services,
                overall_confidence=conf,
                status=assessment_status,
                prompt_hashes=hashes,
                error_detail=err_detail,
            )
            internal_errs = validate_internal_assessment(assessment)
            if internal_errs:
                logger.warning("internal validation: %s", internal_errs)

            write_assessment_files(
                assessments_dir=cfg.paths.assessments_dir,
                reports_dir=cfg.paths.reports_dir,
                provider_slug=slug,
                control_id=cid,
                assessment=assessment,
                control_def=control_def,
                schema_errors=schema_errs,
            )
            if schema_errs:
                assessment["status"] = "validation_error"

            item["status"] = "completed" if not err_detail else "error"
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            text=f"Completed {cid} status={assessment['status']}"
                        )
                    ],
                ),
            )

        _write_dashboard(cfg, controls_index, queue)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text="Pipeline finished all queued controls.")],
            ),
        )

    async def _run_doc_fetch(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        methodology_text: str,
        control_def: dict[str, Any],
    ) -> None:
        doc_prompt = _load_prompt("doc_fetch.md")
        model = LiteLlm(model=cfg.model_for("doc_fetch"))
        agent = LlmAgent(
            name="doc_fetch_agent",
            model=model,
            instruction=doc_prompt,
            tools=doc_fetch_tools(),
            output_schema=DocFetchStructured,
            output_key="doc_fetch_output",
            include_contents="none",
        )
        user_text = _user_block_doc_fetch(cfg, control_def)
        await _inject_user_and_drain(agent, ctx, user_text)

    async def _run_assessor(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        methodology_text: str,
        control_def: dict[str, Any],
    ) -> None:
        assessor_prompt = _load_prompt("assessor.md").format(
            methodology=methodology_text
        )
        model = LiteLlm(model=cfg.model_for("assessor"))
        agent = LlmAgent(
            name="assessor_agent",
            model=model,
            instruction=assessor_prompt,
            output_schema=AssessorStructured,
            output_key="assessor_output",
            include_contents="none",
        )
        user_text = f"CONTROL YAML:\n```yaml\n{yaml.safe_dump(control_def, sort_keys=False)}```"
        await _inject_user_and_drain(agent, ctx, user_text)

    async def _run_skeptic(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
    ) -> None:
        skeptic_prompt = _load_prompt("skeptic.md")
        model = LiteLlm(model=cfg.model_for("skeptic"))
        agent = LlmAgent(
            name="skeptic_agent",
            model=model,
            instruction=skeptic_prompt,
            output_schema=SkepticStructured,
            output_key="skeptic_output",
            include_contents="none",
        )
        user_text = f"CONTROL YAML:\n```yaml\n{yaml.safe_dump(control_def, sort_keys=False)}```"
        await _inject_user_and_drain(agent, ctx, user_text)

    async def _run_consistency(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
        peer_profiles: dict[str, Any],
    ) -> None:
        c_prompt = _load_prompt("consistency.md")
        model = LiteLlm(model=cfg.model_for("consistency"))
        cid = control_def["id"]
        peer_scores = {
            p: (data.get("controls") or {}).get(cid)
            for p, data in peer_profiles.items()
        }
        ctx.session.state["peer_control_scores"] = peer_scores
        agent = LlmAgent(
            name="consistency_agent",
            model=model,
            instruction=c_prompt,
            output_schema=ConsistencyStructured,
            output_key="consistency_output",
            include_contents="none",
        )
        user_text = (
            f"CONTROL: {cid}\nPEER SCORES JSON:\n"
            f"{json.dumps(peer_scores, indent=2, default=str)}"
        )
        await _inject_user_and_drain(agent, ctx, user_text)

    async def _run_deep_research(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
    ) -> None:
        dr_prompt = _load_prompt("deep_research.md")
        model = LiteLlm(model=cfg.model_for("deep_research"))
        agent = LlmAgent(
            name="deep_research_agent",
            model=model,
            instruction=dr_prompt,
            tools=doc_fetch_tools(),
            output_schema=DeepResearchStructured,
            output_key="deep_research_output",
            include_contents="none",
        )
        user_text = f"CONTROL YAML:\n```yaml\n{yaml.safe_dump(control_def, sort_keys=False)}```"
        await _inject_user_and_drain(agent, ctx, user_text)


async def _inject_user_and_drain(
    agent: LlmAgent, ctx: InvocationContext, user_text: str
) -> None:
    """Append user turn to session and run sub-agent with retries."""
    ctx.session.events.append(
        Event(
            invocation_id=ctx.invocation_id,
            author="user",
            content=types.Content(
                role="user",
                parts=[types.Part(text=user_text)],
            ),
        )
    )
    await _with_retries_drain(agent, ctx)


def _user_block_doc_fetch(cfg: AssessmentConfig, control_def: dict[str, Any]) -> str:
    return (
        f"PROVIDER: {cfg.provider.display_name} ({cfg.provider.slug})\n"
        f"DOCS_BASE_URL: {cfg.provider.docs_base_url}\n"
        f"CONTROL: {control_def.get('id')}\n"
        f"NAME: {control_def.get('name')}\n"
        f"DOMAIN: {control_def.get('domain')}\n"
        f"SERVICE_SCOPED: {control_def.get('service_scoped', False)}\n"
        f"DESCRIPTION:\n{control_def.get('description','')}\n"
    )


def _should_deep_research(
    st: dict[str, Any],
    cfg: AssessmentConfig,
    assessor: dict[str, Any],
    skeptic: dict[str, Any] | None,
    consistency: dict[str, Any] | None,
) -> bool:
    if assessor.get("deep_research_recommended"):
        return True
    if skeptic and skeptic.get("verdict") == "flag_deep_research":
        return True
    if consistency and consistency.get("deep_research_triggered"):
        return True
    return False


def _final_recommendation(
    assessor: dict[str, Any],
    skeptic: dict[str, Any] | None,
    deep: dict[str, Any] | None,
) -> tuple[Any, dict[str, int] | None, str]:
    conf = str(assessor.get("confidence") or "low")

    revised = (deep or {}).get("revised_assessment")
    if isinstance(revised, dict) and revised.get("score") is not None:
        score = revised["score"]
        services = None
        if score == "mixed" and revised.get("services"):
            services = {
                k: int(v["score"])
                for k, v in revised["services"].items()
                if isinstance(v, dict) and "score" in v
            }
        return score, services, str(revised.get("confidence") or conf)

    if skeptic and skeptic.get("verdict") == "downgrade":
        rs = skeptic.get("recommended_score")
        if rs is not None:
            if rs == "mixed" and skeptic.get("recommended_services"):
                services = {
                    k: int(v["score"])
                    for k, v in (skeptic["recommended_services"] or {}).items()
                    if isinstance(v, dict)
                }
                return "mixed", services, conf
            if isinstance(rs, int):
                return rs, None, conf

    score = assessor.get("score")
    services = None
    if score == "mixed" and assessor.get("services"):
        services = {
            k: int(v["score"])
            for k, v in assessor["services"].items()
            if isinstance(v, dict) and "score" in v
        }
    return score, services, conf


def _mock_doc_fetch(cfg: AssessmentConfig, control_def: dict[str, Any]) -> dict[str, Any]:
    ids = [s["id"] for s in cfg.provider.services_in_scope]
    return {
        "docs_fetched": [],
        "services_with_docs": [],
        "services_without_docs": ids,
        "doc_quality": "absent",
    }


def _mock_assessor(control_def: dict[str, Any], cfg: AssessmentConfig) -> dict[str, Any]:
    scoped = bool(control_def.get("service_scoped"))
    if scoped:
        services = {
            s["id"]: {
                "score": 0,
                "evidence": "No documentation found for this capability on this service.",
            }
            for s in cfg.provider.services_in_scope
        }
        return {
            "score": "mixed",
            "services": services,
            "evidence": "Mock pipeline run — no docs retrieved.",
            "confidence": "low",
            "flags": ["mock-mode"],
            "deep_research_recommended": False,
            "sources_used": [],
        }
    return {
        "score": 0,
        "services": {},
        "evidence": "Mock pipeline run — no documentation.",
        "confidence": "low",
        "flags": ["mock-mode"],
        "deep_research_recommended": False,
        "sources_used": [],
    }


def _mock_skeptic(assessor: dict[str, Any]) -> dict[str, Any]:
    sc = assessor.get("score")
    if sc not in (0, 1, 2, 3, "mixed"):
        sc = 0
    rec_services: dict[str, Any] = {}
    if sc == "mixed" and isinstance(assessor.get("services"), dict):
        for sid, data in assessor["services"].items():
            if isinstance(data, dict) and "score" in data:
                rec_services[sid] = {
                    "score": int(data["score"]),
                    "evidence": str(data.get("evidence", "")),
                }
    return {
        "verdict": "confirm",
        "original_score": sc,
        "recommended_score": sc,
        "recommended_services": rec_services,
        "reasoning": "Mock pipeline — skeptic confirms assessor output.",
        "skepticism_flags": [],
        "deep_research_questions": [],
    }


def _mock_consistency() -> dict[str, Any]:
    return {
        "consistency_flags": [],
        "deep_research_triggered": False,
        "overall_consistency": "consistent",
    }


def _write_dashboard(
    cfg: AssessmentConfig,
    controls_index: dict[str, Any],
    queue: list[dict[str, Any]],
) -> None:
    rows = []
    prov_dir = cfg.paths.assessments_dir / cfg.provider.slug
    for item in queue:
        cid = item["control"]
        path = prov_dir / f"{cid}.yaml"
        inv = prov_dir / f"{cid}.invalid.yaml"
        p = path if path.is_file() else inv
        score = ""
        conf = ""
        skeptic_v = ""
        status = item.get("status", "")
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            score = data.get("recommended_score", "")
            conf = data.get("overall_confidence", "")
            sk = data.get("skeptic") or {}
            skeptic_v = sk.get("verdict", "")
            status = data.get("status", status)
        domain = (controls_index.get(cid) or {}).get("domain", "")
        href = f"{cid}.html"
        rows.append(
            {
                "control_id": cid,
                "domain": domain,
                "score": score,
                "confidence": conf,
                "skeptic_verdict": skeptic_v,
                "status": status,
                "href": href,
            }
        )
    html = render_index_html(
        cfg.provider.slug,
        cfg.provider.display_name,
        __version__,
        rows,
    )
    out = cfg.paths.reports_dir / cfg.provider.slug / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def initial_session_state(cfg: AssessmentConfig) -> dict[str, Any]:
    from tenant_sec_agentic.repo import (
        load_methodology_or_interim,
        load_provider_profiles_scores,
        validate_repo_layout,
    )

    validate_repo_layout(cfg.paths.repo_root)
    methodology_text, interim = load_methodology_or_interim(cfg.paths.repo_root)
    methodology_version = "interim-prd-4.4" if interim else "1.0"
    controls = load_all_controls(cfg.paths.repo_root)
    control_ids = resolve_control_scope(cfg.controls_scope, controls)
    peer = load_provider_profiles_scores(
        cfg.paths.repo_root / "providers", cfg.provider.slug
    )

    queue = [{"control": cid, "status": "pending"} for cid in control_ids]

    return {
        "assessment_config": config_to_serializable(cfg),
        "methodology_text": methodology_text,
        "methodology_version": methodology_version,
        "methodology_interim": interim,
        "peer_profiles": peer,
        "controls_json": json.dumps(controls),
        "assessment_queue": queue,
        "deep_research_invocations": 0,
        "pipeline_version": __version__,
    }
