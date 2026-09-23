"""Per-request LiteLLM metering at ADK's actual model-call boundary."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.models.lite_llm import LiteLlm
from pydantic import PrivateAttr

from tenant_sec_agentic.config import AssessmentConfig
from tenant_sec_agentic.usage import (
    BudgetExceeded,
    finalize_model_call,
    reserve_model_call,
)


class MeteredLiteLlm(LiteLlm):
    """LiteLLM adapter that reserves and records every ADK model turn."""

    _state: dict[str, Any] = PrivateAttr()
    _cfg: AssessmentConfig = PrivateAttr()
    _role: str = PrivateAttr()
    _control_id: str = PrivateAttr()
    _turns: int = PrivateAttr(default=0)

    def __init__(
        self,
        *,
        model: str,
        state: dict[str, Any],
        cfg: AssessmentConfig,
        role: str,
        control_id: str,
    ) -> None:
        extra: dict[str, Any] = {
            "timeout": cfg.limits.model_timeout_seconds,
            "num_retries": 0,
        }
        if cfg.vertex_traffic_class != "standard":
            extra["headers"] = {
                "X-Vertex-AI-LLM-Request-Type": "shared",
                "X-Vertex-AI-LLM-Shared-Request-Type": (
                    cfg.vertex_traffic_class
                ),
            }
        super().__init__(model=model, **extra)
        self._state = state
        self._cfg = cfg
        self._role = role
        self._control_id = control_id

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        if self._turns >= self._cfg.limits.max_model_turns_per_agent:
            raise BudgetExceeded(
                f"{self._role} agent model-turn hard limit exhausted "
                f"({self._cfg.limits.max_model_turns_per_agent})"
            )
        self._turns += 1
        reservation = reserve_model_call(
            self._state,
            self._cfg,
            role=self._role,
            control_id=self._control_id,
            model=self.model,
            request_text=_serialize_request(llm_request),
        )
        usage: tuple[int, int] | None = None
        try:
            async for response in super().generate_content_async(
                llm_request, stream=stream
            ):
                metadata = response.usage_metadata
                if metadata is not None:
                    prompt = metadata.prompt_token_count
                    output = metadata.candidates_token_count
                    if prompt is not None and output is not None:
                        usage = (int(prompt), int(output))
                yield response
        except BaseException:
            finalize_model_call(
                self._state, self._cfg, reservation, usage
            )
            raise
        else:
            finalize_model_call(
                self._state, self._cfg, reservation, usage
            )


def _serialize_request(llm_request: LlmRequest) -> str:
    try:
        return llm_request.model_dump_json(exclude_none=True)
    except (AttributeError, TypeError, ValueError):
        return repr(llm_request)
