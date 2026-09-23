from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from google.adk.models.lite_llm import LiteLlm
from pydantic import ValidationError

from tenant_sec_agentic.artifacts import (
    control_entry_for_provider_schema,
    validate_control_against_provider_schema,
)
from tenant_sec_agentic.config import (
    AssessmentConfig,
    LimitsConfig,
    PathsConfig,
    ProviderConfig,
    SessionConfig,
    config_from_state,
    config_to_serializable,
    load_assessment_config,
)
from tenant_sec_agentic.pipeline import (
    _drain_agent,
    _enrich_claim_bundle,
    _final_recommendation,
    _load_stage_checkpoint,
    _mock_assessor,
    prompt_hashes,
)
from tenant_sec_agentic.aggregate_cli import approved_entry
from tenant_sec_agentic.batch_cli import BudgetTracker, artifact_is_current
from tenant_sec_agentic.review_apply_cli import apply_manifest
from tenant_sec_agentic.review_bundle_cli import render_bundle
from tenant_sec_agentic.release_check_cli import profile_completeness_errors
from tenant_sec_agentic.metered_model import MeteredLiteLlm
from tenant_sec_agentic.schemas import (
    AssessorStructured,
    ConsistencyFlag,
    SkepticStructured,
)
from tenant_sec_agentic.usage import (
    BudgetExceeded,
    finalize_model_call,
    new_ledger,
    reserve_model_call,
    write_ledger,
)

ROOT = Path(__file__).parent.parent
CANONICAL = ROOT.parent / "tenant-sec"


def _claims() -> dict:
    return {
        "evidence_items": [
            {
                "id": "ev-official-doc",
                "url": "https://example.com/docs/cmk",
                "title": "CMK documentation",
                "source_class": "technical",
                "retrieved_at": "2026-09-23T00:00:00+00:00",
                "content_hash": "sha256:" + "a" * 64,
                "quote": "API accepts a customer key identifier.",
                "applicability": {
                    "offering": "public",
                    "regions": ["fr-par"],
                    "services": ["object-storage"],
                    "edition": "standard",
                },
            }
        ],
        "claims": [
            {
                "id": "cl-cmk-supported",
                "assertion": "The API accepts a customer key identifier.",
                "result": "supported",
                "evidence_item_ids": ["ev-official-doc"],
                "services": ["object-storage"],
            }
        ],
        "criteria_results": [
            {
                "level": level,
                "met": level == 2,
                "reasoning": f"L{level} evaluated against the claim.",
                "claim_ids": ["cl-cmk-supported"],
            }
            for level in range(4)
        ],
    }


def _config(tmp_path: Path) -> AssessmentConfig:
    return AssessmentConfig(
        provider=ProviderConfig(
            slug="scaleway",
            display_name="Scaleway",
            docs_base_url="https://example.com/docs",
            assessment_id="scaleway/public/fr-par/standard",
            offering={
                "id": "public",
                "name": "Public",
                "partition": "commercial",
                "regions": ["fr-par"],
                "edition": "standard",
                "cohort": "eu-public",
            },
            services_in_scope=[
                {
                    "id": "object-storage",
                    "name": "Object Storage",
                    "category": "storage.object",
                    "regions": ["fr-par"],
                    "edition": "standard",
                }
            ],
        ),
        controls_scope="all",
        models={},
        limits=LimitsConfig(),
        paths=PathsConfig(CANONICAL, tmp_path / "a", tmp_path / "r"),
        session=SessionConfig(tmp_path / "session.db", False),
        mock_mode=True,
    )


