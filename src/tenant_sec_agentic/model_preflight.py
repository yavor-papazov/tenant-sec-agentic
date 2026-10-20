"""Fail-fast structured-output canaries for configured Vertex model routes."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Literal

from google.genai import Client, types
from pydantic import BaseModel

from tenant_sec_agentic.config import AssessmentConfig


class ModelPreflightError(RuntimeError):
    """Raised when a configured model route cannot return structured JSON."""


class _CanaryResponse(BaseModel):
    status: Literal["ready"]


def preflight_model_routes(
    cfg: AssessmentConfig,
    *,
    client_factory: Callable[..., Any] = Client,
) -> list[dict[str, str]]:
    """Verify each configured model can return the required structured output.

    This intentionally runs before any batch budget reservation. It prevents a
    broken model/traffic-class route from consuming a control's full pipeline
    budget and leaving a misleading partial assessment.
    """
    if cfg.mock_mode:
        return []

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
        raise ModelPreflightError(
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
    client = client_factory(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(
            timeout=cfg.limits.model_timeout_seconds * 1_000,
            headers=headers,
        ),
    )

    roles = (
        "doc_fetch",
        "assessor",
        "skeptic",
        "consistency",
        "deep_research",
        "research_revision",
    )
    models = sorted(
        {cfg.model_for(role).removeprefix("vertex_ai/") for role in roles}
    )
    results: list[dict[str, str]] = []
    for model in models:
        try:
            response = client.models.generate_content(
                model=model,
                contents=(
                    'Return exactly this JSON object: {"status":"ready"}'
                ),
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=128,
                    response_mime_type="application/json",
                    response_schema=_CanaryResponse,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=0
                    ),
                    automatic_function_calling=(
                        types.AutomaticFunctionCallingConfig(disable=True)
                    ),
                ),
            )
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, _CanaryResponse):
                status = parsed.status
            elif isinstance(parsed, dict):
                status = parsed.get("status")
            else:
                text = getattr(response, "text", None)
                status = json.loads(text or "{}").get("status")
            if status != "ready":
                raise ValueError("response did not contain status=ready")
        except Exception as exc:
            raise ModelPreflightError(
                "structured-output preflight failed for "
                f"{model} ({location}, {cfg.vertex_traffic_class}): {exc}"
            ) from exc
        results.append(
            {
                "model": model,
                "location": location,
                "traffic_class": cfg.vertex_traffic_class,
                "status": "ready",
            }
        )
    return results
