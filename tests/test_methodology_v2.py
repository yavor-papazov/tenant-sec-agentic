import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from google.adk.models import Gemini
from pydantic import ValidationError

from tenant_sec_agentic.artifacts import (
    assessment_review_payload_digest,
    control_entry_for_provider_schema,
    validate_control_against_provider_schema,
    validate_release_ready_assessment,
    write_assessment_files,
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
    _canonical_evidence_url,
    _deep_fetch_limit,
    _drain_agent,
    _enrich_claim_bundle,
    _final_recommendation,
    _load_stage_checkpoint,
    _mock_assessor,
    _normalize_fetched_documents,
    _normalize_research_wrapper,
    _normalize_service_array,
    _normalize_service_evidence,
    _parse_deep_research_output,
    _parse_research_findings_output,
    _reconcile_skeptic,
    _retrieval_outcome,
    _service_scope_contract,
    _validate_deep_question_coverage,
    _validate_service_scope_result,
    current_assessment_input_hashes,
    prompt_hashes,
)
from tenant_sec_agentic.model_preflight import (
    ModelPreflightError,
    preflight_model_routes,
)
import tenant_sec_agentic.batch_cli as batch_cli
from tenant_sec_agentic.aggregate_cli import approved_entry, provisional_entry
from tenant_sec_agentic.batch_cli import (
    BudgetTracker,
    _wait_for_process,
    artifact_is_current,
)
from tenant_sec_agentic.review_apply_cli import apply_manifest
from tenant_sec_agentic.review_bundle_cli import render_bundle
from tenant_sec_agentic.release_check_cli import profile_completeness_errors
from tenant_sec_agentic.metered_model import MeteredGemini
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


def test_example_uses_verified_vertex_baseline():
    config = load_assessment_config(ROOT / "assessment-config.example.yaml")
    config.evidence_seeds = {
        "iam.permission-boundaries": ["https://example.com/iam"]
    }
    assert config.model_for("doc_fetch").endswith("gemini-2.5-flash")
    assert config.model_for("assessor").endswith("gemini-2.5-flash")
    assert config.model_for("consistency").endswith("gemini-2.5-flash")
    assert config.model_for("research_revision").endswith(
        "gemini-2.5-flash"
    )
    assert config.limits.max_parallel_assessments == 1
    assert config.vertex_traffic_class == "standard"
    restored = config_from_state(config_to_serializable(config))
    assert restored.evidence_seeds == config.evidence_seeds
    assert restored.vertex_traffic_class == "standard"
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
    with pytest.raises(ValidationError, match="mixed result requires services"):
        SkepticStructured(
            status="assessed",
            score="mixed",
            reasoning="Per-service variation exists.",
        )


def test_consistency_flags_accept_level_labels():
    flag = ConsistencyFlag(current_score="L3", reference_score="L2")
    assert flag.current_score == "L3"


def test_challenger_downgrade_cannot_rewrite_claim_bundle():
    assessor = {
        "status": "assessed",
        "score": 2,
        "evidence": "Official evidence supports managed CMKs.",
        "confidence": "high",
        "sources_used": ["https://example.com/docs/cmk"],
        "licensing": {
            "status": "paid_add_on",
            "evidence": "Enterprise key management tier required.",
            "sources_used": ["https://example.com/docs/cmk"],
        },
        **_claims(),
    }
    skeptic = {
        "verdict": "downgrade",
        "recommended_status": "assessed",
        "recommended_score": 1,
        "reasoning": "The managed path is incomplete.",
        "sources_used": ["https://example.com/docs/limits"],
    }
    status, score, _, _, final = _final_recommendation(
        assessor,
        skeptic,
        None,
    )
    assert (status, score) == ("conflicting", None)
    assert final["claims"] == assessor["claims"]
    assert final["evidence_items"] == assessor["evidence_items"]
    assert "Unresolved independent challenge" in final["evidence"]


def test_unresolved_challenge_is_not_silently_published():
    assessor = {
        "status": "assessed",
        "score": 2,
        "evidence": "Initial assessment.",
        "confidence": "high",
        **_claims(),
    }
    skeptic = {
        "verdict": "flag_deep_research",
        "reasoning": "The decisive service scope is unresolved.",
    }
    status, score, services, confidence, final = _final_recommendation(
        assessor,
        skeptic,
        None,
    )
    assert (status, score, services, confidence) == (
        "conflicting",
        None,
        None,
        "low",
    )
    assert final["status"] == "conflicting"