def test_example_uses_vertex_hybrid_models():
    config = load_assessment_config(ROOT / "assessment-config.example.yaml")
    assert config.model_for("doc_fetch").endswith("gemini-3.5-flash")
    assert config.model_for("assessor").endswith("gemini-3.5-flash")
    assert config.model_for("consistency").endswith("gemini-3.5-flash")
    assert config.limits.max_parallel_assessments == 1
    assert config.vertex_traffic_class == "priority"
    restored = config_from_state(config_to_serializable(config))
    assert restored.vertex_traffic_class == "priority"
    assert (
        restored.limits.model_timeout_seconds
        == config.limits.model_timeout_seconds
    )
    assert (
        restored.limits.max_model_turns_per_agent
        == config.limits.max_model_turns_per_agent
    )
    assert restored.limits.max_search_calls_per_doc_fetch == 2
    assert restored.limits.max_fetch_calls_per_doc_fetch == 3
    assert restored.session.resume_artifacts is True


def test_unknown_prohibits_score_and_mock_defaults_unknown(tmp_path):
    with pytest.raises(ValidationError):
        AssessorStructured(status="unknown", score=3, evidence="thin")
    config = _config(tmp_path)
    result = _mock_assessor({"service_scoped": True}, config)
    assert result["status"] == "unknown"
    assert result["score"] is None
    status, score, _, _, _ = _final_recommendation(result, None, None)
    assert status == "unknown"
    assert score is None

    with pytest.raises(ValidationError):
        SkepticStructured(status="assessed", reasoning="incomplete")


def test_consistency_flags_accept_level_labels():
    flag = ConsistencyFlag(current_score="L3", reference_score="L2")
    assert flag.current_score == "L3"


def test_canonical_entry_retains_sources_and_validates(tmp_path):
    config = _config(tmp_path)
    assessor = {
        "status": "assessed",
        "score": 2,
        "evidence": "API accepts a customer key identifier.",
        "confidence": "high",
        "sources_used": ["https://example.com/docs/cmk"],
        **_claims(),
    }
    entry = control_entry_for_provider_schema(
        "assessed", 2, assessor, None, "high"
    )
    assert entry["references"][0]["url"] == "https://example.com/docs/cmk"
    errors = validate_control_against_provider_schema(
        CANONICAL,
        config.provider.slug,
        config.provider.display_name,
        config.provider.assessment_id,
        config.provider.offering,
        config.provider.services_in_scope,
        "enc.cmk",
        entry,
    )
    assert errors == []


def test_usage_ledger_is_per_role_control_and_fails_closed(tmp_path):
    config = _config(tmp_path)
    config.mock_mode = False
    config.limits.max_model_calls_per_run = 1
    state = {"usage_ledger": new_ledger(config)}
    reservation = reserve_model_call(
        state,
        config,
        role="assessor",
        control_id="enc.cmk",
        model=config.model_for("assessor"),
        request_text="evidence",
    )
    finalize_model_call(state, config, reservation, (100, 20))
    ledger = state["usage_ledger"]
    assert ledger["by_role"]["assessor"]["model_calls"] == 1
    assert ledger["by_control"]["enc.cmk"]["actual_cost_usd"] > 0
    with pytest.raises(BudgetExceeded, match="model calls"):
        reserve_model_call(
            state,
            config,
            role="skeptic",
            control_id="enc.cmk",
            model=config.model_for("skeptic"),
            request_text="again",
        )


def test_metered_model_counts_each_adk_model_turn(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.mock_mode = False
    config.limits.max_model_calls_per_run = 2
    config.limits.max_model_turns_per_agent = 1
    config.vertex_traffic_class = "priority"
    state = {"usage_ledger": new_ledger(config)}

    async def fake_generate(self, llm_request, stream=False):
        yield SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=4,
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_generate)
    model = MeteredLiteLlm(
        model=config.model_for("assessor"),
        state=state,
        cfg=config,
        role="assessor",
        control_id="iam.mfa-enforcement",
    )
    assert model._additional_args["num_retries"] == 0
    assert (
        model._additional_args["headers"][
            "X-Vertex-AI-LLM-Shared-Request-Type"
        ]
        == "priority"
    )

    async def run_once():
        return [
            response
            async for response in model.generate_content_async(
                SimpleNamespace()
            )
        ]

    import asyncio

    assert len(asyncio.run(run_once())) == 1
    assert state["usage_ledger"]["totals"]["model_calls"] == 1
    assert state["usage_ledger"]["totals"]["input_tokens"] == 10
    assert state["usage_ledger"]["totals"]["output_tokens"] == 4
    with pytest.raises(BudgetExceeded, match="agent model-turn"):
        asyncio.run(run_once())


