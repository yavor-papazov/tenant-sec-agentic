"""Assessment orchestrator: ADK BaseAgent + per-control agent chain."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from json_repair import repair_json
from typing_extensions import override

from tenant_sec_agentic import __version__
from tenant_sec_agentic.artifacts import (
    build_assessment_yaml,
    canonical_service_id,
    canonicalize_service_map,
    control_entry_for_provider_schema,
    validate_control_against_provider_schema,
    validate_internal_assessment,
    validate_release_ready_assessment,
    write_assessment_files,
    render_index_html,
)
from tenant_sec_agentic.config import AssessmentConfig, config_from_state, config_to_serializable
from tenant_sec_agentic.metered_model import MeteredGemini
from tenant_sec_agentic.repo import (
    load_all_controls,
    load_methodology_or_interim,
    load_provider_profiles_scores,
    resolve_control_scope,
)
from tenant_sec_agentic.schemas import (
    AssessorStructured,
    ConsistencyStructured,
    DeepResearchStructured,
    DocFetchStructured,
    ResearchFindingsStructured,
    SkepticStructured,
)
from tenant_sec_agentic.web_tools import (
    STATE_DEEP_FETCH_COUNT,
    STATE_DEEP_SEARCH_COUNT,
    STATE_DOC_FETCH_COUNT,
    STATE_DOC_SEARCH_COUNT,
    STATE_FETCHED_DOCUMENTS,
    doc_fetch_tools,
    web_fetch,
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
        "research_revision": _prompt_hash("research_revision.md"),
    }


def assessment_input_hashes(
    cfg: AssessmentConfig,
    control_def: dict[str, Any],
    methodology_text: str,
    methodology_version: str,
    peer_profiles: dict[str, Any],
) -> dict[str, str]:
    control_id = str(control_def.get("id") or "")
    peer_context = {
        provider: (profile.get("controls") or {}).get(control_id)
        for provider, profile in peer_profiles.items()
    }
    values: dict[str, Any] = {
        "control_definition": control_def,
        "methodology": {
            "version": methodology_version,
            "content": methodology_text,
        },
        "provider_scope": {
            "slug": cfg.provider.slug,
            "display_name": cfg.provider.display_name,
            "docs_base_url": cfg.provider.docs_base_url,
            "assessment_id": cfg.provider.assessment_id,
            "offering": cfg.provider.offering,
            "services_in_scope": cfg.provider.services_in_scope,
        },
        "peer_context": peer_context,
        "evidence_seeds": cfg.evidence_seeds.get(control_id, []),
    }
    return {
        key: hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        for key, value in values.items()
    }


def current_assessment_input_hashes(
    cfg: AssessmentConfig,
    control_id: str,
) -> dict[str, str]:
    methodology_text, interim = load_methodology_or_interim(
        cfg.paths.repo_root
    )
    controls = load_all_controls(cfg.paths.repo_root)
    control_def = controls.get(control_id)
    if not isinstance(control_def, dict):
        return {}
    peer_profiles = load_provider_profiles_scores(
        cfg.paths.repo_root / "providers",
        cfg.provider.slug,
    )
    return assessment_input_hashes(
        cfg,
        control_def,
        methodology_text,
        "interim-prd-4.4" if interim else "2.0",
        peer_profiles,
    )


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
            input_hashes = assessment_input_hashes(
                cfg,
                control_def,
                methodology_text,
                methodology_version,
                peer_profiles,
            )

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
                "post_research_skeptic_output",
                "consistency_output",
                "deep_research_output",
                "deep_research_raw",
                "deep_research_revision_output",
            ):
                st.pop(k, None)

            doc_fetch_out: dict[str, Any] = {}
            assessor_out: dict[str, Any] = {}
            skeptic_out: dict[str, Any] | None = None
            consistency_out: dict[str, Any] | None = None
            checkpoint_skeptic: dict[str, Any] | None = None
            checkpoint_consistency: dict[str, Any] | None = None
            deep_out: dict[str, Any] | None = None
            checkpoint_deep: dict[str, Any] | None = None
            final_status = "needs_human_review"
            err_detail: str | None = None
            warning_detail: str | None = None
            retrieval_outcome = "failed"

            try:
                if cfg.mock_mode:
                    doc_fetch_out = _mock_doc_fetch(cfg, control_def)
                    retrieval_outcome = _retrieval_outcome(doc_fetch_out)
                    doc_fetch_out["retrieval_outcome"] = retrieval_outcome
                    st["doc_fetch_output"] = doc_fetch_out
                    assessor_out = _not_assessed_for_retrieval(
                        retrieval_outcome
                    )
                    st["assessor_output"] = assessor_out
                    skip_skeptic_m = assessor_out.get("status") != "assessed"
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
                    checkpoint = _load_stage_checkpoint(
                        cfg,
                        cid,
                        hashes,
                        input_hashes,
                    )
                    if checkpoint is not None:
                        (
                            doc_fetch_out,
                            assessor_out,
                            checkpoint_skeptic,
                            checkpoint_consistency,
                            checkpoint_deep,
                        ) = checkpoint
                        retrieval_outcome = str(
                            doc_fetch_out.get("retrieval_outcome")
                            or _retrieval_outcome(doc_fetch_out)
                        )
                        doc_fetch_out["retrieval_outcome"] = retrieval_outcome
                        st["doc_fetch_output"] = doc_fetch_out
                        st["assessor_output"] = assessor_out
                        st[STATE_FETCHED_DOCUMENTS] = (
                            _fetched_documents_from_bundle(doc_fetch_out)
                        )
                        logger.info(
                            "resuming %s from doc-fetch/assessor checkpoint",
                            cid,
                        )
                    else:
                        await self._run_doc_fetch(
                            ctx, cfg, methodology_text, control_def
                        )
                        doc_fetch_out = st.get("doc_fetch_output") or {}
                        retrieval_outcome = _retrieval_outcome(doc_fetch_out)
                        doc_fetch_out["retrieval_outcome"] = retrieval_outcome
                        if retrieval_outcome in {"failed", "exhausted"}:
                            assessor_out = _not_assessed_for_retrieval(
                                retrieval_outcome
                            )
                            st["assessor_output"] = assessor_out
                            err_detail = (
                                "Evidence retrieval failed"
                                if retrieval_outcome == "failed"
                                else "Evidence retrieval exhausted without "
                                "fetching a document"
                            )
                        else:
                            await self._run_assessor(
                                ctx, cfg, methodology_text, control_def
                            )
                            assessor_out = st.get("assessor_output") or {}

                    skip_skeptic = (
                        checkpoint_skeptic is not None
                        or retrieval_outcome in {"failed", "exhausted"}
                        or (
                            assessor_out.get("status") != "assessed"
                            and doc_fetch_out.get("doc_quality") == "absent"
                        )
                    )
                    if not skip_skeptic:
                        await self._run_skeptic(ctx, cfg, control_def)
                        skeptic_out = st.get("skeptic_output") or {}
                    else:
                        skeptic_out = checkpoint_skeptic

                    skip_consistency = (
                        checkpoint_consistency is not None
                        or not peer_profiles
                        or retrieval_outcome in {"failed", "exhausted"}
                    )
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
                                "required consistency check failed for %s: %s",
                                cid,
                                exc,
                            )
                            detail = (
                                "Required consistency check failed: "
                                f"{exc}"
                            )
                            err_detail = (
                                f"{err_detail}; {detail}"
                                if err_detail
                                else detail
                            )
                            consistency_out = None
                    else:
                        consistency_out = checkpoint_consistency

                    if (
                        not err_detail
                        and retrieval_outcome
                        not in {"failed", "exhausted"}
                        and (
                            checkpoint_deep is not None
                            or _should_deep_research(
                                st,
                                cfg,
                                assessor_out,
                                skeptic_out,
                                consistency_out,
                            )
                        )
                    ):
                        if checkpoint_deep is not None:
                            deep_out = checkpoint_deep
                            needs_revision = bool(
                                deep_out.pop("_needs_revision", False)
                            )
                            pending_challenge = deep_out.pop(
                                "_pending_challenge",
                                None,
                            )
                            deep_out.pop("post_research_skeptic", None)
                            st["deep_research_output"] = deep_out
                            if needs_revision:
                                st["pipeline_mode"] = "deep_research"
                                deep_out = (
                                    await self
                                    ._supplement_research_if_needed(
                                        ctx,
                                        cfg,
                                        control_def,
                                        deep_out,
                                        skeptic_out or {},
                                    )
                                )
                                if isinstance(
                                    pending_challenge,
                                    dict,
                                ):
                                    deep_out = (
                                        await self
                                        ._supplement_research_if_needed(
                                            ctx,
                                            cfg,
                                            control_def,
                                            deep_out,
                                            pending_challenge,
                                        )
                                    )
                                _validate_deep_question_coverage(
                                    deep_out,
                                    skeptic_out or {},
                                )
                                _fetch_missing_deep_sources(ctx, deep_out)
                                deep_out["revised_assessment"] = (
                                    await self._run_research_revision(
                                        ctx,
                                        cfg,
                                        control_def,
                                        deep_out,
                                        challenge=pending_challenge,
                                    )
                                )
                                _sync_fetched_documents(
                                    doc_fetch_out,
                                    st.get(STATE_FETCHED_DOCUMENTS) or {},
                                )
                                logger.info(
                                    "resuming %s from research findings",
                                    cid,
                                )
                            else:
                                logger.info(
                                    "resuming %s from completed deep research",
                                    cid,
                                )
                        else:
                            used = int(
                                st.get("deep_research_invocations", 0)
                            )
                            if used >= cfg.limits.max_deep_research_per_run:
                                err_detail = (
                                    "Required deep research was not run "
                                    "because the per-run limit was exhausted"
                                )
                            else:
                                st["deep_research_invocations"] = used + 1
                                st["pipeline_mode"] = "deep_research"
                                st["deep_research_max_search"] = (
                                    cfg.limits
                                    .max_search_calls_per_deep_research
                                )
                                st["deep_research_max_fetch"] = (
                                    _deep_fetch_limit(cfg, control_def)
                                )
                                st.pop(STATE_DEEP_SEARCH_COUNT, None)
                                st.pop(STATE_DEEP_FETCH_COUNT, None)
                                try:
                                    await self._run_deep_research(
                                        ctx, cfg, control_def
                                    )
                                    deep_out = st.get(
                                        "deep_research_output"
                                    )
                                    _sync_fetched_documents(
                                        doc_fetch_out,
                                        st.get(STATE_FETCHED_DOCUMENTS)
                                        or {},
                                    )
                                except Exception as exc:
                                    logger.exception(
                                        "required deep research failed for "
                                        "%s: %s",
                                        cid,
                                        exc,
                                    )
                                    err_detail = (
                                        "Required deep research failed: "
                                        f"{exc}"
                                    )
                                    deep_out = None

                        if deep_out is not None and not err_detail:
                            revised = deep_out.get("revised_assessment")
                            if not (
                                isinstance(revised, dict)
                                and revised.get("status")
                            ):
                                err_detail = (
                                    "Required deep research did not produce "
                                    "a revised assessment"
                                )
                            elif deep_out.get(
                                "human_adjudication_required"
                            ):
                                warning_detail = (
                                    "Independent challenge remains "
                                    "unresolved after one adjudication pass; "
                                    "human adjudication is required"
                                )
                            else:
                                try:
                                    await (
                                        self
                                        ._run_post_research_skeptic(
                                            ctx,
                                            cfg,
                                            control_def,
                                            deep_out,
                                        )
                                    )
                                    independent = (
                                        st.get(
                                            "post_research_skeptic_output"
                                        )
                                        or {}
                                    )
                                    challenge = _reconcile_skeptic(
                                        revised,
                                        independent,
                                    )
                                    deep_out[
                                        "post_research_skeptic"
                                    ] = challenge
                                    if challenge.get("verdict") != "confirm":
                                        logger.info(
                                            "revising %s once against the "
                                            "post-research challenge",
                                            cid,
                                        )
                                        deep_out[
                                            "revision_challenge"
                                        ] = challenge
                                        deep_out = (
                                            await self
                                            ._supplement_research_if_needed(
                                                ctx,
                                                cfg,
                                                control_def,
                                                deep_out,
                                                challenge,
                                            )
                                        )
                                        _sync_fetched_documents(
                                            doc_fetch_out,
                                            st.get(
                                                STATE_FETCHED_DOCUMENTS
                                            )
                                            or {},
                                        )
                                        revised = (
                                            await self
                                            ._run_research_revision(
                                                ctx,
                                                cfg,
                                                control_def,
                                                deep_out,
                                                challenge=challenge,
                                            )
                                        )
                                        deep_out[
                                            "revised_assessment"
                                        ] = revised
                                        await (
                                            self
                                            ._run_post_research_skeptic(
                                                ctx,
                                                cfg,
                                                control_def,
                                                deep_out,
                                            )
                                        )
                                        independent = (
                                            st.get(
                                                "post_research_skeptic_output"
                                            )
                                            or {}
                                        )
                                        challenge = _reconcile_skeptic(
                                            revised,
                                            independent,
                                        )
                                        deep_out[
                                            "post_research_skeptic"
                                        ] = challenge
                                        if (
                                            challenge.get("verdict")
                                            != "confirm"
                                        ):
                                            warning_detail = (
                                                "Post-research independent "
                                                "challenge remains "
                                                "unresolved after one "
                                                "adjudication pass; human "
                                                "adjudication is required"
                                            )
                                            deep_out[
                                                "human_adjudication_required"
                                            ] = True
                                except Exception as exc:
                                    logger.exception(
                                        "post-research challenge failed for "
                                        "%s: %s",
                                        cid,
                                        exc,
                                    )
                                    err_detail = (
                                        "Post-research challenge failed: "
                                        f"{exc}"
                                    )

            except Exception as e:
                logger.exception("control %s failed: %s", cid, e)
                item["status"] = "error"
                final_status = "error"
                err_detail = str(e)

            if retrieval_outcome in {"failed", "exhausted"}:
                assessor_out = _not_assessed_for_retrieval(
                    retrieval_outcome,
                    err_detail if retrieval_outcome == "failed" else None,
                )

            result_status, rec_score, rec_services, conf, final_assessor = (
                _final_recommendation(
                assessor_out, skeptic_out, deep_out
            )
            )
            if rec_score == "mixed" and rec_services is not None:
                rec_services = _normalize_service_evidence(
                    rec_services,
                    final_assessor,
                    cfg.provider.services_in_scope,
                )
                final_assessor = dict(final_assessor)
                final_assessor["services"] = rec_services

            control_entry = control_entry_for_provider_schema(
                result_status,
                rec_score,
                final_assessor,
                rec_services,
                conf,
                cfg.provider.services_in_scope,
            )
            scope_errs = _validate_service_scope_result(
                control_def,
                result_status,
                rec_score,
                rec_services,
                cfg.provider.services_in_scope,
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
                assessor=final_assessor,
                assessor_raw=assessor_out,
                skeptic=skeptic_out,
                consistency=consistency_out,
                deep_research=deep_out,
                recommended_score=rec_score,
                recommended_services=rec_services,
                result_status=result_status,
                overall_confidence=conf,
                status=assessment_status,
                prompt_hashes=hashes,
                input_hashes=input_hashes,
                retrieval_outcome=retrieval_outcome,
                error_detail=err_detail or warning_detail,
            )
            if (deep_out or {}).get("human_adjudication_required"):
                assessment["human_adjudication_required"] = True
            internal_errs = validate_internal_assessment(assessment)
            if internal_errs:
                logger.warning("internal validation: %s", internal_errs)
            release_errs = (
                []
                if err_detail
                else validate_release_ready_assessment(assessment)
            )
            validation_errs = [
                *scope_errs,
                *schema_errs,
                *(internal_errs if err_detail else release_errs),
            ]
            if validation_errs:
                logger.error(
                    "release validation failed for %s: %s",
                    cid,
                    validation_errs,
                )

            write_assessment_files(
                assessments_dir=cfg.paths.assessments_dir,
                reports_dir=cfg.paths.reports_dir,
                provider_slug=slug,
                control_id=cid,
                assessment=assessment,
                control_def=control_def,
                schema_errors=validation_errs,
            )
            if validation_errs:
                assessment["status"] = "validation_error"

            item["status"] = (
                "completed"
                if not err_detail and not validation_errs
                else "error"
            )
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
        _normalize_fetched_documents(output, fetched)
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
            f"{_service_scope_contract(cfg, control_def)}\n"
            f"DOCUMENTATION BUNDLE:\n```json\n"
            f"{json.dumps(docs, default=str)}```"
        )
        await _inject_user_and_drain(agent, ctx, user_text, cfg, "assessor")
        output = ctx.session.state.get("assessor_output")
        if not isinstance(output, dict) or not output.get("status"):
            raise RuntimeError(
                "assessor agent produced no structured output"
            )
        _normalize_service_array(output)
        ctx.session.state["assessor_output"] = output
        if isinstance(output, dict) and output.get("status") == "assessed":
            fetched = ctx.session.state.get(STATE_FETCHED_DOCUMENTS) or {}
            ctx.session.state["assessor_output"] = _enrich_claim_bundle(
                output,
                cfg,
                fetched,
            )

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
            f"{_service_scope_contract(cfg, control_def)}\n"
            f"DOCUMENTATION BUNDLE:\n```json\n"
            f"{json.dumps(docs, default=str)}```"
        )
        await _inject_user_and_drain(agent, ctx, user_text, cfg, "skeptic")
        independent = ctx.session.state.get("skeptic_output")
        if not isinstance(independent, dict) or not independent.get("status"):
            raise RuntimeError(
                "skeptic agent produced no structured output"
            )
        _normalize_service_array(independent)
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
        output = ctx.session.state.get("consistency_output")
        if (
            not isinstance(output, dict)
            or not output.get("overall_consistency")
        ):
            raise RuntimeError(
                "consistency agent produced no structured output"
            )

    async def _run_post_research_skeptic(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
        deep_output: dict[str, Any],
    ) -> None:
        """Challenge a revised result without exposing its recommendation."""
        skeptic_prompt = _load_prompt("skeptic.md")
        model = _metered_model(ctx, cfg, "skeptic")
        agent = LlmAgent(
            name="post_research_skeptic_agent",
            model=model,
            instruction=skeptic_prompt,
            output_schema=SkepticStructured,
            output_key="post_research_skeptic_output",
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        fetched = ctx.session.state.get(STATE_FETCHED_DOCUMENTS) or {}
        documents = [
            {
                "url": url,
                "title": value.get("title"),
                "content": value.get("content"),
            }
            for url, value in fetched.items()
            if isinstance(value, dict) and value.get("content")
        ]
        user_text = (
            f"CONTROL YAML:\n```yaml\n"
            f"{yaml.safe_dump(control_def, sort_keys=False)}```\n"
            f"{_service_scope_contract(cfg, control_def)}\n"
            "DOCUMENTATION BUNDLE INCLUDING DEEP-RESEARCH SOURCES:\n"
            f"```json\n{json.dumps(documents, default=str)}```\n"
            "QUESTIONS INVESTIGATED:\n"
            f"```json\n"
            f"{json.dumps(deep_output.get('questions_investigated') or [], default=str)}```\n"
            "UNRESOLVED ITEMS:\n"
            f"```json\n"
            f"{json.dumps(deep_output.get('unresolved') or [], default=str)}```"
        )
        ctx.session.state.pop("post_research_skeptic_output", None)
        await _inject_user_and_drain(
            agent,
            ctx,
            user_text,
            cfg,
            "post_research_skeptic",
        )
        output = ctx.session.state.get("post_research_skeptic_output")
        if not isinstance(output, dict) or not output.get("status"):
            raise RuntimeError(
                "post-research skeptic produced no structured output"
            )
        _normalize_service_array(output)
        ctx.session.state["post_research_skeptic_output"] = output

    async def _supplement_research_if_needed(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
        existing: dict[str, Any],
        challenge: dict[str, Any],
    ) -> dict[str, Any]:
        missing_questions = _missing_deep_questions(
            existing,
            challenge,
        )
        if not missing_questions:
            return existing

        st = ctx.session.state
        used = int(st.get("deep_research_invocations", 0))
        if used >= cfg.limits.max_deep_research_per_run:
            raise RuntimeError(
                "Research omitted required skeptic questions and the "
                "per-run deep-research limit was exhausted"
            )
        st["deep_research_invocations"] = used + 1
        st["pipeline_mode"] = "deep_research"
        st["deep_research_max_search"] = (
            cfg.limits.max_search_calls_per_deep_research
        )
        st["deep_research_max_fetch"] = _deep_fetch_limit(
            cfg,
            control_def,
        )
        st.pop(STATE_DEEP_SEARCH_COUNT, None)
        st.pop(STATE_DEEP_FETCH_COUNT, None)
        logger.info(
            "supplementing %s research for %d missing skeptic question(s)",
            control_def["id"],
            len(missing_questions),
        )
        supplement = await self._run_research_findings(
            ctx,
            cfg,
            control_def,
            skeptic_override={
                "deep_research_questions": missing_questions,
            },
        )
        merged = _merge_research_findings(existing, supplement)
        _validate_deep_question_coverage(merged, challenge)
        st["deep_research_output"] = merged
        return merged

    async def _run_deep_research(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
    ) -> None:
        output = await self._run_research_findings(
            ctx,
            cfg,
            control_def,
        )
        revised = await self._run_research_revision(
            ctx,
            cfg,
            control_def,
            output,
        )
        output["revised_assessment"] = revised
        ctx.session.state["deep_research_output"] = output

    async def _run_research_findings(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
        *,
        skeptic_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dr_prompt = _load_prompt("deep_research.md")
        model = _metered_model(ctx, cfg, "deep_research")
        agent = LlmAgent(
            name="deep_research_agent",
            model=model,
            instruction=dr_prompt,
            tools=doc_fetch_tools(),
            output_key="deep_research_raw",
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        user_text = (
            f"CONTROL YAML:\n```yaml\n"
            f"{yaml.safe_dump(control_def, sort_keys=False)}```\n"
            f"{_service_scope_contract(cfg, control_def)}\n"
            f"DOCUMENTATION BUNDLE:\n```json\n"
            f"{json.dumps(ctx.session.state.get('doc_fetch_output') or {}, default=str)}```\n"
            f"SKEPTIC QUESTIONS:\n```json\n"
            f"{json.dumps(skeptic_override or ctx.session.state.get('skeptic_output') or {}, default=str)}```\n"
            f"CONSISTENCY FLAGS:\n```json\n"
            f"{json.dumps(ctx.session.state.get('consistency_output') or {}, default=str)}```\n"
            "OUTPUT JSON SCHEMA:\n```json\n"
            f"{json.dumps(ResearchFindingsStructured.model_json_schema(), default=str)}```"
        )
        ctx.session.state.pop("deep_research_raw", None)
        await _inject_user_and_drain(
            agent,
            ctx,
            user_text,
            cfg,
            "deep_research",
        )
        raw_output = ctx.session.state.get("deep_research_raw")
        output = _parse_research_findings_output(raw_output)
        if not isinstance(output, dict):
            raise RuntimeError(
                "deep research agent produced no structured output"
            )
        _validate_deep_question_coverage(
            output,
            skeptic_override
            or ctx.session.state.get("skeptic_output")
            or {},
        )
        _fetch_missing_deep_sources(ctx, output)
        return output

    async def _run_research_revision(
        self,
        ctx: InvocationContext,
        cfg: AssessmentConfig,
        control_def: dict[str, Any],
        findings: dict[str, Any],
        *,
        challenge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = _load_prompt("research_revision.md")
        model = _metered_model(ctx, cfg, "research_revision")
        agent = LlmAgent(
            name="research_revision_agent",
            model=model,
            instruction=prompt,
            output_schema=AssessorStructured,
            output_key="deep_research_revision_output",
            include_contents="default",
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=cfg.limits.max_output_tokens_per_call
            ),
        )
        fetched = ctx.session.state.get(STATE_FETCHED_DOCUMENTS) or {}
        documents = [
            {
                "url": url,
                "title": value.get("title"),
                "content": value.get("content"),
            }
            for url, value in fetched.items()
            if isinstance(value, dict) and value.get("content")
        ]
        user_text = (
            f"CONTROL YAML:\n```yaml\n"
            f"{yaml.safe_dump(control_def, sort_keys=False)}```\n"
            f"{_service_scope_contract(cfg, control_def)}\n"
            "ORIGINAL ASSESSMENT:\n"
            f"```json\n{json.dumps(ctx.session.state.get('assessor_output') or {}, default=str)}```\n"
            "RESEARCH FINDINGS:\n"
            f"```json\n{json.dumps(findings, default=str)}```\n"
            "INDEPENDENT CHALLENGE TO RECONCILE:\n"
            f"```json\n{json.dumps(challenge or {}, default=str)}```\n"
            "FETCHED DOCUMENTATION:\n"
            f"```json\n{json.dumps(documents, default=str)}```"
        )
        correction = ""
        for attempt in range(cfg.limits.max_retries_per_call + 1):
            ctx.session.state.pop(
                "deep_research_revision_output",
                None,
            )
            await _inject_user_and_drain(
                agent,
                ctx,
                user_text + correction,
                cfg,
                "research_revision",
            )
            revised = ctx.session.state.get(
                "deep_research_revision_output"
            )
            if not isinstance(revised, dict) or not revised.get("status"):
                raise RuntimeError(
                    "research revision agent produced no structured output"
                )
            _normalize_service_array(revised)
            try:
                if revised.get("status") == "assessed":
                    revised = _enrich_claim_bundle(
                        revised,
                        cfg,
                        fetched,
                    )
            except RuntimeError as exc:
                if (
                    "evidence quote" not in str(exc)
                    or attempt >= cfg.limits.max_retries_per_call
                ):
                    raise
                logger.warning(
                    "research revision evidence retry %d after error: %s",
                    attempt + 1,
                    exc,
                )
                correction = (
                    "\nPREVIOUS REVISION REJECTED:\n"
                    f"{exc}\n"
                    "Return a complete corrected replacement. Copy every "
                    "evidence quote verbatim from the fetched document and "
                    "remove any claim that the fetched text does not support."
                )
                continue
            ctx.session.state["deep_research_revision_output"] = revised
            return revised
        raise RuntimeError("research revision retries exhausted")


def _service_scope_contract(
    cfg: AssessmentConfig,
    control_def: dict[str, Any],
) -> str:
    service_ids = [
        str(service["id"])
        for service in cfg.provider.services_in_scope
    ]
    if not control_def.get("service_scoped"):
        return (
            "SERVICE SCOPE CONTRACT:\n"
            "This is offering-scoped. Return an empty services array and "
            "never return a mixed score."
        )
    return (
        "SERVICE SCOPE CONTRACT:\n"
        "This control is service-scoped. If the top-level status is assessed, "
        'the top-level score MUST be "mixed" and the services array MUST '
        "contain exactly one item for every configured service id, even when "
        "every service has the same numeric score. Each service item needs "
        "service_id, status, "
        "score (0-3 when assessed, null otherwise), evidence, confidence, "
        "and exact fetched sources_used. Do not return an assessed flat "
        f"score. Required service ids:\n{json.dumps(service_ids)}"
    )


def _deep_fetch_limit(
    cfg: AssessmentConfig,
    control_def: dict[str, Any],
) -> int:
    configured = cfg.limits.max_fetch_calls_per_deep_research
    if not control_def.get("service_scoped"):
        return configured
    return min(
        cfg.limits.max_fetch_calls_per_run,
        max(configured, len(cfg.provider.services_in_scope)),
    )


def _normalize_service_array(payload: dict[str, Any]) -> None:
    """Convert constrained-output service arrays to canonical service maps."""
    services = payload.get("services")
    if not isinstance(services, list):
        return
    normalized: dict[str, dict[str, Any]] = {}
    for raw in services:
        if not isinstance(raw, dict):
            raise RuntimeError("service result must be an object")
        service = dict(raw)
        service_id = str(service.pop("service_id", "")).strip()
        if not service_id:
            raise RuntimeError("service result is missing service_id")
        if service_id in normalized:
            raise RuntimeError(f"duplicate service result: {service_id}")
        normalized[service_id] = service
    payload["services"] = normalized


def _fetch_missing_deep_sources(
    ctx: InvocationContext,
    output: dict[str, Any],
) -> None:
    """Fetch every URL cited by deep research before claim enrichment."""
    urls: list[str] = []
    for finding in output.get("questions_investigated") or []:
        if isinstance(finding, dict):
            urls.extend(str(url) for url in finding.get("sources") or [])
    revised = output.get("revised_assessment")
    if isinstance(revised, dict):
        urls.extend(str(url) for url in revised.get("sources_used") or [])
        licensing = revised.get("licensing")
        if isinstance(licensing, dict):
            urls.extend(
                str(url) for url in licensing.get("sources_used") or []
            )
        for service in (revised.get("services") or {}).values():
            if isinstance(service, dict):
                urls.extend(
                    str(url) for url in service.get("sources_used") or []
                )
        for item in revised.get("evidence_items") or []:
            if isinstance(item, dict) and item.get("url"):
                urls.append(str(item["url"]))

    fetched = ctx.session.state.get(STATE_FETCHED_DOCUMENTS) or {}
    tool_context = ToolContext(ctx)
    for url in dict.fromkeys(url for url in urls if url):
        _, existing = _match_fetched_document(url, fetched)
        if isinstance(existing, dict) and existing.get("content"):
            continue
        result = web_fetch(url, tool_context)
        fetched = ctx.session.state.get(STATE_FETCHED_DOCUMENTS) or {}
        if result.get("status") != "ok":
            logger.warning(
                "deep-research source fetch failed for %s: %s",
                url,
                result.get("error_detail"),
            )


def _validate_deep_question_coverage(
    output: dict[str, Any],
    skeptic: dict[str, Any],
) -> None:
    missing = _missing_deep_questions(output, skeptic)
    if missing:
        raise RuntimeError(
            "deep research omitted skeptic questions: "
            + " | ".join(_normalize_text(question) for question in missing)
        )


def _missing_deep_questions(
    output: dict[str, Any],
    skeptic: dict[str, Any],
) -> list[str]:
    expected = [
        str(question).strip()
        for question in skeptic.get("deep_research_questions") or []
        if str(question).strip()
    ]
    investigated = {
        _normalize_text(str(finding.get("question") or ""))
        for finding in output.get("questions_investigated") or []
        if isinstance(finding, dict)
    }
    return [
        question
        for question in expected
        if _normalize_text(question) not in investigated
    ]


def _merge_research_findings(
    existing: dict[str, Any],
    supplement: dict[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(existing)
    merged.pop("_needs_revision", None)
    merged.pop("revised_assessment", None)
    merged.pop("post_research_skeptic", None)

    findings: dict[str, dict[str, Any]] = {}
    for finding in [
        *(existing.get("questions_investigated") or []),
        *(supplement.get("questions_investigated") or []),
    ]:
        if not isinstance(finding, dict):
            continue
        question = _normalize_text(str(finding.get("question") or ""))
        if question:
            findings[question] = finding
    merged["questions_investigated"] = list(findings.values())

    unresolved: list[str] = []
    seen_unresolved: set[str] = set()
    for item in [
        *(existing.get("unresolved") or []),
        *(supplement.get("unresolved") or []),
    ]:
        text = str(item).strip()
        normalized = _normalize_text(text)
        if text and normalized not in seen_unresolved:
            seen_unresolved.add(normalized)
            unresolved.append(text)
    merged["unresolved"] = unresolved
    return merged


def _parse_research_findings_output(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        parsed = raw
    else:
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("deep research agent produced no final text")
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, count=1)
            text = re.sub(r"\s*```$", "", text, count=1)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(
                "deep research agent did not return a JSON object"
            )
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            try:
                parsed = repair_json(
                    text[start : end + 1],
                    return_objects=True,
                )
            except Exception as repair_exc:
                raise RuntimeError(
                    "deep research findings were invalid: "
                    f"{exc}; repair failed: {repair_exc}"
                ) from repair_exc
    try:
        return ResearchFindingsStructured.model_validate(parsed).model_dump(
            mode="json"
        )
    except ValueError as exc:
        raise RuntimeError(
            f"deep research findings were invalid: {exc}"
        ) from exc


def _parse_deep_research_output(
    raw: Any,
    base_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        parsed = raw
    else:
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("deep research agent produced no final text")
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, count=1)
            text = re.sub(r"\s*```$", "", text, count=1)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(
                "deep research agent did not return a JSON object"
            )
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            try:
                parsed = repair_json(
                    text[start : end + 1],
                    return_objects=True,
                )
            except Exception as repair_exc:
                raise RuntimeError(
                    "deep research structured output was invalid: "
                    f"{exc}; repair failed: {repair_exc}"
                ) from repair_exc
            if not isinstance(parsed, dict):
                raise RuntimeError(
                    "deep research JSON repair did not produce an object"
                ) from exc
    try:
        revised = (
            parsed.get("revised_assessment")
            if isinstance(parsed, dict)
            else None
        )
        if isinstance(revised, dict):
            _normalize_research_wrapper(revised)
            if not str(revised.get("evidence") or "").strip():
                revised["evidence"] = (
                    "See the per-service findings and source-backed claim "
                    "chain for the assessment rationale."
                )
            if (
                isinstance(base_assessment, dict)
                and _assessment_verdicts_match(revised, base_assessment)
            ):
                for key in (
                    "evidence_items",
                    "claims",
                    "criteria_results",
                    "sources_used",
                    "licensing",
                ):
                    if not revised.get(key) and base_assessment.get(key):
                        revised[key] = deepcopy(base_assessment[key])
            for item in revised.get("evidence_items") or []:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("quote"), str):
                    item["quote"] = item["quote"][:1200]
                if isinstance(item.get("title"), str):
                    item["title"] = item["title"][:160]
        validated = DeepResearchStructured.model_validate(parsed).model_dump(
            mode="json"
        )
        revised_output = validated.get("revised_assessment")
        if isinstance(revised_output, dict):
            _normalize_service_array(revised_output)
        return validated
    except ValueError as exc:
        raise RuntimeError(
            f"deep research structured output was invalid: {exc}"
        ) from exc


def _normalize_research_wrapper(revised: dict[str, Any]) -> None:
    if revised.get("status") == "mixed":
        revised["status"] = "assessed"
        if revised.get("score") is None:
            revised["score"] = "mixed"


def _assessment_verdicts_match(
    candidate: dict[str, Any],
    base: dict[str, Any],
) -> bool:
    if (
        candidate.get("status") != base.get("status")
        or candidate.get("score") != base.get("score")
    ):
        return False

    def verdicts(services: Any) -> dict[str, tuple[Any, Any]]:
        if isinstance(services, dict):
            return {
                str(service_id): (
                    value.get("status"),
                    value.get("score"),
                )
                for service_id, value in services.items()
                if isinstance(value, dict)
            }
        if isinstance(services, list):
            return {
                str(value.get("service_id")): (
                    value.get("status"),
                    value.get("score"),
                )
                for value in services
                if isinstance(value, dict) and value.get("service_id")
            }
        return {}

    return verdicts(candidate.get("services")) == verdicts(
        base.get("services")
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
) -> MeteredGemini:
    return MeteredGemini(
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
        f"EVIDENCE_SEEDS: "
        f"{json.dumps(cfg.evidence_seeds.get(str(control_def.get('id')), []))}\n"
        f"DESCRIPTION:\n{control_def.get('description','')}\n"
    )


def _load_stage_checkpoint(
    cfg: AssessmentConfig,
    control_id: str,
    hashes: dict[str, str],
    input_hashes: dict[str, str] | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
] | None:
    """Recover completed core stages from a matching failed artifact."""
    if not cfg.session.resume_artifacts:
        return None
    path = (
        cfg.paths.assessments_dir
        / cfg.provider.slug
        / f"{control_id}.yaml"
    )
    if not path.is_file():
        return None
    try:
        artifact = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if (
        not isinstance(artifact, dict)
        or artifact.get("status") not in {"error", "validation_error"}
    ):
        return None
    if artifact.get("assessment_id") != cfg.provider.assessment_id:
        return None
    saved_hashes = artifact.get("prompt_hashes") or {}
    if any(
        not hashes.get(stage)
        or saved_hashes.get(stage) != hashes.get(stage)
        for stage in ("doc_fetch", "assessor")
    ):
        return None
    peer_context_current = True
    if input_hashes is not None:
        saved_inputs = artifact.get("input_hashes") or {}
        if any(
            saved_inputs.get(name) != digest
            for name, digest in input_hashes.items()
            if name != "peer_context"
        ):
            return None
        peer_context_current = (
            saved_inputs.get("peer_context")
            == input_hashes.get("peer_context")
        )
    docs = artifact.get("doc_fetch")
    assessor = artifact.get("assessor_raw") or artifact.get("assessor")
    if not isinstance(docs, dict) or not isinstance(assessor, dict):
        return None
    if not docs.get("docs_fetched") or not assessor.get("status"):
        return None
    if assessor.get("status") == "assessed" and (
        not assessor.get("evidence_items")
        or not assessor.get("claims")
        or {
            value.get("level")
            for value in assessor.get("criteria_results") or []
        }
        != {0, 1, 2, 3}
    ):
        return None
    result_status = str(assessor.get("status") or "unknown")
    score = assessor.get("score")
    services = assessor.get("services") if score == "mixed" else None
    probe = {
        "control": control_id,
        "provider": cfg.provider.slug,
        "status": "needs_human_review",
        "result_status": result_status,
        "recommended_score": score,
        "recommended_services": services,
        "doc_fetch": docs,
        "assessor": assessor,
    }
    if validate_release_ready_assessment(probe):
        return None
    entry = control_entry_for_provider_schema(
        result_status,
        score,
        assessor,
        services,
        str(assessor.get("confidence") or "low"),
        cfg.provider.services_in_scope,
    )
    if validate_control_against_provider_schema(
        cfg.paths.repo_root,
        cfg.provider.slug,
        cfg.provider.display_name,
        cfg.provider.assessment_id,
        cfg.provider.offering,
        cfg.provider.services_in_scope,
        control_id,
        entry,
    ):
        return None
    skeptic: dict[str, Any] | None = None
    stored_skeptic = artifact.get("skeptic")
    if (
        hashes.get("skeptic")
        and saved_hashes.get("skeptic") == hashes.get("skeptic")
        and isinstance(stored_skeptic, dict)
        and stored_skeptic.get("status")
        and stored_skeptic.get("verdict")
    ):
        skeptic = stored_skeptic

    consistency: dict[str, Any] | None = None
    stored_consistency = artifact.get("consistency")
    if (
        hashes.get("consistency")
        and saved_hashes.get("consistency") == hashes.get("consistency")
        and peer_context_current
        and isinstance(stored_consistency, dict)
        and stored_consistency.get("overall_consistency")
    ):
        consistency = stored_consistency

    deep: dict[str, Any] | None = None
    stored_deep = artifact.get("deep_research")
    pending_challenge: dict[str, Any] | None = None
    if isinstance(stored_deep, dict):
        stored_post_research = stored_deep.get(
            "post_research_skeptic"
        )
        if (
            isinstance(stored_post_research, dict)
            and stored_post_research.get("verdict") != "confirm"
        ):
            pending_challenge = stored_post_research
    terminal_adjudication = (
        pending_challenge is not None
        and isinstance(stored_deep, dict)
        and isinstance(stored_deep.get("revision_challenge"), dict)
    )
    if (
        (pending_challenge is None or terminal_adjudication)
        and all(
            hashes.get(stage)
            and saved_hashes.get(stage) == hashes.get(stage)
            for stage in (
                "skeptic",
                "consistency",
                "deep_research",
                "research_revision",
            )
        )
        and peer_context_current
        and skeptic is not None
        and isinstance(stored_deep, dict)
        and isinstance(stored_deep.get("revised_assessment"), dict)
    ):
        revised = stored_deep["revised_assessment"]
        revised_status = str(revised.get("status") or "unknown")
        revised_score = revised.get("score")
        revised_services = (
            revised.get("services")
            if revised_score == "mixed"
            else None
        )
        revised_probe = {
            "control": control_id,
            "provider": cfg.provider.slug,
            "status": "needs_human_review",
            "result_status": revised_status,
            "recommended_score": revised_score,
            "recommended_services": revised_services,
            "doc_fetch": docs,
            "assessor": revised,
        }
        revised_entry = control_entry_for_provider_schema(
            revised_status,
            revised_score,
            revised,
            revised_services,
            str(revised.get("confidence") or "low"),
            cfg.provider.services_in_scope,
        )
        if (
            not validate_release_ready_assessment(revised_probe)
            and not validate_control_against_provider_schema(
                cfg.paths.repo_root,
                cfg.provider.slug,
                cfg.provider.display_name,
                cfg.provider.assessment_id,
                cfg.provider.offering,
                cfg.provider.services_in_scope,
                control_id,
                revised_entry,
            )
        ):
            deep = stored_deep
            if terminal_adjudication:
                deep = {
                    **deep,
                    "human_adjudication_required": True,
                }
    if (
        deep is None
        and hashes.get("deep_research")
        and saved_hashes.get("deep_research")
        == hashes.get("deep_research")
        and peer_context_current
        and isinstance(stored_deep, dict)
    ):
        findings_payload = {
            "questions_investigated": (
                stored_deep.get("questions_investigated") or []
            ),
            "unresolved": stored_deep.get("unresolved") or [],
        }
        try:
            findings = ResearchFindingsStructured.model_validate(
                findings_payload
            ).model_dump(mode="json")
        except ValueError:
            findings = None
        if findings is not None:
            deep = {**findings, "_needs_revision": True}
            if pending_challenge is not None:
                deep["_pending_challenge"] = pending_challenge
    return docs, assessor, skeptic, consistency, deep


def _enrich_claim_bundle(
    assessor: dict[str, Any],
    cfg: AssessmentConfig,
    fetched_documents: dict[str, Any],
) -> dict[str, Any]:
    """Attach immutable source metadata and source-backed quotes."""
    enriched = dict(assessor)
    now = datetime.now(timezone.utc).isoformat()
    items = []
    for raw in enriched.get("evidence_items") or []:
        item = dict(raw)
        url = str(item.get("url") or "")
        matched_url, fetched = _match_fetched_document(
            url,
            fetched_documents,
        )
        if not isinstance(fetched, dict) or not fetched.get("content"):
            raise RuntimeError(
                f"claim evidence URL was not fetched: {url or '<missing>'}"
            )
        item["url"] = matched_url
        content = str(fetched["content"])
        quote = str(item.get("quote") or "").strip()
        if not quote:
            raise RuntimeError(
                "evidence item has no verbatim quote: "
                f"{item.get('id') or '<missing id>'}"
            )
        if _normalize_text(quote) not in _normalize_text(content):
            raise RuntimeError(
                "evidence quote is not present in fetched source: "
                f"{item.get('id') or '<missing id>'} ({matched_url})"
            )
        quote = quote[:1200]
        item["title"] = str(
            item.get("title") or fetched.get("title") or url
        )
        item["quote"] = quote
        item["retrieved_at"] = now
        item["content_hash"] = (
            "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        item["applicability"] = {
            "offering": str(cfg.provider.offering["id"]),
            "regions": list(cfg.provider.offering["regions"]),
            "services": list(item.pop("services", []) or []),
            "edition": str(cfg.provider.offering["edition"]),
        }
        items.append(item)
    enriched["evidence_items"] = items
    return enriched


def _match_fetched_document(
    url: str,
    fetched_documents: dict[str, Any],
) -> tuple[str, Any]:
    fetched = fetched_documents.get(url)
    if isinstance(fetched, dict):
        return url, fetched
    canonical = _canonical_evidence_url(url)
    matches = [
        (str(candidate), value)
        for candidate, value in fetched_documents.items()
        if (
            _canonical_evidence_url(str(candidate)) == canonical
            or (
                isinstance(value, dict)
                and value.get("final_url")
                and _canonical_evidence_url(str(value["final_url"]))
                == canonical
            )
        )
    ]
    if len(matches) == 1:
        return matches[0]
    return url, None


def _canonical_evidence_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    host = parts.netloc.casefold()
    if host == "learn.microsoft.com":
        path = re.sub(r"^/[a-z]{2}-[a-z]{2}(?=/)", "", path)
    if host in {"cloud.google.com", "docs.cloud.google.com"}:
        host = "docs.cloud.google.com"
    return urlunsplit(
        (
            parts.scheme.casefold(),
            host,
            path,
            "",
            "",
        )
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


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
    if skeptic and skeptic.get("verdict") == "downgrade":
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
        if (deep or {}).get("human_adjudication_required"):
            status = str(revised.get("status") or "unknown")
            score = revised.get("score")
            services = (
                revised.get("services")
                if score == "mixed"
                else None
            )
            return status, score, services, "low", revised
        post_research = (deep or {}).get("post_research_skeptic")
        if not isinstance(post_research, dict):
            return _conflicting_recommendation(
                revised,
                "Deep-research revision has no independent challenge.",
            )
        return _apply_skeptic_recommendation(
            revised,
            post_research,
            str(revised.get("confidence") or conf),
        )

    if skeptic:
        return _apply_skeptic_recommendation(assessor, skeptic, conf)

    status = str(assessor.get("status") or "unknown")
    score = assessor.get("score")
    services = assessor.get("services") if score == "mixed" else None
    return status, score, services, conf, assessor


def _apply_skeptic_recommendation(
    assessor: dict[str, Any],
    skeptic: dict[str, Any],
    confidence: str,
) -> tuple[str, Any, dict[str, Any] | None, str, dict[str, Any]]:
    verdict = skeptic.get("verdict")
    if verdict == "flag_deep_research":
        return _conflicting_recommendation(
            assessor,
            str(skeptic.get("reasoning") or "Independent challenge unresolved."),
        )
    if verdict == "downgrade":
        return _conflicting_recommendation(
            assessor,
            str(
                skeptic.get("reasoning")
                or "Independent challenger recommended a lower result "
                "without a revised claim bundle."
            ),
        )

    status = str(assessor.get("status") or "unknown")
    score = assessor.get("score")
    services = assessor.get("services") if score == "mixed" else None
    return status, score, services, confidence, assessor


def _conflicting_recommendation(
    assessor: dict[str, Any],
    reason: str,
) -> tuple[str, None, None, str, dict[str, Any]]:
    final = dict(assessor)
    final["status"] = "conflicting"
    final["score"] = None
    final.pop("services", None)
    final["confidence"] = "low"
    final["evidence"] = (
        str(assessor.get("evidence") or "").strip()
        + "\n\nUnresolved independent challenge: "
        + reason.strip()
    ).strip()
    return "conflicting", None, None, "low", final


def _retrieval_outcome(doc_fetch: dict[str, Any]) -> str:
    if doc_fetch.get("doc_quality") == "error":
        return "failed"
    docs = list(doc_fetch.get("docs_fetched") or [])
    if not docs:
        return "exhausted"
    if doc_fetch.get("services_without_docs"):
        return "partial"
    return "succeeded"


def _validate_service_scope_result(
    control_def: dict[str, Any],
    result_status: str,
    score: Any,
    services: dict[str, Any] | None,
    services_in_scope: list[dict[str, Any]],
) -> list[str]:
    is_service_scoped = bool(control_def.get("service_scoped"))
    if result_status != "assessed":
        return []
    if is_service_scoped and score != "mixed":
        return [
            "service-scoped assessed result must use a per-service mixed score"
        ]
    if not is_service_scoped and score == "mixed":
        return ["offering-scoped control must not use a mixed score"]
    if not is_service_scoped:
        return []
    expected = {
        str(service.get("id"))
        for service in services_in_scope
        if service.get("id")
    }
    actual = set(services or {})
    errors = []
    if missing := sorted(expected - actual):
        errors.append(
            "service-scoped result is missing services: "
            + ", ".join(missing)
        )
    if extra := sorted(actual - expected):
        errors.append(
            "service-scoped result has out-of-scope services: "
            + ", ".join(extra)
        )
    return errors


def _not_assessed_for_retrieval(
    outcome: str,
    detail: str | None = None,
) -> dict[str, Any]:
    evidence = (
        "The evidence-retrieval stage failed; this control was not assessed."
        if outcome == "failed"
        else "The evidence-retrieval stage returned no documents; this "
        "control was not assessed."
    )
    if detail:
        evidence += f" Retrieval error: {detail}"
    return {
        "status": "not_assessed",
        "score": None,
        "services": {},
        "evidence": evidence,
        "licensing": {
            "status": "unknown",
            "evidence": "Commercial availability was not assessed.",
            "sources_used": [],
        },
        "confidence": "low",
        "flags": [f"retrieval-{outcome}"],
        "deep_research_recommended": False,
        "sources_used": [],
        "evidence_items": [],
        "claims": [],
        "criteria_results": [],
    }


def _normalize_fetched_documents(
    output: dict[str, Any],
    fetched: dict[str, Any],
) -> None:
    """Keep only fetched URLs and repair model-appended URL garbage."""
    clean_documents: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for document in output.get("docs_fetched") or []:
        url = str(document.get("url") or "")
        cached = fetched.get(url)
        if not isinstance(cached, dict):
            matches = [
                candidate
                for candidate in fetched
                if url.startswith(str(candidate))
            ]
            if matches:
                url = str(max(matches, key=lambda value: len(str(value))))
                cached = fetched.get(url)
        if not isinstance(cached, dict) or url in seen_urls:
            continue
        seen_urls.add(url)
        document["url"] = url
        document["content"] = cached.get("content", "")
        document["title"] = (
            document.get("title")
            or cached.get("title")
            or ""
        )
        clean_documents.append(document)
    output["docs_fetched"] = clean_documents


def _fetched_documents_from_bundle(
    doc_fetch: dict[str, Any],
) -> dict[str, dict[str, str]]:
    return {
        str(document["url"]): {
            "title": str(document.get("title") or document["url"]),
            "content": str(document["content"]),
        }
        for document in doc_fetch.get("docs_fetched") or []
        if isinstance(document, dict)
        and document.get("url")
        and document.get("content")
    }


def _sync_fetched_documents(
    doc_fetch: dict[str, Any],
    fetched: dict[str, Any],
) -> None:
    """Persist all deep-research documents needed for retry enrichment."""
    documents = list(doc_fetch.get("docs_fetched") or [])
    by_url = {
        str(document.get("url")): document
        for document in documents
        if isinstance(document, dict) and document.get("url")
    }
    for url, raw in fetched.items():
        if not isinstance(raw, dict) or not raw.get("content"):
            continue
        document = {
            "url": str(url),
            "title": str(raw.get("title") or url),
            "relevance": str(raw.get("relevance") or "high"),
            "content": str(raw["content"]),
        }
        if str(url) in by_url:
            by_url[str(url)].update(document)
        else:
            documents.append(document)
            by_url[str(url)] = document
    doc_fetch["docs_fetched"] = documents


def _normalize_service_evidence(
    services: dict[str, Any],
    assessment: dict[str, Any],
    services_in_scope: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Do not publish inferred service scores without a cited source."""
    if services_in_scope:
        services = canonicalize_service_map(services, services_in_scope)
    evidence_urls = {
        str(item.get("id")): str(item.get("url"))
        for item in assessment.get("evidence_items") or []
        if isinstance(item, dict) and item.get("id") and item.get("url")
    }
    urls_by_service: dict[str, list[str]] = {}
    for claim in assessment.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        urls = [
            evidence_urls[str(evidence_id)]
            for evidence_id in claim.get("evidence_item_ids") or []
            if str(evidence_id) in evidence_urls
        ]
        for service_id in claim.get("services") or []:
            canonical_id = (
                canonical_service_id(str(service_id), services_in_scope)
                if services_in_scope
                else str(service_id)
            )
            bucket = urls_by_service.setdefault(canonical_id, [])
            bucket.extend(url for url in urls if url not in bucket)

    normalized: dict[str, Any] = {}
    for service_id, raw in services.items():
        value = dict(raw) if isinstance(raw, dict) else {
            "status": "assessed",
            "score": raw,
        }
        if value.get("status") == "assessed":
            sources = list(value.get("sources_used") or [])
            if not sources:
                sources = urls_by_service.get(str(service_id), [])
            if sources:
                value["sources_used"] = sources
            else:
                value["status"] = "unknown"
                value.pop("score", None)
                value["evidence"] = (
                    str(value.get("evidence") or "").strip()
                    + " No service-specific authoritative source was captured."
                ).strip()
                value["confidence"] = "low"
        normalized[str(service_id)] = value
    return normalized


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

    has_critical_flag = any(
        isinstance(flag, dict) and flag.get("severity") == "critical"
        for flag in independent.get("skepticism_flags") or []
    )
    has_open_questions = any(
        str(question).strip()
        for question in independent.get("deep_research_questions") or []
    )
    if has_critical_flag or has_open_questions:
        out["verdict"] = "flag_deep_research"
        return out

    assessor_value = _comparable_score(assessor)
    skeptic_value = _comparable_score(independent)
    mixed_disagreement = (
        assessor_status == "assessed"
        and (
            assessor.get("score") == "mixed"
            or independent.get("score") == "mixed"
        )
        and _service_verdicts(assessor) != _service_verdicts(independent)
    )
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
    elif assessor_value != skeptic_value or mixed_disagreement:
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


def _service_verdicts(
    result: dict[str, Any],
) -> dict[str, tuple[Any, Any]]:
    return {
        str(service_id): (
            value.get("status"),
            value.get("score"),
        )
        for service_id, value in (result.get("services") or {}).items()
        if isinstance(value, dict)
    }


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
        "licensing": {
            "status": "unknown",
            "evidence": "Commercial availability was not assessed.",
            "sources_used": [],
        },
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