def test_deep_research_revision_requires_post_research_challenge():
    revised = {
        "status": "assessed",
        "score": 3,
        "evidence": "Revised assessment.",
        "confidence": "high",
        **_claims(),
    }
    status, score, _, _, _ = _final_recommendation(
        revised,
        None,
        {"revised_assessment": revised},
    )
    assert (status, score) == ("conflicting", None)

    status, score, _, _, final = _final_recommendation(
        revised,
        None,
        {
            "revised_assessment": revised,
            "post_research_skeptic": {"verdict": "confirm"},
        },
    )
    assert (status, score) == ("assessed", 3)
    assert final is revised


def test_mixed_service_disagreement_cannot_confirm_by_equal_average():
    assessor = {
        "status": "assessed",
        "score": "mixed",
        "services": {
            "a": {"status": "assessed", "score": 3},
            "b": {"status": "assessed", "score": 1},
        },
    }
    independent = {
        "status": "assessed",
        "score": "mixed",
        "services": {
            "a": {"status": "assessed", "score": 1},
            "b": {"status": "assessed", "score": 3},
        },
    }
    reconciled = _reconcile_skeptic(assessor, independent)
    assert reconciled["verdict"] == "flag_deep_research"


def test_equal_score_with_open_skeptic_question_requires_research():
    assessor = {"status": "assessed", "score": 2}
    independent = {
        "status": "assessed",
        "score": 2,
        "reasoning": "The score may hold.",
        "skepticism_flags": [],
        "deep_research_questions": ["Verify managed database coverage."],
    }
    reconciled = _reconcile_skeptic(assessor, independent)
    assert reconciled["verdict"] == "flag_deep_research"


def test_explicit_retrieval_error_is_never_assessed():
    assert (
        _retrieval_outcome(
            {
                "doc_quality": "error",
                "docs_fetched": [{"url": "https://example.com/partial"}],
            }
        )
        == "failed"
    )


def test_deep_research_json_is_parsed_without_adk_schema_tool():
    parsed = _parse_deep_research_output(
        """```json
        {
          "questions_investigated": [{
            "question": "Does the grant constraint exist?",
            "answer": "Evidence remains incomplete.",
            "sources": ["https://example.com/iam"],
            "confidence": "unresolved"
          }],
          "revised_assessment": {
            "status": "unknown",
            "score": null,
            "services": [],
            "evidence": "Evidence remains incomplete."
          },
          "unresolved": ["Confirm delegated grant behavior."]
        }
        ```"""
    )
    assert parsed["questions_investigated"][0]["confidence"] == "unresolved"
    assert parsed["revised_assessment"]["status"] == "unknown"


def test_research_findings_parse_without_assessment_payload():
    parsed = _parse_research_findings_output(
        """{
          "questions_investigated": [{
            "question": "Does Secret Manager support CMEK?",
            "answer": "Yes.",
            "sources": ["https://example.com/secret-manager"],
            "confidence": "high"
          }],
          "unresolved": []
        }"""
    )
    assert set(parsed) == {"questions_investigated", "unresolved"}


def test_deep_research_must_answer_every_skeptic_question():
    question = "Does Secret Manager support CMEK?"
    with pytest.raises(RuntimeError, match="omitted skeptic questions"):
        _validate_deep_question_coverage(
            {"questions_investigated": []},
            {"deep_research_questions": [question]},
        )
    _validate_deep_question_coverage(
        {
            "questions_investigated": [
                {
                    "question": question,
                    "answer": "Yes.",
                    "sources": ["https://example.com/secret-manager"],
                    "confidence": "high",
                }
            ]
        },
        {"deep_research_questions": [question]},
    )


def test_deep_research_json_repairs_minor_model_syntax_errors():
    parsed = _parse_deep_research_output(
        """{
          "questions_investigated": [{
            "question": "Does the "grant" constraint exist?",
            "answer": "Yes",
            "sources": ["https://example.com/iam"],
            "confidence": "high"
          }],
          "revised_assessment": {
            "status": "unknown",
            "score": null,
            "services": [],
            "evidence": "Evidence remains incomplete."
          },
          "unresolved": []
        }"""
    )
    assert parsed["questions_investigated"][0]["answer"] == "Yes"