def test_direct_child_agent_events_are_persisted():
    marker = object()

    class FakeAgent:
        async def run_async(self, ctx):
            yield marker

    class FakeSessionService:
        async def append_event(self, session, event):
            session.events.append(event)

    context = SimpleNamespace(
        session=SimpleNamespace(events=[]),
        session_service=FakeSessionService(),
    )

    import asyncio

    asyncio.run(_drain_agent(FakeAgent(), context))
    assert context.session.events == [marker]


def test_failed_artifact_resumes_completed_core_stages(tmp_path):
    config = _config(tmp_path)
    config.session.resume_artifacts = True
    artifact_dir = config.paths.assessments_dir / config.provider.slug
    artifact_dir.mkdir(parents=True)
    artifact = {
        "status": "error",
        "assessment_id": config.provider.assessment_id,
        "prompt_hashes": {"doc_fetch": "docs", "assessor": "assessor"},
        "doc_fetch": {
            "docs_fetched": [{"url": "https://example.com/docs"}],
            "doc_quality": "adequate",
        },
        "assessor": {
            "status": "assessed",
            "score": 2,
            "evidence": "Supported by official documentation.",
            **_claims(),
        },
    }
    (artifact_dir / "enc.cmk.yaml").write_text(
        yaml.safe_dump(artifact),
        encoding="utf-8",
    )
    checkpoint = _load_stage_checkpoint(
        config,
        "enc.cmk",
        {"doc_fetch": "docs", "assessor": "assessor"},
    )
    assert checkpoint is not None
    assert checkpoint[1]["score"] == 2


def test_claim_bundle_enrichment_hashes_fetched_source(tmp_path):
    config = _config(tmp_path)
    assessor = {
        "status": "assessed",
        "score": 2,
        "evidence": "Supported.",
        **_claims(),
    }
    raw = assessor["evidence_items"][0]
    raw.pop("retrieved_at")
    raw.pop("content_hash")
    raw.pop("applicability")
    raw["services"] = ["object-storage"]
    enriched = _enrich_claim_bundle(
        assessor,
        config,
        {
            "https://example.com/docs/cmk": {
                "content": "API accepts a customer key identifier."
            }
        },
    )
    item = enriched["evidence_items"][0]
    assert item["content_hash"].startswith("sha256:")
    assert item["applicability"]["offering"] == "public"


def test_batch_runner_recognizes_current_review_artifact(tmp_path):
    config = _config(tmp_path)
    artifact_dir = config.paths.assessments_dir / config.provider.slug
    artifact_dir.mkdir(parents=True)
    artifact = {
        "control": "enc.cmk",
        "provider": config.provider.slug,
        "assessment_id": config.provider.assessment_id,
        "methodology_version": "2.0",
        "prompt_hashes": prompt_hashes(),
        "result_status": "assessed",
        "recommended_score": 2,
        "status": "needs_human_review",
        "assessor": _claims(),
    }
    (artifact_dir / "enc.cmk.yaml").write_text(
        yaml.safe_dump(artifact),
        encoding="utf-8",
    )
    assert artifact_is_current(config, "enc.cmk") is True


