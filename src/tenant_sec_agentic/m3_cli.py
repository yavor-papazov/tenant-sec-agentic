"""Small, fail-closed collector for provisional M3 provider metadata."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import trafilatura
import yaml
from dotenv import load_dotenv
from google.genai import Client, types
from jsonschema import Draft202012Validator, FormatChecker

from tenant_sec_agentic.config import (
    AssessmentConfig,
    load_assessment_config,
)
from tenant_sec_agentic.m3_models import M3ResearchOutput
from tenant_sec_agentic.model_preflight import preflight_model_routes
from tenant_sec_agentic.release_check_cli import VIGNETTE_BLOCKS
from tenant_sec_agentic.web_tools import _tavily_search

BLOCK_QUERIES = {
    "legal": "cloud customer agreement data processing terms privacy legal",
    "operations": "cloud security operations incident response trust center",
    "privileged_access": (
        "cloud provider personnel privileged access customer approval controls"
    ),
    "subcontracting": "cloud subprocessors subcontractors supplier list",
    "audit_and_exit": (
        "cloud audit reports customer data export deletion termination"
    ),
    "continuity": (
        "cloud business continuity disaster recovery resilience availability"
    ),
    "vulnerability_disclosure": (
        "cloud vulnerability disclosure security contact bug bounty"
    ),
    "transfers": (
        "cloud international data transfers data residency safeguards"
    ),
}
CERTIFICATIONS_TOPIC = "certifications"
MAX_RESULTS_PER_TOPIC = 3
MAX_FETCHES = 27
MAX_FETCHED_CHARS = 60_000
MAX_MODEL_ATTEMPTS = 2
MAX_M3_OUTPUT_TOKENS = 8_192

# Common multi-label public suffixes. Unknown ccTLD arrangements retain only
# the exact documentation host rather than broadening trust too far.
_COMMON_COMPOUND_SUFFIXES = {
    "ac.uk",
    "co.jp",
    "co.nz",
    "co.uk",
    "com.au",
    "com.br",
    "com.cn",
    "com.sg",
    "com.tr",
    "edu.au",
    "gov.uk",
    "net.au",
    "net.cn",
    "org.au",
    "org.uk",
}


class M3CollectionError(RuntimeError):
    """Raised when provisional M3 metadata cannot be safely written."""


@dataclass(frozen=True)
class PreparedMetadata:
    path: Path
    payload: dict[str, Any]


def _registrable_parent(hostname: str) -> str | None:
    """Derive a conservative registrable parent without adding a PSL dependency."""
    host = hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    labels = host.split(".")
    if len(labels) < 3:
        return None
    suffix = ".".join(labels[-2:])
    if suffix in _COMMON_COMPOUND_SUFFIXES:
        if len(labels) < 4:
            return None
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def official_domains(cfg: AssessmentConfig) -> tuple[str, ...]:
    configured = tuple(cfg.provider.official_domains)
    if configured:
        return configured
    parsed = urlparse(cfg.provider.docs_base_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise M3CollectionError("provider.docs_base_url has no hostname")
    domains = [host]
    parent = _registrable_parent(host)
    if parent and parent != host:
        domains.append(parent)
    return tuple(domains)


def _host_is_official(hostname: str | None, domains: tuple[str, ...]) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _accepted_url(raw_url: str, domains: tuple[str, ...]) -> str | None:
    try:
        parsed = urlparse(raw_url.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not _host_is_official(parsed.hostname, domains)
    ):
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or "/"
    return urlunparse(("https", host, path, "", parsed.query, ""))


def _topic_queries(cfg: AssessmentConfig, domains: tuple[str, ...]) -> dict[str, str]:
    provider = cfg.provider.display_name
    offering = str(cfg.provider.offering.get("name") or "")
    site_terms = " OR ".join(f"site:{domain}" for domain in domains)
    queries = {
        block: f"{provider} {offering} {terms} ({site_terms})"
        for block, terms in BLOCK_QUERIES.items()
    }
    queries[CERTIFICATIONS_TOPIC] = (
        f"{provider} {offering} cloud compliance certifications "
        f"attestations qualifications ({site_terms})"
    )
    return queries


def _fetch_text(
    url: str,
    official: tuple[str, ...],
    max_chars: int,
) -> tuple[str, str]:
    headers = {
        "User-Agent": (
            "tenant-sec-agentic/0.1 "
            "(+https://github.com/ypapazov/tenant-sec)"
        )
    }
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
    final_url = _accepted_url(str(response.url), official)
    if final_url is None:
        raise M3CollectionError(
            f"official source redirected outside allowed domains: {url}"
        )
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type and not any(
        value in content_type for value in ("text/html", "text/plain")
    ):
        raise M3CollectionError(f"source is not a text page: {url}")
    extracted = trafilatura.extract(
        response.text,
        url=final_url,
        include_comments=False,
        include_tables=True,
    )
    text = (extracted or response.text or "").strip()[:max_chars]
    if not text:
        raise M3CollectionError(f"source contained no readable text: {url}")
    return final_url, text


def _collect_sources(
    cfg: AssessmentConfig,
    official: tuple[str, ...],
) -> list[dict[str, Any]]:
    queries = _topic_queries(cfg, official)
    required_calls = len(queries)
    if cfg.limits.max_search_calls_per_run < required_calls:
        raise M3CollectionError(
            "search limit is too small to research every M3 topic"
        )
    if cfg.limits.max_fetch_calls_per_run < required_calls:
        raise M3CollectionError(
            "fetch limit is too small to research every M3 topic"
        )
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise M3CollectionError("TAVILY_API_KEY is required")

    by_topic: dict[str, list[dict[str, str]]] = {}
    search_calls = 0
    for topic, query in queries.items():
        accepted: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_url in cfg.evidence_seeds.get(f"m3.{topic}", []):
            url = _accepted_url(raw_url, official)
            if url is None or url in seen:
                continue
            seen.add(url)
            accepted.append(
                {
                    "url": url,
                    "title": url,
                    "snippet": "Configured official M3 evidence seed.",
                }
            )
        topic_terms = (
            BLOCK_QUERIES.get(topic)
            or "cloud compliance certifications attestations qualifications"
        )
        provider = cfg.provider.display_name
        offering = str(cfg.provider.offering.get("name") or "")
        search_queries = [query] + [
            f"{provider} {offering} {topic_terms} site:{domain}"
            for domain in official
        ]
        for search_query in search_queries:
            if accepted:
                break
            if search_calls >= cfg.limits.max_search_calls_per_run:
                break
            search_calls += 1
            try:
                raw_results = _tavily_search(search_query, api_key)
            except Exception as exc:
                raise M3CollectionError(
                    f"search failed for {topic}: {exc}"
                ) from exc
            for result in raw_results:
                url = _accepted_url(str(result.get("url") or ""), official)
                if url is None or url in seen:
                    continue
                seen.add(url)
                accepted.append(
                    {
                        "url": url,
                        "title": str(result.get("title") or url)[:200],
                        "snippet": str(result.get("snippet") or "")[:1000],
                    }
                )
                if len(accepted) >= MAX_RESULTS_PER_TOPIC:
                    break
            if accepted:
                break
        if not accepted:
            raise M3CollectionError(
                f"search found no official-domain results for {topic}"
            )
        by_topic[topic] = accepted

    candidates: dict[str, dict[str, Any]] = {}
    for rank in range(MAX_RESULTS_PER_TOPIC):
        for topic in queries:
            topic_results = by_topic[topic]
            if rank >= len(topic_results):
                continue
            result = topic_results[rank]
            candidate = candidates.setdefault(
                result["url"],
                {**result, "topics": []},
            )
            if topic not in candidate["topics"]:
                candidate["topics"].append(topic)

    fetch_limit = min(cfg.limits.max_fetch_calls_per_run, MAX_FETCHES)
    per_source_chars = max(
        1000,
        min(8000, MAX_FETCHED_CHARS // max(1, fetch_limit)),
    )
    fetched: dict[str, dict[str, Any]] = {}
    fetch_count = 0
    for candidate in candidates.values():
        if fetch_count >= fetch_limit:
            break
        fetch_count += 1
        try:
            final_url, content = _fetch_text(
                candidate["url"],
                official,
                per_source_chars,
            )
        except Exception:
            continue
        existing = fetched.get(final_url)
        if existing is not None:
            for topic in candidate["topics"]:
                if topic not in existing["topics"]:
                    existing["topics"].append(topic)
            continue
        fetched[final_url] = {
            "url": final_url,
            "title": candidate["title"],
            "snippet": candidate["snippet"],
            "topics": list(candidate["topics"]),
            "content": content,
        }

    covered = {
        topic
        for source in fetched.values()
        for topic in source["topics"]
    }
    missing = sorted(set(queries) - covered)
    if missing:
        raise M3CollectionError(
            "no fetched official source for M3 topics: " + ", ".join(missing)
        )
    return list(fetched.values())


def _load_catalog(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "schema" / "certification-catalog.yaml"
    try:
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise M3CollectionError(f"cannot load certification catalog: {exc}") from exc
    if not isinstance(catalog, dict) or not isinstance(
        catalog.get("programmes"), dict
    ):
        raise M3CollectionError("certification catalog has no programmes mapping")
    return catalog


def _research_input(
    cfg: AssessmentConfig,
    catalog: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    programmes = {
        programme_id: {
            "name": value.get("name"),
            "kind": value.get("kind"),
            "holdable": value.get("holdable", True),
        }
        for programme_id, value in catalog["programmes"].items()
    }
    return {
        "provider": {
            "slug": cfg.provider.slug,
            "display_name": cfg.provider.display_name,
            "assessment_id": cfg.provider.assessment_id,
            "offering": cfg.provider.offering,
            "services_in_scope": cfg.provider.services_in_scope,
        },
        "required_vignette_blocks": sorted(VIGNETTE_BLOCKS),
        "certification_catalog": programmes,
        "sources": sources,
    }


def _prompt(research_input: dict[str, Any], retry_error: str | None = None) -> str:
    retry = ""
    if retry_error:
        retry = (
            "\nThe previous response was rejected. Correct this validation "
            f"error without adding unsupported claims: {retry_error[:1000]}\n"
        )
    return (
        "Produce concise provisional M3 metadata for the provider and offering "
        "in RESEARCH_INPUT. Return only one JSON object with this shape: "
        '{"vignette":[{"block":"legal","summary":"...",'
        '"facts":[{"key":"snake_case","value":"..."}],'
        '"references":[{"url":"...","title":"..."}]}],'
        '"certifications":[{"id":"catalog-id","kind":"certification",'
        '"status":"unknown","validity":"unknown","all_services":false,'
        '"evidence":[{"url":"...","title":"..."}]}]}. '
        "Optional certification fields are valid_from, valid_until, scope, "
        "offering, regions, services, and report_access. Do not "
        "include analysis, hidden reasoning, or chain of thought.\n"
        "Use only facts supported by the supplied source text. Every reference "
        "and certification evidence URL must be copied exactly from a source "
        "URL in RESEARCH_INPUT. Return vignette as a list and populate all "
        "eight required blocks exactly once "
        "with a concise nonempty summary and at least one reference. Facts use "
        "short snake_case keys and concise string values.\n"
        "For certifications, use only IDs and matching kinds from the supplied "
        "catalog. An official provider compliance page is acceptable evidence "
        "for this provisional inventory. Prefer status='unknown' and "
        "validity='unknown' unless exact status and validity are established. "
        "Never invent dates, scope, service coverage, or all-services coverage. "
        "Set all_services=false. If scope is supplied it must be exact wording "
        "present in the cited source. Supply a date only when its exact ISO "
        "YYYY-MM-DD text appears in the cited source. Omit uncertain optional "
        "fields. Do not claim a non-holdable programme is held."
        f"{retry}\nOUTPUT_JSON_SCHEMA:\n"
        + json.dumps(
            M3ResearchOutput.model_json_schema(),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\nRESEARCH_INPUT:\n"
        + json.dumps(research_input, sort_keys=True, separators=(",", ":"))
    )


def _vertex_client(cfg: AssessmentConfig) -> Client:
    project = (
        os.environ.get("VERTEXAI_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    location = (
        os.environ.get("VERTEXAI_LOCATION")
        or os.environ.get("GOOGLE_CLOUD_LOCATION")
        or "global"
    )
    if not project:
        raise M3CollectionError(
            "VERTEXAI_PROJECT or GOOGLE_CLOUD_PROJECT is required"
        )
    headers: dict[str, str] = {}
    if cfg.vertex_traffic_class != "standard":
        headers = {
            "X-Vertex-AI-LLM-Request-Type": "shared",
            "X-Vertex-AI-LLM-Shared-Request-Type": (
                cfg.vertex_traffic_class
            ),
        }
    return Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(
            timeout=min(cfg.limits.model_timeout_seconds, 180) * 1000,
            headers=headers,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def _model_output(
    client: Any,
    cfg: AssessmentConfig,
    prompt: str,
) -> M3ResearchOutput:
    configured_model = cfg.model_for("deep_research")
    if not configured_model.startswith("vertex_ai/"):
        raise M3CollectionError(
            "M3 collector requires a vertex_ai deep_research model"
        )
    response = client.models.generate_content(
        model=configured_model.removeprefix("vertex_ai/"),
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=min(
                cfg.limits.max_output_tokens_per_call,
                MAX_M3_OUTPUT_TOKENS,
            ),
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            automatic_function_calling=(
                types.AutomaticFunctionCallingConfig(disable=True)
            ),
        ),
    )
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, M3ResearchOutput):
        return parsed
    if isinstance(parsed, dict):
        return M3ResearchOutput.model_validate(parsed)
    return M3ResearchOutput.model_validate_json(
        getattr(response, "text", None) or ""
    )


def _metadata_payload(output: M3ResearchOutput) -> dict[str, Any]:
    vignette: dict[str, Any] = {}
    for value in output.vignette:
        vignette[value.block] = {
            "summary": value.summary,
            "facts": {fact.key: fact.value for fact in value.facts},
            "references": [
                reference.model_dump() for reference in value.references
            ],
        }
    certifications = [
        certification.model_dump(mode="json", exclude_none=True)
        for certification in output.certifications
    ]
    return {
        "vignette": vignette,
        "certifications": certifications,
    }


def _validate_candidate(
    cfg: AssessmentConfig,
    output: M3ResearchOutput,
    catalog: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    if set(BLOCK_QUERIES) != set(VIGNETTE_BLOCKS):
        raise M3CollectionError(
            "collector queries do not cover release-check vignette blocks"
        )
    metadata = _metadata_payload(output)
    source_by_url = {source["url"]: source for source in sources}
    source_urls = set(source_by_url)
    for block in sorted(VIGNETTE_BLOCKS):
        value = metadata["vignette"].get(block)
        if not isinstance(value, dict):
            raise M3CollectionError(f"vignette.{block} is missing")
        if not str(value.get("summary") or "").strip():
            raise M3CollectionError(f"vignette.{block} has no summary")
        references = value.get("references") or []
        if not references:
            raise M3CollectionError(f"vignette.{block} has no references")
        for reference in references:
            if reference["url"] not in source_urls:
                raise M3CollectionError(
                    f"vignette.{block} cites an uncollected URL"
                )

    programmes = catalog["programmes"]
    seen_ids: set[str] = set()
    service_ids = {
        str(service.get("id"))
        for service in cfg.provider.services_in_scope
    }
    regions = {
        str(region)
        for region in (cfg.provider.offering.get("regions") or [])
    }
    offering_id = str(cfg.provider.offering.get("id") or "")
    for certification in metadata["certifications"]:
        cert_id = certification["id"]
        if cert_id in seen_ids:
            raise M3CollectionError(f"duplicate certification id: {cert_id}")
        seen_ids.add(cert_id)
        programme = programmes.get(cert_id)
        if not isinstance(programme, dict):
            raise M3CollectionError(
                f"unknown certification catalog id: {cert_id}"
            )
        if certification["kind"] != programme.get("kind"):
            raise M3CollectionError(
                f"certification kind does not match catalog: {cert_id}"
            )
        if (
            programme.get("holdable", True) is False
            and certification["status"] == "held"
        ):
            raise M3CollectionError(
                f"non-holdable programme cannot be held: {cert_id}"
            )
        if (
            certification["status"] == "held"
            and certification["validity"] == "fixed"
            and not certification.get("valid_until")
        ):
            raise M3CollectionError(
                f"fixed held certification lacks valid_until: {cert_id}"
            )
        evidence = certification.get("evidence") or []
        if not evidence:
            raise M3CollectionError(
                f"certification has no evidence: {cert_id}"
            )
        evidence_urls = [item["url"] for item in evidence]
        unknown_urls = set(evidence_urls) - source_urls
        if unknown_urls:
            raise M3CollectionError(
                f"certification {cert_id} cites an uncollected URL"
            )
        evidence_text = "\n".join(
            source_by_url[url]["content"] for url in evidence_urls
        )
        for field in ("valid_from", "valid_until"):
            value = certification.get(field)
            if value and value not in evidence_text:
                raise M3CollectionError(
                    f"certification {cert_id} has unsupported {field}"
                )
        scope = certification.get("scope")
        if scope and " ".join(scope.casefold().split()) not in " ".join(
            evidence_text.casefold().split()
        ):
            raise M3CollectionError(
                f"certification {cert_id} has unsupported scope"
            )
        if certification.get("all_services"):
            raise M3CollectionError(
                f"provisional certification cannot claim all_services: {cert_id}"
            )
        if (
            certification.get("offering")
            and certification["offering"] != offering_id
        ):
            raise M3CollectionError(
                f"certification {cert_id} names a different offering"
            )
        if set(certification.get("regions") or []) - regions:
            raise M3CollectionError(
                f"certification {cert_id} names an out-of-scope region"
            )
        if set(certification.get("services") or []) - service_ids:
            raise M3CollectionError(
                f"certification {cert_id} names an out-of-scope service"
            )
        if (
            certification["status"] == "held"
            and certification["validity"] == "unknown"
        ):
            certification["status"] = "unknown"
        if not certification.get("scope"):
            certification["scope"] = (
                "Provider programme scope; not assumed to cover every "
                "assessed service."
            )
        if not certification.get("offering"):
            certification["offering"] = offering_id
        if not certification.get("regions"):
            certification["regions"] = sorted(regions)
        if not certification.get("report_access"):
            certification["report_access"] = "public"

    profile_path = (
        cfg.paths.repo_root / "providers" / f"{cfg.provider.slug}.yaml"
    )
    try:
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise M3CollectionError(
            f"cannot load current provider profile: {exc}"
        ) from exc
    if not isinstance(profile, dict):
        raise M3CollectionError("current provider profile is not a mapping")
    profile = dict(profile)
    profile.update(metadata)
    schema_path = cfg.paths.repo_root / "schema" / "provider.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise M3CollectionError(f"cannot load provider schema: {exc}") from exc
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )
    schema_errors = sorted(
        validator.iter_errors(profile),
        key=lambda error: list(error.absolute_path),
    )
    if schema_errors:
        rendered = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: "
            f"{error.message}"
            for error in schema_errors[:10]
        )
        raise M3CollectionError(
            "generated metadata fails provider.schema.json: " + rendered
        )
    return metadata


def prepare_metadata(cfg: AssessmentConfig) -> PreparedMetadata:
    print(f"[{cfg.provider.slug}] collecting official M3 sources", flush=True)
    official = official_domains(cfg)
    catalog = _load_catalog(cfg.paths.repo_root)
    sources = _collect_sources(cfg, official)
    print(
        f"[{cfg.provider.slug}] collected {len(sources)} sources; "
        "generating metadata",
        flush=True,
    )
    research_input = _research_input(cfg, catalog, sources)
    client = _vertex_client(cfg)
    retry_error: str | None = None
    final_prompt = ""
    metadata: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        print(
            f"[{cfg.provider.slug}] model attempt "
            f"{attempt}/{MAX_MODEL_ATTEMPTS}",
            flush=True,
        )
        final_prompt = _prompt(research_input, retry_error)
        try:
            output = _model_output(client, cfg, final_prompt)
            metadata = _validate_candidate(
                cfg,
                output,
                catalog,
                sources,
            )
        except Exception as exc:
            last_error = exc
            retry_error = str(exc)
            continue
        break
    if metadata is None:
        raise M3CollectionError(
            "model did not produce valid M3 metadata after two attempts: "
            f"{last_error}"
        ) from last_error
    print(f"[{cfg.provider.slug}] metadata validated", flush=True)

    configured_model = cfg.model_for("deep_research")
    metadata["_provenance"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": configured_model,
        "input_sha256": hashlib.sha256(
            final_prompt.encode("utf-8")
        ).hexdigest(),
        "source_index": [
            {
                "url": source["url"],
                "title": source["title"],
                "content_sha256": hashlib.sha256(
                    source["content"].encode("utf-8")
                ).hexdigest(),
            }
            for source in sources
        ],
    }
    path = (
        cfg.paths.repo_root
        / "providers"
        / "_metadata"
        / f"{cfg.provider.slug}.yaml"
    )
    return PreparedMetadata(path=path, payload=metadata)


def _atomic_write_yaml(prepared: PreparedMetadata) -> None:
    prepared.path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        prepared.payload,
        sort_keys=False,
        allow_unicode=True,
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=prepared.path.parent,
            prefix=f".{prepared.path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, prepared.path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def run_configs(config_paths: list[Path]) -> list[Path]:
    configs = [
        load_assessment_config(path.resolve()) for path in config_paths
    ]
    output_paths = [
        cfg.paths.repo_root
        / "providers"
        / "_metadata"
        / f"{cfg.provider.slug}.yaml"
        for cfg in configs
    ]
    if len(output_paths) != len(set(output_paths)):
        raise M3CollectionError("configs resolve to duplicate metadata outputs")
    for cfg in configs:
        preflight_model_routes(cfg)
    prepared = [prepare_metadata(cfg) for cfg in configs]
    for item in prepared:
        _atomic_write_yaml(item)
    return [item.path for item in prepared]


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Collect provisional M3 provider metadata"
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        required=True,
        help="Assessment config (repeatable)",
    )
    args = parser.parse_args(argv)
    try:
        written = run_configs(args.config)
    except Exception as exc:
        raise SystemExit(f"M3 collection failed: {exc}") from exc
    for path in written:
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