def test_deep_research_truncates_oversized_verbatim_quote():
    payload = {
        "questions_investigated": [],
        "revised_assessment": {
            "status": "assessed",
            "score": 2,
            "services": [],
            "confidence": "high",
            "sources_used": ["https://example.com/docs"],
            "evidence_items": [
                {
                    "id": "ev-doc",
                    "url": "https://example.com/docs",
                    "title": "Documentation",
                    "source_class": "technical",
                    "quote": "x" * 1300,
                    "services": [],
                }
            ],
            "claims": [
                {
                    "id": "cl-supported",
                    "assertion": "The capability is supported.",
                    "result": "supported",
                    "evidence_item_ids": ["ev-doc"],
                    "services": [],
                }
            ],
            "criteria_results": [
                {
                    "level": level,
                    "met": level <= 2,
                    "reasoning": "Evidence.",
                    "claim_ids": ["cl-supported"],
                }
                for level in range(4)
            ],
        },
        "unresolved": [],
    }
    parsed = _parse_deep_research_output(payload)
    assert (
        len(parsed["revised_assessment"]["evidence_items"][0]["quote"])
        == 1200
    )
    assert "source-backed claim chain" in parsed["revised_assessment"]["evidence"]
    replacement = dict(parsed["revised_assessment"])
    replacement["services"] = []
    for key in ("evidence_items", "claims", "criteria_results"):
        replacement.pop(key)
    reused = _parse_deep_research_output(
        {
            "questions_investigated": [],
            "revised_assessment": replacement,
            "unresolved": [],
        },
        parsed["revised_assessment"],
    )
    assert reused["revised_assessment"]["claims"]
    assert reused["revised_assessment"]["criteria_results"]


def test_service_scoped_release_requires_complete_mixed_result():
    services = [{"id": "a"}, {"id": "b"}]
    assert _validate_service_scope_result(
        {"service_scoped": True},
        "assessed",
        3,
        None,
        services,
    )
    errors = _validate_service_scope_result(
        {"service_scoped": True},
        "assessed",
        "mixed",
        {"a": {"status": "assessed", "score": 3}},
        services,
    )
    assert errors == ["service-scoped result is missing services: b"]
    assert not _validate_service_scope_result(
        {"service_scoped": True},
        "assessed",
        "mixed",
        {
            "a": {"status": "assessed", "score": 3},
            "b": {"status": "unknown"},
        },
        services,
    )


def test_service_scope_contract_lists_every_required_service(tmp_path):
    config = _config(tmp_path)
    contract = _service_scope_contract(
        config,
        {"service_scoped": True},
    )
    for service in config.provider.services_in_scope:
        assert service["id"] in contract
    assert 'score MUST be "mixed"' in contract
    config.limits.max_fetch_calls_per_deep_research = 2
    assert _deep_fetch_limit(config, {"service_scoped": True}) == max(
        2,
        len(config.provider.services_in_scope),
    )
    assert _deep_fetch_limit(config, {"service_scoped": False}) == 2


def test_constrained_service_array_normalizes_to_canonical_map():
    payload = {
        "services": [
            {
                "service_id": "cloud-storage",
                "status": "assessed",
                "score": 3,
                "evidence": "CMEK is supported.",
                "confidence": "high",
                "sources_used": ["https://example.com/storage"],
            }
        ]
    }
    _normalize_service_array(payload)
    assert set(payload["services"]) == {"cloud-storage"}
    assert payload["services"]["cloud-storage"]["score"] == 3
    assert "service_id" not in payload["services"]["cloud-storage"]


def test_research_wrapper_normalizes_mixed_status():
    replacement = {"status": "mixed", "score": None}
    _normalize_research_wrapper(replacement)
    assert replacement == {"status": "assessed", "score": "mixed"}


def test_fetched_document_urls_are_repaired_and_deduplicated():
    url = "https://example.com/docs/image-scanning"
    output = {
        "docs_fetched": [
            {"url": url + "garbage", "title": "", "relevance": "high"},
            {"url": url, "title": "Duplicate", "relevance": "high"},
            {
                "url": "https://unfetched.example/doc",
                "title": "Unfetched",
                "relevance": "low",
            },
        ]
    }
    _normalize_fetched_documents(
        output,
        {url: {"title": "Scanning", "content": "Fetched body"}},
    )
    assert output["docs_fetched"] == [
        {
            "url": url,
            "title": "Scanning",
            "relevance": "high",
            "content": "Fetched body",
        }
    ]