def test_batch_budget_reservations_fail_closed(tmp_path):
    tracker = BudgetTracker(tmp_path / "release-ledger.json", 5.0)

    async def exercise():
        first = await tracker.reserve(
            provider="scaleway",
            control="enc.cmk",
            attempt=1,
            amount_usd=4.0,
        )
        assert first is not None
        import asyncio

        waiting = asyncio.create_task(
            tracker.reserve(
                provider="aws",
                control="enc.cmk",
                attempt=1,
                amount_usd=4.0,
            )
        )
        await asyncio.sleep(0)
        assert waiting.done() is False
        await tracker.finalize(
            first,
            status="completed",
            actual_model_cost_usd=0.5,
            tavily_max_cost_usd=0.02,
            exit_code=0,
            artifact_current=True,
            log_path=tmp_path / "run.log",
        )
        second = await waiting
        assert second is not None
        await tracker.finalize(
            second,
            status="completed",
            actual_model_cost_usd=4.4,
            tavily_max_cost_usd=0.08,
            exit_code=0,
            artifact_current=True,
            log_path=tmp_path / "run-2.log",
        )
        blocked = await tracker.reserve(
            provider="gcp",
            control="enc.cmk",
            attempt=1,
            amount_usd=0.01,
        )
        assert blocked is None

    import asyncio

    asyncio.run(exercise())
    assert tracker.charged_usd == pytest.approx(5.0)


def test_single_control_usage_gets_concurrency_safe_ledger(tmp_path):
    config = _config(tmp_path)
    state = {"usage_ledger": new_ledger(config)}
    state["usage_ledger"]["by_control"]["enc.cmk"] = {}
    write_ledger(state, config)
    path = (
        config.paths.assessments_dir
        / config.provider.slug
        / "usage-ledgers"
        / "enc.cmk.json"
    )
    assert path.is_file()


def test_review_bundle_surfaces_claims_and_skeptic_flags():
    artifact = {
        "control": "enc.cmk",
        "recommended_score": 2,
        "result_status": "assessed",
        "overall_confidence": "medium",
        "assessor": {
            "evidence": "CMK support is documented.",
            **_claims(),
        },
        "skeptic": {
            "reasoning": "Coverage may vary.",
            "skepticism_flags": [
                {
                    "type": "scope",
                    "severity": "warning",
                    "detail": "Check all services.",
                }
            ],
        },
        "consistency": {
            "overall_consistency": "minor_tension",
            "consistency_flags": [],
        },
    }
    bundle = render_bundle("example", [artifact])
    assert "enc.cmk" in bundle
    assert "The API accepts a customer key identifier." in bundle
    assert "Check all services." in bundle


def test_review_manifest_applies_explicit_decision(tmp_path):
    artifact_path = tmp_path / "enc.cmk.yaml"
    artifact_path.write_text(
        yaml.safe_dump(
            {
                "control": "enc.cmk",
                "review": {"status": "pending"},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "review.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "assessments": [
                    {
                        "control": "enc.cmk",
                        "artifact_path": str(artifact_path),
                        "decision": "approved",
                        "notes": "Evidence checked.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert apply_manifest(manifest_path, "human:reviewer") == (1, 0)
    reviewed = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    assert reviewed["review"]["status"] == "approved"
    assert reviewed["review"]["reviewer"] == "human:reviewer"


def test_release_check_rejects_silent_or_untraceable_results():
    profile = {
        "methodology_version": "2.0",
        "assessed_at": "2026-09-23",
        "controls": {
            "enc.cmk": {
                "status": "assessed",
                "score": 2,
                "evidence": "Supported.",
            }
        },
        "vignette": {},
        "certifications": [],
    }
    errors = profile_completeness_errors(
        profile,
        {
            "enc.cmk": {
                "surface": "tenant",
                "service_scoped": True,
            },
            "supply-chain.fact": {"surface": "vignette"},
        },
    )
    assert "enc.cmk: assessed result has no references" in errors
    assert any("service-scoped result must be mixed" in error for error in errors)
    assert "vignette.legal: missing M3 block" in errors
    assert "certifications: M3 inventory is empty" in errors


def test_approved_unknown_survives_aggregation_without_score():
    ok, entry = approved_entry(
        {
            "result_status": "unknown",
            "recommended_score": None,
            "overall_confidence": "low",
            "assessor": {
                "evidence": "Documentation was insufficient.",
                "sources_used": ["https://example.com/thin"],
            },
            "review": {"status": "approved"},
        }
    )
    assert ok is True
    assert entry["status"] == "unknown"
    assert "score" not in entry
    assert entry["references"][0]["url"] == "https://example.com/thin"
