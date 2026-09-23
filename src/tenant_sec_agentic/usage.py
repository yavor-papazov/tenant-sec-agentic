"""Hard run budgets and deterministic usage/cost ledger."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tenant_sec_agentic.config import AssessmentConfig

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "vertex_ai/gemini-2.5-flash": {
        "input_per_million": 0.30,
        "output_per_million": 2.50,
    },
    "vertex_ai/gemini-3.5-flash-lite": {
        "input_per_million": 0.30,
        "output_per_million": 2.50,
    },
    "vertex_ai/gemini-3.5-flash": {
        "input_per_million": 1.50,
        "output_per_million": 9.00,
    },
}


class BudgetExceeded(RuntimeError):
    """Raised before an operation that would exceed a hard run limit."""


def new_ledger(cfg: AssessmentConfig) -> dict[str, Any]:
    return {
        "version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mock_mode": cfg.mock_mode,
        "vertex_traffic_class": cfg.vertex_traffic_class,
        "limits": {
            key: value
            for key, value in vars(cfg.limits).items()
            if key != "max_parallel_assessments"
        },
        "totals": _empty_totals(),
        "by_role": {},
        "by_control": {},
        "events": [],
    }


def _empty_totals() -> dict[str, Any]:
    return {
        "model_calls": 0,
        "retries": 0,
        "searches": 0,
        "fetches": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reserved_output_tokens": 0,
        "projected_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "tavily_projected_cost_usd": 0.0,
        "unmetered_model_calls": 0,
    }


def estimate_tokens(text: str) -> int:
    """Conservative text-only estimate including request framing overhead."""
    return max(1, math.ceil(len(text) / 3) + 2_048)


def reserve_model_call(
    state: dict[str, Any],
    cfg: AssessmentConfig,
    *,
    role: str,
    control_id: str,
    model: str,
    request_text: str,
) -> dict[str, Any]:
    ledger = _ledger(state, cfg)
    totals = ledger["totals"]
    estimated_input = estimate_tokens(request_text)
    reserved_output = cfg.limits.max_output_tokens_per_call
    price = _price(cfg, model)
    projected = _cost(estimated_input, reserved_output, price)

    _assert_limit(
        totals["model_calls"] + 1,
        cfg.limits.max_model_calls_per_run,
        "model calls",
    )
    _assert_limit(
        totals["input_tokens"] + estimated_input,
        cfg.limits.max_input_tokens_per_run,
        "input tokens",
    )
    _assert_limit(
        totals["output_tokens"]
        + totals["reserved_output_tokens"]
        + reserved_output,
        cfg.limits.max_output_tokens_per_run,
        "output tokens",
    )
    if (
        totals["projected_cost_usd"] + projected
        > cfg.limits.max_projected_cost_usd
    ):
        raise BudgetExceeded(
            "projected model cost would exceed "
            f"${cfg.limits.max_projected_cost_usd:.2f}"
        )

    totals["model_calls"] += 1
    totals["input_tokens"] += estimated_input
    totals["reserved_output_tokens"] += reserved_output
    totals["projected_cost_usd"] += projected
    bucket = _bucket(ledger, role, control_id)
    bucket["model_calls"] += 1
    bucket["input_tokens"] += estimated_input
    bucket["reserved_output_tokens"] += reserved_output
    bucket["projected_cost_usd"] += projected
    reservation = {
        "role": role,
        "control_id": control_id,
        "model": model,
        "estimated_input": estimated_input,
        "reserved_output": reserved_output,
        "projected_cost_usd": projected,
    }
    ledger["events"].append({"type": "model_call_reserved", **reservation})
    return reservation


def finalize_model_call(
    state: dict[str, Any],
    cfg: AssessmentConfig,
    reservation: dict[str, Any],
    usage: tuple[int, int] | None,
) -> None:
    ledger = _ledger(state, cfg)
    totals = ledger["totals"]
    bucket = _bucket(
        ledger, reservation["role"], reservation["control_id"]
    )
    reserved = reservation["reserved_output"]
    totals["reserved_output_tokens"] -= reserved
    bucket["reserved_output_tokens"] -= reserved

    if usage is None:
        totals["unmetered_model_calls"] += 1
        bucket["unmetered_model_calls"] += 1
        # Keep the conservative projected input/output reservation committed.
        totals["output_tokens"] += reserved
        bucket["output_tokens"] += reserved
        return

    actual_input, actual_output = usage
    estimated_input = reservation["estimated_input"]
    totals["input_tokens"] += actual_input - estimated_input
    bucket["input_tokens"] += actual_input - estimated_input
    totals["output_tokens"] += actual_output
    bucket["output_tokens"] += actual_output
    actual_cost = _cost(
        actual_input,
        actual_output,
        _price(cfg, reservation["model"]),
    )
    totals["actual_cost_usd"] += actual_cost
    bucket["actual_cost_usd"] += actual_cost
    ledger["events"].append(
        {
            "type": "model_call_completed",
            **reservation,
            "actual_input_tokens": actual_input,
            "actual_output_tokens": actual_output,
            "actual_cost_usd": actual_cost,
        }
    )


def consume_retry(
    state: dict[str, Any],
    cfg: AssessmentConfig,
    role: str,
    control_id: str,
) -> None:
    ledger = _ledger(state, cfg)
    value = ledger["totals"]["retries"] + 1
    _assert_limit(value, cfg.limits.max_retries_per_run, "retries")
    ledger["totals"]["retries"] = value
    _bucket(ledger, role, control_id)["retries"] += 1
    ledger["events"].append(
        {"type": "retry", "role": role, "control_id": control_id}
    )


def consume_tool_call(
    state: dict[str, Any],
    *,
    kind: str,
    role: str,
    control_id: str,
) -> None:
    cfg = _config_from_state_for_usage(state)
    ledger = _ledger(state, cfg)
    key = "searches" if kind == "search" else "fetches"
    limit = (
        cfg.limits.max_search_calls_per_run
        if kind == "search"
        else cfg.limits.max_fetch_calls_per_run
    )
    value = ledger["totals"][key] + 1
    _assert_limit(value, limit, key)
    ledger["totals"][key] = value
    bucket = _bucket(ledger, role, control_id)
    bucket[key] += 1
    if kind == "search":
        # Maximum PAYG exposure; free-plan credits can make actual cost zero.
        ledger["totals"]["tavily_projected_cost_usd"] += 0.008
        bucket["tavily_projected_cost_usd"] += 0.008
    ledger["events"].append(
        {
            "type": kind,
            "role": role,
            "control_id": control_id,
        }
    )


def write_ledger(state: dict[str, Any], cfg: AssessmentConfig) -> Path:
    ledger = _ledger(state, cfg)
    ledger["finished_at"] = datetime.now(timezone.utc).isoformat()
    path = cfg.paths.assessments_dir / cfg.provider.slug / "usage-ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(ledger, indent=2, sort_keys=True)
    path.write_text(serialized, encoding="utf-8")
    control_ids = sorted(ledger.get("by_control", {}))
    if len(control_ids) == 1:
        control_path = path.parent / "usage-ledgers" / f"{control_ids[0]}.json"
        control_path.parent.mkdir(parents=True, exist_ok=True)
        control_path.write_text(serialized, encoding="utf-8")
    return path


def _ledger(state: dict[str, Any], cfg: AssessmentConfig) -> dict[str, Any]:
    ledger = state.get("usage_ledger")
    if not isinstance(ledger, dict):
        ledger = new_ledger(cfg)
        state["usage_ledger"] = ledger
    return ledger


def _bucket(
    ledger: dict[str, Any], role: str, control_id: str
) -> dict[str, Any]:
    by_role = ledger["by_role"].setdefault(role, _empty_totals())
    by_control = ledger["by_control"].setdefault(control_id, _empty_totals())
    # Callers update both through a small proxy-like merged helper.
    return _DualBucket(by_role, by_control)


class _DualBucket(dict):
    """Apply numeric updates to role and control ledgers together."""

    def __init__(self, first: dict[str, Any], second: dict[str, Any]) -> None:
        self.first = first
        self.second = second

    def __getitem__(self, key: str) -> Any:
        return self.first[key]

    def __setitem__(self, key: str, value: Any) -> None:
        delta = value - self.first[key]
        self.first[key] = value
        self.second[key] += delta


def _price(cfg: AssessmentConfig, model: str) -> dict[str, float]:
    price = cfg.pricing.get(model) or DEFAULT_PRICING.get(model)
    if price is None:
        raise BudgetExceeded(
            f"No token pricing configured for model '{model}'; "
            "cost cannot fail closed"
        )
    return price


def _cost(
    input_tokens: int,
    output_tokens: int,
    price: dict[str, float],
) -> float:
    return (
        input_tokens * price["input_per_million"]
        + output_tokens * price["output_per_million"]
    ) / 1_000_000


def _assert_limit(value: int, limit: int, label: str) -> None:
    if value > limit:
        raise BudgetExceeded(f"{label} hard limit exhausted ({limit})")


def _config_from_state_for_usage(state: dict[str, Any]) -> AssessmentConfig:
    from tenant_sec_agentic.config import config_from_state

    return config_from_state(state["assessment_config"])