def test_evidence_url_matching_ignores_locale_and_query():
    localized = (
        "https://learn.microsoft.com/en-us/azure/update-manager/overview"
        "?tabs=azure-vms"
    )
    plain = "https://learn.microsoft.com/azure/update-manager/overview"
    assert _canonical_evidence_url(localized) == _canonical_evidence_url(plain)
    legacy_gcp = "https://cloud.google.com/logging/docs/audit"
    current_gcp = "https://docs.cloud.google.com/logging/docs/audit/"
    assert _canonical_evidence_url(legacy_gcp) == _canonical_evidence_url(
        current_gcp
    )


def test_unreferenced_service_scores_become_unknown():
    services = {
        "document-db": {
            "status": "assessed",
            "score": 1,
            "evidence": "Patching appears provider managed.",
            "sources_used": [],
        },
        "kapsule": {
            "status": "assessed",
            "score": 2,
            "evidence": "Managed upgrades are documented.",
            "sources_used": ["https://example.com/kapsule"],
        },
    }
    normalized = _normalize_service_evidence(
        services,
        {"evidence_items": [], "claims": []},
    )
    assert normalized["document-db"]["status"] == "unknown"
    assert "score" not in normalized["document-db"]
    assert normalized["kapsule"]["score"] == 2


def test_service_names_and_separator_variants_use_configured_ids():
    services = {
        "Object Storage": {
            "status": "assessed",
            "score": 2,
            "sources_used": ["https://example.com/object-storage"],
        },
        "document_db": {
            "status": "assessed",
            "score": 1,
            "sources_used": ["https://example.com/document-db"],
        },
    }
    normalized = _normalize_service_evidence(
        services,
        {"evidence_items": [], "claims": []},
        [
            {"id": "object-storage", "name": "Object Storage"},
            {"id": "document-db", "name": "Document DB"},
        ],
    )
    assert set(normalized) == {"object-storage", "document-db"}


def test_canonical_entry_retains_sources_and_validates(tmp_path):
    config = _config(tmp_path)
    assessor = {
        "status": "assessed",
        "score": 2,
        "evidence": "API accepts a customer key identifier.",
        "confidence": "high",
        "sources_used": ["https://example.com/docs/cmk"],
        "licensing": {
            "status": "paid_add_on",
            "evidence": "Enterprise key management tier required.",
            "sources_used": ["https://example.com/docs/cmk"],
        },
        **_claims(),
    }
    entry = control_entry_for_provider_schema(
        "assessed", 2, assessor, None, "high"
    )
    assert entry["references"][0]["url"] == "https://example.com/docs/cmk"
    assert entry["licensing"]["status"] == "paid_add_on"
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


def test_release_validation_rejects_score_and_bundle_mismatch():
    artifact = {
        "control": "enc.cmk",
        "provider": "scaleway",
        "status": "needs_human_review",
        "result_status": "assessed",
        "recommended_score": 3,
        "recommended_services": None,
        "doc_fetch": {"retrieval_outcome": "succeeded"},
        "prompt_hashes": prompt_hashes(),
        "assessor": {
            "status": "assessed",
            "score": 2,
            "evidence": "Supported.",
            **_claims(),
        },
    }
    errors = validate_release_ready_assessment(artifact)
    assert "final assessor score differs from recommended score" in errors
    assert (
        "recommended score must equal highest supported met criterion"
        in errors
    )


def test_validation_failure_is_written_to_canonical_retry_path(tmp_path):
    assessment = {
        "control": "enc.cmk",
        "provider": "scaleway",
        "status": "needs_human_review",
    }
    yaml_path, _ = write_assessment_files(
        assessments_dir=tmp_path / "assessments",
        reports_dir=tmp_path / "reports",
        provider_slug="scaleway",
        control_id="enc.cmk",
        assessment=assessment,
        control_def={"name": "CMK", "criteria": {}},
        schema_errors=["score mismatch"],
    )
    assert yaml_path.name == "enc.cmk.yaml"
    stored = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert stored["status"] == "validation_error"
    assert not yaml_path.with_suffix(".invalid.yaml").exists()


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

    monkeypatch.setattr(Gemini, "generate_content_async", fake_generate)
    model = MeteredGemini(
        model=config.model_for("assessor"),
        state=state,
        cfg=config,
        role="assessor",
        control_id="iam.mfa-enforcement",
    )
    assert model.model == "gemini-2.5-flash"
    assert model._vertex_headers()[
        "X-Vertex-AI-LLM-Shared-Request-Type"
    ] == "priority"

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
            "sources_used": ["https://example.com/docs/cmk"],
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


