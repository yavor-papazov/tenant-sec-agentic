import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import tenant_sec_agentic.m3_cli as m3_cli
from tenant_sec_agentic.release_check_cli import VIGNETTE_BLOCKS


ROOT = Path(__file__).parent.parent
CANONICAL = ROOT.parent / "tenant-sec"
SOURCE_URL = "https://docs.example.com/m3"


def _write_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "tenant-sec"
    (repo / "schema").mkdir(parents=True)
    (repo / "providers").mkdir()
    shutil.copy(
        CANONICAL / "schema" / "provider.schema.json",
        repo / "schema" / "provider.schema.json",
    )
    (repo / "schema" / "certification-catalog.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "test",
                "programmes": {
                    "iso-27001": {
                        "name": "ISO/IEC 27001",
                        "kind": "certification",
                    },
                    "eucs": {
                        "name": "EUCS",
                        "kind": "scheme-draft",
                        "holdable": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    profile = {
        "provider": "example",
        "assessment_id": "example/public/eu-1/standard",
        "display_name": "Example Cloud",
        "assessed_by": "community:pipeline",
        "assessed_at": "2026-10-26",
        "methodology_version": "2.0",
        "revision": 1,
        "review_status": "unreviewed",
        "offering": {
            "id": "public",
            "name": "Example Public Cloud",
            "partition": "commercial",
            "regions": ["eu-1"],
            "edition": "standard",
            "cohort": "eu-public",
        },
        "services_in_scope": [
            {
                "id": "compute",
                "name": "Compute",
                "category": "compute.vm",
                "regions": ["eu-1"],
                "edition": "standard",
            }
        ],
        "controls": {},
    }
    (repo / "providers" / "example.yaml").write_text(
        yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )
    config = {
        "provider": {
            "slug": "example",
            "display_name": "Example Cloud",
            "docs_base_url": "https://docs.example.com",
            "official_domains": ["docs.example.com"],
            "assessment_id": "example/public/eu-1/standard",
            "offering": profile["offering"],
            "services_in_scope": profile["services_in_scope"],
        },
        "controls": {"scope": "all"},
        "models": {"deep_research": "vertex_ai/gemini-test"},
        "limits": {
            "max_search_calls_per_run": 20,
            "max_fetch_calls_per_run": 20,
            "max_output_tokens_per_call": 2048,
        },
        "paths": {
            "repo_root": str(repo),
            "assessments_dir": str(tmp_path / "assessments"),
            "reports_dir": str(tmp_path / "reports"),
        },
        "session": {"database_path": str(tmp_path / "sessions.db")},
    }
    config_path = tmp_path / "assessment-config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def _valid_output() -> dict:
    return {
        "vignette": [
            {
                "block": block,
                "summary": f"Concise source-backed {block} summary.",
                "facts": [],
                "references": [
                    {"url": SOURCE_URL, "title": "Official M3 page"}
                ],
            }
            for block in VIGNETTE_BLOCKS
        ],
        "certifications": [
            {
                "id": "iso-27001",
                "kind": "certification",
                "status": "unknown",
                "validity": "unknown",
                "all_services": False,
                "evidence": [
                    {"url": SOURCE_URL, "title": "Official M3 page"}
                ],
            }
        ],
    }


class _FakeModels:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payload() if callable(self.payload) else self.payload
        return SimpleNamespace(parsed=payload, text=None)


class _FakeClient:
    def __init__(self, payload):
        self.models = _FakeModels(payload)


def _mock_dependencies(monkeypatch, payload):
    client = _FakeClient(payload)
    fetched = []
    preflighted = []

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(
        m3_cli,
        "preflight_model_routes",
        lambda cfg: preflighted.append(cfg.provider.slug),
    )

    def search(_query, _api_key):
        return [
            {
                "url": "https://evil.example/m3",
                "title": "Untrusted",
                "snippet": "Must be filtered.",
            },
            {
                "url": SOURCE_URL,
                "title": "Official M3 page",
                "snippet": "Official provider material.",
            },
        ]

    def fetch(url, _official, _max_chars):
        fetched.append(url)
        return url, (
            "Official provider material describing legal, operations, "
            "privileged access, subcontracting, audit and exit, continuity, "
            "vulnerability disclosure, transfers, and ISO/IEC 27001."
        )

    monkeypatch.setattr(m3_cli, "_tavily_search", search)
    monkeypatch.setattr(m3_cli, "_fetch_text", fetch)
    monkeypatch.setattr(m3_cli, "_vertex_client", lambda _cfg: client)
    return client, fetched, preflighted


def test_valid_write_is_structured_validated_and_atomic(tmp_path, monkeypatch):
    config_path = _write_fixture(tmp_path)
    client, fetched, preflighted = _mock_dependencies(
        monkeypatch,
        _valid_output(),
    )

    written = m3_cli.run_configs([config_path])

    assert preflighted == ["example"]
    assert written == [
        tmp_path
        / "tenant-sec"
        / "providers"
        / "_metadata"
        / "example.yaml"
    ]
    metadata = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert set(metadata["vignette"]) == VIGNETTE_BLOCKS
    certification = metadata["certifications"][0]
    assert certification["id"] == "iso-27001"
    assert certification["scope"].startswith("Provider programme scope")
    assert certification["offering"] == "public"
    assert certification["regions"] == ["eu-1"]
    assert certification["report_access"] == "public"
    assert len(metadata["_provenance"]["input_sha256"]) == 64
    assert metadata["_provenance"]["source_index"][0]["url"] == SOURCE_URL
    assert fetched == [SOURCE_URL]
    assert len(client.models.calls) == 1
    generation_config = client.models.calls[0]["config"]
    assert generation_config.temperature == 0
    assert generation_config.max_output_tokens == 2048
    assert generation_config.response_mime_type == "application/json"


def test_unknown_cited_url_does_not_replace_existing_file(
    tmp_path,
    monkeypatch,
):
    config_path = _write_fixture(tmp_path)
    metadata_path = (
        tmp_path
        / "tenant-sec"
        / "providers"
        / "_metadata"
        / "example.yaml"
    )
    metadata_path.parent.mkdir()
    metadata_path.write_text("sentinel: keep\n", encoding="utf-8")
    payload = _valid_output()
    legal = next(
        item for item in payload["vignette"] if item["block"] == "legal"
    )
    legal["references"][0]["url"] = "https://docs.example.com/not-collected"
    client, _, _ = _mock_dependencies(monkeypatch, payload)

    with pytest.raises(
        m3_cli.M3CollectionError,
        match="after two attempts",
    ):
        m3_cli.run_configs([config_path])

    assert metadata_path.read_text(encoding="utf-8") == "sentinel: keep\n"
    assert len(client.models.calls) == 2


@pytest.mark.parametrize(
    ("cert_id", "kind", "error"),
    [
        ("not-in-catalog", "certification", "unknown certification"),
        ("iso-27001", "attestation", "kind does not match"),
    ],
)
def test_bad_certification_id_or_kind_rejects(
    tmp_path,
    monkeypatch,
    cert_id,
    kind,
    error,
):
    config_path = _write_fixture(tmp_path)
    payload = _valid_output()
    payload["certifications"][0]["id"] = cert_id
    payload["certifications"][0]["kind"] = kind
    _mock_dependencies(monkeypatch, payload)

    with pytest.raises(m3_cli.M3CollectionError, match=error):
        m3_cli.run_configs([config_path])


def test_missing_vignette_block_rejects(tmp_path, monkeypatch):
    config_path = _write_fixture(tmp_path)
    payload = _valid_output()
    payload["vignette"] = [
        item for item in payload["vignette"] if item["block"] != "transfers"
    ]
    _mock_dependencies(monkeypatch, payload)

    with pytest.raises(m3_cli.M3CollectionError, match="after two attempts"):
        m3_cli.run_configs([config_path])


def test_search_results_are_restricted_to_official_domains(
    tmp_path,
    monkeypatch,
):
    config_path = _write_fixture(tmp_path)
    _, fetched, _ = _mock_dependencies(monkeypatch, _valid_output())

    m3_cli.run_configs([config_path])

    assert fetched
    assert all(url.startswith("https://docs.example.com/") for url in fetched)
    assert not any("evil.example" in url for url in fetched)


def test_derived_domains_include_exact_host_and_safe_parent(
    tmp_path,
):
    config_path = _write_fixture(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    del raw["provider"]["official_domains"]
    raw["provider"]["docs_base_url"] = "https://docs.example.com/guide"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = m3_cli.load_assessment_config(config_path)

    assert m3_cli.official_domains(cfg) == (
        "docs.example.com",
        "example.com",
    )


def test_output_is_valid_json_serializable(tmp_path):
    config_path = _write_fixture(tmp_path)
    cfg = m3_cli.load_assessment_config(config_path)
    output = m3_cli.M3ResearchOutput.model_validate(_valid_output())

    metadata = m3_cli._metadata_payload(output)

    json.dumps(metadata)
    assert metadata["certifications"][0]["validity"] == "unknown"


def test_unsupported_held_claim_is_downgraded(tmp_path, monkeypatch):
    config_path = _write_fixture(tmp_path)
    payload = _valid_output()
    payload["certifications"][0]["status"] = "held"
    _mock_dependencies(monkeypatch, payload)

    written = m3_cli.run_configs([config_path])

    metadata = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert metadata["certifications"][0]["status"] == "unknown"
