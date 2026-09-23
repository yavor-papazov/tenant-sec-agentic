from pathlib import Path
from types import SimpleNamespace

import pytest
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
    _final_recommendation,
    _mock_assessor,
)
from tenant_sec_agentic.aggregate_cli import approved_entry
from tenant_sec_agentic.metered_model import MeteredLiteLlm
from tenant_sec_agentic.schemas import AssessorStructured, SkepticStructured
from tenant_sec_agentic.usage import (
    BudgetExceeded,
    finalize_model_call,
    new_ledger,
    reserve_model_call,
)

ROOT = Path(__file__).parent.parent
CANONICAL = ROOT.parent / "tenant-sec"


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


def test_canonical_entry_retains_sources_and_validates(tmp_path):
    config = _config(tmp_path)
    assessor = {
        "status": "assessed",
        "score": 2,
        "evidence": "API accepts a customer key identifier.",
        "confidence": "high",
        "sources_used": ["https://example.com/docs/cmk"],
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