def test_failed_artifact_reuses_research_before_revision(tmp_path):
    config = _config(tmp_path)
    config.session.resume_artifacts = True
    artifact_dir = config.paths.assessments_dir / config.provider.slug
    artifact_dir.mkdir(parents=True)
    question = "Does the service support CMK?"
    artifact = {
        "status": "error",
        "assessment_id": config.provider.assessment_id,
        "prompt_hashes": {
            "doc_fetch": "docs",
            "assessor": "assessor",
            "skeptic": "skeptic",
            "consistency": "consistency",
            "deep_research": "research",
            "research_revision": "old-revision",
        },
        "doc_fetch": {
            "docs_fetched": [{"url": "https://example.com/docs"}],
            "doc_quality": "adequate",
        },
        "assessor": {
            "status": "assessed",
            "score": 2,
            "evidence": "Supported by official documentation.",
            "sources_used": ["https://example.com/docs/cmk"],
            **_claims(),
        },
        "skeptic": {
            "status": "assessed",
            "score": 2,
            "reasoning": "Research needed.",
            "deep_research_questions": [question],
            "verdict": "flag_deep_research",
        },
        "consistency": {
            "consistency_flags": [],
            "deep_research_triggered": True,
            "overall_consistency": "suspicious",
        },
        "deep_research": {
            "questions_investigated": [
                {
                    "question": question,
                    "answer": "Yes.",
                    "sources": ["https://example.com/docs"],
                    "confidence": "high",
                }
            ],
            "unresolved": [],
        },
    }
    (artifact_dir / "enc.cmk.yaml").write_text(
        yaml.safe_dump(artifact),
        encoding="utf-8",
    )
    checkpoint = _load_stage_checkpoint(
        config,
        "enc.cmk",
        {
            "doc_fetch": "docs",
            "assessor": "assessor",
            "skeptic": "skeptic",
            "consistency": "consistency",
            "deep_research": "research",
            "research_revision": "new-revision",
        },
    )
    assert checkpoint is not None
    assert checkpoint[4]["_needs_revision"] is True
    assert checkpoint[4]["questions_investigated"][0]["answer"] == "Yes."


def test_validation_artifact_resumes_from_raw_assessor(tmp_path):
    config = _config(tmp_path)
    config.session.resume_artifacts = True
    artifact_dir = config.paths.assessments_dir / config.provider.slug
    artifact_dir.mkdir(parents=True)
    raw_assessor = {
        "status": "assessed",
        "score": 2,
        "evidence": "Supported by official documentation.",
        "confidence": "high",
        "sources_used": ["https://example.com/docs/cmk"],
        **_claims(),
    }
    artifact = {
        "status": "validation_error",
        "assessment_id": config.provider.assessment_id,
        "prompt_hashes": {"doc_fetch": "docs", "assessor": "assessor"},
        "doc_fetch": {
            "retrieval_outcome": "succeeded",
            "docs_fetched": [{"url": "https://example.com/docs/cmk"}],
            "doc_quality": "adequate",
        },
        "assessor": {
            "status": "conflicting",
            "score": None,
            "evidence": "Derived bundle failed validation.",
        },
        "assessor_raw": raw_assessor,
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
    assert checkpoint[1] == raw_assessor


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
        "input_hashes": current_assessment_input_hashes(
            config, "enc.cmk"
        ),
        "result_status": "assessed",
        "recommended_score": 2,
        "status": "needs_human_review",
        "assessor": {
            "status": "assessed",
            "score": 2,
            "evidence": "Supported.",
            **_claims(),
        },
    }
    (artifact_dir / "enc.cmk.yaml").write_text(
        yaml.safe_dump(artifact),
        encoding="utf-8",
    )
    assert artifact_is_current(config, "enc.cmk") is True
    artifact["input_hashes"]["control_definition"] = "stale"
    (artifact_dir / "enc.cmk.yaml").write_text(
        yaml.safe_dump(artifact),
        encoding="utf-8",
    )
    assert artifact_is_current(config, "enc.cmk") is False


def test_batch_network_preflight_fails_before_reservation(monkeypatch):
    def fail_dns(*_args, **_kwargs):
        raise OSError("host unavailable")

    monkeypatch.setattr(batch_cli.socket, "getaddrinfo", fail_dns)
    with pytest.raises(
        batch_cli.InfrastructureFailure,
        match="oauth2.googleapis.com",
    ):
        batch_cli._assert_network_ready()


