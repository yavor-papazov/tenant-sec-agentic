"""Assessment orchestrator: ADK BaseAgent + per-control agent chain."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Any

import yaml
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
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
from tenant_sec_agentic.metered_model import MeteredLiteLlm
from tenant_sec_agentic.repo import load_all_controls, resolve_control_scope
from tenant_sec_agentic.schemas import (
    AssessorStructured,
    ConsistencyStructured,
    DeepResearchStructured,
    DocFetchStructured,
    SkepticStructured,
)
from tenant_sec_agentic.web_tools import (
    STATE_DEEP_FETCH_COUNT,
    STATE_DEEP_SEARCH_COUNT,
    STATE_DOC_FETCH_COUNT,
    STATE_DOC_SEARCH_COUNT,
    STATE_FETCHED_DOCUMENTS,
    doc_fetch_tools,
)
from tenant_sec_agentic.usage import (
    BudgetExceeded,
    consume_retry,
    new_ledger,
    write_ledger,
)

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
    async for event in agent.run_async(ctx):
        # Runner normally persists yielded events. These role agents are
        # invoked directly by the orchestrator, so retain their model/tool
        # turns explicitly; otherwise every tool iteration sees the same
        # request and repeats indefinitely.
        await ctx.session_service.append_event(
            session=ctx.session, event=event
        )


async def _with_retries_drain(
    agent: LlmAgent,
    ctx: InvocationContext,
    cfg: AssessmentConfig,
    role: str,
) -> None:
    last: BaseException | None = None
    control_id = str(ctx.session.state.get("current_control_id", ""))
    attempts = cfg.limits.max_retries_per_call + 1
    for i in range(attempts):
        try:
            await _drain_agent(agent, ctx)
            return
        except Exception as e:
            if isinstance(e, BudgetExceeded):
                raise
            last = e
            if i + 1 >= attempts:
                break
            consume_retry(ctx.session.state, cfg, role, control_id)
            base_delay = min(60.0, float(2**i))
            await asyncio.sleep(base_delay + random.uniform(0.0, 1.0))
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
        if not isinstance(st.get("usage_ledger"), dict):
            st["usage_ledger"] = new_ledger(cfg)

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
            st.pop(STATE_DOC_SEARCH_COUNT, None)
            st.pop(STATE_DOC_FETCH_COUNT, None)
            st.pop(STATE_FETCHED_DOCUMENTS, None)
            st["doc_fetch_max_search"] = (
                cfg.limits.max_search_calls_per_doc_fetch
            )
            st["doc_fetch_max_fetch"] = (
                cfg.limits.max_fetch_calls_per_doc_fetch
            )
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
            warning_detail: str | None = None

            try:
                if cfg.mock_mode:
                    doc_fetch_out = _mock_doc_fetch(cfg, control_def)
                    st["doc_fetch_output"] = doc_fetch_out
                    assessor_out = _mock_assessor(control_def, cfg)
                    st["assessor_output"] = assessor_out
                    skip_skeptic_m = (
                        assessor_out.get("status") == "unknown"
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
                        assessor_out.get("status") == "unknown"
                        and doc_fetch_out.get("doc_quality") == "absent"
                    )
                    if not skip_skeptic:
                        await self._run_skeptic(ctx, cfg, control_def)
                        skeptic_out = st.get("skeptic_output") or {}
                    else:
                        skeptic_out = None

                    skip_consistency = not peer_profiles
                    if not skip_consistency:
                        try:
                            await self._run_consistency(
                                ctx, cfg, control_def, peer_profiles
                            )
                            consistency_out = (
                                st.get("consistency_output") or {}
                            )
                        except Exception as exc:
                            logger.exception(
                                "optional consistency check failed for %s: %s",
                                cid,
                                exc,
                            )
                            warning_detail = (
                                "Optional consistency check failed: "
                                f"{exc}"
                            )
                            consistency_out = None
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
                            try:
                                await self._run_deep_research(
                                    ctx, cfg, control_def
                                )
                                deep_out = st.get("deep_research_output")
                            except Exception as exc:
                                logger.exception(
                                    "optional deep research failed for %s: %s",
                                    cid,
                                    exc,
                                )
                                detail = (
                                    "Optional deep research failed: "
                                    f"{exc}"
                                )
                                warning_detail = (
                                    f"{warning_detail}; {detail}"
                                    if warning_detail
                                    else detail
                                )
                                deep_out = None

            except Exception as e:
                logger.exception("control %s failed: %s", cid, e)
                item["status"] = "error"
                final_status = "error"
                err_detail = str(e)

            result_status, rec_score, rec_services, conf, final_assessor = (
                _final_recommendation(
                assessor_out, skeptic_out, deep_out
            )
            )

            control_entry = control_entry_for_provider_schema(
                result_status,
                rec_score,
                final_assessor,
                rec_services,
                conf,
            )
            schema_errs = validate_control_against_provider_schema(
                repo_root,
                slug,
                cfg.provider.display_name,
                cfg.provider.assessment_id,
                cfg.provider.offering,
                cfg.provider.services_in_scope,
                cid,
                control_entry,
            )

            assessment_status = "error" if err_detail else final_status
            assessment = build_assessment_yaml(
                control_id=cid,
                provider_slug=slug,
                provider_display_name=cfg.provider.display_name,
                assessment_id=cfg.provider.assessment_id,
                offering=cfg.provider.offering,
                services_in_scope=cfg.provider.services_in_scope,
                session_id=ctx.session.id,
                methodology_version=methodology_version,
                doc_fetch=doc_fetch_out,
                assessor=assessor_out,
                skeptic=skeptic_out,
                consistency=consistency_out,
                deep_research=deep_out,
                recommended_score=rec_score,
                recommended_services=rec_services,
                result_status=result_status,
                overall_confidence=conf,
                status=assessment_status,
                prompt_hashes=hashes,
                error_detail=err_detail or warning_detail,
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
        write_ledger(st, cfg)
        failed_controls = [
            str(item.get("control"))
            for item in queue
            if item.get("status") == "error"
        ]
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(
                state_delta={
                    "assessment_queue": queue,
                    "pipeline_failed_controls": failed_controls,
                    "usage_ledger": st["usage_ledger"],
                }
            ),
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
        model = _metered_model(ctx, cfg, "doc_fetch")
        agent = LlmAgent(
            name="doc_fetch_agent",
            model=model,
            instruction=doc_prompt,
            tools=doc_fetch_tools(),
            output_schema=DocFetchStructured,
            output_key="doc_fetch_output",
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        user_text = _user_block_doc_fetch(cfg, control_def)
        await _inject_user_and_drain(agent, ctx, user_text, cfg, "doc_fetch")
        output = ctx.session.state.get("doc_fetch_output")
        if not isinstance(output, dict):
            raise RuntimeError("doc_fetch agent produced no structured output")
        fetched = ctx.session.state.get(STATE_FETCHED_DOCUMENTS) or {}
        for document in output.get("docs_fetched") or []:
            cached = fetched.get(document.get("url"))
            if isinstance(cached, dict):
                document["content"] = cached.get("content", "")
        ctx.session.state["doc_fetch_output"] = output

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
        model = _metered_model(ctx, cfg, "assessor")
        agent = LlmAgent(
            name="assessor_agent",
            model=model,
            instruction=assessor_prompt,
            output_schema=AssessorStructured,
            output_key="assessor_output",
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        docs = ctx.session.state.get("doc_fetch_output") or {}
        user_text = (
            f"CONTROL YAML:\n```yaml\n"
            f"{yaml.safe_dump(control_def, sort_keys=False)}```\n"
            f"DOCUMENTATION BUNDLE:\n```json\n"
            f"{json.dumps(docs, default=str)}```"
        )
        await _inject_user_and_drain(agent, ctx, user_text, cfg, "assessor")

    async def _run_skeptic(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
    ) -> None:
        skeptic_prompt = _load_prompt("skeptic.md")
        model = _metered_model(ctx, cfg, "skeptic")
        agent = LlmAgent(
            name="skeptic_agent",
            model=model,
            instruction=skeptic_prompt,
            output_schema=SkepticStructured,
            output_key="skeptic_output",
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        docs = ctx.session.state.get("doc_fetch_output") or {}
        user_text = (
            f"CONTROL YAML:\n```yaml\n"
            f"{yaml.safe_dump(control_def, sort_keys=False)}```\n"
            f"DOCUMENTATION BUNDLE:\n```json\n"
            f"{json.dumps(docs, default=str)}```"
        )
        await _inject_user_and_drain(agent, ctx, user_text, cfg, "skeptic")
        independent = ctx.session.state.get("skeptic_output") or {}
        ctx.session.state["skeptic_output"] = _reconcile_skeptic(
            ctx.session.state.get("assessor_output") or {},
            independent,
        )

    async def _run_consistency(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
        peer_profiles: dict[str, Any],
    ) -> None:
        c_prompt = _load_prompt("consistency.md")
        model = _metered_model(ctx, cfg, "consistency")
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
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        user_text = (
            f"CONTROL: {cid}\nPEER SCORES JSON:\n"
            f"{json.dumps(peer_scores, indent=2, default=str)}"
        )
        await _inject_user_and_drain(agent, ctx, user_text, cfg, "consistency")

    async def _run_deep_research(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
    ) -> None:
        dr_prompt = _load_prompt("deep_research.md")
        model = _metered_model(ctx, cfg, "deep_research")
        agent = LlmAgent(
            name="deep_research_agent",
            model=model,
            instruction=dr_prompt,
            tools=doc_fetch_tools(),
            output_schema=DeepResearchStructured,
            output_key="deep_research_output",
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        user_text = (
            f"CONTROL YAML:\n```yaml\n"
            f"{yaml.safe_dump(control_def, sort_keys=False)}```\n"
            f"DOCUMENTATION BUNDLE:\n```json\n"
            f"{json.dumps(ctx.session.state.get('doc_fetch_output') or {}, default=str)}```\n"
            f"SKEPTIC QUESTIONS:\n```json\n"
            f"{json.dumps(ctx.session.state.get('skeptic_output') or {}, default=str)}```\n"
            f"CONSISTENCY FLAGS:\n```json\n"
            f"{json.dumps(ctx.session.state.get('consistency_output') or {}, default=str)}```"
        )
        await _inject_user_and_drain(
            agent,
            ctx,
            user_text,
            cfg,
            "deep_research",
        )


async def _inject_user_and_drain(
    agent: LlmAgent,
    ctx: InvocationContext,
    user_text: str,
    cfg: AssessmentConfig,
    role: str,
) -> None:
    """Append user turn to session and run sub-agent with retries."""
    control_id = str(ctx.session.state.get("current_control_id", "control"))
    branch = ".".join(
        part
        for part in (ctx.branch, control_id, role)
        if part
    )
    role_ctx = ctx.model_copy(update={"branch": branch})
    await role_ctx.session_service.append_event(
        session=role_ctx.session,
        event=Event(
            invocation_id=role_ctx.invocation_id,
            author="user",
            branch=branch,
            content=types.Content(
                role="user",
                parts=[types.Part(text=user_text)],
            ),
        ),
    )
    await _with_retries_drain(agent, role_ctx, cfg, role)


def _metered_model(
    ctx: InvocationContext,
    cfg: AssessmentConfig,
    role: str,
) -> MeteredLiteLlm:
    return MeteredLiteLlm(
        model=cfg.model_for(role),
        state=ctx.session.state,
        cfg=cfg,
        role=role,
        control_id=str(ctx.session.state.get("current_control_id", "")),
    )


def _user_block_doc_fetch(cfg: AssessmentConfig, control_def: dict[str, Any]) -> str:
    return (
        f"PROVIDER: {cfg.provider.display_name} ({cfg.provider.slug})\n"
        f"DOCS_BASE_URL: {cfg.provider.docs_base_url}\n"
        f"SERVICES_IN_SCOPE: {json.dumps(cfg.provider.services_in_scope)}\n"
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
) -> tuple[str, Any, dict[str, Any] | None, str, dict[str, Any]]:
    conf = str(assessor.get("confidence") or "low")

    revised = (deep or {}).get("revised_assessment")
    if isinstance(revised, dict) and revised.get("status"):
        status = str(revised["status"])
        score = revised.get("score")
        services = revised.get("services") if score == "mixed" else None
        return (
            status,
            score,
            services,
            str(revised.get("confidence") or conf),
            revised,
        )

    if skeptic and skeptic.get("verdict") == "downgrade":
        status = str(skeptic.get("recommended_status") or "unknown")
        score = skeptic.get("recommended_score")
        services = (
            skeptic.get("recommended_services")
            if score == "mixed"
            else None
        )
        return status, score, services, conf, skeptic

    status = str(assessor.get("status") or "unknown")
    score = assessor.get("score")
    services = assessor.get("services") if score == "mixed" else None
    return status, score, services, conf, assessor


def _reconcile_skeptic(
    assessor: dict[str, Any],
    independent: dict[str, Any],
) -> dict[str, Any]:
    """Compare independent output only after it has been produced."""
    out = dict(independent)
    out["independent_status"] = independent.get("status", "unknown")
    out["independent_score"] = independent.get("score")
    out["independent_services"] = independent.get("services") or {}
    out["recommended_status"] = assessor.get("status", "unknown")
    out["recommended_score"] = assessor.get("score")
    out["recommended_services"] = assessor.get("services") or {}

    assessor_status = assessor.get("status")
    skeptic_status = independent.get("status")
    if assessor_status != skeptic_status:
        out["verdict"] = "flag_deep_research"
        questions = list(out.get("deep_research_questions") or [])
        questions.append(
            "Resolve the independent assessors' disagreement on assessment status."
        )
        out["deep_research_questions"] = questions
        return out

    assessor_value = _comparable_score(assessor)
    skeptic_value = _comparable_score(independent)
    if (
        assessor_status == "assessed"
        and assessor_value is not None
        and skeptic_value is not None
        and skeptic_value < assessor_value
    ):
        out["verdict"] = "downgrade"
        out["recommended_status"] = skeptic_status
        out["recommended_score"] = independent.get("score")
        out["recommended_services"] = independent.get("services") or {}
    elif assessor_value != skeptic_value:
        out["verdict"] = "flag_deep_research"
    else:
        out["verdict"] = "confirm"
    return out


def _comparable_score(result: dict[str, Any]) -> float | None:
    score = result.get("score")
    if isinstance(score, int):
        return float(score)
    if score == "mixed":
        values = [
            service.get("score")
            for service in (result.get("services") or {}).values()
            if isinstance(service, dict)
            and service.get("status") == "assessed"
            and isinstance(service.get("score"), int)
        ]
        if values:
            return sum(values) / len(values)
    return None


def _mock_doc_fetch(cfg: AssessmentConfig, control_def: dict[str, Any]) -> dict[str, Any]:
    ids = [s["id"] for s in cfg.provider.services_in_scope]
    return {
        "docs_fetched": [],
        "services_with_docs": [],
        "services_without_docs": ids,
        "doc_quality": "absent",
    }


def _mock_assessor(control_def: dict[str, Any], cfg: AssessmentConfig) -> dict[str, Any]:
    return {
        "status": "unknown",
        "score": None,
        "services": {},
        "evidence": "Mock pipeline run — no authoritative documentation.",
        "confidence": "low",
        "flags": ["mock-mode"],
        "deep_research_recommended": False,
        "sources_used": [],
    }


def _mock_skeptic(assessor: dict[str, Any]) -> dict[str, Any]:
    independent = {
        "status": assessor.get("status", "unknown"),
        "score": assessor.get("score"),
        "services": assessor.get("services") or {},
        "reasoning": "Mock pipeline — skeptic confirms assessor output.",
        "skepticism_flags": [],
        "deep_research_questions": [],
    }
    return _reconcile_skeptic(assessor, independent)


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
            score = data.get("recommended_score")
            if score is None:
                score = str(data.get("result_status") or "unknown").upper()
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
    methodology_version = "interim-prd-4.4" if interim else "2.0"
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
        "usage_ledger": new_ledger(cfg),
        "pipeline_version": __version__,
    }