def test_batch_adc_preflight_fails_before_reservation(monkeypatch):
    class ExpiredCredentials:
        def refresh(self, _request):
            raise RuntimeError("reauthentication needed")

    monkeypatch.setattr(
        batch_cli.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        batch_cli.google.auth,
        "default",
        lambda **_kwargs: (ExpiredCredentials(), "project"),
    )
    with pytest.raises(
        batch_cli.InfrastructureFailure,
        match="ADC preflight failed",
    ):
        batch_cli._assert_network_ready()


def test_batch_classifies_infrastructure_failure_log(tmp_path):
    log_path = tmp_path / "attempt.log"
    log_path.write_text(
        "google.auth TransportError: NameResolutionError",
        encoding="utf-8",
    )
    assert (
        batch_cli._infrastructure_failure(log_path)
        == "DNS resolution failed"
    )


def test_model_preflight_validates_structured_response(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.mock_mode = False
    monkeypatch.setenv("VERTEXAI_PROJECT", "tenantsec")
    monkeypatch.delenv("VERTEXAI_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    calls = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(parsed={"status": "ready"}, text=None)

    class Client:
        def __init__(self, **kwargs):
            self.models = Models()

    results = preflight_model_routes(config, client_factory=Client)
    assert results == [
        {
            "model": "gemini-2.5-flash",
            "location": "global",
            "traffic_class": "standard",
            "status": "ready",
        }
    ]
    assert len(calls) == 1


def test_batch_preflight_fails_before_any_budget_reservation(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    config.mock_mode = False
    monkeypatch.setattr(
        batch_cli,
        "load_assessment_config",
        lambda _: config,
    )
    monkeypatch.setattr(
        batch_cli,
        "preflight_model_routes",
        lambda _: (_ for _ in ()).throw(ModelPreflightError("bad route")),
    )
    args = SimpleNamespace(
        config=[tmp_path / "provider.yaml"],
        ledger=tmp_path / "ledger.json",
        budget_usd=10.0,
    )
    import asyncio

    assert asyncio.run(batch_cli._run(args)) == 2
    ledger = yaml.safe_load(args.ledger.read_text(encoding="utf-8"))
    assert ledger["runs"] == []
    assert ledger["summary"]["status"] == "preflight_failed"


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


def test_batch_budget_keeps_unmetered_model_charge(tmp_path):
    tracker = BudgetTracker(tmp_path / "release-ledger.json", 5.0)

    async def exercise():
        run_id = await tracker.reserve(
            provider="aws",
            control="compute.image-management",
            attempt=1,
            amount_usd=4.0,
        )
        assert run_id is not None
        await tracker.finalize(
            run_id,
            status="failed",
            actual_model_cost_usd=0.1,
            model_charge_usd=1.25,
            tavily_max_cost_usd=0.02,
            exit_code=1,
            artifact_current=False,
            log_path=tmp_path / "run.log",
        )

    import asyncio

    asyncio.run(exercise())
    assert tracker.charged_usd == pytest.approx(1.27)
    run = tracker.data["runs"][0]
    assert run["actual_model_cost_usd"] == pytest.approx(0.1)
    assert run["model_charge_usd"] == pytest.approx(1.25)


def test_batch_process_watchdog_kills_hung_child():
    class FakeProcess:
        def __init__(self):
            import asyncio

            self.stopped = asyncio.Event()
            self.killed = False

        async def wait(self):
            await self.stopped.wait()
            return -9

        def kill(self):
            self.killed = True
            self.stopped.set()

    async def exercise():
        process = FakeProcess()
        result = await _wait_for_process(process, 0.001)
        assert result == 124
        assert process.killed is True

    import asyncio

    asyncio.run(exercise())


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
    config = _config(tmp_path)
    artifact_path = tmp_path / "enc.cmk.yaml"
    artifact = {
        "control": "enc.cmk",
        "provider": "scaleway",
        "status": "needs_human_review",
        "result_status": "assessed",
        "recommended_score": 2,
        "recommended_services": None,
        "doc_fetch": {"retrieval_outcome": "succeeded"},
        "prompt_hashes": prompt_hashes(),
        "input_hashes": current_assessment_input_hashes(
            config,
            "enc.cmk",
        ),
        "assessor": {
            "status": "assessed",
            "score": 2,
            "evidence": "Supported.",
            **_claims(),
        },
        "review": {"status": "pending"},
    }
    artifact_path.write_text(
        yaml.safe_dump(artifact),
        encoding="utf-8",
    )
    artifact_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "review.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "assessments": [
                    {
                        "control": "enc.cmk",
                        "artifact_path": str(artifact_path),
                        "artifact_digest": artifact_digest,
                        "decision": "approved",
                        "notes": "Evidence checked.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert apply_manifest(
        manifest_path,
        "human:reviewer",
        config,
    ) == (1, 0)
    reviewed = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    assert reviewed["review"]["status"] == "approved"
    assert reviewed["review"]["reviewer"] == "human:reviewer"
    assert reviewed["review"]["payload_digest"]
    assert approved_entry(reviewed)[0] is True
    reviewed["assessor"]["evidence"] = "Tampered after approval."
    assert approved_entry(reviewed) == (False, None)
    with pytest.raises(ValueError, match="artifact changed"):
        apply_manifest(manifest_path, "human:reviewer", config)


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
    artifact = {
        "control": "enc.cmk",
        "provider": "scaleway",
        "status": "needs_human_review",
        "result_status": "unknown",
        "recommended_score": None,
        "recommended_services": None,
        "overall_confidence": "low",
        "doc_fetch": {"retrieval_outcome": "succeeded"},
        "prompt_hashes": prompt_hashes(),
        "assessor": {
            "status": "unknown",
            "score": None,
            "evidence": "Documentation was insufficient.",
            "sources_used": ["https://example.com/thin"],
        },
        "review": {"status": "approved"},
    }
    artifact["review"]["payload_digest"] = (
        assessment_review_payload_digest(artifact)
    )
    ok, entry = approved_entry(artifact)
    assert ok is True
    assert entry["status"] == "unknown"
    assert "score" not in entry
    assert entry["references"][0]["url"] == "https://example.com/thin"
    assert approved_entry(
        artifact,
        {"control_definition": "changed"},
    ) == (False, None)


def test_adjustment_cannot_publish_stale_claim_criteria():
    original = {
        "status": "assessed",
        "score": 2,
        "evidence": "L2 is supported.",
        "confidence": "high",
        "sources_used": ["https://example.com/docs/cmk"],
        **_claims(),
    }
    adjusted = dict(original)
    adjusted["score"] = 1
    artifact = {
        "control": "enc.cmk",
        "provider": "scaleway",
        "status": "needs_human_review",
        "result_status": "assessed",
        "recommended_score": 2,
        "recommended_services": None,
        "overall_confidence": "high",
        "doc_fetch": {"retrieval_outcome": "succeeded"},
        "prompt_hashes": prompt_hashes(),
        "assessor": original,
        "review": {
            "status": "adjusted",
            "adjusted_score": 1,
            "adjusted_services": None,
            "adjusted_assessor": adjusted,
            "adjustment_reasoning": "Human adjustment.",
        },
    }
    artifact["review"]["payload_digest"] = (
        assessment_review_payload_digest(artifact)
    )
    ok, entry = approved_entry(artifact)
    assert (ok, entry) == (False, None)


def test_current_pending_assessment_can_be_published_as_unreviewed_rc():
    assessment = {
        "control": "enc.cmk",
        "provider": "scaleway",
        "status": "needs_human_review",
        "result_status": "unknown",
        "recommended_score": None,
        "recommended_services": None,
        "overall_confidence": "low",
        "prompt_hashes": prompt_hashes(),
        "doc_fetch": {"retrieval_outcome": "succeeded"},
        "assessor": {
            "status": "unknown",
            "score": None,
            "evidence": "Official documentation was insufficient.",
            "sources_used": ["https://example.com/thin"],
        },
        "review": {"status": "pending"},
    }
    ok, entry = provisional_entry(assessment)
    assert ok is True
    assert entry["status"] == "unknown"
    assert "score" not in entry

    profile = {
        "methodology_version": "2.0",
        "review_status": "unreviewed",
        "assessed_at": "2026-09-23",
        "controls": {"enc.cmk": entry},
        "vignette": {},
        "certifications": [],
    }
    errors = profile_completeness_errors(
        profile,
        {
            "enc.cmk": {
                "surface": "tenant",
                "service_scoped": False,
            }
        },
    )
    assert not any("publication state 'unknown'" in error for error in errors)
